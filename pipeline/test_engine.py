"""Correctness tests for the Stage B engine, on a synthetic ledger.

Run: python -m pipeline.test_engine
The real ledger is withheld from this practice release, so we construct a small ledger
whose true aggregates are known, and assert the engine reproduces status/actual and finds
the reclassification-driven evidence transaction. This proves the MATH and the leave-one-out
evidence logic, independent of the (unavailable) real data."""
from __future__ import annotations
from .ledger import Txn
from . import engine
from .engine import Reclass, Categorizer, CAPEX, OPEX, LEASE, RELATED_PARTY, REVENUE, OTHER


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


def main():
    test_capex_intensity_ratio_and_no_evidence()
    test_reclassification_flips_and_is_the_evidence()
    test_rejected_reclassification_is_not_evidence()
    test_min_revenue_breach_no_single_evidence()
    print("\nALL ENGINE TESTS PASSED")


if __name__ == "__main__":
    main()
