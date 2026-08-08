"""Correctness tests for the Stage B engine, on a synthetic ledger.

Run: python -m pipeline.test_engine
The real ledger is withheld from this practice release, so we construct a small ledger
whose true aggregates are known, and assert the engine reproduces status/actual and finds
the reclassification-driven evidence transaction. This proves the MATH and the leave-one-out
evidence logic, independent of the (unavailable) real data."""
from __future__ import annotations
from .ledger import Txn
from . import engine
from .engine import (Reclass, Categorizer, CAPEX, OPEX, LEASE, RELATED_PARTY, REVENUE,
                     OTHER, PAYROLL, UTILITIES, TAX)


def _t(txn_id, amount, cp="", desc="", cur="USD"):
    t = Txn(txn_id, "ACC-0001", "2025-06-01", amount, cur, cp, desc, "X")
    t.amount_usd = amount
    return t


def _base_classifier(keyword_map):
    def clf(t: Txn) -> str:
        blob = f"{t.counterparty} {t.description}".lower()
        for kw, cat in keyword_map.items():
            if kw in blob:
                return cat
        return OTHER
    return clf


def test_capex_intensity_ratio_and_no_evidence():
    # capex/(opex+lease); threshold <= 0.42. Build capex=210k, opex=400k, lease=100k -> 0.42 exactly.
    txns = [
        _t("TXN-X-0001", -110_000, desc="capex machinery"),
        _t("TXN-X-0002", -100_000, desc="capex building"),
        _t("TXN-X-0003", -400_000, desc="opex operating"),
        _t("TXN-X-0004", -100_000, desc="lease rent"),
    ]
    clf = _base_classifier({"capex": CAPEX, "opex": OPEX, "lease": LEASE})
    catf = Categorizer(clf, reclasses=[])
    spec = {"name": "Maximum Capital Intensity Ratio", "operator": "<=", "threshold": 0.42}
    r = engine.evaluate(spec, txns, catf, reclasses=[])
    assert r.kind == "CAPEX_INTENSITY", r.kind
    assert abs(r.actual - 0.42) < 1e-9, r.actual
    assert r.status == "COMPLIANT", r.status          # 0.42 <= 0.42
    assert r.evidence_txn_id is None
    print("ok  capex-intensity ratio, boundary COMPLIANT, no evidence")


def test_reclassification_flips_and_is_the_evidence():
    # Related-party payments, cap <= $450,000. A reclassification MOVES a $120k payment
    # INTO the related-party bucket, pushing the total over the cap -> BREACH, and that
    # single reclassified txn is the evidence (remove the reclass -> COMPLIANT again).
    txns = [
        _t("TXN-X-0010", -300_000, cp="RelatedCo",  desc="mgmt fee"),      # related
        _t("TXN-X-0011", -50_000,  cp="RelatedCo",  desc="mgmt fee"),      # related
        _t("TXN-X-0012", -120_000, cp="VendorLtd",  desc="consulting"),    # base=other...
    ]
    clf = _base_classifier({"relatedco": RELATED_PARTY})
    reclasses = [Reclass(txn_id="TXN-X-0012", to_category=RELATED_PARTY,
                         from_category=OTHER, applied=True)]
    catf = Categorizer(clf, reclasses)
    spec = {"name": "Maximum Related-Party Payments", "operator": "<=", "threshold": 450_000}
    r = engine.evaluate(spec, txns, catf, reclasses)
    assert r.kind == "RELATED_PARTY_ABS", r.kind
    assert abs(r.actual - 470_000) < 1e-6, r.actual   # 300k+50k+120k
    assert r.status == "BREACH", r.status
    assert r.evidence_txn_id == "TXN-X-0012", r.evidence_txn_id
    print("ok  reclassification pushes over cap; evidence = the reclassified txn")


def test_rejected_reclassification_is_not_evidence():
    # Same as above but the reclassification is REJECTED (applied=False): total stays 350k,
    # COMPLIANT, and there is no evidence. (Guards against the 'considered but rejected' trap.)
    txns = [
        _t("TXN-X-0010", -300_000, cp="RelatedCo", desc="mgmt fee"),
        _t("TXN-X-0011", -50_000,  cp="RelatedCo", desc="mgmt fee"),
        _t("TXN-X-0012", -120_000, cp="VendorLtd", desc="consulting"),
    ]
    clf = _base_classifier({"relatedco": RELATED_PARTY})
    reclasses = [Reclass("TXN-X-0012", RELATED_PARTY, OTHER, applied=False)]
    catf = Categorizer(clf, reclasses)
    spec = {"name": "Maximum Related-Party Payments", "operator": "<=", "threshold": 450_000}
    r = engine.evaluate(spec, txns, catf, reclasses)
    assert abs(r.actual - 350_000) < 1e-6, r.actual
    assert r.status == "COMPLIANT", r.status
    assert r.evidence_txn_id is None
    print("ok  rejected reclassification ignored; no false breach, no evidence")


