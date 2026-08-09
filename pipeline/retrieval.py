"""Retrieval over the document corpus: a passage index, and the vocabulary it can mine.

Until now nothing in this pipeline retrieved anything. Documents were routed whole by regex
(`docmap`), clauses were sliced by regex (`covenants`), and the classifier prompt carried no
document context at all — the LLM was asked to categorise Kazakh bank narrations against a
category list we wrote, with the borrower's own contract sitting unread on disk. That is the
gap this module closes.

Two products, both grounded in the corpus rather than in our own guesses:

  1. `search()` — BM25 over passages, filterable by account and document kind, with RU/KZ
     morphology folded in. This is what a prompt should be built from: the borrower's own
     definitions, not a generic category list.

  2. `mined_vocabulary()` — the contracts DEFINE their categories in prose:

         «Расходы на оплату труда означают все выплаты персоналу и связанные с ними расходы,
          Коммунальные расходы означают расходы на электроэнергию, водоснабжение и
          аналогичные поставки»

     so category membership is written down. Mining those definitions turns the classifier's
     hand-written table into a table the DOCUMENTS extend. On event day a contract that
     defines its categories with different words teaches the classifier those words without
     a code change — which is the only kind of fix that survives contact with new data, and
     the opposite of the keyword patching the handoff warns about.

Two hard rules, both enforced by `test_retrieval`:

  * The challenge spec and the answer key are NEVER indexed. They name a borrower's account in
    a worked example, so a retriever that indexes them will happily serve the answer key back
    as "context" and every number downstream becomes self-fulfilling.
  * Outdated contracts (the 2024 version trap) are never indexed either. Retrieving a
    superseded threshold is worse than retrieving nothing.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict

from . import config, docmap, pdftext
from .engine import (label_to_category, CAPEX, OPEX, LEASE, REVENUE, INSURANCE, PAYROLL,
                     FINANCING, INTEREST, TAX, UTILITIES, OTHER)

# --- normalisation --------------------------------------------------------------------
# The recurring bug in this repo is inflection: «оплатУ труда» not matching «оплат труда»,
# «основныХ средств» not matching «основны средств». A retriever that indexes raw surface
# forms inherits that bug at corpus scale, so tokens are stemmed on the way in AND on the way
# out — one function, used by both sides, so they cannot drift.
_WORD_RE = re.compile(r"[а-яёa-zәғқңөұүhіʼ0-9]+", re.IGNORECASE)

# Longest first: strip at most one ending, and only when a usable stem survives.
_RU_ENDINGS = (
    "ениями", "ованием", "ования", "ование", "иями", "ениям", "ением", "ения", "ение",
    "ством", "ства", "ство", "ству", "иях", "иям", "ием", "ами", "ями", "ыми", "ими",
    "ого", "его", "ому", "ему", "ах", "ях", "ов", "ев", "ей", "ий", "ый", "ая", "яя",
    "ое", "ее", "ые", "ие", "ых", "их", "ую", "юю", "ою", "ею", "ом", "ем", "ам",
    "ям", "ой", "ии", "ья", "ье", "ы", "и", "е", "о", "а", "я", "у", "ю", "й", "ь",
)
# Kazakh is agglutinative and the same logic applies to its case/plural suffixes — but its
# two-letter endings COLLIDE with ordinary Russian ones. Stripping KZ «те» from «оплате» left
# «опла», and «ты» from «затраты» left «затра», so the two inflections of the same Russian word
# stopped sharing a stem and the retriever quietly lost half its recall. Kazakh suffixes are
# therefore applied only to words carrying a Kazakh-specific letter.
_KZ_ENDINGS = ("ларға", "лерге", "дарға", "дерге", "тарға", "терге", "ларды", "лерді",
               "лар", "лер", "дар", "дер", "тар", "тер", "ның", "нің", "дың", "дің",
               "тың", "тің", "ға", "ге", "қа", "ке", "да", "де", "та", "те", "ды",
               "ді", "ты", "ті", "сы", "сі", "ын", "ін")
_KZ_LETTERS = frozenset("әғқңөұүіһ")

_RU_SORTED = tuple(sorted(set(_RU_ENDINGS), key=len, reverse=True))
_KZ_SORTED = tuple(sorted(set(_RU_ENDINGS + _KZ_ENDINGS), key=len, reverse=True))
_MIN_STEM = 4


def normalize(text: str) -> str:
    """Fold the encodings the corpus actually mixes: ё/е, NBSP, unicode dashes, case."""
    t = text.lower().replace("ё", "е")
    return t.replace(" ", " ").replace("‑", "-").replace("–", "-")


def stem(word: str) -> str:
    """Conservative suffix stripper. Over-stemming costs precision in a ranked list, which
    BM25 absorbs; under-stemming costs recall, which nothing recovers."""
    w = word
    if len(w) <= _MIN_STEM:
        return w
    is_kz = bool(_KZ_LETTERS & set(w))
    endings = _KZ_SORTED if is_kz else _RU_SORTED
    # Kazakh STACKS suffixes — «жалақы-сы» carries both a possessive and a case ending — so one
    # strip leaves «жалақысы» and «жалақы» in different buckets. Russian does not stack, and a
    # second pass there just over-stems, so the extra pass is Kazakh-only.
    for _ in range(2 if is_kz else 1):
        for e in endings:
            if w.endswith(e) and len(w) - len(e) >= _MIN_STEM:
                w = w[: -len(e)]
                break
        else:
            break
    return w


def tokenize(text: str) -> list[str]:
    return [stem(m.group(0)) for m in _WORD_RE.finditer(normalize(text))]


# --- passages -------------------------------------------------------------------------
@dataclass
class Passage:
    doc: str
    kind: str                 # contract | audit | kyc | other
    accs: list[str]
    idx: int                  # position within the document
    text: str


# pdftotext -raw emits hard-wrapped lines; a blank line is the only reliable paragraph mark,
# and a paragraph is the right retrieval unit here — the definitional sentences that carry
# category membership are one paragraph long, and splitting finer loses the definiendum.
_PARA_RE = re.compile(r"\n\s*\n")
_MIN_PASSAGE = 60
_MAX_PASSAGE = 1200


def split_passages(text: str) -> list[str]:
    out: list[str] = []
    for para in _PARA_RE.split(text):
        para = re.sub(r"\s+", " ", para).strip()
        if len(para) < _MIN_PASSAGE:
            continue
        # A long paragraph is split on sentence ends, not mid-word, so a retrieved snippet is
        # always quotable. The `.` inside «($418,204.37)» must not end a sentence — the same
        # trap that once silently disabled every reclassification carrying an amount.
        while len(para) > _MAX_PASSAGE:
            cut = para.rfind(". ", _MIN_PASSAGE, _MAX_PASSAGE)
            if cut < 0:
                cut = _MAX_PASSAGE
            out.append(para[: cut + 1].strip())
            para = para[cut + 1:].strip()
        if len(para) >= _MIN_PASSAGE:
            out.append(para)
    return out


def _kind(d: dict) -> str:
    if d["has_covenants"] and d["is_contract"]:
        return "contract"
    if d["is_kyc_dossier"] or d["is_kyc"]:
        return "kyc"
    if d["is_audit"]:
        return "audit"
    return "other"


# --- the index ------------------------------------------------------------------------
_K1, _B = 1.5, 0.75


class Index:
    """BM25 over corpus passages. Small enough (a few thousand passages) that a plain
    dict-of-postings beats any dependency, and stdlib-only keeps the event-day machine
    reproducible."""

    def __init__(self, passages: list[Passage]):
        self.passages = passages
        self.tokens: list[Counter] = []
        self.lens: list[int] = []
        self.postings: dict[str, list[int]] = defaultdict(list)
        for i, p in enumerate(passages):
            toks = tokenize(p.text)
            c = Counter(toks)
            self.tokens.append(c)
            self.lens.append(len(toks))
            for t in c:
                self.postings[t].append(i)
        self.avgdl = (sum(self.lens) / len(self.lens)) if self.lens else 1.0
        self.n = len(passages)

    def idf(self, term: str) -> float:
        df = len(self.postings.get(term, ()))
        if not df:
            return 0.0
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def doc_freq(self, term: str) -> int:
        return len(self.postings.get(term, ()))

    def search(self, query: str, k: int = 5, acc: str | None = None,
               kinds: tuple[str, ...] | None = None) -> list[tuple[float, Passage]]:
        """Top-k passages. `acc` restricts to one borrower's documents — retrieving another
        borrower's clause is worse than retrieving nothing, because it reads as authoritative
        and names the wrong threshold."""
        qt = [t for t in tokenize(query) if t not in _STOP]
        if not qt:
            return []
        scores: dict[int, float] = defaultdict(float)
        for term in set(qt):
            idf = self.idf(term)
            if idf <= 0:
                continue
            for i in self.postings[term]:
                p = self.passages[i]
                if acc and acc not in p.accs:
                    continue
                if kinds and p.kind not in kinds:
                    continue
                f = self.tokens[i][term]
                dl = self.lens[i] or 1
                scores[i] += idf * (f * (_K1 + 1)) / (f + _K1 * (1 - _B + _B * dl / self.avgdl))
        top = sorted(scores.items(), key=lambda kv: -kv[1])[:k]
        return [(s, self.passages[i]) for i, s in top]


# Function words plus the boilerplate every document in this corpus repeats. They carry no
# retrieval signal and would otherwise dominate a short query's score.
_STOP = {
    "и", "в", "на", "по", "с", "за", "к", "от", "для", "до", "из", "не", "или", "а", "то",
    "что", "как", "при", "об", "о", "у", "же", "их", "его", "ее", "быт", "был", "эт", "так",
    "котор", "так", "чем", "все", "так", "either", "the", "of", "and", "to", "for", "in",
    "заемщик", "кредитор", "договор", "настоящ", "цел", "период", "сумм", "case", "случа",
    "соответств", "производ", "являт", "являет", "средств",
}


_INDEX: Index | None = None
_INDEX_FINGERPRINT: str | None = None


def corpus_docs(dm: dict | None = None) -> list[tuple[str, dict]]:
    """Every document that may be retrieved, with the two exclusions that matter.

    The spec/answer key exclusion is not tidiness: `docmap.SPEC_RE` exists because both files
    name ACC-7801 in a worked example, so anything that routes by account id pulls the answer
    key into a borrower's context. A retriever is exactly such a thing."""
    dm = dm or docmap.build(save=False)
    out = []
    for name, d in dm["docs"].items():
        if d["is_spec"]:
            continue
        if d["outdated"]:                      # the 2024 version trap
            continue
        if d["ftype"] not in ("pdf", "utf8-text", "text/csv", "other"):
            continue
        out.append((name, d))
    return out


