"""Validate the document-reading half against the answer key.

For each covenant we extract an operator + threshold from the CURRENT contract, and
the answer key gives (actual, status). A correct extraction must "bracket": the key's
actual must sit on the side of the threshold implied by the key's status. Any row that
fails to bracket means either a wrong threshold, wrong operator, or wrong borrower map."""
from __future__ import annotations
import json
from . import config, covenants, scorer


def _brackets(op: str, actual: float, thr: float, status: str) -> bool | None:
    if not isinstance(actual, (int, float)) or not isinstance(thr, (int, float)):
        return None
    if abs(actual - thr) < 0.005 * max(abs(thr), 1e-9):
        return None   # within 2-dp rounding of the boundary: undecidable from the key alone
    if op in ("<=", "<"):
        implied = "COMPLIANT" if actual <= thr else "BREACH"
    elif op in (">=", ">"):
        implied = "COMPLIANT" if actual >= thr else "BREACH"
    else:
        return None
    return implied == status


def run(use_llm: bool = True) -> dict:
    specs = covenants.build(use_llm=use_llm, save=True)
    key = scorer.load_key()
    ok = bad = unknown = 0
    print(f"{'cell':>7}  {'op':>2} {'threshold':>13}  {'key.actual':>13}  {'status':>9}  bracket")
    print("-" * 66)
    for sc in config.SCENARIO_TO_ACC:
        for cid in ("6.1", "6.2", "6.3"):
            spec = specs.get(sc, {}).get("covenants", {}).get(cid, {})
            kcell = key.get(sc, {}).get(cid, {})
            op, thr = spec.get("operator", "?"), spec.get("threshold")
            b = _brackets(op, kcell.get("actual"), thr, kcell.get("status"))
            mark = {True: "OK", False: "MISMATCH", None: "n/a"}[b]
            if b is True: ok += 1
            elif b is False: bad += 1
            else: unknown += 1
            thr_s = f"{thr:,.2f}" if isinstance(thr, (int, float)) else str(thr)
            print(f"{sc+' '+cid:>7}  {str(op):>2} {thr_s:>13}  "
                  f"{str(kcell.get('actual')):>13}  {str(kcell.get('status')):>9}  {mark}")
    print("-" * 66)
    print(f"bracket OK={ok}  MISMATCH={bad}  n/a(ratio-vs-abs or unparsed)={unknown}  / 36")
    print("\nNote: 'n/a' is expected where the covenant metric is a ratio but the threshold "
          "is absolute (or vice-versa) — those need the ledger to resolve, not a bracket test.")
    return {"ok": ok, "mismatch": bad, "na": unknown}


if __name__ == "__main__":
    run(use_llm=True)
