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
_DEBT_CTX = ("займ", "кредит", "облигац", "овердрафт", "транш", "ссуд", "loan", "facility",
             "қарыз", "несие", "банктік")
# A prepayment or advance is a buy verb only in company of an asset noun (below), so the
# common "авансовый платёж за <услуги>" stays opex. English forms matter: the ledger carries
# supplier narrations from Turkish, Chinese and German vendors.
_BUY = ("приобрет", "покупк", "закуп", "поставк", "монтаж", "аванс", "устройств",
        "prepayment", "purchase", "acquisition", "advance payment", "supply of")
_ASSET = ("оборудован", "техник", "кран", "тягач", "грузов", "погрузчик", "машин",
          "автопарк", "транспортн", "корпус", "здани", "склад", "экскаватор",
          "станок", "установк", "сооружен", "лини", "ктп", "подстанц", "жабдық",
          "excavator", "equipment", "crane", "machinery", "vehicle", "truck", "loader",
          "железнодорожн", "тупик", "причал", "цех", "путей")


# Kazakh is an official language and the banks emit it, but this table was Russian-only, so
# "Жалақы төлеу" (wages), "жалдау ақысы" (rent) and "Электр энергиясы" all fell through to the
# sign guess and became opex. Kazakh is agglutinative — match the stem and let suffixes run.
_RULES = [("аренд", LEASE), ("лизинг", LEASE), ("lease", LEASE),
          ("жалдау", LEASE), ("жалға", LEASE), ("жалдам", LEASE),
          # "вознаграждение" is the standard KZ banking word for INTEREST, but
          # "управленческое вознаграждение" is a management fee — excluded below.
          ("процент", INTEREST), ("interest", INTEREST), ("вознагражден", INTEREST),
          ("сыйақы", INTEREST), ("пайыз", INTEREST),
          ("налог", TAX), ("tax", TAX), ("ндс", TAX), ("vat", TAX), ("кпн", TAX),
          ("пошлин", TAX), ("госдоход", TAX), ("салық", TAX),
          ("окружающ", TAX), ("эмисси в окруж", TAX),
          ("водоснаб", UTILITIES), ("водоотвед", UTILITIES), ("коммунал", UTILITIES),
          ("электр", UTILITIES), ("тепло", UTILITIES), ("газоснаб", UTILITIES),
          ("газ", UTILITIES), ("utility", UTILITIES), ("жарық", UTILITIES),
          ("жылу", UTILITIES), ("сумен жабд", UTILITIES),
          ("страхов", INSURANCE), ("insurance", INSURANCE), ("полис", INSURANCE),
          ("огпо", INSURANCE), ("каско", INSURANCE), ("policy", INSURANCE),
          ("premium", INSURANCE), ("сақтандыр", INSURANCE),
          # "труда" (not "оплат труда") so the contracts' canonical label
          # "Расходы на оплатУ труда" matches — the inflected form broke the substring.
          ("труда", PAYROLL), ("персонал", PAYROLL), ("зарплат", PAYROLL),
          ("заработн", PAYROLL), ("payroll", PAYROLL), ("фот", PAYROLL),
          ("пенсионн", PAYROLL), ("сотрудник", PAYROLL), ("трудов", PAYROLL),
          ("работник", PAYROLL), ("отпускн", PAYROLL), ("отпуск", PAYROLL),
          ("материальной помощи", PAYROLL), ("больничн", PAYROLL),
          ("совета директоров", PAYROLL), ("преми", PAYROLL),
          ("жалақы", PAYROLL), ("еңбекақы", PAYROLL), ("зейнетақы", PAYROLL),
          ("финансирован", FINANCING), ("займ", FINANCING), ("кредитн", FINANCING),
          ("транш", FINANCING), ("drawdown", FINANCING), ("credit facility", FINANCING),
          ("loan", FINANCING), ("borrowing", FINANCING), ("tranche", FINANCING),
          ("қарыз", FINANCING), ("несие", FINANCING),
          ("выручк", REVENUE), ("продаж", REVENUE), ("реализац", REVENUE), ("revenue", REVENUE),
          ("операционн", OPEX), ("opex", OPEX)]

SIGN_FALLBACK = "sign-fallback"

# A bare `in` test matches across word boundaries, which is not a nuance — it is wrong twice
# over. "PREPAYMENT FOR EXCAVATOR" was booked as tax because "exca-VAT-or" contains "vat", and
# "договор БЕСПРОЦЕНТНОГО займа" — an interest-FREE loan — was booked as interest because
# "бес-ПРОЦЕНТ-ного" contains "процент". Both are inverted answers, not near misses.
#
# So every token must start at a word boundary. Russian tokens are STEMS and still match any
# suffix ("электр" -> "электрическую", "электроэнергия", "электр энергиясы"). Latin and very
# short tokens are anchored at both ends, since those are the ones that hide inside longer
# words. Tokens containing a space are matched as a phrase from a word boundary.
_TOKEN_RE: dict[str, re.Pattern] = {}