def build_index(dm: dict | None = None, verbose: bool = False) -> Index:
    dm = dm or docmap.build(save=False)
    passages: list[Passage] = []
    for name, d in corpus_docs(dm):
        try:
            text = pdftext.extract_text(config.DATASET / name)
        except Exception as e:                 # a single unreadable file must not cost the index
            if verbose:
                print(f"!! retrieval: {name} unreadable ({str(e)[:60]}); skipped")
            continue
        kind = _kind(d)
        for i, chunk in enumerate(split_passages(text)):
            passages.append(Passage(doc=name, kind=kind, accs=list(d["accs"]),
                                    idx=i, text=chunk))
    idx = Index(passages)
    if verbose:
        by_kind = Counter(p.kind for p in passages)
        print(f"retrieval index: {len(passages)} passages from "
              f"{len(set(p.doc for p in passages))} documents  {dict(by_kind)}")
    return idx


def index(dm: dict | None = None) -> Index:
    """Process-cached index. Rebuilt when the corpus changes — the fingerprint is over file
    contents, not names, because event day ships a new archive that reuses these hashed
    filenames and a name-keyed cache would serve the practice release's text forever."""
    global _INDEX, _INDEX_FINGERPRINT
    fp = _corpus_fingerprint()
    if _INDEX is None or _INDEX_FINGERPRINT != fp:
        _INDEX = build_index(dm)
        _INDEX_FINGERPRINT = fp
    return _INDEX


