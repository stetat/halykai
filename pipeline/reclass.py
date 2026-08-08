"""Extract auditor reclassifications + related parties from the documents.

Audit reports carry a "Дополнение о соблюдении ковенантов" with reclassifications:
  APPLIED:  "Операция TXN-..., первоначально учтённая как <A> ($amt), переклассифицирована
             ... как <B>."
  REJECTED: "...рассматривалась на предмет ... переклассификации ...; первоначальная
             классификация сохраняется, ... корректировка ... не производилась."
Only APPLIED reclassifications change `actual`; the REJECTED ones are traps (and the
'ПРОМЕЖУТОЧНАЯ/предварительн' interim worksheets are drafts — lower trust than the final report)."""
from __future__ import annotations
import json
import re
from . import config, docmap, pdftext
from .engine import Reclass, OTHER, label_to_category as engine_label_to_category

# Category labels are mapped by engine.label_to_category — ONE shared vocabulary, so the
# reclassification parser and the covenant parser cannot drift apart. The previous local
# copy had no entry for коммунальные / процентные / налоги / консультационные, so those
# reclassification targets became OTHER: a bucket no covenant reads, silently dropping the
# transaction from the metric it was reclassified into.
REJECT_RE = re.compile(r"не\s+производил|сохран\w+|без\s+корректир|не\s+переклассифиц", re.I)
INTERIM_RE = re.compile(r"промежуточн|предварительн|черновик|interim|preliminary", re.I)
# INTERIM_RE alone mistakes the FINAL report for a draft. The final report's whole point is to
# say "Настоящий отчёт ЗАМЕНЯЕТ ЛЮБЫЕ ПРОМЕЖУТОЧНЫЕ ведомости" — which contains "промежуточн",
# so a bare keyword search reads the superseding document as the superseded one. A document that
# declares it replaces the drafts IS the final one; check that first and let it win.
_SUPERSEDES_RE = re.compile(r"замен\w+\s+любые\s+промежуточн|"
                            r"настоящий\s+отчёт\s+замен\w+", re.I)
# A final document only supersedes the drafts if it actually speaks to classification. Without
# this guard an unrelated final doc filed under "audits" (a KYC dossier lands there for
# ACC-7809) would silently void a borrower's only reclassification data.
_RECLASS_SECTION_RE = re.compile(r"замен\w+\s+любые\s+промежуточн|переклассифиц", re.I)

# One reclassification = one sentence beginning at a TXN id. The sentence terminator is
# a period NOT followed by a digit: amounts like "($418,204.37)" contain periods, and
# treating those as sentence ends truncated the clause before "переклассифицирована",
# so every reclassification carrying a dollar amount parsed as {from:'К', to:None} and
# silently became "not applied" -> no evidence anywhere.
RECLASS_RE = re.compile(r"(TXN-[A-Za-z0-9-]+)((?:[^.]|\.(?=\d))*)\.(?!\d)", re.S)
_FROM_RE = re.compile(r"первоначальн\w*\s+(?:учтённ\w+|учтен\w+|отражённ\w+|отражен\w+|"
                      r"классифицирован\w*)?\s*как\s+([^,(]+)", re.I)
_TO_RE = re.compile(r"(?:пере|ре)классифицирован\w*.*?\bкак\s+([^,(]+)", re.I | re.S)
_AMT_RE = re.compile(r"\$\s*([0-9][0-9\s.,]*)")

# A final report often does NOT cite a TXN id. It names the payment by amount and counterparty:
#   "(7.1) Сумма в размере $142,118.64, выплаченная контрагенту Tengiz Risk Engineering Bureau,
#    первоначально учтённая как Операционные расходы, переклассифицирована ... как Страховые
#    премии."
# RECLASS_RE starts at "TXN-", so every one of these was invisible — and they are APPLIED
# reclassifications in the authoritative document. They are resolved against the ledger at
# solve time (see engine.Categorizer), because only the ledger knows which row this is.
_AMT_CP_RE = re.compile(
    r"Сумма\s+в\s+размере\s*\$\s*([0-9][0-9\s.,]*)\s*,\s*"
    r"выплаченн\w*\s+контрагенту\s+([^,]{3,70}?)\s*,"
    r"((?:[^.]|\.(?=\d))*)\.(?!\d)", re.S | re.I)