def test_min_revenue_breach_no_single_evidence():
    # Revenue floor >= $7,100,000; income positive. Sum 6.84M -> BREACH, ratio/aggregate
    # with no reclassification -> evidence null.
    txns = [_t(f"TXN-X-{i:04d}", amt, desc="revenue sales")
            for i, amt in enumerate([3_000_000, 2_840_000, 1_000_000], start=20)]
    clf = _base_classifier({"revenue": REVENUE})
    catf = Categorizer(clf, reclasses=[])
    spec = {"name": "Минимальная выручка", "operator": ">=", "threshold": 7_100_000}
    r = engine.evaluate(spec, txns, catf, reclasses=[])
    assert r.kind == "MIN_REVENUE", r.kind
    assert abs(r.actual - 6_840_000) < 1e-6, r.actual
    assert r.status == "BREACH" and r.evidence_txn_id is None
    print("ok  min-revenue breach, aggregate -> evidence null")


# --- metric-definition tests -----------------------------------------------------------
# These use the ACTUAL clause wording mined from the contract PDFs. They exist because
# neither reconstruct.py nor validate.py can catch a wrong metric: reconstruct synthesises
# a ledger to satisfy whatever formula the engine uses (so a wrong formula still scores
# 36/36), and validate only bracket-checks thresholds. Only these tests pin the semantics.

def _lines_ledger():
    """payroll = 1,284,663.42 (the larger line), utilities = 900,000."""
    return [
        _t("TXN-X-0001", -1_284_663.42, desc="payroll wages"),
        _t("TXN-X-0002", -900_000.00, desc="utilities power"),
    ], Categorizer(_base_classifier({"payroll": PAYROLL, "utilities": UTILITIES}),
                   reclasses=[])


def test_max_line_is_max_not_sum():
    # B1 6.2: "отдельными статьями ... по отдельности, а НЕ в совокупности: (а) расходы на
    # оплату труда и (б) расходы на коммунальные услуги ... Соблюдение проверяется по
    # НАИБОЛЬШЕЙ из указанных сумм; их сумма НЕ является показателем настоящего ковенанта."
    txns, catf = _lines_ledger()
    spec = {"name": "Individual Overhead Line Ceiling", "operator": "<=",
            "threshold": 1_500_000.0, "unit": "usd",
            "raw_text": "отдельными статьями накладных расходов признаются, по отдельности, "
                        "а не в совокупности: (а) расходы на оплату труда и (б) расходы на "
                        "коммунальные услуги. Соблюдение проверяется по наибольшей из "
                        "указанных сумм; их сумма не является показателем."}
    r = engine.evaluate(spec, txns, catf, reclasses=[])
    assert r.kind == "MAX_LINE", r.kind
    # the trap: sum = 2,184,663.42 would BREACH; the correct max = 1,284,663.42 is COMPLIANT
    assert abs(r.actual - 1_284_663.42) < 0.005, r.actual
    assert r.status == "COMPLIANT", r.status
    print("ok  max-line ceiling uses max(lines), not their sum")


def test_revenue_less_max_line():
    # P10 6.2: "Выручка ЗА ВЫЧЕТОМ наибольшей из величин Расходов на оплату труда и Налогов"
    txns = [
        _t("TXN-X-0001", 8_000_000.00, desc="revenue sales"),
        _t("TXN-X-0002", -2_000_000.00, desc="payroll wages"),
        _t("TXN-X-0003", -500_000.00, desc="tax profit"),
    ]
    catf = Categorizer(_base_classifier(
        {"revenue": REVENUE, "payroll": PAYROLL, "tax": TAX}), reclasses=[])
    spec = {"name": "Минимальная выручка за вычетом наибольшей статьи накладных расходов",
            "operator": ">=", "threshold": 5_000_000.0, "unit": "usd",
            "raw_text": "чтобы Выручка за вычетом наибольшей из величин Расходов на оплату "
                        "труда и Налогов составляла не менее $5,000,000.00"}
    r = engine.evaluate(spec, txns, catf, reclasses=[])
    assert r.kind == "REVENUE_LESS_MAX", r.kind
    # 8,000,000 - max(2,000,000, 500,000) = 6,000,000 (subtracting the SUM would give 5.5M)
    assert abs(r.actual - 6_000_000.0) < 0.005, r.actual
    assert r.status == "COMPLIANT", r.status
    print("ok  revenue-less-max subtracts only the largest line")


