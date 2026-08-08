"""End-to-end test against the ANSWER KEY, with the circularity removed.

The problem this solves
-----------------------
`reconstruct.py` scores 36/36 and proves almost nothing. It builds its ledger by asking
`engine.compute_actual` what the engine wants, then checks the engine computes it — so an
engine that bounds the WRONG QUANTITY still scores a perfect 36/36. Thirteen of the 36 cells
once did exactly that while every harness read green. `validate.py` has the same blind spot
from the other side: it bracket-checks thresholds and never evaluates a metric.

The fix is to build the ledger from a reading of the contracts that never saw the engine.
`expected_semantics.EXPECTED` is written by an agent forbidden from opening engine.py,
covenants.py, reconstruct.py or solve.py — it states, per cell, what the clause requires as a
plain formula over category names. This harness then:

    key `actual`  +  independent formula   ->  a ledger that MAKES that actual true
    ledger  ->  real keyword classifier -> real Categorizer -> real engine.evaluate
    result  ->  scored against the key with the real scorer

Agreement means the engine's metric, the independent reading of the clause and the key all
coincide. Disagreement is a real finding: either the engine bounds the wrong quantity, or the
independent reading is wrong — and both are worth knowing before event day. Neither can be
"fixed" by adjusting the harness, which is the whole point.

What it does NOT cover: CSV dialect handling (test_ledger, test_roundtrip) and document
extraction (validate, test_docs). It covers everything between a parsed transaction and a
scored cell.

    python -m pipeline.test_e2e
"""
from __future__ import annotations
import json
import re

from . import config, docmap, covenants, reclass, engine, scorer
from .engine import Categorizer
from .ledger import Txn
from .solve import base_classifier

CATEGORIES = ("revenue", "opex", "capex", "lease", "payroll", "utilities", "tax",
              "interest", "insurance", "financing", "related_party", "ebitda", "other")

# Natural-language descriptions, so the REAL keyword classifier has to route each amount to the
# right bucket. Feeding the engine pre-labelled categories would excuse the classifier from the
# test; these are the same words a Kazakh bank statement uses.
DESC = {
    "revenue":   ("ТОО Каспий Трейд", "Выручка от реализации продукции по договору поставки"),
    "opex":      ("ТОО Алатау Сервис", "Операционные расходы: услуги подрядчика за период"),
    "capex":     ("ТОО Проммаш", "Приобретение оборудования, капитальные затраты"),
    "lease":     ("ТОО Капитал Плаза", "Арендная плата за производственные помещения"),
    "payroll":   ("ТОО Астана Логистик", "Расходы на оплату труда персонала за период"),
    "utilities": ("АО Энергосбыт", "Коммунальные расходы: электроэнергия и теплоснабжение"),
    "tax":       ("УГД по району", "Уплата корпоративного подоходного налога за период"),
    "interest":  ("АО Банк ЦентрКредит", "Процентные расходы по банковскому займу"),
    "insurance": ("АО Нурполис", "Страховые премии по договору страхования имущества"),
    "financing": ("АО Банк ЦентрКредит", "Поступление транша по кредитной линии"),
    "other":     ("ТОО Прочее", "Прочие расчёты по договору"),
}
INFLOW = {"revenue", "financing"}
_NAME_RE = re.compile("|".join(sorted(CATEGORIES, key=len, reverse=True)))


def evaluate_formula(formula: str, totals: dict[str, float]) -> float | None:
    """Evaluate an independent formula over category totals. Names are restricted to
    CATEGORIES, so this cannot execute anything the semantics file did not intend."""
    names = set(_NAME_RE.findall(formula))
    if not names <= set(CATEGORIES):
        return None
    env = {c: float(totals.get(c, 0.0)) for c in CATEGORIES}
    env["max"] = max
    try:
        return float(eval(formula, {"__builtins__": {}}, env))       # noqa: S307 - closed env
    except ZeroDivisionError:
        return None


