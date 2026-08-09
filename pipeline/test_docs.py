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

from . import config, covenants, docmap, pdfimages, pdftext, reclass, solve

FAILURES: list[str] = []
_DM = docmap.build(save=False)
_KEY = json.loads(config.ANSWER_KEY.read_text(encoding="utf-8"))["scenarios"]
# Source documents only. The answer key and the ground-truth decoy must NOT be in here:
# they contain every evidence id, so including them would make "is this id documented?"
# trivially true and silently turn the evidence check into a no-op.
_EXCLUDE = ("submission_template.json", "ground_truth.json")
_ALL_TEXT = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                      for p in config.TXT_CACHE.glob("*.txt")
                      if not p.name.startswith(_EXCLUDE))


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

# The challenge spec and the answer key both name ACC-7801 in a worked example, so routing
# by ACC id alone files them under that borrower — and the spec's example transaction
# (TXN-P1-0039) then surfaces as one of P1's reclassifications.
_spec_docs = {n for n, d in _DM["docs"].items() if d.get("is_spec")}
check("challenge spec + answer key are detected as spec files",
      {"028324997d3c.pdf", "CASE.ru.md", "submission_template.json"} <= _spec_docs,
      f"got {sorted(_spec_docs)}")
for acc, groups in _DM["by_acc"].items():
    routed = {n for names in groups.values() for n in names}
    check(f"{acc}: no spec/answer-key document routed to it", not (routed & _spec_docs),
          f"got {sorted(routed & _spec_docs)}")
check("the spec's example txn TXN-P1-0039 is not a P1 reclassification",
      "TXN-P1-0039" not in {r.txn_id for r in reclass.for_account("ACC-7801", _DM)})

# A reclassification quoting another borrower (or the spec's example scenario T1) must not
# leak into this borrower's set.
for sc, acc in config.SCENARIO_TO_ACC.items():
    # reclassifications identified by amount+counterparty carry no txn id to check
    foreign = [r.txn_id for r in reclass.for_account(acc, _DM)
               if r.txn_id and r.txn_id.split("-")[1].upper() != sc.upper()]
    check(f"{sc}: no foreign-scenario reclassifications", not foreign, f"got {foreign}")

# Amounts contain periods ("($418,204.37)"), which must not end the clause early —
# the parse must reach the "переклассифицирована ... как <category>" half.
p9 = [r for r in reclass.for_account("ACC-7809", _DM) if r.txn_id == "TXN-P9-0025"]
check("TXN-P9-0025 parsed past the $ amount into from/to categories",
      bool(p9) and p9[0].from_category == "capex" and p9[0].to_category == "payroll",
      f"got {[(r.from_category, r.to_category) for r in p9]}")

# A FINAL audit report supersedes the interim worksheets wholesale — both documents say so
# outright. B1's draft moves TXN-B1-0023 ($6,166,592.66) from opex to utilities, and B1 6.2 is
# max(payroll, utilities), so honouring the draft turns a COMPLIANT cell into a BREACH. The
# answer key's $1,284,663.42 is consistent only with the draft being ignored.
_b1 = reclass.for_account("ACC-7201", _DM)
check("B1: the superseded draft's TXN-B1-0023 reclassification is not applied",
      not any(r.txn_id == "TXN-B1-0023" and r.applied for r in _b1),
      f"got {[(r.txn_id, r.to_category, r.applied) for r in _b1]}")

# The trap underneath that one: the final report is recognised BY the sentence "Настоящий отчёт
# заменяет любые промежуточные ведомости", which contains "промежуточн" — so a bare interim
# keyword search reads the superseding document as the superseded one.
_final = pdftext.extract_text(config.dataset_path("46587c5f8e49.pdf"))
check("the final report is not misread as an interim worksheet",
      not ((not reclass._SUPERSEDES_RE.search(_final))
           and reclass.INTERIM_RE.search(_final)))
check("the interim worksheet is still recognised as interim",
      bool(reclass.INTERIM_RE.search(
          pdftext.extract_text(config.dataset_path("2d42722d9dec.pdf")))))

# Borrowers whose reclassifications come from a final report must be untouched by the rule.
check("P9's final-report reclassification survives the interim/final split",
      any(r.txn_id == "TXN-P9-0025" and r.applied
          for r in reclass.for_account("ACC-7809", _DM)))

