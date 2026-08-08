"""End-to-end: documents (+ ledger) -> submission.json.

  python -m pipeline.solve --ledger path/to/master_ledger_2025.csv [--fx fx.csv]
  python -m pipeline.solve            # no ledger: emits a readiness skeleton

Pipeline:
  Stage A (docs, works now): current-contract selection, covenant specs (operator/threshold),
           reclassifications, related parties.
  Stage B (needs ledger): categorise txns, compute actual/status, find evidence via
           leave-one-out over applied reclassifications. -> fills each cell.
Without a ledger the ledger-dependent fields stay null; the file is still valid JSON."""
from __future__ import annotations
import argparse
import json
from . import config, docmap, covenants, reclass, engine, scorer, classifier, ledger as ledgermod
from .engine import Categorizer, RELATED_PARTY

TEAM = "your-team-name"
CONTACT = "adarhan76@gmail.com"
MODEL = config.MODEL_PRO

# Base keyword classifier over a txn's counterparty/description — the main data-dependent
# knob, and the ONLY classifier running whenever the Gemini free tier 429s.
#
# This is deliberately an alias, not a copy. A stale 14-keyword copy used to live here and
# had silently diverged: it could only ever emit 4 of the 13 categories, so every ratio
# covenant with interest/tax/utilities/insurance/financing in its denominator divided by
# zero and produced UNKNOWN -> empty cell -> 0 points. It scored 23% where the shared
# implementation scores 100% on the labelled fixture. Keep exactly one implementation.
base_classifier = classifier.keyword_category


def empty_cell():
    return {"status": None, "actual": None, "evidence_txn_id": None}


def _report_ledger(txns, txns_by_sc, rates, missing) -> None:
    """Loud sanity report on ingestion. A ledger that parses into the wrong shape is the
    one failure that silently scores 0, so it gets shouted about rather than logged."""
    print(f"Ledger: {len(txns)} rows, "
          f"{len(set(t.currency for t in txns))} currencies, "
          f"{sum(1 for s in txns_by_sc if s)} scenarios resolved / 12")
    unmapped = len(txns_by_sc.get("", []))
    if unmapped:
        print(f"!! {unmapped}/{len(txns)} rows have NO scenario — neither the txn_id "
              f"prefix nor account_id matched. Check ledger.TXN_RE / config.SCENARIO_TO_ACC.")
        for t in txns_by_sc[""][:3]:
            print(f"     sample: txn_id={t.txn_id!r} account_id={t.account_id!r}")
    for sc in config.SCENARIO_TO_ACC:
        if sc not in txns_by_sc:
            print(f"!! scenario {sc} has no transactions — its 3 cells will be empty.")
    if not any(t.counterparty or t.description for t in txns):
        print("!! counterparty AND description are both empty for every row — the text "
              "columns did not resolve; categorisation will be garbage. Fix ledger._ALIASES.")
    non_usd = {t.currency for t in txns if t.currency != "USD"}
    if non_usd and rates is None:
        print(f"!! ledger has non-USD rows {sorted(non_usd)} but NO --fx table was given; "
              f"they are being counted 1:1 and every affected `actual` will be wrong.")
    elif missing:
        print(f"!! no FX rate for {missing} — counted 1:1.")
    _report_classifier_coverage(txns)


def _report_classifier_coverage(txns) -> None:
    """What share of the ledger the vocabulary actually recognised.

    Whatever matches no keyword falls through to the sign of the amount: every unmatched
    credit becomes revenue and every unmatched DEBIT becomes opex. The fallback cannot emit
    capex, lease, utilities, tax, interest or insurance at all, so a real capex or tax payment
    with unfamiliar wording is silently booked as opex — and opex is a denominator in
    CAPEX_INTENSITY and in P6 6.1, where inflating it understates the ratio and can turn a
    BREACH into a COMPLIANT. Held-out sets put this at ~28% of rows, so it is shouted about.
    Anything here is a list of rows to eyeball before trusting the affected cells."""
    if not txns:
        return
    fallback = [t for t in txns
                if classifier.categorize_verbose(t)[1] == classifier.SIGN_FALLBACK]
    if not fallback:
        return
    pct = len(fallback) / len(txns)
    mark = "!!" if pct >= 0.15 else "  "
    print(f"{mark} {len(fallback)}/{len(txns)} rows ({pct:.0%}) matched NO category keyword "
          f"and were classified by the sign of the amount alone.")
    if pct >= 0.15:
        print("     Those can only come out as revenue/opex; a capex or tax row among them is "
              "now opex.")
        for t in fallback[:5]:
            print(f"     sample: {t.counterparty} | {t.description[:58]}")


