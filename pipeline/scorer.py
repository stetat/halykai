"""Local scorer — implements the exact rubric from CASE.ru.md.

Per cell (max 1.0):
  status   0.50  exact "COMPLIANT"/"BREACH"; wrong status => WHOLE CELL = 0
  actual   0.30  linear decay: 0.30 * max(0, 1 - e/0.05), e = |pred-key|/|key|
  evidence 0.20  if key evidence is non-null: exact match else 0
                 if key evidence IS null: rides the actual scale (earned, not free)
"""
from __future__ import annotations
import json
from pathlib import Path
from . import config

W_STATUS, W_ACTUAL, W_EVID = 0.50, 0.30, 0.20


def score_cell(pred: dict | None, key: dict) -> tuple[float, dict]:
    detail = {"status": 0.0, "actual": 0.0, "evidence": 0.0}
    if not isinstance(pred, dict):
        return 0.0, detail
    if pred.get("status") != key.get("status"):
        return 0.0, detail          # wrong/invalid status zeroes the cell
    detail["status"] = W_STATUS

    a_key, a_pred = key.get("actual"), pred.get("actual")
    if isinstance(a_pred, (int, float)) and not isinstance(a_pred, bool) \
            and isinstance(a_key, (int, float)) and a_key != 0:
        e = abs(a_pred - a_key) / abs(a_key)
        detail["actual"] = W_ACTUAL * max(0.0, 1.0 - e / 0.05)

    if key.get("evidence_txn_id") is not None:
        detail["evidence"] = W_EVID if pred.get("evidence_txn_id") == key["evidence_txn_id"] else 0.0
    else:
        # null-evidence cell: the 0.20 decays on the same scale as actual
        detail["evidence"] = W_EVID * (detail["actual"] / W_ACTUAL if W_ACTUAL else 0.0)

    return round(sum(detail.values()), 6), detail


def load_key() -> dict:
    data = json.loads(config.ANSWER_KEY.read_text(encoding="utf-8"))
    return {sc: v["covenants"] for sc, v in data["scenarios"].items()}


def score_submission(submission: dict, verbose: bool = True) -> float:
    key = load_key()
    answers = submission.get("answers", {})
    total = 0.0
    n = 0
    rows = []
    for sc, covs in key.items():
        for cid, kcell in covs.items():
            pred = answers.get(sc, {}).get(cid)
            s, d = score_cell(pred, kcell)
            total += s
            n += 1
            rows.append((sc, cid, s, d, kcell, pred))
    if verbose:
        print(f"{'cell':>7} {'score':>6}  {'st':>4} {'act':>5} {'ev':>4}   key.status / pred.status")
        for sc, cid, s, d, k, p in rows:
            ps = (p or {}).get("status", "—")
            print(f"{sc+' '+cid:>7} {s:6.3f}  {d['status']:4.2f} {d['actual']:5.3f} "
                  f"{d['evidence']:4.2f}   {k['status']} / {ps}")
    avg = total / n if n else 0.0
    print(f"\nTOTAL {total:.3f} / {n}  =  mean cell score {avg:.4f}")
    print("(Final leaderboard weights cells 'by complexity'; weights aren't published, "
          "so this is the unweighted mean.)")
    return avg


def score_file(path: str | Path) -> float:
    sub = json.loads(Path(path).read_text(encoding="utf-8"))
    return score_submission(sub)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        score_file(sys.argv[1])
    else:
        # Sanity check: scoring the answer key against itself must give 1.0000.
        data = json.loads(config.ANSWER_KEY.read_text(encoding="utf-8"))
        perfect = {"answers": {sc: v["covenants"] for sc, v in data["scenarios"].items()}}
        score_submission(perfect)