# A blank cell scores zero with certainty; a guess cannot score less. Whatever else fails,
# every one of the 36 cells must carry a status and an actual. This is checked on the WORST
# case — no ledger at all — because that is the path that used to emit 36 blanks.
_sub = solve.solve(None, write=False)   # must not clobber a real submission.json
_cells = [(sc, cid, c) for sc, covs in _sub["answers"].items() for cid, c in covs.items()]
check("solve emits all 36 cells even with no ledger", len(_cells) == 36, f"got {len(_cells)}")
check("no cell is left without a status",
      all(c["status"] in ("COMPLIANT", "BREACH") for _, _, c in _cells),
      f"blank: {[(s, c) for s, c, v in _cells if not v['status']]}")
check("no cell is left without an actual",
      all(c["actual"] is not None for _, _, c in _cells),
      f"blank: {[(s, c) for s, c, v in _cells if v['actual'] is None]}")
check("the team name is set, not the spec placeholder", _sub["team"] != "your-team-name",
      f"got {_sub['team']!r}")

# Images are the dataset's only silent failure mode: pdftotext returns nothing, so a document
# whose ownership table is a picture reads as an ordinary file with no covenant data and the
# pipeline reports a confident zero. Four such documents exist here and are transcribed. Any
# OTHER document with a sizeable image is one nobody has read.
_unread = pdfimages.untranscribed_image_docs()
check("every document carrying a sizeable image is transcribed", not _unread,
      f"unread: {_unread}")

# Verified transcriptions must outrank model-vision ones. `cli ocr` writes to a separate cache
# file precisely so unverified output can never overwrite a fact checked against the picture.
_ocr = config.CACHE / "image_facts_ocr.json"
if _ocr.exists():
    import json as _j
    _raw = _j.loads(_ocr.read_text(encoding="utf-8"))
    _verified = _j.loads((config.ROOT / "image_facts.json").read_text(encoding="utf-8"))
    _facts = reclass.image_facts()
    check("model-vision transcriptions never override a hand-verified account",
          all(_facts.get(a) == v for a, v in _verified.items() if not a.startswith("_")))

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
check("related parties resolve for ALL 12 borrowers", resolved == 12, f"got {resolved}")

# --- determinations that exist only inside embedded images ------------------------------
# pdftotext sees nothing in these, so a text-only pipeline reports a confident wrong answer.
check("ACC-7802 related party comes from the image ownership table (>=25.0%)",
      {n.lower() for n in reclass.related_parties("ACC-7802", _DM)}
      == {"zhetysu capital partners llp"},
      f"got {sorted(reclass.related_parties('ACC-7802', _DM))}")
check("ACC-7802 excludes Tien Shan Advisory Bureau (23.4% < 25.0%)",
      not any("tien shan" in n.lower() for n in reclass.related_parties("ACC-7802", _DM)))
check("ACC-7806 related party comes from the scanned dossier (>=40.0%)",
      {n.lower() for n in reclass.related_parties("ACC-7806", _DM)}
      == {"taraz holding group llp"},
      f"got {sorted(reclass.related_parties('ACC-7806', _DM))}")
check("ACC-7806 excludes Taraz Kiln Services LLP (38.1% < 40.0%)",
      not any("kiln" in n.lower() for n in reclass.related_parties("ACC-7806", _DM)))
check("ACC-7809 unrestricted subsidiary = the one under 50% pledged",
      {n.lower() for n in reclass.unrestricted_subsidiaries("ACC-7809")}
      == {"zhezkazgan processing holdings llp"},
      f"got {sorted(reclass.unrestricted_subsidiaries('ACC-7809'))}")
check("ACC-7804 EBITDA add-back applies the $300k floor (excludes the $251k item)",
      abs(reclass.ebitda_addback("ACC-7804") - (342905.28 + 481247.63)) < 0.005,
      f"got {reclass.ebitda_addback('ACC-7804')}")

# The images must remain discoverable — this is how the facts above were found at all.
_img_docs = {n for n, _ in pdfimages.find_image_docs()}
for need in ("f5e315b390df.pdf", "6686c0493014.pdf", "2fe3878667db.pdf", "abe2474bd443.pdf"):
    check(f"{need} is flagged as carrying an embedded image", need in _img_docs)

# --- document routing & clause slicing --------------------------------------------------
check("every borrower has a live 2025 contract selected",
      not _DM.get("accounts_without_contract"),
      f"missing {_DM.get('accounts_without_contract')}")

