"""Gemini transaction classifier — maps each ledger transaction to an engine category.

The ledger has NO category column, so this is the main data-dependent knob. It is BATCHED
one call per borrower (all that borrower's transactions in a single prompt) to stay inside
the free tier's ~20-requests/window budget, and every reply is cached by gemini.generate.
Falls back to the keyword classifier on any failure so the pipeline never hard-stops."""
from __future__ import annotations
import json
import re
from . import config, gemini
from .engine import (CAPEX, OPEX, LEASE, REVENUE, RELATED_PARTY, INSURANCE, PAYROLL,
                     FINANCING, INTEREST, TAX, UTILITIES, GROUP_CAPEX,
                     UNRESTRICTED_ASSETS, OTHER)

CATEGORIES = {
    CAPEX: "капитальные затраты (покупка/строительство основных средств, оборудования)",
    OPEX: "операционные расходы (обычная операционная деятельность)",
    LEASE: "арендные и лизинговые платежи",
    REVENUE: "выручка и поступления от продаж (обычно положительные суммы)",
    FINANCING: "поступления по финансированию, займы, кредитные транши",
    INTEREST: "процентные расходы по займам",
    TAX: "налоги и обязательные платежи в бюджет",
    UTILITIES: "коммунальные расходы (электро/тепло/вода и т.п.)",
    INSURANCE: "страховые премии",
    PAYROLL: "расходы на оплату труда, заработная плата",
    RELATED_PARTY: "платежи в пользу связанных/аффилированных сторон (см. список ниже)",
    GROUP_CAPEX: "капитальные затраты уровня Группы (консолидированные)",
    UNRESTRICTED_ASSETS: "активы, переданные Неограниченным дочерним организациям",
    OTHER: "прочее / не подпадает под перечисленные категории",
}
_VALID = set(CATEGORIES)

_SYSTEM = ("Ты финансовый аналитик банка. Классифицируй банковские транзакции по категориям "
           "для проверки кредитных ковенантов. Отвечай СТРОГО одним JSON-объектом без пояснений.")


def keyword_category(t) -> str:
    """Deterministic categoriser: free, offline, and the ONLY thing running whenever the
    Gemini free tier 429s. It is therefore a primary classifier, not just a fallback —
    `solve.base_classifier` is this function. Keep the two paths identical; they silently
    diverged once (a stale copy in solve.py scored 23% against this one's 100%)."""
    blob = f"{t.counterparty} {t.description}".lower()
    # capex: an acquisition/construction verb near a capital-asset noun (checked first)
    buy = any(w in blob for w in ("приобрет", "покупк", "закуп", "строительств",
                                  "реконструкц", "модерниз", "капитальн"))
    asset = any(w in blob for w in ("оборудован", "техник", "кран", "тягач", "грузов",
                                    "погрузчик", "машин", "автопарк", "транспортн",
                                    "корпус", "здани", "склад", "основны средств"))
    if (buy and asset) or "капитальн" in blob or "строительств" in blob or "capex" in blob:
        return CAPEX
    rules = [("аренд", LEASE), ("лизинг", LEASE), ("lease", LEASE),
             ("процент", INTEREST), ("interest", INTEREST),
             ("налог", TAX), ("tax", TAX),
             ("водоснаб", UTILITIES), ("водоотвед", UTILITIES), ("коммунал", UTILITIES),
             ("электро", UTILITIES), ("тепло", UTILITIES), ("газоснаб", UTILITIES), ("utility", UTILITIES),
             ("страхов", INSURANCE), ("insurance", INSURANCE),
             # "труда" (not "оплат труда") so the contracts' canonical label
             # "Расходы на оплатУ труда" matches — the inflected form broke the substring.
             ("труда", PAYROLL), ("персонал", PAYROLL), ("зарплат", PAYROLL),
             ("заработн", PAYROLL), ("payroll", PAYROLL), ("фот", PAYROLL),
             ("финансирован", FINANCING), ("займ", FINANCING), ("кредитн", FINANCING), ("транш", FINANCING),
             ("выручк", REVENUE), ("продаж", REVENUE), ("реализац", REVENUE), ("revenue", REVENUE),
             ("операционн", OPEX), ("opex", OPEX)]
    for kw, cat in rules:
        if kw in blob:
            return cat
    return REVENUE if (t.amount_usd or t.amount) > 0 else OPEX


