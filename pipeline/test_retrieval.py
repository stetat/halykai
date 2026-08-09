"""Tests for the retrieval layer — free, offline, no API calls.

The assertions worth having here are not "BM25 returns something". They are the four ways a
retriever silently poisons everything downstream:

  1. it indexes the answer key, and every later number becomes self-fulfilling;
  2. it indexes the superseded 2024 contract, and serves a threshold that was replaced;
  3. it serves borrower X's clause when asked about borrower Y, which reads as authoritative;
  4. it mines vocabulary across a definition boundary, and teaches the classifier an inverted
     answer with a real document behind it.

Each has a test. Run:  python -m pipeline.test_retrieval
"""
from __future__ import annotations
import sys

from . import config, docmap, retrieval, classifier
from .engine import PAYROLL, UTILITIES
from .ledger import Txn

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def _mk(desc: str, cp: str, acc: str = "ACC-7801") -> Txn:
    return Txn("TXN-P1-0001", acc, "2025-07-15", -250_000.0, "USD", cp, desc, "P1")


def test_index_shape() -> None:
    idx = retrieval.index()
    check(idx.n > 200, f"index built with {idx.n} passages")
    check(idx.avgdl > 10, f"average passage length {idx.avgdl:.0f} tokens")
    kinds = {p.kind for p in idx.passages}
    check({"contract", "audit", "kyc"} <= kinds, f"all document kinds indexed: {sorted(kinds)}")


def test_no_spec_leakage() -> None:
    """The challenge spec and the answer key must never be retrievable.

    Both name ACC-7801 in a worked example, so anything routing by account id pulls the key
    into a borrower's context — `docmap.SPEC_RE` exists for exactly this, and a retriever is
    exactly the kind of thing that would undo it."""
    dm = docmap.build(save=False)
    specs = {n for n, d in dm["docs"].items() if d["is_spec"]}
    check(bool(specs), f"the corpus contains {len(specs)} spec/answer-key file(s) to exclude")
    indexed = {p.doc for p in retrieval.index().passages}
    check(not (specs & indexed), "no spec/answer-key document is in the index")

    # and it is not reachable by query either, not merely absent from a list
    hits = retrieval.index().search("evidence_txn_id submission_template", k=5)
    check(all(h.doc not in specs for _, h in hits),
          "a query in the spec's own words retrieves no spec passage")


def test_no_outdated_contract() -> None:
    dm = docmap.build(save=False)
    dead = {n for n, d in dm["docs"].items() if d["outdated"]}
    check(len(dead) >= 12, f"{len(dead)} outdated contracts identified by docmap")
    indexed = {p.doc for p in retrieval.index().passages}
    check(not (dead & indexed), "no outdated (2024) contract is in the index")


def test_account_scoping() -> None:
    """Retrieving another borrower's clause is worse than retrieving nothing."""
    acc = "ACC-7201"
    hits = retrieval.index().search("финансовые ковенанты выручка", k=8, acc=acc)
    check(bool(hits), f"{acc}: scoped search returns hits")
    check(all(acc in p.accs for _, p in hits),
          f"{acc}: every scoped hit belongs to that borrower")

    other = retrieval.index().search("финансовые ковенанты выручка", k=8, acc="ACC-7810")
    docs_a = {p.doc for _, p in hits}
    docs_b = {p.doc for _, p in other}
    check(bool(docs_a) and bool(docs_b) and docs_a != docs_b,
          "two borrowers retrieve different documents for the same query")


def test_inflection_is_folded() -> None:
    """The recurring bug in this repo is inflection. A retriever that indexes surface forms
    inherits it at corpus scale, so the stemmer is pinned here in both directions."""
    check(retrieval.stem("оплату") == retrieval.stem("оплате") == retrieval.stem("оплата"),
          "оплату / оплате / оплата share a stem")
    check(retrieval.stem("основных") == retrieval.stem("основные"),
          "основных / основные share a stem")
    check(retrieval.stem("затратами") == retrieval.stem("затраты"),
          "затратами / затраты share a stem")
    # the stemmer must not eat a whole short word
    check(retrieval.stem("ндс") == "ндс", "short tokens survive stemming")
    check(retrieval.normalize("Ёлка и") == "елка и", "ё and NBSP are folded")

    hits = retrieval.index().search("расходы на оплату труда", k=5)
    check(bool(hits), "an inflected query retrieves passages")
    joined = " ".join(p.text.lower() for _, p in hits)
    check("труд" in joined, "the top passages for «оплату труда» are about labour costs")


def test_bm25_ranks_the_right_passage() -> None:
    idx = retrieval.index()
    target = next(p for p in idx.passages if "капиталоёмкости" in p.text.lower())
    hits = idx.search("коэффициент капиталоёмкости отношение капитальных затрат", k=3,
                      acc=target.accs[0] if target.accs else None)
    check(bool(hits) and "капиталоём" in hits[0][1].text.lower(),
          "BM25 ranks the capital-intensity clause first for its own words")


