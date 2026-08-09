"""Classify every file, route to borrowers, and pick the CURRENT contract per account.

Beating the version trap is the single highest-leverage step: each borrower has an
outdated 2024 contract stamped "НЕДЕЙСТВУЮЩАЯ РЕДАКЦИЯ ... НЕ ПРИМЕНЯЕТСЯ" and a live
2025 contract. Using the wrong one poisons every number for that borrower."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from . import config, pdftext

# The practice release numbered every borrower ACC-####. The real one does not: scenario KC's
# account is `TELE-4471`. A pattern hardcoding "ACC" drops that borrower's documents on the
# floor and costs all of its cells, so the ids the LEDGER actually reported are matched
# literally, with ACC-#### kept as the fallback for a run with no ledger.
_GENERIC_ACC_RE = re.compile(r"ACC-\d{4}")


def account_pattern() -> re.Pattern:
    known = [a for a in config.SCENARIO_TO_ACC.values() if a]
    if not known:
        return _GENERIC_ACC_RE
    alts = "|".join(sorted((re.escape(a) for a in known), key=len, reverse=True))
    return re.compile(rf"(?:{alts}|ACC-\d{{4}})")


# Covenants are not always Article 6: J4's contract numbers them 5.1/5.2/5.3, and X1..X3 carry
# a fourth, 6.4. Anchoring on "6.[123]" silently classifies those contracts as non-contracts.
# One borrower's entire document set is in ENGLISH (ACC-7604 / J4 — a Dutch auditor and a
# «CREDIT AGREEMENT» rather than a «ДОГОВОР БАНКОВСКОГО ЗАЙМА»), so every Russian-only pattern
# here silently classified its contract as not-a-contract and cost all three of its cells.
COVENANT_RE = re.compile(r"Пункт\s+[56]\.\d\b|Section\s+[56]\.\d\b", re.I)
DEAD_RE = re.compile(r"НЕДЕЙСТВУЮЩАЯ|НЕ ПРИМЕНЯЕТСЯ|устаревш|предыдущ(?:ая|ей) редакц|черновик"
                     r"|SUPERSEDED|NOT OPERATIVE|PRIOR-YEAR AGREEMENT|superseded by", re.I)
YEAR_COV_RE = re.compile(r"с (20\d{2})-01-01 по (20\d{2})-12-31")
CONTRACT_RE = re.compile(r"ДОГОВОР БАНКОВСКОГО ЗАЙМА|CREDIT AGREEMENT", re.I)
AUDIT_RE = re.compile(r"аудит|независим\w+ заключени|финансов\w+ отч[её]тност"
                      r"|Notes to the Financial Statements|Agreed-Upon|Registered Auditors", re.I)
KYC_RE = re.compile(r"KYC|клиентск\w+ дось|идентификаци\w+ клиента|надлежащ\w+ проверк"
                    r"|Know Your Customer|customer due-diligence", re.I)
# A dossier is identified by its registration number / title, not by loose keywords. Needed
# because the dossiers mention "финансовой отчётностью" and so also match AUDIT_RE — the
# elif routing below then filed them under "audits" (ACC-7809's dossier ended up there).
STRONG_KYC_RE = re.compile(r"KYC-ACC-\d{4}|Досье\s+«?Знай своего клиент", re.I)
# The challenge spec and the answer key are NOT borrower documents. Both name ACC-7801 in a
# worked example ("txn_id = TXN-P1-0039 ... account_id = ACC-7801"), so routing by ACC id
# alone files the specification itself under ACC-7801's audit reports — and its example
# transaction then surfaces as one of that borrower's reclassifications.
SPEC_RE = re.compile(r"Halyk AI Challenge|submission_template\.json|evidence_txn_id")

# The transaction ledger is DATA, not a borrower document. It names every borrower's account, so
# routing by account id files a 310,000-character CSV under all 27 of them — and then every
# document regex runs over it, 27 times. Two consequences, and the slow one is the lesser:
#   * `reclass` found SIX "reclassifications" inside the ledger's own rows, because the file is
#     full of TXN- ids and prose descriptions. Invented reclassifications move real money
#     between categories.
#   * it cost ~110 seconds per borrower — 45 minutes for a 27-borrower run, silently.
LEDGER_RE = re.compile(r"^[^\n]{0,200}\btxn_id\b[^\n]{0,200}[,;\t][^\n]{0,200}$", re.I | re.M)


@dataclass
class Doc:
    name: str
    ftype: str
    accs: list[str] = field(default_factory=list)
    is_contract: bool = False
    is_audit: bool = False
    is_kyc: bool = False
    has_covenants: bool = False
    cov_year: int | None = None
    outdated: bool = False
    n_dead_markers: int = 0
    is_kyc_dossier: bool = False       # the authoritative ownership file, not a procedure
    is_spec: bool = False              # challenge spec / answer key, not a borrower doc
    is_ledger: bool = False            # the transaction ledger itself — data, not a document


def classify(path: Path) -> Doc:
    ftype = pdftext.true_type(path)
    text = pdftext.extract_text(path)
    accs = sorted(set(account_pattern().findall(text)))
    has_cov = bool(COVENANT_RE.search(text))
    year = None
    m = YEAR_COV_RE.search(text)
    if m:
        year = int(m.group(1))
    n_dead = len(DEAD_RE.findall(text))
    # A contract is the OUTDATED edition when it carries the strong "do not apply"
    # stamp (appears multiple times) — a single stray match is benign boilerplate.
    outdated = n_dead >= 3 or (year is not None and year < 2025 and n_dead >= 1)
    return Doc(
        name=path.name, ftype=ftype, accs=accs,
        is_contract=bool(CONTRACT_RE.search(text)),
        is_audit=bool(AUDIT_RE.search(text)),
        is_kyc=bool(KYC_RE.search(text)),
        has_covenants=has_cov, cov_year=year,
        outdated=outdated, n_dead_markers=n_dead,
        is_kyc_dossier=bool(STRONG_KYC_RE.search(text)),
        is_spec=bool(SPEC_RE.search(text)),
        is_ledger=bool(LEDGER_RE.search(text[:4000])),
    )


def build(save: bool = True) -> dict:
    files = config.dataset_files()
    if not files:
        print(f"!! {config.DATASET} contains no files. Every cell will be empty.")
    # The spec's dataset table says the PDFs arrive inside a `documents/` folder; this release
    # extracted flat. Say which shape actually turned up, because "1 document classified" is
    # otherwise indistinguishable from "the archive is empty".
    nested = {p.parent.name for p in files if p.parent != config.DATASET}
    if nested:
        print(f"   dataset is nested — reading {len(files)} files from "
              f"{sorted(nested)} as well as the root.")
    docs = [classify(p) for p in files]
    by_acc: dict[str, dict] = {}
    for d in docs:
        if d.is_spec or d.is_ledger:   # never route the spec/key/ledger to a borrower
            continue
        for acc in d.accs:
            by_acc.setdefault(acc, {"contracts": [], "audits": [], "kyc": [], "other": []})
            if d.has_covenants and d.is_contract:
                by_acc[acc]["contracts"].append(d.name)
            elif d.is_kyc_dossier:        # strongest signal wins, before the audit keywords
                by_acc[acc]["kyc"].append(d.name)
            elif d.is_audit:
                by_acc[acc]["audits"].append(d.name)
            elif d.is_kyc:
                by_acc[acc]["kyc"].append(d.name)
            else:
                by_acc[acc]["other"].append(d.name)

    doc_by_name = {d.name: d for d in docs}
    current_contract: dict[str, str | None] = {}
    for acc, groups in by_acc.items():
        # current = the contract with covenants that is NOT flagged outdated
        live = [n for n in groups["contracts"] if not doc_by_name[n].outdated]
        # prefer the one whose covenant period is 2025
        live.sort(key=lambda n: (doc_by_name[n].cov_year != 2025, n))
        current_contract[acc] = live[0] if live else None

    # Losing a borrower's contract costs all 3 of its cells, so say so rather than
    # emitting an empty covenant set that looks like "this borrower has no covenants".
    missing = [acc for acc in config.SCENARIO_TO_ACC.values()
               if not current_contract.get(acc)]
    for acc in missing:
        n_out = len([n for n in by_acc.get(acc, {}).get("contracts", [])
                     if doc_by_name[n].outdated])
        print(f"!! {acc}: NO live contract selected ({n_out} outdated candidate(s)). "
              f"Its 3 covenant cells will be empty — check docmap.CONTRACT_RE/DEAD_RE.")

    result = {
        "docs": {d.name: asdict(d) for d in docs},
        "by_acc": by_acc,
        "current_contract": current_contract,
        "accounts_without_contract": missing,
        "scenario_current_contract": {
            sc: current_contract.get(acc)
            for sc, acc in config.SCENARIO_TO_ACC.items()
        },
    }
    if save:
        (config.ROOT / "docmap.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    r = build()
    print(f"Classified {len(r['docs'])} files across {len(r['by_acc'])} accounts.\n")
    print(f"{'scenario':>8}  {'account':>9}  current contract")
    for sc, acc in config.SCENARIO_TO_ACC.items():
        print(f"{sc:>8}  {acc:>9}  {r['current_contract'].get(acc)}")
    dead = [n for n, d in r["docs"].items() if d["outdated"] and d["has_covenants"]]
    print(f"\nOutdated contracts correctly quarantined: {len(dead)}")