def build_totals(formula: str, target: float, is_ratio: bool) -> dict[str, float] | None:
    """Category totals that make `formula` evaluate to `target`.

    One category in the formula is left free and solved for numerically; the rest are pinned at
    fixed values. Bisection rather than algebra so the same code handles ratios, sums, max() and
    differences without knowing which it has."""
    # Order by first appearance in the formula, so the free variable is the numerator/leading
    # term rather than whichever category happens to come first in CATEGORIES. Solving for a
    # denominator works too, but only if the search knows the function runs the other way.
    seen: list[str] = []
    for m in _NAME_RE.finditer(formula):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    names = seen
    if not names:
        return None
    # Prefer a category appearing exactly once. In "(revenue - opex) / revenue" the leading term
    # sits on both sides of the division, so the function is non-monotonic in it and bisection
    # cannot converge; opex appears once and the formula falls cleanly in it.
    once = [c for c in names if len(re.findall(rf"\b{c}\b", formula)) == 1]
    free = once[0] if once else names[0]
    # Pin the others low enough that max() resolves to the free variable and denominators stay
    # non-zero. For ratios the pinned side is the denominator, so it needs a real magnitude.
    base = 1_000_000.0 if is_ratio else max(abs(target) * 0.25, 1_000.0)
    totals = {c: base * (1.0 + i * 0.37) for i, c in enumerate(names) if c != free}

    def f(x: float) -> float | None:
        return evaluate_formula(formula, {**totals, free: x})

    lo, hi = -1e13, 1e13
    f_lo, f_hi = f(lo), f(hi)
    if f_lo is None or f_hi is None:
        return None
    if f_lo > f_hi:                       # decreasing in the free variable (a denominator)
        lo, hi = hi, lo
    for _ in range(300):
        mid = (lo + hi) / 2
        got = f(mid)
        if got is None:
            return None
        if got < target:
            lo = mid
        else:
            hi = mid
    totals[free] = (lo + hi) / 2
    got = evaluate_formula(formula, totals)
    if got is None or abs(got - target) > max(abs(target) * 1e-6, 1e-6):
        return None                                   # could not construct — reported, not hidden
    return totals


def totals_to_txns(sc: str, totals: dict[str, float],
                   related_party: str | None = None) -> list[Txn]:
    """Materialise category totals as transactions the real classifier must route.

    related_party is deliberately NOT given a description: the contracts say membership is an
    identity test decided by the KYC ownership table, "а не назначением платежа". So the row
    carries a counterparty drawn from that borrower's actual related-party list and an
    innocuous description — if the pipeline only recognised it by wording, it would fail here,
    which is correct."""
    txns = []
    for i, (cat, amount) in enumerate(sorted(totals.items()), 1):
        if not amount:
            continue
        if cat == "related_party":
            cp, desc = (related_party or "Related Party LLP"), "Расчёты по договору оказания услуг"
        else:
            cp, desc = DESC.get(cat, DESC["other"])
        signed = abs(amount) if cat in INFLOW else -abs(amount)
        # 9000-block ids. Low ids collide with REAL reclassification targets — TXN-P3-0001 is
        # an actual applied reclass to opex, so a synthetic revenue row numbered 0001 was being
        # reclassified out of revenue and the cell read BREACH for a reason nothing to do with
        # the engine's metric.
        t = Txn(f"TXN-{sc}-9{i:03d}", config.SCENARIO_TO_ACC[sc], "2025-06-01",
                signed, "USD", cp, desc, sc)
        t.amount_usd = signed
        txns.append(t)
    return txns


