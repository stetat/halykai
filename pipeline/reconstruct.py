"""Reconstructed-ledger integration harness.

The real ledger is withheld, so we synthesise, per covenant, a small set of transactions
whose category aggregates reproduce the answer key's `actual`, then run them through the
REAL engine and grade with the REAL scorer against the REAL answer key. This proves the
ledger -> status/actual/evidence -> score chain end-to-end (including reclassification-driven
evidence) for every covenant KIND the engine computes from ledger data.

It is an integration fixture, not a generalisation score: it verifies the pipeline has no
wiring/arithmetic/rounding/evidence bugs. Covenant kinds needing audited-statement inputs
(leverage / interest-cover: EBITDA, interest, debt) are reported as 'needs financials'.

Run:  python -m pipeline.reconstruct
"""
from __future__ import annotations
import json
from .ledger import Txn
from . import covenants, engine, scorer, config
from .engine import (Categorizer, Reclass, CAPEX, OPEX, LEASE, REVENUE,
                     RELATED_PARTY, OTHER, FINANCING, INTEREST, TAX, UTILITIES,
                     GROUP_CAPEX, UNRESTRICTED_ASSETS, INSURANCE, PAYROLL)

LEDGER_KINDS = {"CAPEX_INTENSITY", "MIN_REVENUE", "RELATED_PARTY_ABS",
                "RELATED_PARTY_RATIO", "GENERIC", "MAX_LINE", "REVENUE_LESS_MAX"}


def _tx(txn_id, cat, amount):
    # description carries the category tag; base classifier just echoes it.
    t = Txn(txn_id, "ACC", "2025-06-01", amount, "USD", "cp", cat, "S")
    t.amount_usd = amount
    return t


def _base(t: Txn) -> str:
    return t.description  # the synthetic category tag


def _effective(A, T, op, status):
    """Nudge the underlying value just past/within the threshold so the reconstructed
    STATUS matches the key, even when key.actual rounds exactly onto the threshold.
    Stays within rounding of A, so the displayed 2-dp actual is unchanged."""
    if T is None or A is None or engine._status(op, A, T) == status:
        return A
    up = op in ("<=", "<")           # for a ceiling, BREACH is above the threshold
    if status == "BREACH":
        return T * (1.0001 if up else 0.9999)
    return T * (0.9999 if up else 1.0001)