def _corpus_fingerprint() -> str:
    h = hashlib.sha256()
    for p in sorted(config.DATASET.iterdir()):
        if p.is_file():
            h.update(p.name.encode())
            h.update(str(p.stat().st_size).encode())
    return h.hexdigest()[:16]


def reset() -> None:
    """Drop the process cache (tests that swap the dataset need this)."""
    global _INDEX, _INDEX_FINGERPRINT
    _INDEX, _INDEX_FINGERPRINT = None, None


# --- product 1: grounded context for a prompt -----------------------------------------
def context_for(query: str, acc: str | None = None, k: int = 4,
                kinds: tuple[str, ...] | None = None, max_chars: int = 1800) -> str:
    """Retrieved passages formatted for an LLM prompt, with provenance on every snippet.

    Provenance is not decoration. A model given unattributed context will blend it with its
    own priors and there is no way afterwards to tell which sentence produced an answer; with
    the document name attached, any surprising classification can be traced to the passage
    that caused it."""
    hits = index().search(query, k=k, acc=acc, kinds=kinds)
    out, used = [], 0
    for score, p in hits:
        snippet = p.text[:600]
        block = f"[{p.doc} · {p.kind}] {snippet}"
        if used + len(block) > max_chars:
            break
        out.append(block)
        used += len(block)
    return "\n\n".join(out)


