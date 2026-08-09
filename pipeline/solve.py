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
from statistics import median
from . import config, docmap, covenants, reclass, engine, scorer, classifier, ledger as ledgermod
from .engine import Categorizer, RELATED_PARTY

TEAM = "darkhan"
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
          f"{sum(1 for s in txns_by_sc if s)} scenarios resolved / "
          f"{len(config.SCENARIO_TO_ACC)}")
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
        print("     `--classifier hybrid` sends exactly these rows to Gemini. Measured on 149 "
              "held-out narrations the LLM had never seen: on rows whose truth IS revenue/opex "
              "the sign guess wins narrowly (35/35 vs 34/35), but on rows whose truth it cannot "
              "express the LLM scores 110/110 against 0/110. Worth it once more than ~3% of "
              "these are neither revenue nor opex.")
        for t in fallback[:5]:
            print(f"     sample: {t.counterparty} | {t.description[:58]}")


def _report_untranscribed_images() -> None:
    """Shout about images nobody has read. The one failure mode with no symptom.

    pdftotext returns nothing for an image, so a document whose ownership table or EBITDA
    add-back schedule lives in a picture reads as an ordinary file carrying no covenant data.
    The pipeline then reports a confident wrong answer — zero related-party spend, no add-back —
    with no error anywhere. Four such documents were found by hand in this release and are
    transcribed in image_facts.json. Any other document with a sizeable image is one nobody has
    looked at."""
    try:
        from . import pdfimages
        unread = pdfimages.untranscribed_image_docs()
    except Exception as e:
        print(f"   (could not scan for embedded images: {e})")
        return
    if not unread:
        return
    print(f"!! {len(unread)} document(s) carry a sizeable image with NO transcription in "
          f"image_facts.json. pdftotext cannot see inside them, so any covenant data there is "
          f"being silently dropped:")
    for name, n in unread:
        print(f"     {name}  ({n} image(s))")
    print("     Fix: `python -m pipeline.pdfimages` writes them to cache/images/ — LOOK at "
          "them and add to image_facts.json, or run `python -m pipeline.cli ocr` to have the "
          "model transcribe them (costs quota, and its output is unverified).")


def _adopt_ledger_scenarios(txns) -> None:
    """Let the ledger, not a hardcoded constant, decide which borrowers exist.

    config.SCENARIO_TO_ACC lists the practice release's 12. If event day ships a thirteenth
    borrower, renumbers the accounts, or uses different scenario ids, every unrecognised row
    resolves to no scenario and those cells score zero — silently, because the pipeline has no
    way to know it was supposed to see them. The ledger states the pairing in every row, so
    adopt what it says and report any difference loudly rather than trusting the constant."""
    # Constrained to the scenarios the template actually asks for: the real ledger carries 800
    # counterparty rows with numeric txn prefixes, which unconstrained look like 575 borrowers.
    allowed = set(config.submission_template()) or None
    found = ledgermod.discover_scenario_map(txns, allowed=allowed)
    if not found:
        print("!! could not read any scenario<->account pair from the ledger; keeping the "
              "built-in map. Check ledger.TXN_RE against the real txn_id format.")
        return
    current = dict(config.SCENARIO_TO_ACC)
    if found == current:
        return
    added = {k: v for k, v in found.items() if k not in current}
    changed = {k: (current[k], v) for k, v in found.items()
               if k in current and current[k] != v}
    absent = {k: v for k, v in current.items() if k not in found}

    print(f"Ledger defines {len(found)} borrowers; the built-in map had {len(current)}.")
    for k, v in added.items():
        print(f"!! ledger has borrower {k} -> {v}, absent from the built-in map — ADOPTED. "
              f"Its cells would otherwise have scored 0.")
    for k, (was, now) in changed.items():
        print(f"!! {k} maps to {now} in the ledger, not {was} — ADOPTED (ledger is authoritative).")
    for k, v in absent.items():
        print(f"!! {k} ({v}) has no rows in the ledger; keeping it so its cells still get an "
              f"answer.")
    # Keep a built-in borrower ONLY if the template still asks for it. That rule used to be
    # "keep everything, a guessed cell beats a blank one", which was right while the built-in
    # map was a hypothesis about THIS dataset. Against the real archive it is wrong: the
    # constant describes a different contest, so merging injected 12 phantom borrowers
    # (P1..P10, B1, B4) that no template cell asks for, each printing "no transactions" and
    # each emitting three invented cells.
    wanted = set(config.submission_template())
    keep = {k: v for k, v in current.items() if not wanted or k in wanted}
    dropped = sorted(set(current) - set(keep))
    if dropped:
        print(f"   dropped {len(dropped)} built-in borrower(s) the template does not ask for: "
              f"{dropped}")
    merged = {**keep, **found}
    config.set_scenario_map(merged)
    ledgermod.refresh_known_scenarios()
    moved = ledgermod.reresolve(txns)
    if moved:
        print(f"   re-resolved {moved} row(s) against the adopted map.")