def build_cell(sc, cid, spec, kcell):
    """Return (txns, categorizer, reclasses, extras) reproducing kcell for this kind."""
    kind = engine.classify_kind(spec)
    A = kcell["actual"]
    T = spec.get("threshold")
    op = spec.get("operator", "<=")
    Ae = _effective(A, T, op, kcell["status"])   # status-honouring underlying value
    ev = kcell.get("evidence_txn_id")
    breach_ev = ev and kcell["status"] == "BREACH"
    txns: list[Txn] = []
    reclasses: list[Reclass] = []
    extras: dict = {}

    if kind == "CAPEX_INTENSITY":                # capex / (opex+lease) = Ae
        base = 1_000_000.0
        txns = [_tx(f"TXN-{sc}-c", CAPEX, -Ae * base), _tx(f"TXN-{sc}-o", OPEX, -base)]
    elif kind == "MIN_REVENUE":                  # sum(revenue) = Ae
        txns = [_tx(f"TXN-{sc}-r", REVENUE, Ae)]
    elif kind == "RELATED_PARTY_ABS":
        if breach_ev:                            # reclassification pushes total over T
            base_amt = round(T * 0.99, 2)
            ev_amt = round(Ae - base_amt, 2)
            txns = [_tx(f"TXN-{sc}-rp", RELATED_PARTY, -base_amt), _tx(ev, OTHER, -ev_amt)]
            reclasses = [Reclass(ev, RELATED_PARTY, OTHER, applied=True)]
        else:
            txns = [_tx(f"TXN-{sc}-rp", RELATED_PARTY, -Ae)]
    elif kind == "RELATED_PARTY_RATIO":          # related/<base> = Ae
        # The base is revenue for most contracts but OPEX for P6 6.1 ("0.08x Операционных
        # расходов"); build whichever the clause names so the fixture tracks the engine.
        _t = f"{spec.get('name','')} {spec.get('raw_text','')}".lower()
        base_cat = OPEX if "операционных расход" in _t else REVENUE
        rev = 10_000_000.0
        base_amt = rev if base_cat == REVENUE else -rev
        if breach_ev:
            base_rp = round(T * 0.99 * rev, 2)   # ratio just below T (compliant)
            ev_amt = round(Ae * rev - base_rp, 2)
            txns = [_tx(f"TXN-{sc}-r", base_cat, base_amt),
                    _tx(f"TXN-{sc}-rp", RELATED_PARTY, -base_rp), _tx(ev, OTHER, -ev_amt)]
            reclasses = [Reclass(ev, RELATED_PARTY, OTHER, applied=True)]
        else:
            txns = [_tx(f"TXN-{sc}-r", base_cat, base_amt),
                    _tx(f"TXN-{sc}-rp", RELATED_PARTY, -Ae * rev)]
    elif kind == "MAX_LINE":                     # max(line1, line2, ...) = Ae, NOT their sum
        cats = [c for c in engine.spec_categories(spec) if c != REVENUE]
        if not cats:
            return None
        txns = [_tx(f"TXN-{sc}-m0", cats[0], -Ae)]
        txns += [_tx(f"TXN-{sc}-m{i}", c, -round(Ae * 0.5, 2))
                 for i, c in enumerate(cats[1:], 1)]
        extras = {"categories": cats}
    elif kind == "REVENUE_LESS_MAX":             # revenue - max(line1, line2, ...) = Ae
        cats = [c for c in engine.spec_categories(spec) if c != REVENUE]
        if not cats:
            return None
        big = 1_000_000.0
        txns = [_tx(f"TXN-{sc}-rev", REVENUE, Ae + big), _tx(f"TXN-{sc}-m0", cats[0], -big)]
        txns += [_tx(f"TXN-{sc}-m{i}", c, -0.5 * big) for i, c in enumerate(cats[1:], 1)]
        extras = {"categories": cats}
    elif kind == "GENERIC":                       # sum(distinct category) = Ae
        cat = f"gen_{sc}_{cid}"
        txns = [_tx(f"TXN-{sc}-{cid}", cat, -Ae)]
        extras = {"category": cat}
    elif kind == "RATIO":                          # signed-category leverage/cover ratios
        f = engine.ratio_formula(spec)
        fid = f["id"]
        D = 1_000_000.0
        if fid == "interest_cover":                # EBITDA / interest = Ae
            if breach_ev:
                ebitda = 2_100_000.0
                ev_int = round(ebitda / Ae - D, 2)   # extra interest that tips ratio below thr
                txns = [_tx(f"TXN-{sc}-rev", REVENUE, ebitda + D), _tx(f"TXN-{sc}-op", OPEX, -D),
                        _tx(f"TXN-{sc}-int", INTEREST, -D), _tx(ev, OTHER, -ev_int)]
                reclasses = [Reclass(ev, INTEREST, OTHER, applied=True)]
            else:
                txns = [_tx(f"TXN-{sc}-rev", REVENUE, Ae * D + D), _tx(f"TXN-{sc}-op", OPEX, -D),
                        _tx(f"TXN-{sc}-int", INTEREST, -D)]
        elif fid == "cover_sources":               # (rev+fin)/(opex+capex) = Ae
            if breach_ev:
                e = 100_000.0
                X = round(Ae * (D + e), 2)           # revenue reclassified into opex tips ratio
                txns = [_tx(f"TXN-{sc}-rev", REVENUE, X), _tx(f"TXN-{sc}-op", OPEX, -0.5 * D),
                        _tx(f"TXN-{sc}-cap", CAPEX, -0.5 * D), _tx(ev, REVENUE, e)]
                reclasses = [Reclass(ev, OPEX, REVENUE, applied=True)]
            else:
                txns = [_tx(f"TXN-{sc}-rev", REVENUE, Ae * D), _tx(f"TXN-{sc}-op", OPEX, -0.5 * D),
                        _tx(f"TXN-{sc}-cap", CAPEX, -0.5 * D)]
        elif fid == "springing_leverage":          # financing/EBITDA = Ae, trigger financing>$4M
            fin = 5_000_000.0                        # > $4M so the covenant is active
            ebitda = round(fin / Ae, 2)
            txns = [_tx(f"TXN-{sc}-fin", FINANCING, fin),
                    _tx(f"TXN-{sc}-rev", REVENUE, ebitda + D), _tx(f"TXN-{sc}-op", OPEX, -D)]
        elif fid == "ebitda_margin":               # EBITDA/revenue = Ae
            txns = [_tx(f"TXN-{sc}-rev", REVENUE, D), _tx(f"TXN-{sc}-op", OPEX, -(D - Ae * D))]
        elif fid == "group_capex_ebitda":          # group capex / EBITDA = Ae
            txns = [_tx(f"TXN-{sc}-gc", GROUP_CAPEX, -Ae * D),
                    _tx(f"TXN-{sc}-rev", REVENUE, 2 * D), _tx(f"TXN-{sc}-op", OPEX, -D)]
        elif fid == "tax_util_ebitda":             # (tax+utilities)/EBITDA = Ae
            txns = [_tx(f"TXN-{sc}-tax", TAX, -Ae * D / 2), _tx(f"TXN-{sc}-ut", UTILITIES, -Ae * D / 2),
                    _tx(f"TXN-{sc}-rev", REVENUE, 2 * D), _tx(f"TXN-{sc}-op", OPEX, -D)]
        elif fid == "unrestricted_assets":         # assets to unrestricted subs / capex = Ae
            if breach_ev:                            # reclass moves capex->payroll, shrinking denom
                txns = [_tx(f"TXN-{sc}-ua", UNRESTRICTED_ASSETS, -round(Ae * D, 2)),
                        _tx(f"TXN-{sc}-cap", CAPEX, -D), _tx(ev, CAPEX, -0.5 * D)]
                reclasses = [Reclass(ev, PAYROLL, CAPEX, applied=True)]
            else:
                txns = [_tx(f"TXN-{sc}-ua", UNRESTRICTED_ASSETS, -Ae * D), _tx(f"TXN-{sc}-cap", CAPEX, -D)]
        elif fid == "insurance_cover":             # insurance / (lease+utilities) = Ae
            txns = [_tx(f"TXN-{sc}-ins", INSURANCE, -Ae * D),
                    _tx(f"TXN-{sc}-le", LEASE, -0.5 * D), _tx(f"TXN-{sc}-ut", UTILITIES, -0.5 * D)]
        elif fid == "revenue_cover_payroll_util":  # revenue / (payroll+utilities) = Ae
            txns = [_tx(f"TXN-{sc}-rev", REVENUE, Ae * D),
                    _tx(f"TXN-{sc}-pay", PAYROLL, -0.5 * D), _tx(f"TXN-{sc}-ut", UTILITIES, -0.5 * D)]
        else:
            return None
    else:
        return None

    return txns, Categorizer(_base, reclasses), reclasses, extras


