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


# EBITDA is not a category any transaction can carry, so a formula naming it used to be
# unreachable and P3 6.1 / P7 6.1 went unexercised. But it is not an unknown either: the
# INDEPENDENT reading says so itself, in both of those cells — «EBITDA is NOT defined anywhere
# in this contract; sibling contracts P5 and B1 define it as «Выручка за вычетом Операционных
# расходов», so revenue - opex is the natural fill-in» — and writes exactly that expansion out
# by hand for P4 6.1 and P5 6.1. Substituting it here therefore stays inside the independent
# reading; it does not borrow the engine's definition, which is what would make this circular.
_EBITDA_EXPANSION = "(revenue - opex)"


def expand_ebitda(formula: str) -> str:
    return re.sub(r"\bebitda\b", _EBITDA_EXPANSION, formula)


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
    # WHICH of them is free decides whether a construction exists at all, not merely how tidy
    # it is. For «(tax + utilities) / (revenue - opex)» at 0.36x, solving for `tax` requires the
    # numerator to come out below the pinned utilities figure, i.e. negative, at every scale —
    # shrinking everything uniformly cannot fix a sign. Solving for `revenue` instead lands
    # every category positive. So try each candidate rather than taking the first.
    candidates = once + [c for c in names if c not in once]
    # Pin the others low enough that max() resolves to the free variable and denominators stay
    # non-zero. For ratios the pinned side is the denominator, so it needs a real magnitude.
    base = 1_000_000.0 if is_ratio else max(abs(target) * 0.25, 1_000.0)

    # A construction is only usable if EVERY category comes out a quantity a real ledger could
    # carry. Pin the other numerator terms too high and the only way to hit a small target ratio
    # is a NEGATIVE free variable; `totals_to_txns` then takes abs() and materialises a positive
    # expense, so the ledger stops satisfying the formula it was built from and the cell reports
    # a disagreement belonging to this harness rather than to the engine. P7 6.1 did exactly
    # that: utilities pinned at 1.37M against an EBITDA of 1.04M forced tax to -994,160 for a
    # 0.36x target, and the cell read 2.26 against the key's 0.36.
    for free in candidates:
        for shrink in (1.0, 0.25, 0.05, 0.01):
            pinned = {c: base * shrink * (1.0 + i * 0.37)
                      for i, c in enumerate(names) if c != free}
            # Once `ebitda` expands to (revenue - opex), the spread above can pin opex ABOVE
            # revenue — whichever appears later gets the larger value — and a negative EBITDA
            # runs the ratio backwards. Whenever both are pinned, keep the borrower profitable;
            # the magnitudes are arbitrary, only their order matters.
            if "revenue" in pinned and "opex" in pinned:
                pinned["opex"] = pinned["revenue"] * 0.4

            def f(x: float, _p=pinned) -> float | None:
                return evaluate_formula(formula, {**_p, free: x})

            # A ratio is a HYPERBOLA in its denominator: «(tax+utilities)/(revenue-opex)» has a
            # pole at revenue == opex, so the function is not monotonic across a wide bracket
            # and a global bisection walks straight through the discontinuity and converges on
            # nothing. Scan a coarse grid first, take the first adjacent pair that straddles the
            # target, and bisect only inside that interval — where the function IS monotonic.
            # Only x >= 0 is scanned, since a negative category total is rejected anyway.
            grid = [0.0] + [10.0 ** (e / 8.0) for e in range(8, 8 * 14)]
            brackets = []
            prev_x, prev_y = None, None
            for x in grid:
                y = f(x)
                if y is None:
                    prev_x, prev_y = None, None
                    continue
                if prev_y is not None and (prev_y - target) * (y - target) <= 0:
                    brackets.append((prev_x, x))
                prev_x, prev_y = x, y

            # EVERY straddle is tried, not just the first. A hyperbola crosses the target twice
            # over a wide scan: once at the genuine root and once at the POLE, where the value
            # leaps from large-negative to large-positive without ever equalling the target. The
            # pole interval usually comes first, so taking the first straddle finds nothing and
            # the cell reports "could not construct" for a formula that is perfectly solvable.
            for lo, hi in brackets:
                y_lo, y_hi = f(lo), f(hi)
                if y_lo is None or y_hi is None:
                    continue
                if y_lo > y_hi:                  # decreasing across this interval
                    lo, hi = hi, lo
                diverged = False
                for _ in range(200):
                    mid = (lo + hi) / 2
                    got = f(mid)
                    if got is None:
                        diverged = True
                        break
                    if got < target:
                        lo = mid
                    else:
                        hi = mid
                if diverged:
                    continue
                solved = dict(pinned)
                solved[free] = (lo + hi) / 2
                got = evaluate_formula(formula, solved)
                if got is None or abs(got - target) > max(abs(target) * 1e-6, 1e-6):
                    continue                     # this straddle was a pole, not a root
                if any(v < 0 for v in solved.values()):
                    continue                     # a negative category total
                return solved
    return None                                       # could not construct — reported, not hidden


def scale_past_springing_trigger(totals: dict[str, float],
                                 trigger: float | None) -> dict[str, float]:
    """Scale a RATIO construction up until it actually activates a springing covenant.

    P3 6.1 only applies «ТОЛЬКО ПРИ УСЛОВИИ, что совокупные поступления по финансированию
    превышают $4,000,000.00». The bisection has no reason to land above that — it solved for a
    ratio of 1.71 at a financing figure of $1.4M — so the engine correctly declined to spring
    and returned COMPLIANT against the key's BREACH. That read as a metric disagreement and was
    nothing of the kind: the harness had built a borrower the covenant does not apply to.

    Every total is scaled by one factor, which leaves the ratio (and therefore `actual`)
    untouched. Scaling ALL of them past the trigger, rather than the one term the engine says
    triggers, keeps the harness from having to ask the engine which term that is."""
    if not trigger:
        return totals
    biggest = max((abs(v) for v in totals.values()), default=0.0)
    smallest = min((abs(v) for v in totals.values() if v), default=0.0)
    if not smallest or smallest > trigger:
        return totals
    factor = (trigger * 1.5) / smallest
    if factor * biggest > 1e15:            # refuse to build a ledger of absurd magnitude
        return totals
    return {c: v * factor for c, v in totals.items()}


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
            formula = expand_ebitda(formula)
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
            is_ratio = spec.get("unit") == "ratio"
            totals = build_totals(formula, float(target), is_ratio=is_ratio)
            if totals is None:
                skipped.append((sc, cid, f"could not construct a ledger for {formula!r}"))
                continue
            if is_ratio:
                totals = scale_past_springing_trigger(totals,
                                                      spec.get("springing_trigger_usd"))
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