# --- product 2: vocabulary mined from the corpus's own definitions ---------------------
# «X означает/означают Y», «под X понимаются Y», «X включают Y». The definiendum is mapped to
# an engine category through engine.label_to_category — the SAME vocabulary the covenant
# parser and the reclassification parser use, so a definition can never teach the classifier
# a category the engine does not compute.
_DEFINES = r"(?:означа(?:ет|ют)|понима(?:ется|ются)|включа(?:ет|ют))"
_DEF_RE = re.compile(
    r"(?:под\s+)?([«\"']?[А-ЯЁа-яё][^.;»\"']{2,60}[»\"']?)\s+" + _DEFINES + r"\s+([^.;]{10,400})",
    re.IGNORECASE)

# A definiens runs until the sentence ends OR until the next definition starts. Both happen in
# one sentence here:
#
#   «Расходы на оплату труда означают все выплаты персоналу и связанные с ними расходы,
#    Коммунальные расходы означают расходы на электроэнергию, водоснабжение ...»
#
# — one sentence, two definitions, separated by a comma. Reading to the full stop hands
# ЭЛЕКТРОЭНЕРГИЮ and ВОДОСНАБЖЕНИЕ to PAYROLL, i.e. it teaches the classifier the exact
# inversion the handoff warns about: not a near miss, a wrong answer with a document behind it.
_NEXT_DEF_RE = re.compile(r"[А-ЯЁ][^,;.]{2,60}\s+" + _DEFINES)


def _definiens(text: str) -> str:
    """Trim a captured definiens at the start of the NEXT definition in the same sentence."""
    m = _NEXT_DEF_RE.search(text)
    return text[: m.start()] if m else text

# Terms that describe the ACT of accounting rather than the thing bought. Mining these would
# teach the classifier that any sentence mentioning an audit is a payroll payment.
_DEF_STOP = {
    "расход", "затрат", "сумм", "суммы", "аудирова", "отчетност", "аудитор", "заемщик",
    "период", "аналогичн", "прочи", "прочее", "связанн", "все", "весь", "каждом", "случа",
    "определя", "принят", "ковенант", "договор", "статья", "стать", "показател", "величин",
    "включа", "учет", "учета", "данн", "соответств", "признан", "выплат", "поставк",
    "оказан", "услуг", "работ", "товар", "платеж", "оплат", "средств", "актив", "групп",
    # observed leakage: these describe the ACT of accounting, not what was bought
    "означа", "понима", "примечани", "раскрыт", "запис", "наступл", "вычет", "квартал",
    "метод", "начисл", "совокупн", "отражен", "обязатель", "тольк", "корректировк",
    "переквалификац", "переклассификац", "соблюд", "независим", "конкретн", "первоначальн",
}

# Russian marks participles and verbal adjectives morphologically, and every one of them that
# reached the mined table was noise («переквалифицированные», «отнесённые», «действующие»):
# they describe what an auditor DID to a sum, never what the money bought. Filtering the form
# is more durable than listing the words, because the next contract will use different ones.
_PARTICIPLE_SUFFIXES = ("нн", "ющ", "вш", "ащ", "ящ", "уем", "аем", "ляем", "ируем")


def _is_participle(stem_: str) -> bool:
    return stem_.endswith(_PARTICIPLE_SUFFIXES)

# A mined term is only useful if it DISCRIMINATES. A stem appearing in most of the corpus is
# boilerplate no matter how promising it looks in one definition; this is the same idf the
# retriever ranks with, reused as an admission test.
_MIN_TERM_IDF = 1.2
_MIN_TERM_LEN = 5


@dataclass
class MinedTerm:
    term: str
    category: str
    doc: str
    sentence: str