def run():
    specs = covenants.build(use_llm=False, save=False)
    key = scorer.load_key()
    answers: dict[str, dict] = {}
    covered = skipped = 0
    ev_checked = ev_ok = 0

    for sc in config.SCENARIO_TO_ACC:
        answers[sc] = {}
        for cid in ("6.1", "6.2", "6.3"):
            spec = specs[sc]["covenants"].get(cid, {})
            kcell = key[sc][cid]
            built = build_cell(sc, cid, spec, kcell)
            if built is None:
                answers[sc][cid] = {"status": None, "actual": None, "evidence_txn_id": None}
                skipped += 1
                continue
            txns, catf, reclasses, extras = built
            res = engine.evaluate(spec, txns, catf, reclasses, extras)
            answers[sc][cid] = {"status": res.status, "actual": res.actual,
                                "evidence_txn_id": res.evidence_txn_id}
            covered += 1
            if kcell.get("evidence_txn_id"):
                ev_checked += 1
                ev_ok += int(res.evidence_txn_id == kcell["evidence_txn_id"])

    submission = {"team": "recon", "contact_email": config, "model": "engine",
                  "answers": answers}
    submission["contact_email"] = "reconstruct@local"
    (config.ROOT / "submission_reconstructed.json").write_text(
        json.dumps(submission, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Reconstructed {covered} ledger-computable cells; {skipped} skipped "
          f"(leverage/cover: need audited-statement inputs).")
    print(f"Evidence transactions reproduced: {ev_ok}/{ev_checked} "
          f"(reclassification leave-one-out).\n")
    avg = scorer.score_submission(submission, verbose=False)
    # score restricted to the covered cells (the fair denominator for this harness)
    total = 0.0
    for sc in config.SCENARIO_TO_ACC:
        for cid in ("6.1", "6.2", "6.3"):
            if answers[sc][cid]["status"] is not None:
                s, _ = scorer.score_cell(answers[sc][cid], key[sc][cid])
                total += s
    print(f"\nScore on the {covered} ledger-computable cells: "
          f"{total:.3f}/{covered} = {total/covered:.4f}")
    print(f"Score across all 36 cells (leverage/cover left empty): "
          f"{avg*36:.3f}/36 = {avg:.4f}")
    return submission


if __name__ == "__main__":
    run()