# Covenant testing is period-bound: "Выручка признаётся в том ковенантном периоде, в котором
# фактически оказаны услуги, независимо от даты счёта-фактуры." A 2025-dated invoice for work
# performed in 2026 belongs to neither the numerator nor the denominator of a 2025 covenant, so
# the transaction must leave the period entirely — this is an EXCLUSION, not a reclassification.
_CUTOFF_RE = re.compile(
    r"(TXN-[A-Za-z0-9-]+)((?:[^.]|\.(?=\d))*?)"
    r"оказанн\w*\s+в\s+период\s+с\s+(\d{4})-\d{2}-\d{2}\s+по\s+(\d{4})-", re.S | re.I)
COVENANT_YEAR = "2025"


def _to_cat(text: str | None) -> str:
    return engine_label_to_category(text)


def _amt(s: str | None) -> float | None:
    if not s:
        return None
    s = s.replace(" ", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_reclasses(text: str, want: str) -> list[Reclass]:
    """Every reclassification sentence in one document, for this borrower only."""
    found = []
    for m in RECLASS_RE.finditer(text):
        txn, body = m.group(1), m.group(2)
        parts = txn.split("-")
        # A reclassification only counts for THIS borrower. Documents quote the spec's
        # example scenario ("TXN-T1-0020") and occasionally other borrowers; ignore both.
        if want and len(parts) > 1 and parts[1].upper() != want:
            continue
        frm = _FROM_RE.search(body)
        to = _TO_RE.search(body)
        # APPLIED when the clause states a new category and does not walk it back
        # ("первоначальная классификация сохраняется", "корректировка не производилась").
        found.append(Reclass(txn_id=txn,
                             to_category=_to_cat(to.group(1) if to else None),
                             from_category=_to_cat(frm.group(1) if frm else None),
                             applied=bool(to) and not REJECT_RE.search(body)))
    for m in _AMT_CP_RE.finditer(text):
        amt, cp, body = m.group(1), m.group(2).strip(), m.group(3)
        to = _TO_RE.search(body)
        if not to:
            continue
        frm = _FROM_RE.search(body)
        found.append(Reclass(txn_id="",                     # unknown until the ledger arrives
                             to_category=_to_cat(to.group(1)),
                             from_category=_to_cat(frm.group(1) if frm else None),
                             applied=not REJECT_RE.search(body),
                             amount=_amt(amt), counterparty=cp))
    return found


def period_exclusions(acc: str, dm: dict | None = None) -> set[str]:
    """Transactions whose services fall OUTSIDE the covenant year, so they leave the period.

    "Примечание 7 — Отсечение и начисления. Выручка признаётся в том ковенантном периоде, в
    котором фактически оказаны услуги, независимо от даты счёта-фактуры … (7.1) Операция
    TXN-P1-0045 (счёт-фактура от 2025-08-12) относится к услугам, оказанным в период с
    2026-01-15 по 2026-03-20."

    This is not a reclassification — no category is correct for it, because the transaction is
    not in the period at all. reclass.py parsed it as from=other -> to=other, i.e. a no-op, and
    nothing anywhere implemented cut-offs, so the amount stayed in whichever metric its category
    fed."""
    dm = dm or docmap.build(save=False)
    groups = dm["by_acc"].get(acc, {})
    want = config.ACC_TO_SCENARIO.get(acc, "").upper()
    out: set[str] = set()
    for name in {n for names in groups.values() for n in names}:
        text = pdftext.extract_text(config.DATASET / name)
        for m in _CUTOFF_RE.finditer(text):
            txn, _, y_from, y_to = m.groups()
            parts = txn.split("-")
            if want and len(parts) > 1 and parts[1].upper() != want:
                continue
            if COVENANT_YEAR not in (y_from, y_to):
                out.add(txn)
    return out


def for_account(acc: str, dm: dict | None = None) -> list[Reclass]:
    """Reclassifications for one borrower, with the final report outranking the drafts.

    The precedence used to hinge on a `seen_final` set filled only when a FINAL report named
    the same TXN id. Final reports do not cite TXN ids — they identify a reclassification by
    amount and counterparty ("Сумма в размере $592,296.10, выплаченная контрагенту Irtysh
    Advisory Bureau") — so that set stayed empty and a superseded DRAFT won by default.

    Both documents say plainly what should happen. The interim worksheet disclaims itself:
    "ПРОЕКТ — ПРОМЕЖУТОЧНАЯ ВЕДОМОСТЬ … НЕ ЯВЛЯЕТСЯ ОКОНЧАТЕЛЬНОЙ ПОЗИЦИЕЙ АУДИТОРА … вывод
    далее не переносится и ПЕРВОНАЧАЛЬНАЯ КЛАССИФИКАЦИЯ СОХРАНЯЕТСЯ. Следует руководствоваться
    исключительно окончательным отчётом." The final report agrees: "Настоящий отчёт заменяет
    любые промежуточные ведомости". So once a final report exists, a draft-only reclassification
    is void — it does not merely rank lower.

    This was not academic: B1's draft moved TXN-B1-0023 ($6,166,592.66) from opex to utilities,
    and B1 6.2 is max(payroll, utilities) — so the phantom reclass turned a COMPLIANT cell into
    a BREACH. No existing test could see it, because reconstruct.py synthesises a ledger to fit
    whatever the engine does and the rehearsal ledger gives that txn a token amount."""
    dm = dm or docmap.build(save=False)
    groups = dm["by_acc"].get(acc, {})
    audit_docs = groups.get("audits", []) + groups.get("other", [])
    want = config.ACC_TO_SCENARIO.get(acc, "").upper()

    final_rcs: dict = {}
    interim_rcs: dict = {}
    has_final = False
    for name in audit_docs:
        text = pdftext.extract_text(config.DATASET / name)
        interim = (not _SUPERSEDES_RE.search(text)) and bool(INTERIM_RE.search(text))
        target = interim_rcs if interim else final_rcs
        for rc in _parse_reclasses(text, want):
            # amount+counterparty reclassifications have no txn id yet, so they cannot share
            # a key — dedupe them on the pair the document actually gave us
            target[rc.txn_id or (rc.amount, rc.counterparty)] = rc
        if not interim and _RECLASS_SECTION_RE.search(text):
            has_final = True

    if has_final:
        # drafts are superseded wholesale, not just where the final report overlaps
        return list(final_rcs.values())
    merged = dict(interim_rcs)
    merged.update(final_rcs)
    return list(merged.values())


# The KYC dossier decides related-party status by an OWNERSHIP THRESHOLD that differs per
# borrower (seen: 20%–38%): "Организации, в которых Группа владеет 36.0% и более голосующих
# прав, признаются связанными сторонами для целей Договора." Entities listed below that
# threshold are decoys — e.g. Saryarka Terminal Properties LLP at 33.5% against a 36.0% bar.
_OWN_ROW_RE = re.compile(r'^[ \t]*("?[A-Za-zА-Яа-я][^\n%]{2,70}?)[ \t]+(\d{1,3}[.,]\d+)[ \t]*%[ \t]*$',
                         re.M)
_THRESHOLD_RE = re.compile(r"владе\w*\s+(\d{1,3}[.,]\d+)\s*%\s*и\s+более", re.I)
# A holding can be disclaimed in a footnote: the table shows the gross stake, but the
# Group's actual voting rights are lower ("удерживается косвенно ... Группе принадлежит X%").
_INDIRECT_RE = re.compile(
    r"Дол\w+\s+в\s+(.+?)\s+удерживается[^;.]*[;.]\s*Групп\w+\s+принадлежит\s+"
    r"(\d{1,3}[.,]\d+)\s*%", re.I | re.S)


def _pct(s: str) -> float:
    return float(s.replace(",", "."))


def _clean_name(s: str) -> str:
    # quotes sit INSIDE the name too ('"Saryarka Capital Partners" LLP'), so strip them all
    s = re.sub(r'["«»„“”]', " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(?:Организация|Доля голосующих прав)\s+", "", s, flags=re.I)
    return s.strip(" .,")


def image_facts() -> dict:
    """Determinations that live only inside embedded images (see pipeline/pdfimages.py).

    Two sources, and the precedence matters. `image_facts.json` was transcribed by reading the
    pictures by eye and is authoritative. `cache/image_facts_ocr.json` is what the model's
    vision read from images nobody has checked, written by `cli ocr`; it fills accounts the
    verified file does not mention and never overrides one it does. Against this release's four
    known images the model reproduced every threshold and amount exactly, but it also prefixed
    party names with their descriptions in one case — harmless for an add-back, where only
    amounts and the floor are read, and wrong for an ownership table, where names are matched
    against ledger counterparties. So: verified first, model second, and the model's entries
    stay labelled as unverified."""
    p = config.ROOT / "image_facts.json"
    verified = {}
    if p.exists():
        verified = {k: v for k, v in json.loads(p.read_text(encoding="utf-8")).items()
                    if not k.startswith("_")}
    ocr_path = config.CACHE / "image_facts_ocr.json"
    if not ocr_path.exists():
        return verified
    try:
        raw = json.loads(ocr_path.read_text(encoding="utf-8"))
    except Exception:
        return verified
    # cli ocr keys by DOCUMENT; map each onto its account, and only where nothing verified exists
    out = dict(verified)
    dm = docmap.build(save=False)
    for doc, facts in raw.items():
        acc = (dm.get("docs", {}).get(doc) or {}).get("account")
        if not acc:
            for a, groups in dm.get("by_acc", {}).items():
                if any(doc in names for names in groups.values()):
                    acc = a
                    break
        if acc and acc not in out:
            out[acc] = facts
    return out


def _related_from_image(acc: str) -> set[str]:
    e = image_facts().get(acc) or {}
    thr = e.get("ownership_threshold_pct")
    if thr is None:
        return set()
    return {n for n, pct in (e.get("holdings_pct") or {}).items() if pct >= thr}


def unrestricted_subsidiaries(acc: str) -> set[str]:
    """Subsidiaries outside the security perimeter (pledged share below the threshold).

    Needed by the unrestricted-assets covenant, which is an identity test no description
    classifier can perform. Disclosed only in an image for ACC-7809."""
    e = image_facts().get(acc) or {}
    thr = e.get("pledged_threshold_pct")
    if thr is None:
        return set()
    return {n for n, pct in (e.get("subsidiary_pledged_pct") or {}).items() if pct < thr}


def ebitda_addback(acc: str) -> float:
    """One-off items added back to EBITDA, applying the contract's materiality floor.

    Items BELOW the floor are explicitly not added back — they are decoys."""
    e = image_facts().get(acc) or {}
    floor = e.get("ebitda_addback_floor_usd")
    if floor is None:
        return 0.0
    return sum(v for v in (e.get("one_off_items_usd") or {}).values() if v >= floor)


def related_parties(acc: str, dm: dict | None = None) -> set[str]:
    """Counterparties that qualify as related parties for this borrower.

    Membership is an ownership test in the KYC dossier, NOT a payment description — the
    contracts say so explicitly ("Отнесение контрагента к аффилированным лицам определяется
    ... а не назначением платежа"). Returning every company name in the file (the previous
    behaviour) inflates every related-party covenant; 12 of the 36 cells depend on this."""
    dm = dm or docmap.build(save=False)
    groups = dm["by_acc"].get(acc, {})
    # The dossier is not always filed under "kyc" — for ACC-7809 it lands in "audits".
    docs = groups.get("kyc", []) + groups.get("audits", []) + groups.get("other", [])
    parties: set[str] = set()
    for name in docs:
        text = pdftext.extract_text(config.DATASET / name)
        thr = _THRESHOLD_RE.search(text)
        if not thr:
            continue
        threshold = _pct(thr.group(1))
        holdings: dict[str, float] = {}
        for m in _OWN_ROW_RE.finditer(text[:thr.start()]):
            nm = _clean_name(m.group(1))
            if nm and not nm.lower().startswith(("организац", "групп")):
                holdings[nm] = _pct(m.group(2))
        # footnotes override the table (indirect holdings count at the lower real stake)
        for m in _INDIRECT_RE.finditer(text):
            nm = _clean_name(m.group(1))
            for k in list(holdings):
                if k.lower() == nm.lower() or nm.lower() in k.lower():
                    holdings[k] = _pct(m.group(2))
        parties |= {n for n, p in holdings.items() if p >= threshold}
    # ACC-7802's ownership section and ACC-7806's whole dossier are IMAGES; the text layer
    # yields nothing, so without this both report zero related-party spend and a confident
    # COMPLIANT on cells the key marks BREACH.
    return parties or _related_from_image(acc)


if __name__ == "__main__":
    dm = docmap.build(save=False)
    for sc, acc in config.SCENARIO_TO_ACC.items():
        rcs = for_account(acc, dm)
        applied = [r.txn_id for r in rcs if r.applied]
        rejected = [r.txn_id for r in rcs if not r.applied]
        print(f"{sc} {acc}: applied={applied} rejected={rejected}")