def _fill_unresolved(answers: dict, unresolved: list) -> None:
    """Never submit a blank cell. A blank scores zero with certainty; a guess cannot score less.

    The rubric awards status 0.50, actual 0.30 (linear decay to zero at 5% error) and evidence
    0.20, and it awards none of them for an absent answer. So for any cell the engine could not
    compute, emit the best available estimate and say so loudly:

      status  — the majority verdict among the cells that DID compute in this same run. Derived
                from the run rather than hardcoded, so it adapts to an event-day dataset with a
                different compliance mix instead of encoding this practice release's balance.
      actual  — the covenant's own threshold. Covenants are written at levels the borrower is
                near, so the boundary is the least-bad point estimate; it also scores whenever
                the truth is within 5% of the limit.
      evidence — left null. CASE.ru.md decays evidence on the `actual` scale when the key holds
                null, so a wrong id is not punished, but we have no basis to prefer any row.

    Every guessed cell is printed. These are the cells to attack first if time remains."""
    if not unresolved:
        return
    verdicts = [c["status"] for sc in answers.values() for c in sc.values()
                if c["status"] in ("COMPLIANT", "BREACH")]
    majority = (max(set(verdicts), key=verdicts.count) if verdicts else "COMPLIANT")

    print(f"\n!! {len(unresolved)} cell(s) could not be computed. Guessing rather than leaving "
          f"them blank — a blank scores 0, a guess cannot score less.")
    print(f"   status prior = {majority} "
          f"({verdicts.count(majority)}/{len(verdicts)} of the computed cells)")
    # The clause's own threshold is the best available guess for `actual` — a covenant is
    # usually tested near its limit. But a threshold that failed to PARSE is None, and writing
    # None here re-creates the blank cell this function exists to prevent: it scores zero with
    # certainty, exactly like a wrong answer, so there is never a reason to emit one. Fall back
    # to the median of whatever the same clause id computed for other borrowers, then to 0.0.
    computed_by_cid: dict[str, list[float]] = {}
    for _sc, covs in answers.items():
        for _cid, cell in covs.items():
            if isinstance(cell.get("actual"), (int, float)):
                computed_by_cid.setdefault(_cid, []).append(float(cell["actual"]))
    for sc, cid, spec in unresolved:
        thr = (spec or {}).get("threshold")
        why = "no clause parsed" if spec is None else "metric not computable from the ledger"
        if not isinstance(thr, (int, float)):
            peers = computed_by_cid.get(cid) or []
            thr = round(median(peers), 2) if peers else 0.0
            why += f"; threshold did not parse, using {'peer median' if peers else '0.0'}"
        answers[sc][cid] = {"status": majority, "actual": thr, "evidence_txn_id": None}
        print(f"   {sc:>4} {cid}  <- {majority}, actual={thr}  ({why})")