# Cyrillic tokens are STEMS and must stay open-ended, or "займ" stops matching "займа" and
# every loan in the ledger becomes opex. Only ASCII tokens get a closing boundary — they are
# the ones that hide inside longer words ("vat" in "excavator") — plus the handful of Cyrillic
# abbreviations that are whole words in their own right.
_CLOSED = {"фот", "опв", "осмс", "ктп", "огпо"}

# Requiring a leading word boundary for Cyrillic too was overcorrecting. Russian builds words
# with prefixes that PRESERVE meaning — "СУБаренда" is still rent, "ДОоборудование" is still
# equipment, "ПРЕДоплата" is still payment — and a strict \b threw all of them away. Only a
# NEGATING prefix flips the sense, and there are few of them: "БЕСпроцентный" is the opposite
# of interest. So Cyrillic stems may match inside a word unless a negation sits immediately in
# front. ASCII keeps both boundaries, since Latin abbreviations genuinely do hide inside longer
# words rather than attach to them.
_NEGATING = ("без", "бес", "не", "анти", "контр")
_NEG_RE = re.compile("(?:" + "|".join(_NEGATING) + r")$", re.UNICODE)


def _tok(kw: str) -> re.Pattern:
    rx = _TOKEN_RE.get(kw)
    if rx is None:
        if kw.isascii() or kw in _CLOSED:
            pat = r"\b" + re.escape(kw) + r"\b"
        else:
            pat = re.escape(kw)                 # stem: any position, negation checked below
        rx = _TOKEN_RE[kw] = re.compile(pat, re.UNICODE)
    return rx


def _matches(kw: str, blob: str) -> bool:
    """Token search that tolerates meaning-preserving prefixes but not negating ones."""
    for m in _tok(kw).finditer(blob):
        head = blob[:m.start()]
        # a match at a word boundary is always good; inside a word it must not follow a negation
        if not head or not head[-1].isalnum():
            return True
        word_head = re.split(r"[^\w]", head)[-1]
        if not _NEG_RE.search(word_head):
            return True
    return False


# "НДС"/"VAT" name the tax only when the payment IS the tax. On a Kazakh invoice narration the
# far commoner uses are "в т.ч. НДС 12%" and "БЕЗ НДС", which mark a customer receipt — the
# opposite category. Any of these nearby means the token is describing the invoice, not the
# payment's purpose.
_VAT_MENTION = ("в т.ч", "в том числе", "вкл. ндс", "включая ндс", "без ндс", "не облагается",
                "incl", "including", "excl", "w/o vat", "without vat")


def _rule_applies(kw: str, blob: str) -> bool:
    """Whether a rule token fires, including its exceptions."""
    if not _matches(kw, blob):
        return False
    if kw in ("вознагражден", "сыйақы"):
        # a management fee or a plain fee is not interest — see _DEBT_CTX above
        return "управленческ" not in blob and any(_matches(d, blob) for d in _DEBT_CTX)
    if kw in ("ндс", "vat"):
        return not any(m in blob for m in _VAT_MENTION)
    if kw == "персонал":
        # "услуги по подбору персонала" is a recruitment agency's fee — opex, not payroll
        return "подбор" not in blob
    return True


def categorize_verbose(t) -> tuple[str, str, bool]:
    """Return (category, deciding_rule, contested).

    `deciding_rule` is the token that settled it, or SIGN_FALLBACK when NOTHING in the
    vocabulary matched and the answer came from the sign of the amount alone. A sign-fallback
    answer is a coin-flip dressed as a classification: it says "positive so probably revenue",
    which is right often enough to flatter an accuracy score while carrying no evidence. Runs
    with many of them should be treated as unclassified, not as classified-correctly.

    `contested` means vocabulary from more than one category appeared, so the answer depended
    on _RULES ordering rather than on an unambiguous signal. Adding a token to this table can
    silently steal transactions from another category — that is exactly how "вознаграждение"
    moved money into INTEREST, a denominator in nine RATIO covenants."""
    blob = f"{t.counterparty} {t.description}".lower()

    if any(w in blob for w in _CONSUMABLE):
        return OPEX, "consumable", False
    # "основных/основного средства" — match the stem pair, never a fixed inflection
    fixed_assets = "основн" in blob and "средств" in blob
    if (any(w in blob for w in _BUY) and any(w in blob for w in _ASSET)) \
            or any(w in blob for w in _CAPEX_STRONG) or fixed_assets:
        return CAPEX, "capex-asset", False

    # Statutory payroll deductions appear as bare abbreviations, which need word boundaries —
    # as substrings they would fire inside unrelated words. ("СО" for социальные отчисления is
    # deliberately absent: "со" is a Russian preposition.) Tax words still take precedence.
    if re.search(r"\b(опв|осмс|впосмс)\b", blob) and not any(
            w in blob for w in ("налог", "ндс", "кпн")):
        return PAYROLL, "opv-abbrev", False

    # "Оплата % по договору займа" — the percent SIGN carries the meaning and no word matches,
    # so "займ" would otherwise book an interest payment as a financing drawdown.
    if "%" in blob and any(_matches(d, blob) for d in _DEBT_CTX):
        return INTEREST, "percent-sign", False

    hits = [(kw, cat) for kw, cat in _RULES if _rule_applies(kw, blob)]
    if hits:
        kw, cat = hits[0]
        return cat, kw, len({c for _, c in hits}) > 1
    return (REVENUE if (t.amount_usd or t.amount) > 0 else OPEX), SIGN_FALLBACK, False


