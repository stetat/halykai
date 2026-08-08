"""Stage B compute engine: covenant spec + ledger -> {status, actual, evidence_txn_id}.

Key design points, straight from CASE.ru.md:
 * The ledger has NO category column. Categories come from (1) auditor reclassifications
   (highest priority) and (2) a base classifier over counterparty/description.
 * `actual` is the magnitude of the metric the covenant bounds. Category aggregates net
   signed amounts first (so refunds reduce a category), then take the magnitude; metrics
   that can legitimately go negative (margins, revenue-less-largest-line) keep their sign.
 * `evidence_txn_id` is the SINGLE transaction whose reclassification/inclusion/exclusion
   flips the verdict — found by leave-one-out over the reclassified set, NOT by picking the
   biggest/last contributor. Ratio & aggregate cells that no single txn decides -> null.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Callable
from .ledger import Txn

# --- categories used by covenant math -------------------------------------------------
CAPEX = "capex"
OPEX = "opex"
LEASE = "lease"
REVENUE = "revenue"
RELATED_PARTY = "related_party"
INSURANCE = "insurance"
PAYROLL = "payroll"
OTHER = "other"
# categories used by the leverage/cover ratio covenants
FINANCING = "financing"          # поступления по финансированию
INTEREST = "interest"            # процентные расходы
TAX = "tax"                      # налоги
UTILITIES = "utilities"          # коммунальные расходы
GROUP_CAPEX = "group_capex"      # капитальные затраты Группы (консолидированные)
UNRESTRICTED_ASSETS = "unrestricted_assets"  # активы, переданные Неограниченным дочерним
EBITDA = "__ebitda__"            # composite sentinel: sum(REVENUE) - sum(OPEX)


@dataclass
class Reclass:
    """An auditor reclassification. `applied=False` = considered but rejected (a trap)."""
    txn_id: str
    to_category: str
    from_category: str | None = None
    applied: bool = True


Classifier = Callable[[Txn], str]   # base categoriser over a raw txn


def _norm_party(s: str) -> str:
    """Punctuation-insensitive party name: the KYC table and the ledger will not agree on
    commas, quotes or dots ('"Aral Capital Partners", LLP' vs 'Aral Capital Partners LLP')."""
    return re.sub(r"[^0-9a-zа-яё]+", " ", (s or "").lower()).strip()


class Categorizer:
    def __init__(self, base: Classifier, reclasses: list[Reclass],
                 related_parties: set[str] | None = None):
        self.base = base
        self.related = {p for p in (_norm_party(r) for r in (related_parties or set())) if p}
        self._applied = {r.txn_id: r.to_category for r in reclasses if r.applied}

    def _is_related(self, counterparty: str) -> bool:
        cp = _norm_party(counterparty)
        if not cp:
            return False
        # exact, else containment either way (guarded by length so short tokens can't match)
        return any(cp == r or (len(r) >= 6 and r in cp) or (len(cp) >= 6 and cp in r)
                   for r in self.related)

    def category(self, t: Txn, ignore_reclass: str | None = None) -> str:
        if t.txn_id in self._applied and t.txn_id != ignore_reclass:
            return self._applied[t.txn_id]
        # base classification; related-party membership overrides on the counterparty axis.
        # The contracts are explicit that this is an identity test, not a description test.
        if self._is_related(t.counterparty):
            return RELATED_PARTY
        return self.base(t)


def _amt(t: Txn) -> float:
    """Signed USD amount of a transaction (expenses negative, income positive)."""
    return t.amount_usd if t.amount_usd is not None else t.amount


def _sum(txns: list[Txn], cat: str, catf: Categorizer, ignore: str | None = None) -> float:
    """Magnitude of a category: sum the SIGNED amounts, then take the magnitude.

    Taking abs() per transaction instead would make refunds, credit notes and reversals
    ADD to a category rather than net against it — a $400k refund against $1.0M of capex
    would report $1.4M. Netting first is both correct accounting and the only reading
    consistent with the case's stated sign convention."""
    return abs(sum(_amt(t) for t in txns if catf.category(t, ignore_reclass=ignore) == cat))