def test_related_party_ratio_base_is_opex_when_clause_says_so():
    # P6 6.1: "...превышал 0.08x Операционных расходов Заёмщика" — NOT revenue.
    txns = [
        _t("TXN-X-0001", 10_000_000.00, desc="revenue sales"),
        _t("TXN-X-0002", -1_000_000.00, desc="opex operating"),
        _t("TXN-X-0003", -100_000.00, cp="Affiliate LLP", desc="services"),
    ]
    catf = Categorizer(_base_classifier({"revenue": REVENUE, "opex": OPEX}),
                       reclasses=[], related_parties={"Affiliate LLP"})
    spec = {"name": "Максимальная доля платежей связанным сторонам в операционных расходах",
            "operator": "<=", "threshold": 0.08, "unit": "ratio",
            "raw_text": "чтобы совокупный объём платежей в пользу связанных сторон превышал "
                        "0.08x Операционных расходов Заёмщика за этот период"}
    r = engine.evaluate(spec, txns, catf, reclasses=[])
    assert r.kind == "RELATED_PARTY_RATIO", r.kind
    # 100k/1M = 0.10 BREACH. Against revenue it would be 0.01 -> a false COMPLIANT.
    assert abs(r.actual - 0.10) < 1e-9, r.actual
    assert r.status == "BREACH", r.status
    print("ok  related-party ratio divides by the base the clause names (opex, not revenue)")


def test_associated_expenses_is_not_a_related_party_covenant():
    # P6 6.2 trap: "Расходы на оплату труда означают все выплаты персоналу и СВЯЗАННЫЕ С НИМИ
    # расходы" — "связанные" here means "associated", not "related party".
    txns = [
        _t("TXN-X-0001", 7_280_000.00, desc="revenue sales"),
        _t("TXN-X-0002", -1_000_000.00, desc="payroll wages"),
        _t("TXN-X-0003", -1_000_000.00, desc="utilities power"),
    ]
    catf = Categorizer(_base_classifier(
        {"revenue": REVENUE, "payroll": PAYROLL, "utilities": UTILITIES}), reclasses=[])
    spec = {"name": "Минимальное покрытие расходов на персонал и коммунальные услуги выручкой",
            "operator": ">=", "threshold": 3.0, "unit": "ratio",
            "raw_text": "чтобы Выручка составляла не менее 3.00x совокупной величины Расходов "
                        "на оплату труда и Коммунальных расходов. Расходы на оплату труда "
                        "означают все выплаты персоналу и связанные с ними расходы"}
    r = engine.evaluate(spec, txns, catf, reclasses=[])
    assert r.kind == "RATIO", r.kind
    assert engine.ratio_formula(spec)["id"] == "revenue_cover_payroll_util"
    assert abs(r.actual - 3.64) < 0.005, r.actual      # 7.28M / 2.0M
    assert r.status == "COMPLIANT", r.status
    print("ok  'связанные с ними расходы' is not read as a related-party covenant")


def test_generic_uses_the_quoted_category():
    # "...расходов по статье «Капитальные затраты» ... превышал $1,600,000.00"
    txns = [
        _t("TXN-X-0001", -1_482_663.28, desc="capex plant"),
        _t("TXN-X-0002", -900_000.00, desc="opex operating"),
    ]
    catf = Categorizer(_base_classifier({"capex": CAPEX, "opex": OPEX}), reclasses=[])
    spec = {"name": "Максимальные расходы по категории", "operator": "<=",
            "threshold": 1_600_000.0, "unit": "usd",
            "raw_text": "совокупный объём расходов по статье «Капитальные затраты» за период "
                        "превышал $1,600,000.00"}
    r = engine.evaluate(spec, txns, catf, reclasses=[])
    assert r.kind == "GENERIC", r.kind
    assert abs(r.actual - 1_482_663.28) < 0.005, r.actual   # not 0.0 from the OTHER bucket
    assert r.status == "COMPLIANT", r.status
    print("ok  generic category covenant reads its «quoted» category")


def main():
    test_capex_intensity_ratio_and_no_evidence()
    test_reclassification_flips_and_is_the_evidence()
    test_rejected_reclassification_is_not_evidence()
    test_min_revenue_breach_no_single_evidence()
    test_max_line_is_max_not_sum()
    test_revenue_less_max_line()
    test_related_party_ratio_base_is_opex_when_clause_says_so()
    test_associated_expenses_is_not_a_related_party_covenant()
    test_generic_uses_the_quoted_category()
    print("\nALL ENGINE TESTS PASSED")


if __name__ == "__main__":
    main()