def solve(ledger_path: str | None = None, fx_path: str | None = None,
          classifier_mode: str = "keyword") -> dict:
    dm = docmap.build(save=True)
    specs = covenants.build(use_llm=False, save=True)   # regex specs (free, exact thresholds)

    txns_by_sc = {}
    if ledger_path:
        # Never let an ingestion failure leave us with no submission file at all:
        # warn loudly, emit the doc-only skeleton, fix the dialect, re-run.
        try:
            txns = ledgermod.load(ledger_path)
            rates = None
            if fx_path:
                try:
                    rates = ledgermod.load_fx(fx_path)
                    print(f"FX: {len(rates)} rates loaded from {fx_path}")
                except Exception as e:
                    print(f"!! FX table {fx_path} failed to load ({e}); non-USD rows "
                          f"will be counted 1:1.")
            missing = ledgermod.convert_fx(txns, rates)
            txns_by_sc = ledgermod.by_scenario(txns)
            _report_ledger(txns, txns_by_sc, rates, missing)
        except Exception as e:
            print(f"!! LEDGER LOAD FAILED ({e}) — writing the doc-only skeleton. "
                  f"Fix ledger._ALIASES/dialect and re-run.")

    answers: dict[str, dict] = {}
    for sc, acc in config.SCENARIO_TO_ACC.items():
        answers[sc] = {"6.1": empty_cell(), "6.2": empty_cell(), "6.3": empty_cell()}
        covs = specs.get(sc, {}).get("covenants", {})
        if not (ledger_path and sc in txns_by_sc):
            continue
        txns = txns_by_sc[sc]
        rcs = reclass.for_account(acc, dm)
        rps = reclass.related_parties(acc, dm)
        # Identity/adjustment facts that live only inside embedded images (image_facts.json)
        unrestricted = reclass.unrestricted_subsidiaries(acc)
        addback = reclass.ebitda_addback(acc)
        if classifier_mode in ("gemini", "hybrid"):
            # one Gemini call for this borrower; LLM handles related-party via the KYC list,
            # so we don't also apply the noisy counterparty override here. "hybrid" asks only
            # about rows the keyword table could not decide — same answer where it was already
            # confident, far less quota, and a 429 degrades to the deterministic result.
            fn = (classifier.classify_hybrid if classifier_mode == "hybrid"
                  else classifier.classify_batch)
            try:
                cat_map = fn(txns, related_parties=rps)
                st = fn.last_stats
                if classifier_mode == "hybrid":
                    print(f"   {sc}: asked Gemini about {st['asked']}/{len(txns)} rows, "
                          f"{st['llm_used']} answers used"
                          + (f" ({len(st['errors'])} call(s) failed)" if st["errors"] else ""))
                base = classifier.make_base_classifier(cat_map)
                catf = Categorizer(base, rcs, related_parties=set(),
                                   unrestricted_parties=unrestricted)
            except Exception as e:                       # quota/network -> keywords
                print(f"!! {sc}: Gemini classifier failed ({e}); using keywords")
                catf = Categorizer(base_classifier, rcs, related_parties=rps,
                                   unrestricted_parties=unrestricted)
        else:
            catf = Categorizer(base_classifier, rcs, related_parties=rps,
                               unrestricted_parties=unrestricted)
        if not rps:
            # All 12 borrowers resolve today (two of them only via image_facts.json, since
            # their ownership tables are images). This stays as a safety net for event day:
            # identity is authoritative, but a description guess beats a confident 0.
            inner = catf.base
            catf.base = lambda t: (RELATED_PARTY if classifier.looks_related_party(t)
                                   else inner(t))
        for cid in ("6.1", "6.2", "6.3"):
            spec = covs.get(cid)
            if not spec:
                continue
            # A related-party covenant with an empty KYC list computes 0 and reports a
            # confident COMPLIANT — the most dangerous kind of wrong answer here.
            if not rps and "RELATED" in engine.classify_kind(spec):
                print(f"!! {sc} {cid} is a related-party covenant but {acc} has no KYC "
                      f"ownership list; falling back to description hints. Re-check the "
                      f"archive for a dossier — identity, not description, is authoritative.")
            # One bad cell must never cost us the other 35.
            try:
                res = engine.evaluate(spec, txns, catf, rcs,
                                      extras={"ebitda_addback": addback})
            except Exception as e:
                print(f"!! {sc} {cid}: engine error ({e}); left empty")
                continue
            if res.status in ("COMPLIANT", "BREACH"):
                answers[sc][cid] = {
                    "status": res.status, "actual": res.actual,
                    "evidence_txn_id": res.evidence_txn_id,
                }

    if TEAM == "your-team-name":
        print("!! solve.TEAM is still the spec's placeholder 'your-team-name' — "
              "set it before submitting.")
    submission = {"team": TEAM, "contact_email": CONTACT, "model": MODEL, "answers": answers}
    (config.ROOT / "submission.json").write_text(
        json.dumps(submission, ensure_ascii=False, indent=2), encoding="utf-8")
    return submission


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", help="path to master_ledger_2025.csv")
    ap.add_argument("--fx", help="path to fx rates csv (optional)")
    ap.add_argument("--classifier", choices=("keyword", "gemini"), default="keyword",
                    help="transaction categoriser (default keyword; gemini = 1 call/borrower)")
    ap.add_argument("--score", action="store_true", help="score against the answer key")
    a = ap.parse_args()
    sub = solve(a.ledger, a.fx, classifier_mode=a.classifier)
    filled = sum(1 for sc in sub["answers"].values()
                 for c in sc.values() if c["status"] in ("COMPLIANT", "BREACH"))
    print(f"Wrote submission.json — {filled}/36 cells computed "
          f"({'ledger supplied' if a.ledger else 'no ledger: skeleton only'}).")
    if a.score:
        print()
        scorer.score_submission(sub)


if __name__ == "__main__":
    main()