def _cat_value(txns, cat, catf, ignore=None) -> float:
    """Value of a category, expanding the EBITDA composite (Revenue - Opex)."""
    if cat == EBITDA:
        return _sum(txns, REVENUE, catf, ignore) - _sum(txns, OPEX, catf, ignore)
    return _sum(txns, cat, catf, ignore)


def _terms_sum(txns, terms, catf, ignore=None) -> float:
    """Signed sum over [(sign, category), ...] where sign is +1/-1."""
    return sum(sign * _cat_value(txns, cat, catf, ignore) for sign, cat in terms)


# Formula table for the ratio (leverage/cover) covenants, keyed by definition keywords.
# Each: numerator terms / denominator terms, with an optional springing activation trigger.
def ratio_formula(spec: dict) -> dict | None:
    t = f"{spec.get('name','')} {spec.get('metric','')} {spec.get('raw_text','')}".lower()
    def has(*ks): return all(k in t for k in ks)
    if has("покрыт", "процент"):
        return {"id": "interest_cover", "num": [(1, EBITDA)], "den": [(1, INTEREST)]}
    if has("поступлений по финансировани", "операционных и капитальных"):
        return {"id": "cover_sources", "num": [(1, REVENUE), (1, FINANCING)],
                "den": [(1, OPEX), (1, CAPEX)]}
    if has("поступлений по финансировани", "ebitda"):
        return {"id": "springing_leverage", "num": [(1, FINANCING)], "den": [(1, EBITDA)],
                "springing": {"terms": [(1, FINANCING)], "op": ">",
                              "threshold": spec.get("springing_trigger_usd") or 0.0}}
    if has("ebitda", "выручк") and ("рентабельн" in t or "margin" in t or "маржа" in t):
        return {"id": "ebitda_margin", "num": [(1, EBITDA)], "den": [(1, REVENUE)]}
    if has("затрат группы", "ebitda") or has("группы", "ebitda"):
        return {"id": "group_capex_ebitda", "num": [(1, GROUP_CAPEX)], "den": [(1, EBITDA)]}
    if has("налог", "коммунальн", "ebitda"):
        return {"id": "tax_util_ebitda", "num": [(1, TAX), (1, UTILITIES)], "den": [(1, EBITDA)]}
    if has("неограниченн", "капитальных затрат"):
        return {"id": "unrestricted_assets", "num": [(1, UNRESTRICTED_ASSETS)],
                "den": [(1, CAPEX)]}
    if has("страховых преми") and ("арендн" in t or "коммунальн" in t):
        return {"id": "insurance_cover", "num": [(1, INSURANCE)],
                "den": [(1, LEASE), (1, UTILITIES)]}
    # "Выручка ... не менее 3.00x совокупной величины Расходов на оплату труда и
    # Коммунальных расходов" (P6 6.2)
    if has("покрыт") and ("персонал" in t or "оплату труда" in t) and "коммунальн" in t:
        return {"id": "revenue_cover_payroll_util", "num": [(1, REVENUE)],
                "den": [(1, PAYROLL), (1, UTILITIES)]}
    return None


# --- naming the metric a covenant actually bounds --------------------------------------
# The contracts name their categories in a fixed vocabulary (confirmed by mining every PDF:
# «Капитальные затраты», «Операционные расходы», «Расходы на оплату труда», «Коммунальные
# расходы», «Процентные расходы», «Налоги», «Страховые премии»). A covenant that bounds one
# category names it, usually inside «...» quotes.
_RU_CATEGORY_LABELS = [
    ("капитальн", CAPEX),
    ("оплату труда", PAYROLL), ("оплате труда", PAYROLL), ("оплаты труда", PAYROLL),
    ("персонал", PAYROLL), ("заработн", PAYROLL),
    ("коммунальн", UTILITIES),
    ("процентн", INTEREST),
    ("налог", TAX),
    ("страхов", INSURANCE),
    ("аренд", LEASE), ("лизинг", LEASE),
    ("консультационн", OPEX),          # «Консультационные услуги» — an opex line
    ("операционн", OPEX),
    ("аффилирован", RELATED_PARTY),
    ("выручк", REVENUE),
]


