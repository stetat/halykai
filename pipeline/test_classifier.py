"""Repeatable accuracy test for the Gemini transaction classifier.

A labelled, realistic mini-ledger (natural-language descriptions, no category tags) covering
every category plus deliberately tricky rows. Reports overall accuracy and every miss.
This is an indicative number: it measures the classifier against OUR labels, which approximate
the grader's real transaction style — treat it as a calibration signal, not a guarantee.

Run:  python -m pipeline.test_classifier
"""
from __future__ import annotations
from .ledger import Txn
from . import classifier
from .engine import (CAPEX, OPEX, LEASE, REVENUE, RELATED_PARTY, INSURANCE, PAYROLL,
                     FINANCING, INTEREST, TAX, UTILITIES)

RELATED = {"Aktau Port Holding LLP", "Caspian Shared Services LLP"}

# (counterparty, description, amount, expected_category)
LABELS = [
    ("KEGOC JSC", "Оплата за электроэнергию за май", -41000, UTILITIES),
    ("Vodokanal LLP", "Водоснабжение и водоотведение терминала", -8800, UTILITIES),
    ("MAN Truck & Bus", "Приобретение седельного тягача для автопарка", -310000, CAPEX),
    ("Liebherr Cranes", "Покупка портального крана", -1250000, CAPEX),
    ("StroyMontazh LLP", "Строительство складского корпуса, 2 этап", -640000, CAPEX),
    ("Halyk Bank", "Проценты по старшему обеспеченному займу", -88000, INTEREST),
    ("State Revenue Committee", "Уплата корпоративного подоходного налога", -125000, TAX),
    ("State Revenue Committee", "Земельный налог за 2025 год", -14200, TAX),
    ("Aktau Port Holding LLP", "Управленческое вознаграждение материнской компании", -90000, RELATED_PARTY),
    ("Caspian Shared Services LLP", "Возмещение расходов общего центра услуг", -55000, RELATED_PARTY),
    ("ContainerLine Co", "Выручка за перевалку контейнеров", 5400000, REVENUE),
    ("Grain Traders JSC", "Оплата за перевалку зерна по договору", 1320000, REVENUE),
    ("Jusan Insurance JSC", "Страховая премия по полису имущественного страхования", -31000, INSURANCE),
    ("Internal payroll", "Заработная плата производственного персонала за май", -210000, PAYROLL),
    ("Internal payroll", "Отчисления и выплаты по оплате труда АУП", -76000, PAYROLL),
    ("TerminalProperty LLP", "Ежемесячная аренда причальной инфраструктуры", -75000, LEASE),
    ("LeaseFin LLP", "Лизинговый платёж за погрузчики", -22000, LEASE),
    ("Development Bank of KZ", "Поступление транша по кредитной линии", 2000000, FINANCING),
    ("Office World", "Канцтовары и хозяйственные принадлежности", -4300, OPEX),
    ("CleanPro LLP", "Клининговые услуги офиса и склада", -6100, OPEX),
    ("Logistics Fuel Co", "ГСМ для внутреннего транспорта", -38000, OPEX),
    ("IT Services LLP", "Абонентская плата за ПО учёта", -9500, OPEX),
]


def _mk(i, cp, desc, amt):
    t = Txn(f"TXN-P1-{i:04d}", "ACC-7801", "2025-05-01", amt, "USD", cp, desc, "P1")
    t.amount_usd = amt
    return t


def run_keyword():
    """Accuracy of the DETERMINISTIC path — free, offline, no API, always runnable.

    This is the path that actually runs by default and whenever Gemini 429s, so it gets a
    test that never depends on quota. It also pins `solve.base_classifier` to the shared
    implementation: the two were once separate copies, and the stale one could emit just
    4 of 13 categories (23% here), silently zeroing every ratio covenant that divides by
    interest/tax/utilities/insurance/financing."""
    from . import solve
    from .engine import Categorizer

    same = solve.base_classifier is classifier.keyword_category
    print(f"{'ok ' if same else 'FAIL'} solve.base_classifier is classifier.keyword_category")

    txns = [_mk(i, cp, d, a) for i, (cp, d, a, _) in enumerate(LABELS, 1)]
    catf = Categorizer(solve.base_classifier, [], related_parties=RELATED)
    misses = [(d[:44], exp, catf.category(t))
              for t, (cp, d, a, exp) in zip(txns, LABELS) if catf.category(t) != exp]
    ok = len(LABELS) - len(misses)
    print(f"Deterministic classifier accuracy: {ok}/{len(LABELS)} = {ok/len(LABELS):.0%} "
          f"(no API calls)")
    for d, e, p in misses:
        print(f"{d:>46}  {e:>13} -> {p:<13}")

    # Every category the covenant formulas consume must be reachable without an LLM.
    from .engine import (CAPEX, OPEX, LEASE, REVENUE, INSURANCE, PAYROLL,
                         FINANCING, INTEREST, TAX, UTILITIES)
    reachable = {catf.category(t) for t in txns}
    needed = {CAPEX, OPEX, LEASE, REVENUE, INSURANCE, PAYROLL, FINANCING, INTEREST,
              TAX, UTILITIES}
    gap = needed - reachable
    print(f"{'ok ' if not gap else 'FAIL'} all formula categories reachable offline"
          + (f" — MISSING {sorted(gap)}" if gap else ""))

    passed = same and not misses and not gap
    print("\n" + ("DETERMINISTIC CLASSIFIER OK" if passed
                  else "DETERMINISTIC CLASSIFIER REGRESSION"))
    return passed


def run():
    txns = [_mk(i, cp, d, a) for i, (cp, d, a, _) in enumerate(LABELS, 1)]
    expected = {t.txn_id: LABELS[i][3] for i, t in enumerate(txns)}
    pred = classifier.classify_batch(txns, related_parties=RELATED)

    ok = 0
    misses = []
    for t in txns:
        p, e = pred[t.txn_id], expected[t.txn_id]
        ok += (p == e)
        if p != e:
            misses.append((t.description[:44], e, p))
    n = len(txns)
    st = classifier.classify_batch.last_stats
    print(f"Classifier accuracy: {ok}/{n} = {ok/n:.0%}")
    print(f"source: {st.get('llm',0)} from Gemini, {st.get('related_override',0)} related-override, "
          f"{st.get('fallback',0)} keyword-fallback")
    if st.get("errors"):
        print(f"!! DEGRADED RUN — Gemini calls failed ({st['errors'][0][:60]}). "
              f"This accuracy reflects the keyword fallback, not Gemini. Retry after the "
              f"rate-limit window (free tier = ~20 requests/minute).")
    print()
    if misses:
        print(f"{'description':>46}  {'expected':>13} -> {'predicted':<13}")
        for d, e, p in misses:
            print(f"{d:>46}  {e:>13} -> {p:<13}")
    else:
        print("No misses.")
    return ok / n


if __name__ == "__main__":
    import sys
    # default: the free offline test. `--gemini` additionally spends quota on the LLM path.
    ok = run_keyword()
    if "--gemini" in sys.argv:
        print("\n" + "=" * 60)
        run()
    if not ok:
        raise SystemExit(1)
