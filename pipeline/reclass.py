"""Extract auditor reclassifications + related parties from the documents.

Audit reports carry a "Дополнение о соблюдении ковенантов" with reclassifications:
  APPLIED:  "Операция TXN-..., первоначально учтённая как <A> ($amt), переклассифицирована
             ... как <B>."
  REJECTED: "...рассматривалась на предмет ... переклассификации ...; первоначальная
             классификация сохраняется, ... корректировка ... не производилась."
Only APPLIED reclassifications change `actual`; the REJECTED ones are traps (and the
'ПРОМЕЖУТОЧНАЯ/предварительн' interim worksheets are drafts — lower trust than the final report)."""
from __future__ import annotations
import re
from . import config, docmap, pdftext
from .engine import (Reclass, CAPEX, OPEX, LEASE, REVENUE, RELATED_PARTY,
                     INSURANCE, PAYROLL, OTHER)

_CAT_MAP = [
    (r"капитальн\w+ затрат|капвложен", CAPEX),
    (r"операционн\w+ расход", OPEX),
    (r"арендн\w+|лизинг", LEASE),
    (r"выручк", REVENUE),
    (r"страхов\w+ преми", INSURANCE),
    (r"оплат\w+ труда|заработн|payroll", PAYROLL),
    (r"аффилированн|связанн\w+ сторон|related", RELATED_PARTY),
]
REJECT_RE = re.compile(r"не\s+производил|сохран\w+|без\s+корректир|не\s+переклассифиц", re.I)
INTERIM_RE = re.compile(r"промежуточн|предварительн|черновик|interim|preliminary", re.I)
# TXN, original category, amount, [new category]
RECLASS_RE = re.compile(
    r"(TXN-[A-Za-z0-9-]+)[^.]*?первоначально[^.]*?как\s+([^.,($]+?)\s*"
    r"(?:\(\s*\$?([0-9][0-9\s.,]*[0-9])\s*\))?[^.]*?"
    r"(?:переклассифиц\w+[^.]*?как\s+([^.,\n]+))?\.",
    re.S | re.I)


def _to_cat(text: str | None) -> str:
    if not text:
        return OTHER
    low = text.lower()
    for pat, cat in _CAT_MAP:
        if re.search(pat, low):
            return cat
    return OTHER


def _amt(s: str | None) -> float | None:
    if not s:
        return None
    s = s.replace(" ", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def for_account(acc: str, dm: dict | None = None) -> list[Reclass]:
    dm = dm or docmap.build(save=False)
    groups = dm["by_acc"].get(acc, {})
    # prefer the final audit report over interim worksheets
    audit_docs = groups.get("audits", []) + groups.get("other", [])
    out: dict[str, Reclass] = {}
    for name in audit_docs:
        text = pdftext.extract_text(config.DATASET / name)
        interim = bool(INTERIM_RE.search(text))
        for m in RECLASS_RE.finditer(text):
            txn, from_txt, amt_txt, to_txt = m.groups()
            applied = bool(to_txt) and not REJECT_RE.search(m.group(0))
            rc = Reclass(txn_id=txn, to_category=_to_cat(to_txt),
                         from_category=_to_cat(from_txt), applied=applied)
            # final report wins over interim; applied wins over a prior rejected read
            prev = out.get(txn)
            if prev is None or (not interim) or (rc.applied and not prev.applied):
                out[txn] = rc
    return list(out.values())


def related_parties(acc: str, dm: dict | None = None) -> set[str]:
    """Related-party counterparties named in the KYC dossier (IAS 24 disclosures)."""
    dm = dm or docmap.build(save=False)
    groups = dm["by_acc"].get(acc, {})
    parties: set[str] = set()
    for name in groups.get("kyc", []) + groups.get("other", []):
        text = pdftext.extract_text(config.DATASET / name)
        # company-like names (…LLP / …JSC / …LLC / ТОО / АО)
        for m in re.finditer(r"([A-ZА-Я][\w&.\- ]{3,60}?(?:LLP|JSC|LLC|ТОО|АО|Ltd))", text):
            parties.add(m.group(1).strip())
    return parties


if __name__ == "__main__":
    dm = docmap.build(save=False)
    for sc, acc in config.SCENARIO_TO_ACC.items():
        rcs = for_account(acc, dm)
        applied = [r.txn_id for r in rcs if r.applied]
        rejected = [r.txn_id for r in rcs if not r.applied]
        print(f"{sc} {acc}: applied={applied} rejected={rejected}")