# Last-resort related-party hints, used ONLY when the borrower has no KYC ownership list.
# The contracts say identity governs, "а не назначением платежа" — so this is explicitly a
# worse signal, justified only because the alternative is reporting a confident 0.
# ACC-7802's dossier omits its ownership section and ACC-7806 has no dossier at all.
_RP_HINTS = ("материнск", "аффилирован", "связанной стороне", "связанным сторонам",
             "внутригруппов", "внутри группы", "группы компаний", "общего центра услуг",
             "общий центр услуг", "управленческое вознаграждение", "management fee",
             "related party", "intragroup", "intra-group")


def looks_related_party(t) -> bool:
    blob = f"{t.counterparty} {t.description}".lower()
    return any(h in blob for h in _RP_HINTS)


def _prompt(txns, related_parties) -> str:
    cats = "\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items())
    rp = ", ".join(sorted(related_parties)) if related_parties else "(список пуст)"
    rows = [f"{t.txn_id} | {t.counterparty} | {t.description} | {t.amount} {t.currency}"
            for t in txns]
    return (
        f"Категории (используй ТОЛЬКО эти ключи):\n{cats}\n\n"
        "Правила:\n"
        f"1) ПРИОБРЕТЕНИЕ/ПОКУПКА/СТРОИТЕЛЬСТВО долгосрочных активов — это '{CAPEX}', НЕ '{OPEX}'. "
        "Примеры capex: покупка крана, тягача, грузовика, погрузчика, оборудования, техники, "
        "машин; строительство/реконструкция корпуса, склада, здания. "
        f"'{OPEX}' — это только текущие расходы (материалы, услуги, ГСМ, ПО, клининг).\n"
        f"2) Коммунальные ('{UTILITIES}') включают электроэнергию, тепло, газ, ВОДОСНАБЖЕНИЕ и "
        "водоотведение.\n"
        f"3) СПИСОК СВЯЗАННЫХ СТОРОН (из KYC): {rp}\n"
        f"   Если контрагент совпадает с этим списком, категория ОБЯЗАТЕЛЬНО '{RELATED_PARTY}' — "
        "даже если платёж выглядит как управленческое вознаграждение, услуги или аренда.\n"
        f"4) Положительные суммы обычно '{REVENUE}' или '{FINANCING}'; отрицательные — расход.\n\n"
        f"Транзакции (формат: txn_id | контрагент | описание | сумма):\n" + "\n".join(rows) +
        f"\n\nВерни JSON: {{\"<txn_id>\": \"<категория>\", ...}} для ВСЕХ перечисленных txn_id."
    )


def _parse(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else {}


def _is_related(counterparty: str, related: set[str]) -> bool:
    cp = counterparty.lower().strip()
    return any(cp == r.lower().strip() or r.lower().strip() in cp for r in related if r)


def classify_batch(txns, related_parties=None, model=None, chunk=150) -> dict[str, str]:
    """Return {txn_id: category} for one borrower's transactions (one call per <=chunk txns).

    Related-party is decided deterministically by counterparty match against the KYC list
    (a factual lookup, more reliable than prompting); the LLM handles the accounting
    categories; keyword fallback covers any missing/invalid LLM answer."""
    related = related_parties or set()
    out: dict[str, str] = {}
    stats = {"llm": 0, "fallback": 0, "related_override": 0, "errors": []}
    for i in range(0, len(txns), chunk):
        part = txns[i:i + chunk]
        try:
            raw = gemini.generate(_prompt(part, related),
                                  model=model or config.MODEL_FLASH, system=_SYSTEM,
                                  json_out=True, temperature=0.0)
            mp = _parse(raw)
        except Exception as e:
            mp = {}
            stats["errors"].append(str(e)[:80])
        for t in part:
            if _is_related(t.counterparty, related):
                out[t.txn_id] = RELATED_PARTY
                stats["related_override"] += 1
                continue
            cat = mp.get(t.txn_id)
            if cat in _VALID:
                out[t.txn_id] = cat
                stats["llm"] += 1
            else:
                out[t.txn_id] = keyword_category(t)
                stats["fallback"] += 1
    classify_batch.last_stats = stats            # inspectable after the call
    return out


classify_batch.last_stats = {}


def make_base_classifier(cat_map: dict[str, str]):
    """A base Classifier (Txn -> category) backed by the LLM map, keyword fallback otherwise."""
    def base(t):
        return cat_map.get(t.txn_id) or keyword_category(t)
    return base