def label_to_category(text: str | None) -> str:
    """Map ONE Russian category label (e.g. «Расходы на оплату труда») to a category.

    Shared with reclass.py so the reclassification parser and the covenant parser cannot
    drift apart. Whitespace is normalised first: pdftotext breaks labels across lines
    ('Расходы на оплату\\n\\nтруда'), which defeats any literal-space pattern."""
    if not text:
        return OTHER
    flat = re.sub(r"\s+", " ", text)
    found = _labels_in(flat)
    return found[0] if found else OTHER

# "связанные С НИМИ расходы" means "associated expenses" and has nothing to do with related
# parties — requiring сторон/аффилир after it stops P6 6.2 (a payroll+utilities coverage
# ratio) from being read as a related-party covenant.
_RELATED_RE = re.compile(r"связанн\w*\s+сторон|со\s+связанными\s+сторонами|аффилир|related[\s-]part")


def _labels_in(fragment: str) -> list[str]:
    """Every category named in `fragment`, ordered by where it appears. Order matters:
    a covenant listing "Расходов на оплату труда и Налогов" bounds both, and returning
    only the first would silently drop a term from the metric."""
    f = fragment.lower()
    found = [(f.find(kw), cat) for kw, cat in _RU_CATEGORY_LABELS if kw in f]
    out: list[str] = []
    for _, cat in sorted(found):
        if cat not in out:
            out.append(cat)
    return out


def spec_categories(spec: dict) -> list[str]:
    """Categories this covenant names — «quoted» defined terms win, else scan the clause."""
    text = f"{spec.get('name','')} {spec.get('metric','')} {spec.get('raw_text','')}"
    quoted: list[str] = []
    for q in re.findall(r"«([^»]{3,60})»", text):
        for c in _labels_in(q):
            if c not in quoted:
                quoted.append(c)
    return quoted or _labels_in(text)


# --- covenant kinds -------------------------------------------------------------------
def classify_kind(spec: dict) -> str:
    text = f"{spec.get('name','')} {spec.get('metric','')} {spec.get('raw_text','')}".lower()
    unit = spec.get("unit")
    def has(*ks): return any(k in text for k in ks)
    related = bool(_RELATED_RE.search(text))
    if has("капиталоёмк", "capital intensity"):
        return "CAPEX_INTENSITY"
    # The threshold UNIT decides absolute-aggregate vs ratio covenants.
    if unit == "ratio":
        if related:
            return "RELATED_PARTY_RATIO"
        # a leverage/cover ratio we can express as a signed-category formula
        if ratio_formula(spec):
            return "RATIO"
        return "RATIO_OTHER"         # unknown ratio -> not computable from ledger
    # absolute ($) aggregates
    if related:
        return "RELATED_PARTY_ABS"
    # Two different covenants both say "наибольш", and conflating them is a real trap:
    #  P10 6.2 "Выручка ЗА ВЫЧЕТОМ наибольшей из величин Расходов на оплату труда и
    #           Налогов" -> Revenue - max(payroll, tax)
    #  B1  6.2 "Соблюдение проверяется по наибольшей из указанных сумм; их сумма НЕ
    #           является показателем" -> max(payroll, utilities), never their sum
    if has("за вычетом") and has("наибольш"):
        return "REVENUE_LESS_MAX"
    if has("наибольш") or has("не в совокупности"):
        return "MAX_LINE"
    if has("выручк", "revenue") and spec.get("operator") in (">=", ">"):
        return "MIN_REVENUE"
    return "GENERIC"