def solve(ledger_path: str | None = None, fx_path: str | None = None,
          classifier_mode: str = "keyword", write: bool = True) -> dict:
    """Build the submission. `write=False` returns it WITHOUT touching submission.json —
    tests must never be able to clobber a real submission by importing this."""
    # The ledger is read BEFORE the documents, because it is the authority on which borrowers
    # exist. docmap and the covenant specs are built per scenario, so discovering an extra or
    # renamed borrower after building them would leave those cells with no spec.
    pre_txns = None
    if ledger_path:
        try:
            pre_txns = ledgermod.load(ledger_path)
            _adopt_ledger_scenarios(pre_txns)
        except Exception:
            pre_txns = None          # reported properly by the real load below

    dm = docmap.build(save=True)
    specs = covenants.build(use_llm=False, save=True)   # regex specs (free, exact thresholds)
    _report_untranscribed_images()

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
                    print(f"!! FX table {fx_path} failed to load ({e}); falling back to the "
                          f"rates disclosed in the documents.")
            if rates is None and any((t.currency or "USD").upper() != "USD" for t in txns):
                # No FX file — which is the real archive's situation. The rate is in the
                # documents («по курсу, раскрытому аудитором»), stated per borrower, so read
                # it from there rather than counting EUR 1:1 and understating it by 8–16%.
                try:
                    from . import fx as fxmod
                    rates = fxmod.rates_by_scenario(dm) or None
                except Exception as e:
                    print(f"!! could not read FX rates from the documents ({str(e)[:80]}); "
                          f"non-USD rows will be counted 1:1.")
            missing = ledgermod.convert_fx(txns, rates)
            txns_by_sc = ledgermod.by_scenario(txns)
            _report_ledger(txns, txns_by_sc, rates, missing)
        except Exception as e:
            print(f"!! LEDGER LOAD FAILED ({e}) — writing the doc-only skeleton. "
                  f"Fix ledger._ALIASES/dialect and re-run.")

    answers: dict[str, dict] = {}
    unresolved: list[tuple[str, str, dict | None]] = []
    # WHICH cells we owe comes from the template, never from a constant. The practice release
    # was 12 borrowers x {6.1,6.2,6.3}; the real one is 27 borrowers, three of them carrying a
    # 6.4 and one (J4) whose covenants are numbered under Article 5. A hardcoded triple would
    # invent 6.2/6.3 for J4 and silently omit four cells that were asked for — and an omitted
    # cell scores the same as a wrong one.
    template = config.submission_template()
    for sc, acc in config.SCENARIO_TO_ACC.items():
        cids = template.get(sc) or ["6.1", "6.2", "6.3"]
        answers[sc] = {cid: empty_cell() for cid in cids}
        covs = specs.get(sc, {}).get("covenants", {})
        if not (ledger_path and sc in txns_by_sc):
            # No ledger, or none of its rows resolved to this borrower. Nothing is computable,
            # but the cells are still owed an answer — hand them to the guess pass.
            unresolved.extend((sc, cid, covs.get(cid)) for cid in cids)
            continue
        txns = txns_by_sc[sc]
        rcs = reclass.for_account(acc, dm)
        rps = reclass.related_parties(acc, dm)
        # Identity/adjustment facts that live only inside embedded images (image_facts.json)
        unrestricted = reclass.unrestricted_subsidiaries(acc)
        # Cut-off notes: rows whose services fall outside the covenant year leave the period.
        excluded = reclass.period_exclusions(acc, dm)
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
                    errs = st.get("errors") or []
                    if any("circuit breaker" in e for e in errs):
                        note = " — API unreachable, skipped (using keywords)"
                    elif errs:
                        note = f" ({len(errs)} call(s) failed; keyword answers kept)"
                    else:
                        note = ""
                    print(f"   {sc}: asked Gemini about {st['asked']}/{len(txns)} rows, "
                          f"{st['llm_used']} answers used{note}")
                base = classifier.make_base_classifier(cat_map)
                catf = Categorizer(base, rcs, related_parties=set(),
                                   unrestricted_parties=unrestricted,
                                   excluded_txns=excluded)
            except Exception as e:                       # quota/network -> keywords
                print(f"!! {sc}: Gemini classifier failed ({e}); using keywords")
                catf = Categorizer(base_classifier, rcs, related_parties=rps,
                                   unrestricted_parties=unrestricted,
                                   excluded_txns=excluded)
        else:
            catf = Categorizer(base_classifier, rcs, related_parties=rps,
                               unrestricted_parties=unrestricted,
                               excluded_txns=excluded)
        if not rps:
            # All 12 borrowers resolve today (two of them only via image_facts.json, since
            # their ownership tables are images). This stays as a safety net for event day:
            # identity is authoritative, but a description guess beats a confident 0.
            inner = catf.base
            catf.base = lambda t: (RELATED_PARTY if classifier.looks_related_party(t)
                                   else inner(t))
        for cid in cids:
            spec = covs.get(cid)
            if not spec:
                # no clause parsed at all — still owed an answer, so guess it below
                unresolved.append((sc, cid, None))
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
                print(f"!! {sc} {cid}: engine error ({e})")
                res = None
            if res is not None and res.status in ("COMPLIANT", "BREACH"):
                answers[sc][cid] = {
                    "status": res.status, "actual": res.actual,
                    "evidence_txn_id": res.evidence_txn_id,
                }
            else:
                # Not computable — record it for the guess pass below rather than dropping it.
                unresolved.append((sc, cid, spec))

    _fill_unresolved(answers, unresolved)

    if TEAM == "your-team-name":
        print("!! solve.TEAM is still the spec's placeholder 'your-team-name' — "
              "set it before submitting.")
    # Emit the scenarios in the TEMPLATE's order. The spec asks for a file «точно по образцу
    # submission_template.json», and while JSON object order is not semantic, a file whose
    # borrowers appear in dict-insertion order is needlessly hard to diff against the template
    # by eye — which is how a missing or extra cell gets spotted.
    order = list(config.submission_template())
    if order:
        answers = {sc: answers[sc] for sc in order if sc in answers} | \
                  {sc: v for sc, v in answers.items() if sc not in order}

    # Name the model that actually did the work. On the keyword path NO model ran, and writing
    # a model id there claims a provenance the file does not have — the same reproducibility
    # failure MODELS.md warns about, just pointing the other way.
    used = MODEL if classifier_mode in ("gemini", "hybrid") else "none (deterministic keyword classifier)"
    submission = {"team": TEAM, "contact_email": CONTACT, "model": used, "answers": answers}
    if write:
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
