"""Document-reading tests against REAL ground truth.

Unlike reconstruct.py (which synthesises inputs to fit the engine) these assert what the
actual PDFs say, checked against the answer key where the key can see it. This is the only
harness in the repo measuring generalisation rather than self-consistency.

    python -m pipeline.test_docs
"""
from __future__ import annotations
import json
import pathlib
import re

from . import config, docmap, reclass

FAILURES: list[str] = []
_DM = docmap.build(save=False)
_KEY = json.loads(config.ANSWER_KEY.read_text(encoding="utf-8"))["scenarios"]
_ALL_TEXT = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                      for p in (config.TXT_CACHE).glob("*.txt"))


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# --- reclassifications ------------------------------------------------------------------
# The answer key's evidence_txn_id is the transaction whose APPLIED reclassification decides
# a breach. Where the documents name it, we must extract it and mark it applied.
recovered = documented = 0
for sc, v in _KEY.items():
    applied = {r.txn_id for r in reclass.for_account(config.SCENARIO_TO_ACC[sc], _DM) if r.applied}
    for cid, cell in v["covenants"].items():
        ev = cell.get("evidence_txn_id")
        if not ev or ev not in _ALL_TEXT:
            continue                      # 7 of the 9 are only derivable from the ledger
        documented += 1
        recovered += ev in applied
        check(f"{sc} {cid}: evidence {ev} extracted as APPLIED", ev in applied)
check("all documented evidence transactions recovered", recovered == documented,
      f"{recovered}/{documented}")

# A reclassification quoting another borrower (or the spec's example scenario T1) must not
# leak into this borrower's set.
for sc, acc in config.SCENARIO_TO_ACC.items():
    foreign = [r.txn_id for r in reclass.for_account(acc, _DM)
               if r.txn_id.split("-")[1].upper() != sc.upper()]
    check(f"{sc}: no foreign-scenario reclassifications", not foreign, f"got {foreign}")

# Amounts contain periods ("($418,204.37)"), which must not end the clause early —
# the parse must reach the "переклассифицирована ... как <category>" half.
p9 = [r for r in reclass.for_account("ACC-7809", _DM) if r.txn_id == "TXN-P9-0025"]
check("TXN-P9-0025 parsed past the $ amount into from/to categories",
      bool(p9) and p9[0].from_category == "capex" and p9[0].to_category == "payroll",
      f"got {[(r.from_category, r.to_category) for r in p9]}")

# --- related parties --------------------------------------------------------------------
# Membership is an ownership threshold in the KYC dossier, and the thresholds differ per
# borrower. Entities listed BELOW the bar are decoys and must be excluded.
rp9 = reclass.related_parties("ACC-7809", _DM)
check("ACC-7809 related parties = only the >=34.0% holder",
      {n.lower() for n in rp9} == {"ulytau capital llp"}, f"got {sorted(rp9)}")
check("ACC-7809 excludes Ural Haul Systems LLP (31.4% < 34.0%)",
      not any("ural haul" in n.lower() for n in rp9), f"got {sorted(rp9)}")
check("ACC-7809 excludes Kazakhmys Smelting JSC (6.9%)",
      not any("kazakhmys" in n.lower() for n in rp9), f"got {sorted(rp9)}")

# The 36.0% dossier lists Saryarka Terminal Properties LLP at 33.5% — a near-miss decoy.
rp10 = reclass.related_parties("ACC-7810", _DM)
check("ACC-7810 excludes the 33.5% near-miss decoy",
      not any("terminal properties" in n.lower() for n in rp10), f"got {sorted(rp10)}")

# Footnote trap: a stake shown as 48.0% in the table but held indirectly, with the Group's
# real voting rights disclosed as 27.3% — below that dossier's 30.0% bar.
indirect_acc = next((acc for acc in config.SCENARIO_TO_ACC.values()
                     if any("shymkent fuel" in n.lower()
                            for n in reclass.related_parties(acc, _DM))), None)
check("indirect 27.3% holding is not treated as a related party "
      "(table shows 48.0%)", indirect_acc is None, f"leaked into {indirect_acc}")

resolved = sum(1 for acc in config.SCENARIO_TO_ACC.values()
               if reclass.related_parties(acc, _DM))
print(f"\nRelated-party lists resolved for {resolved}/12 borrowers "
      f"(P2 and P6 ship no ownership dossier in this release).")
check("related parties resolved for every borrower whose dossier exists", resolved >= 10,
      f"got {resolved}")

print()
if FAILURES:
    print(f"{len(FAILURES)} DOCUMENT TEST(S) FAILED:")
    for f in FAILURES:
        print("   -", f)
    raise SystemExit(1)
print("ALL DOCUMENT TESTS PASSED")