def compute_actual(kind: str, txns: list[Txn], catf: Categorizer,
                   extras: dict | None = None, ignore: str | None = None) -> float | None:
    extras = extras or {}
    if kind == "CAPEX_INTENSITY":
        num = _sum(txns, CAPEX, catf, ignore)
        den = _sum(txns, OPEX, catf, ignore) + _sum(txns, LEASE, catf, ignore)
        return num / den if den else None
    if kind == "RATIO":
        f = extras.get("formula")
        if not f:
            return None
        den = _terms_sum(txns, f["den"], catf, ignore)
        return _terms_sum(txns, f["num"], catf, ignore) / den if den else None
    if kind == "MIN_REVENUE":
        return _sum(txns, REVENUE, catf, ignore)
    if kind == "RELATED_PARTY_ABS":
        return _sum(txns, RELATED_PARTY, catf, ignore)
    if kind == "RELATED_PARTY_RATIO":
        # The base differs per contract: most say "0.0Nx от выручки", but P6 6.1 says
        # "0.08x Операционных расходов". Using revenue for both silently mis-scales it.
        base_cat = extras.get("rp_base", REVENUE)
        base = _sum(txns, base_cat, catf, ignore) or extras.get("revenue")
        rp = _sum(txns, RELATED_PARTY, catf, ignore)
        return rp / base if base else None
    if kind == "MAX_LINE":
        cats = extras.get("categories") or []
        sums = [_sum(txns, c, catf, ignore) for c in cats]
        return max(sums) if sums else None
    if kind == "REVENUE_LESS_MAX":
        cats = extras.get("categories") or []
        sums = [_sum(txns, c, catf, ignore) for c in cats]
        return _sum(txns, REVENUE, catf, ignore) - (max(sums) if sums else 0.0)
    if kind == "LEVERAGE":
        ebitda = extras.get("ebitda")
        debt = extras.get("total_debt")
        return (debt / ebitda) if (ebitda and debt is not None) else None
    if kind == "GENERIC":
        cat = (extras.get("category") or OTHER)
        return _sum(txns, cat, catf, ignore)
    return None


def _status(op: str, actual: float, thr: float) -> str:
    if op in ("<=", "<"):
        return "COMPLIANT" if (actual <= thr if op == "<=" else actual < thr) else "BREACH"
    return "COMPLIANT" if (actual >= thr if op == ">=" else actual > thr) else "BREACH"


@dataclass
class Result:
    status: str
    actual: float | None
    evidence_txn_id: str | None
    kind: str = ""
    detail: dict = field(default_factory=dict)


def evaluate(spec: dict, txns: list[Txn], catf: Categorizer,
             reclasses: list[Reclass], extras: dict | None = None) -> Result:
    kind = classify_kind(spec)
    op = spec.get("operator", "<=")
    thr = spec.get("threshold")
    extras = dict(extras or {})
    formula = ratio_formula(spec) if kind == "RATIO" else None
    if formula:
        extras.setdefault("formula", formula)
    # Resolve which category/base the covenant text actually names, so the metric matches
    # the clause rather than a per-kind default.
    text = f"{spec.get('name','')} {spec.get('metric','')} {spec.get('raw_text','')}".lower()
    if kind == "RELATED_PARTY_RATIO":
        extras.setdefault("rp_base", OPEX if "операционных расход" in text else REVENUE)
    elif kind == "GENERIC":
        cats = spec_categories(spec)
        if cats and "category" not in extras:
            extras["category"] = cats[0]
    elif kind in ("MAX_LINE", "REVENUE_LESS_MAX"):
        # REVENUE is the outer term, not one of the lines being maximised over.
        extras.setdefault("categories",
                          [c for c in spec_categories(spec) if c != REVENUE])
    actual = compute_actual(kind, txns, catf, extras)
    if actual is None or thr is None:
        return Result("UNKNOWN", None, None, kind, {"reason": "insufficient data"})

    # Springing covenants only bind when their activation trigger is met; otherwise the
    # covenant is COMPLIANT by non-application (but `actual` is still the reported ratio).
    active = True
    if formula and formula.get("springing"):
        s = formula["springing"]
        trig = _terms_sum(txns, s["terms"], catf)
        active = trig > s["threshold"] if s["op"] == ">" else trig >= s["threshold"]
    status = _status(op, actual, thr) if active else "COMPLIANT"

    # Evidence = the single APPLIED reclassification whose removal flips the verdict.
    evidence = None
    if status == "BREACH":
        for r in (x for x in reclasses if x.applied):
            alt = compute_actual(kind, txns, catf, extras, ignore=r.txn_id)
            if alt is not None and _status(op, alt, thr) == "COMPLIANT":
                evidence = r.txn_id
                break
    # No abs() here: category aggregates are already magnitudes (see _sum), so a negative
    # value now only arises where the sign is real — a loss-making EBITDA margin, or
    # revenue below its largest overhead line. Reporting those as positive would invert
    # the finding.
    return Result(status, round(actual, 2), evidence, kind,
                  {"threshold": thr, "operator": op, "active": active})