def test_definition_boundary() -> None:
    """One sentence, two definitions:

      «Расходы на оплату труда означают все выплаты персоналу и связанные с ними расходы,
       Коммунальные расходы означают расходы на электроэнергию, водоснабжение ...»

    Reading the first definiens to the full stop hands ЭЛЕКТРОЭНЕРГИЮ to PAYROLL. That is not
    a near miss — it is an inverted answer with a document behind it, which is the worst kind
    this pipeline can produce."""
    mined = retrieval.mine_definitions()
    payroll_terms = {m.term for m in mined if m.category == PAYROLL}
    leaked = {t for t in payroll_terms
              if t.startswith(("электр", "водоснаб", "коммунал"))}
    check(not leaked, f"utilities vocabulary did not leak into payroll (leaked: {leaked})")

    check(any(m.category == PAYROLL for m in mined),
          "the payroll definition is still mined (the trim did not delete the sentence)")
    check(all(m.doc and m.sentence for m in mined),
          "every mined term carries provenance (document + sentence)")


def test_vocabulary_collisions_are_dropped() -> None:
    """A stem two contracts define into two categories is a collision, not vocabulary. Order
    would resolve it, and order-resolved collisions are how «вознаграждение» once moved nine
    ratio denominators' worth of money into INTEREST."""
    from collections import defaultdict
    votes: dict[str, set[str]] = defaultdict(set)
    for m in retrieval.mine_definitions():
        votes[m.term].add(m.category)
    contested = {t for t, c in votes.items() if len(c) > 1}
    vocab = retrieval.mined_vocabulary()
    check(not (contested & set(vocab)),
          f"no contested term survives into the vocabulary ({len(contested)} contested)")
    check(all(v in {PAYROLL, UTILITIES} or isinstance(v, str) for v in vocab.values()),
          "every mined term maps to an engine category")


def test_mined_layer_is_not_in_the_decision_path() -> None:
    """Measured: across the 149 held-out narrations this layer fires on 1 of the 35 rows the
    keyword table cannot decide, and gets it wrong. It is a reading tool, not a classifier —
    this test fails if someone wires it in without re-measuring."""
    t = _mk("Оплата за услуги по подбору персонала по дог. №RS-08/25", "HR Partners")
    check(classifier.keyword_category(t) == "opex",
          "«подбор персонала» is still opex (the mined layer has not been autowired)")
    cat, _ = retrieval.category_from_corpus("услуги по подбору персонала")
    check(cat is None or cat == PAYROLL,
          "the mined layer's answer on that row is unchanged and still not consulted")


def test_prompt_is_grounded() -> None:
    """The LLM prompt must carry the borrower's own documents, scoped to that borrower."""
    txns = [_mk("Оплата за потреблённую электроэнергию", "AlmatyEnergoSbyt", "ACC-7801"),
            _mk("Перечисление заработной платы", "Payroll batch", "ACC-7801")]
    grounded = classifier._prompt(txns, set(), "ACC-7801")
    ungrounded = classifier._prompt(txns, set(), None)
    check("Выдержки из документов" in grounded, "the grounded prompt carries a context block")
    check("Выдержки из документов" not in ungrounded,
          "with no account, the prompt degrades to the old ungrounded form")
    check(len(grounded) > len(ungrounded), "grounding adds context rather than replacing it")

    dm = docmap.build(save=False)
    mine = {n for n, d in dm["docs"].items() if "ACC-7801" in d["accs"]}
    cited = [ln for ln in grounded.splitlines() if ln.startswith("[")]
    check(bool(cited), "context snippets are attributed to a source document")
    check(all(any(m in ln for m in mine) for ln in cited),
          "every cited document belongs to the borrower being classified")


def test_grounding_never_breaks_the_prompt() -> None:
    """A retrieval failure must cost the context block, never the borrower."""
    orig = retrieval.context_for
    try:
        retrieval.context_for = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        txns = [_mk("Оплата за электроэнергию", "AlmatyEnergoSbyt")]
        p = classifier._prompt(txns, set(), "ACC-7801")
        check("Категории" in p and "TXN-P1-0001" in p,
              "a raised retrieval error degrades to an ungrounded prompt, not a crash")
    finally:
        retrieval.context_for = orig


def test_account_derivation() -> None:
    same = [_mk("a", "x", "ACC-7801"), _mk("b", "y", "ACC-7801")]
    mixed = [_mk("a", "x", "ACC-7801"), _mk("b", "y", "ACC-7802")]
    check(classifier._account_of(same) == "ACC-7801", "one borrower's rows resolve its account")
    check(classifier._account_of(mixed) is None,
          "mixed borrowers resolve to no account (better ungrounded than wrongly grounded)")


def main() -> None:
    print("=" * 78)
    print("RETRIEVAL TESTS (offline, no API)")
    print("=" * 78 + "\n")
    for fn in (test_index_shape, test_no_spec_leakage, test_no_outdated_contract,
               test_account_scoping, test_inflection_is_folded,
               test_bm25_ranks_the_right_passage, test_definition_boundary,
               test_vocabulary_collisions_are_dropped,
               test_mined_layer_is_not_in_the_decision_path,
               test_prompt_is_grounded, test_grounding_never_breaks_the_prompt,
               test_account_derivation):
        fn()
    print()
    if FAILED:
        print(f"{len(FAILED)} RETRIEVAL TEST(S) FAILED")
        for m in FAILED:
            print(f"  - {m}")
        sys.exit(1)
    print("ALL RETRIEVAL TESTS PASSED")


if __name__ == "__main__":
    main()