def keyword_category(t) -> str:
    """Deterministic categoriser: free, offline, and the ONLY thing running whenever the
    Gemini free tier 429s. It is therefore a primary classifier, not just a fallback —
    `solve.base_classifier` is this function. Keep the two paths identical; they silently
    diverged once (a stale copy in solve.py scored 23% against this one's 100%).

    A thin wrapper over categorize_verbose so there is exactly ONE rule table. Vocabulary is
    deliberately over-inclusive on RU inflection: substring rules must match the case-inflected
    form that actually appears ("основныХ средств", not "основны средств"), a class of bug this
    table has shipped three times."""
    return categorize_verbose(t)[0]


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


# Once the API is unreachable it stays unreachable for the rest of the run — an expired key, an
# exhausted quota, no network. Retrying per borrower would pay the full HTTP timeout twelve
# times over (180s each, times four retries inside gemini.generate), turning a degraded run
# into a hung one. Two consecutive failures and we stop asking; the deterministic answer was
# already in place, so nothing is lost but the waiting.
_BREAKER = {"consecutive": 0, "limit": 2}


def _breaker_open() -> bool:
    return _BREAKER["consecutive"] >= _BREAKER["limit"]


def _breaker_trip() -> None:
    _BREAKER["consecutive"] += 1


def _breaker_reset() -> None:
    _BREAKER["consecutive"] = 0


def reset_breaker() -> None:
    """Re-arm between runs (and in tests)."""
    _BREAKER["consecutive"] = 0


def classify_hybrid(txns, related_parties=None, model=None, chunk=150) -> dict[str, str]:
    """Deterministic first; spend the LLM only on rows the vocabulary could not decide.

    `classify_batch` sends every transaction, which wastes the free tier's ~20-request budget
    re-deriving answers the keyword table already had, and makes the whole borrower's result
    hostage to one 429. Held-out measurement says the table is ~100% on rows where a rule
    cleanly fires and a coin-flip on the ~28% where none does, so those rows — plus rows where
    several categories' vocabulary collided — are the only ones worth an API call.

    Falls back to the deterministic answer for anything the LLM does not return, so a quota
    failure degrades to exactly the keyword result rather than to nothing."""
    related = related_parties or set()
    out: dict[str, str] = {}
    uncertain = []
    stats = {"deterministic": 0, "asked": 0, "llm_used": 0, "related_override": 0, "errors": []}

    for t in txns:
        if _is_related(t.counterparty, related):
            out[t.txn_id] = RELATED_PARTY
            stats["related_override"] += 1
            continue
        cat, rule, contested = categorize_verbose(t)
        out[t.txn_id] = cat                      # provisional; may be overwritten below
        if rule == SIGN_FALLBACK or contested:
            uncertain.append(t)
        else:
            stats["deterministic"] += 1

    stats["asked"] = len(uncertain)
    for i in range(0, len(uncertain), chunk):
        part = uncertain[i:i + chunk]
        if _breaker_open():
            stats["errors"].append("circuit breaker open — not calling")
            continue
        try:
            raw = gemini.generate(_prompt(part, related),
                                  model=model or config.MODEL_FLASH, system=_SYSTEM,
                                  json_out=True, temperature=0.0)
            mp = _parse(raw)
            _breaker_reset()
        except Exception as e:
            stats["errors"].append(str(e)[:80])
            _breaker_trip()
            continue                              # keep the deterministic answer
        for t in part:
            if mp.get(t.txn_id) in _VALID:
                out[t.txn_id] = mp[t.txn_id]
                stats["llm_used"] += 1

    classify_hybrid.last_stats = stats
    return out


classify_hybrid.last_stats = {}


def make_base_classifier(cat_map: dict[str, str]):
    """A base Classifier (Txn -> category) backed by the LLM map, keyword fallback otherwise."""
    def base(t):
        return cat_map.get(t.txn_id) or keyword_category(t)
    return base
