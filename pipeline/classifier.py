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


# Consumables and services stay OPEX even when a capital-asset noun is nearby: the asset word
# is usually just naming a LOCATION ("спецодежда для персонала СКЛАДА" bought a jacket, not a
# warehouse). Checked before capex because the capex test is bag-of-words, not syntactic.
_CONSUMABLE = ("спецодежд", "канцтовар", "расходн материал", "гсм", "запчаст",
               "хозяйствен", "инвентар", "питьев", "униформ")

# Strong enough to mean capex on their own — you do not "reconstruct" or "modernise" an
# operating expense, and these appear with asset nouns too varied to enumerate ("цех", "путь").
# NB "строитель" — the common prefix of "строительСТВо" and "строительНО-монтажные". Neither
# longer form covers the other; matching the shared STEM rather than one inflection is the fix
# for a bug this table has now shipped three times (ср. "оплатУ труда", "основныХ средств").
_CAPEX_STRONG = ("капитальн", "capex", "строитель", "реконструкц", "модерниз",
                 "основные фонды", "капремонт", "возведен", "дооборудован")

# "Вознаграждение" only means INTEREST next to a debt: on its own it is an ordinary fee
# ("комиссионное вознаграждение агента") or remuneration ("вознаграждение членам совета
# директоров"). Mapping it unconditionally cost as many cells as it fixed, and mis-routed
# money into INTEREST — a denominator in nine RATIO covenants.
_DEBT_CTX = ("займ", "кредит", "облигац", "овердрафт", "транш", "ссуд", "loan", "facility")
_BUY = ("приобрет", "покупк", "закуп", "поставк", "монтаж")
_ASSET = ("оборудован", "техник", "кран", "тягач", "грузов", "погрузчик", "машин",
          "автопарк", "транспортн", "корпус", "здани", "склад", "экскаватор",
          "станок", "установк", "сооружен", "лини сортировк", "линии сортировк")


def keyword_category(t) -> str:
    """Deterministic categoriser: free, offline, and the ONLY thing running whenever the
    Gemini free tier 429s. It is therefore a primary classifier, not just a fallback —
    `solve.base_classifier` is this function. Keep the two paths identical; they silently
    diverged once (a stale copy in solve.py scored 23% against this one's 100%).

    Vocabulary is deliberately over-inclusive on RU inflection: substring rules must match
    the case-inflected form that actually appears ("основныХ средств", not "основны средств"),
    a class of bug this table has shipped twice."""
    blob = f"{t.counterparty} {t.description}".lower()

    if any(w in blob for w in _CONSUMABLE):
        return OPEX
    # "основных/основного средства" — match the stem pair, never a fixed inflection
    fixed_assets = "основн" in blob and "средств" in blob
    if (any(w in blob for w in _BUY) and any(w in blob for w in _ASSET)) \
            or any(w in blob for w in _CAPEX_STRONG) or fixed_assets:
        return CAPEX

    # Statutory payroll deductions appear as bare abbreviations, which need word boundaries —
    # as substrings they would fire inside unrelated words. ("СО" for социальные отчисления is
    # deliberately absent: "со" is a Russian preposition.) Tax words still take precedence.
    if re.search(r"\b(опв|осмс|впосмс)\b", blob) and not any(
            w in blob for w in ("налог", "ндс", "кпн")):
        return PAYROLL

    rules = [("аренд", LEASE), ("лизинг", LEASE), ("lease", LEASE),
             # "вознаграждение" is the standard KZ banking word for INTEREST, but
             # "управленческое вознаграждение" is a management fee — excluded below.
             ("процент", INTEREST), ("interest", INTEREST), ("вознагражден", INTEREST),
             ("налог", TAX), ("tax", TAX), ("ндс", TAX), ("vat", TAX), ("кпн", TAX),
             ("пошлин", TAX), ("госдоход", TAX),
             ("водоснаб", UTILITIES), ("водоотвед", UTILITIES), ("коммунал", UTILITIES),
             ("электро", UTILITIES), ("тепло", UTILITIES), ("газоснаб", UTILITIES), ("utility", UTILITIES),
             ("страхов", INSURANCE), ("insurance", INSURANCE),
             # "труда" (not "оплат труда") so the contracts' canonical label
             # "Расходы на оплатУ труда" matches — the inflected form broke the substring.
             ("труда", PAYROLL), ("персонал", PAYROLL), ("зарплат", PAYROLL),
             ("заработн", PAYROLL), ("payroll", PAYROLL), ("фот", PAYROLL),
             ("пенсионн", PAYROLL), ("сотрудник", PAYROLL), ("трудов", PAYROLL),
             ("работник", PAYROLL), ("отпускн", PAYROLL), ("отпуск", PAYROLL),
             ("материальной помощи", PAYROLL), ("больничн", PAYROLL),
             ("совета директоров", PAYROLL),
             ("финансирован", FINANCING), ("займ", FINANCING), ("кредитн", FINANCING),
             ("транш", FINANCING), ("drawdown", FINANCING), ("credit facility", FINANCING),
             ("loan", FINANCING), ("borrowing", FINANCING), ("tranche", FINANCING),
             ("выручк", REVENUE), ("продаж", REVENUE), ("реализац", REVENUE), ("revenue", REVENUE),
             ("операционн", OPEX), ("opex", OPEX)]
    for kw, cat in rules:
        if kw in blob:
            if kw == "вознагражден" and (
                    "управленческ" in blob                      # management fee
                    or not any(d in blob for d in _DEBT_CTX)):  # a plain fee, not interest
                continue
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