def mine_definitions(dm: dict | None = None) -> list[MinedTerm]:
    """Every category-defining sentence in the corpus, as (term -> category) with provenance."""
    idx = index(dm)
    seen_sent: set[str] = set()
    mined: list[MinedTerm] = []
    for p in idx.passages:
        if p.kind not in ("contract", "audit"):
            continue
        for m in _DEF_RE.finditer(p.text):
            definiendum, definiens = m.group(1), _definiens(m.group(2))
            cat = label_to_category(definiendum)
            if cat == OTHER:
                continue
            sent = re.sub(r"\s+", " ", m.group(0)).strip()
            key = (cat, sent[:120])
            if key in seen_sent:
                continue
            seen_sent.add(key)
            for tok in dict.fromkeys(tokenize(definiens)):
                if len(tok) < _MIN_TERM_LEN or tok in _STOP or _is_participle(tok):
                    continue
                if any(tok.startswith(s) or s.startswith(tok) for s in _DEF_STOP):
                    continue
                if idx.idf(tok) < _MIN_TERM_IDF:
                    continue
                mined.append(MinedTerm(term=tok, category=cat, doc=p.doc, sentence=sent[:300]))
    return mined


def mined_vocabulary(dm: dict | None = None) -> dict[str, str]:
    """{stem: category}, keeping only terms the corpus is UNANIMOUS about.

    A stem defined into two categories by two different contracts is not vocabulary, it is a
    collision — and a collision resolved by ordering is exactly how «вознаграждение» once
    moved nine ratio denominators' worth of money into INTEREST. Drop it and let the sign
    fallback keep the row, which at least reports itself as a guess."""
    votes: dict[str, set[str]] = defaultdict(set)
    for mt in mine_definitions(dm):
        votes[mt.term].add(mt.category)
    return {t: next(iter(c)) for t, c in votes.items() if len(c) == 1}


_VOCAB: dict[str, str] | None = None


def vocabulary(dm: dict | None = None) -> dict[str, str]:
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = mined_vocabulary(dm)
    return _VOCAB


def reset_vocabulary() -> None:
    global _VOCAB
    _VOCAB = None


def category_from_corpus(blob: str, dm: dict | None = None) -> tuple[str | None, str | None]:
    """(category, deciding term) for a narration, using ONLY corpus-mined vocabulary.

    NOT WIRED INTO THE CLASSIFIER, and the measurement is why. Across the 149 held-out
    narrations this layer fires on exactly ONE of the 35 rows the keyword table cannot decide,
    and that one firing is wrong: «услуги по подбору ПЕРСОНАЛА» is a recruitment agency's fee,
    which the hand table already routes to OPEX on purpose. Net effect on the only honest
    number in the repo: −1. So it stays out of the decision path.

    The reason the yield is nil is worth writing down, because it is a property of the corpus
    and not of this code: these contracts define categories PROCEDURALLY («суммы, отнесённые к
    данной статье в аудированной отчётности»), not by membership («электроэнергия, вода,
    тепло»). Procedural definitions describe what an auditor does to a sum, and no amount of
    mining turns that into vocabulary for a payment narration.

    It is kept, and exposed as `cli definitions`, because on event day the contracts are new:
    a corpus that DOES define membership will surface it here, and the ranked output tells a
    human which words to add to `classifier._RULES`. Read it, do not autowire it.

    Returns (None, None) when the corpus is silent or ambiguous."""
    vocab = vocabulary(dm)
    if not vocab:
        return None, None
    toks = set(tokenize(blob))
    hits = {vocab[t] for t in toks if t in vocab}
    if len(hits) != 1:
        return None, None                       # silent, or contested — say nothing
    cat = next(iter(hits))
    term = next(t for t in toks if t in vocab)
    return cat, term


if __name__ == "__main__":
    idx = build_index(verbose=True)
    globals()["_INDEX"] = idx
    globals()["_INDEX_FINGERPRINT"] = _corpus_fingerprint()
    mined = mine_definitions()
    vocab = mined_vocabulary()
    print(f"\ndefinitional sentences mined: {len(set(m.sentence for m in mined))}")
    print(f"terms admitted (unanimous, idf>={_MIN_TERM_IDF}): {len(vocab)}\n")
    by_cat: dict[str, list[str]] = defaultdict(list)
    for t, c in sorted(vocab.items()):
        by_cat[c].append(t)
    for c, ts in sorted(by_cat.items()):
        print(f"  {c:<12} {', '.join(ts)}")
    print("\nsample retrieval — 'коэффициент покрытия процентов' for ACC-7201:")
    for s, p in index().search("коэффициент покрытия процентов", k=3, acc="ACC-7201"):
        print(f"  {s:6.2f}  [{p.doc} · {p.kind}] {p.text[:120]}...")
