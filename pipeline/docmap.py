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

ACC_RE = re.compile(r"ACC-\d{4}")
COVENANT_RE = re.compile(r"Пункт\s+6\.[123]\b")
DEAD_RE = re.compile(r"НЕДЕЙСТВУЮЩАЯ|НЕ ПРИМЕНЯЕТСЯ|устаревш|предыдущ(?:ая|ей) редакц|черновик")
YEAR_COV_RE = re.compile(r"с (20\d{2})-01-01 по (20\d{2})-12-31")
CONTRACT_RE = re.compile(r"ДОГОВОР БАНКОВСКОГО ЗАЙМА")
AUDIT_RE = re.compile(r"аудит|независим\w+ заключени|финансов\w+ отч[её]тност", re.I)
KYC_RE = re.compile(r"KYC|клиентск\w+ дось|идентификаци\w+ клиента|надлежащ\w+ проверк", re.I)


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


def classify(path: Path) -> Doc:
    ftype = pdftext.true_type(path)
    text = pdftext.extract_text(path)
    accs = sorted(set(ACC_RE.findall(text)))
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
    )


def build(save: bool = True) -> dict:
    docs = [classify(p) for p in sorted(config.DATASET.iterdir()) if p.is_file()]
    by_acc: dict[str, dict] = {}
    for d in docs:
        for acc in d.accs:
            by_acc.setdefault(acc, {"contracts": [], "audits": [], "kyc": [], "other": []})
            if d.has_covenants and d.is_contract:
                by_acc[acc]["contracts"].append(d.name)
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

    result = {
        "docs": {d.name: asdict(d) for d in docs},
        "by_acc": by_acc,
        "current_contract": current_contract,
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