for sc, acc in config.SCENARIO_TO_ACC.items():
    d = _DM["docs"].get(_DM["current_contract"].get(acc) or "", {})
    check(f"{sc}: current contract is the 2025 edition, not the 2024 decoy",
          d.get("cov_year") == 2025 and not d.get("outdated"),
          f"year={d.get('cov_year')} outdated={d.get('outdated')}")

# Clause slicing: exactly one chunk per clause id. A duplicate would mean a cross-reference
# elsewhere in the contract silently OVERWRITES the real clause (last write wins).
for sc, acc in config.SCENARIO_TO_ACC.items():
    contract = _DM["current_contract"][acc]
    text = pdftext.extract_text(config.dataset_path(contract))
    ids = [m.group(1) for m in
           (re.match(r"Пункт\s+(6\.[123])", ch) for ch in covenants.CLAUSE_RE.findall(text))
           if m]
    check(f"{sc}: exactly one text chunk per clause 6.1/6.2/6.3",
          sorted(ids) == ["6.1", "6.2", "6.3"], f"got {ids}")

# raw_text must carry the WHOLE clause — the metric-defining sentence is often last.
_specs = covenants.build(use_llm=False, save=False)
for sc, acc in config.SCENARIO_TO_ACC.items():
    full = covenants.clause_texts(_DM["current_contract"][acc])
    for cid, ctext in full.items():
        stored = _specs[sc]["covenants"][cid]["raw_text"]
        check(f"{sc} {cid}: raw_text is not truncated", stored == ctext,
              f"stored {len(stored)} of {len(ctext)} chars")

# --- the archive may not extract flat -----------------------------------------------------
# The practice release puts every PDF directly in dataset/. The spec's dataset table does not
# promise that: it says the documents arrive inside a `documents/` folder. Discovery used to be
# `DATASET.iterdir()` filtered by is_file(), which skips a subdirectory in silence — against a
# nested archive that classified ONE document, resolved ZERO accounts and wrote 36 empty cells,
# with no exception and no `!!` line naming the cause. Total loss, quietly.
#
# So the same corpus is re-read through a nested layout and must come out identical.
def _nested_archive_reads_the_same() -> None:
    import shutil
    import tempfile
    from . import retrieval

    flat_docs, flat_accs = len(_DM["docs"]), len(_DM["by_acc"])
    flat_contracts = sum(1 for v in _DM["current_contract"].values() if v)
    real_dataset, real_key = config.DATASET, config.ANSWER_KEY

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nested-archive-"))
    try:
        docs_dir = tmp / "dataset" / "documents"
        docs_dir.mkdir(parents=True)
        for p in config.dataset_files():
            shutil.copy2(p, docs_dir / p.name)
        shutil.move(str(docs_dir / "submission_template.json"),
                    str(tmp / "dataset" / "submission_template.json"))

        config.DATASET = tmp / "dataset"
        config.ANSWER_KEY = config.DATASET / "submission_template.json"
        config.reset_dataset_cache()
        retrieval.reset()

        nested = docmap.build(save=False)
        n_contracts = sum(1 for v in nested["current_contract"].values() if v)
        check("nested archive: every document is still found",
              len(nested["docs"]) == flat_docs,
              f"{len(nested['docs'])} vs {flat_docs} flat")
        check("nested archive: every borrower is still routed",
              len(nested["by_acc"]) == flat_accs,
              f"{len(nested['by_acc'])} vs {flat_accs} flat")
        check("nested archive: every live contract is still selected",
              n_contracts == flat_contracts, f"{n_contracts} vs {flat_contracts} flat")
        check("nested archive: related parties still resolve 12/12",
              all(reclass.related_parties(a, nested)
                  for a in config.SCENARIO_TO_ACC.values()))
        check("nested archive: retrieval still indexes the corpus",
              retrieval.index().n > 200, f"{retrieval.index().n} passages")
    finally:
        config.DATASET, config.ANSWER_KEY = real_dataset, real_key
        config.reset_dataset_cache()
        retrieval.reset()
        shutil.rmtree(tmp, ignore_errors=True)


_nested_archive_reads_the_same()

print()
if FAILURES:
    print(f"{len(FAILURES)} DOCUMENT TEST(S) FAILED:")
    for f in FAILURES:
        print("   -", f)
    raise SystemExit(1)
print("ALL DOCUMENT TESTS PASSED")