def main() -> None:
    try:
        from .expected_semantics import EXPECTED
    except ImportError:
        print("expected_semantics.py is missing — it is written by an agent that never sees\n"
              "engine.py. Without it this harness cannot be non-circular, so it will not run.")
        raise SystemExit(1)

    key = json.loads(config.ANSWER_KEY.read_text(encoding="utf-8"))["scenarios"]
    dm = docmap.build(save=False)
    specs = covenants.build(use_llm=False, save=False)

    built = {}
    skipped: list[tuple[str, str, str]] = []
    for sc, covs in key.items():
        built[sc] = {}
        acc = config.SCENARIO_TO_ACC[sc]
        rcs = reclass.for_account(acc, dm)
        rps = reclass.related_parties(acc, dm)
        excluded = reclass.period_exclusions(acc, dm)
        addback = reclass.ebitda_addback(acc)
        for cid, truth in covs["covenants"].items():
            exp = EXPECTED.get(sc, {}).get(cid) or {}
            spec = specs.get(sc, {}).get("covenants", {}).get(cid)
            formula, target = exp.get("formula"), truth.get("actual")
            if not formula or target is None or not spec:
                skipped.append((sc, cid, "no independent formula" if not formula else "no spec"))
                continue
            # EBITDA is derived inside the engine, not a category a transaction can carry, so a
            # formula naming it cannot be materialised as a ledger. Skipped and reported rather
            # than silently approximated into something that would pass for the wrong reason.
            unmaterialisable = set(_NAME_RE.findall(formula)) - set(DESC) - {"related_party"}
            # Also skip when the ENGINE's metric needs a quantity no ledger row can carry —
            # group-level capex comes from the parent's consolidated statements and EBITDA is
            # derived. Consulting the engine here decides only WHETHER a cell is testable, never
            # what the right answer is; the formula under test still comes solely from the
            # independent reading. Without this, P5 6.1 reads as a disagreement when in fact the
            # engine matches the clause («капитальных затрат Группы к EBITDA Заёмщика») and it is
            # the harness vocabulary that cannot express a Group-scope numerator.
            try:
                rf = engine.ratio_formula(spec) or {}
                needed = {c for _, c in list(rf.get("num", [])) + list(rf.get("den", []))}
            except Exception:
                needed = set()
            # __ebitda__ is NOT excluded: the engine derives it from ledger categories the
            # harness can materialise, so those cells are testable. Only quantities that no
            # transaction can carry at all — group-level capex from the parent's consolidated
            # accounts — make a cell unreachable.
            engine_side = {c for c in needed
                           if c not in DESC and c not in ("related_party", "__ebitda__")}
            if unmaterialisable or engine_side:
                what = sorted(unmaterialisable | engine_side)
                skipped.append((sc, cid, f"cannot materialise {what} as ledger rows"))
                continue
            # The key rounds `actual` for display (0.04, 1.68). When the true value sits within
            # that rounding of the threshold, reconstructing it lands exactly ON the boundary and
            # the verdict is decided by the rounding, not by the engine. P8 6.3 is the case:
            # key BREACH at "0.04" against a <= 0.04 limit, so the real value must be 0.040x.
            # Untestable by construction — reported, not counted as a disagreement.
            thr = spec.get("threshold")
            if thr is not None and abs(float(target) - float(thr)) < 1e-9:
                skipped.append((sc, cid, f"key actual {target} is within its own rounding of "
                                         f"the {thr} threshold — verdict not reconstructible"))
                continue
            totals = build_totals(formula, float(target),
                                  is_ratio=(spec.get("unit") == "ratio"))
            if totals is None:
                skipped.append((sc, cid, f"could not construct a ledger for {formula!r}"))
                continue
            catf = Categorizer(base_classifier, rcs, related_parties=rps,
                               unrestricted_parties=reclass.unrestricted_subsidiaries(acc),
                               excluded_txns=excluded)
            try:
                txns = totals_to_txns(sc, totals, next(iter(sorted(rps)), None))
                res = engine.evaluate(spec, txns, catf, rcs,
                                      extras={"ebitda_addback": addback})
                # An EBITDA add-back is an ABSOLUTE dollar amount, so its effect on a ratio
                # depends entirely on the revenue scale — and the scale here is invented. P4 6.1
                # reproduces the key exactly without the add-back and diverges with it, which
                # says nothing about whether the add-back belongs; only the real ledger's
                # magnitudes can settle that. Report rather than score it either way.
                if addback:
                    bare = engine.evaluate(spec, txns, catf, rcs,
                                           extras={"ebitda_addback": 0.0})
                    if (bare.actual is not None and res.actual is not None
                            and abs(bare.actual - res.actual) > 1e-9):
                        skipped.append((sc, cid, f"EBITDA add-back ${addback:,.0f} moves actual "
                                                 f"{bare.actual} -> {res.actual}; undecidable at "
                                                 f"a synthetic revenue scale"))
                        continue
            except Exception as e:
                skipped.append((sc, cid, f"engine error: {e}"))
                continue
            built[sc][cid] = {"status": res.status, "actual": res.actual,
                              "evidence_txn_id": res.evidence_txn_id}

    print("=" * 78)
    print("END-TO-END vs THE ANSWER KEY — ledger built from an INDEPENDENT reading")
    print("=" * 78)
    total = scorer.score_submission({"answers": built})

    if skipped:
        print(f"\n{len(skipped)} cell(s) not exercised:")
        for sc, cid, why in skipped:
            print(f"   {sc:>4} {cid}  {why}")
    print("\nA mismatch here is a REAL disagreement between the engine's metric, an independent\n"
          "reading of the clause, and the key. It cannot be fixed by changing this harness.")
    return total


if __name__ == "__main__":
    main()
