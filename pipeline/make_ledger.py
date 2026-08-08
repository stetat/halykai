"""Generate a REALISTIC rehearsal ledger, then run the real CLI against it.

Every other harness feeds the engine directly. This one writes an actual multi-borrower
CSV to disk and drives `solve()` exactly as event day will: file -> ledger.load -> FX ->
keyword classifier -> engine -> submission.json. It is the only test that exercises the
whole chain as one program, including the parts no unit test touches (the classifier
reading natural-language descriptions, per-borrower routing, the ingestion report).

It deliberately does NOT try to reproduce the answer key. A single ledger serves all three
covenants of a borrower, so the per-cell aggregates cannot match a key built from separate
cell fixtures — `reconstruct.py` is the scored harness. What this proves is that the
plumbing survives a real file: 36 cells computed, no crash, sane categories, evidence found.

    python -m pipeline.make_ledger          # write the ledger + fx, then solve and report
"""
from __future__ import annotations
import csv
import random
from pathlib import Path

from . import config, docmap, reclass

OUT_LEDGER = config.CACHE / "rehearsal_ledger.csv"
OUT_FX = config.CACHE / "rehearsal_fx.csv"

# Natural-language descriptions per category — the classifier must infer the category from
# these, exactly as it will on event day. No category tags anywhere.
DESCRIPTIONS = {
    "revenue":   ["Выручка за перевалку контейнеров", "Оплата за перевалку зерна по договору",
                  "Поступление от реализации продукции", "Выручка от оказания услуг"],
    "opex":      ["Канцтовары и хозяйственные принадлежности", "Клининговые услуги офиса",
                  "ГСМ для внутреннего транспорта", "Абонентская плата за ПО учёта",
                  "Консультационные услуги по сопровождению"],
    "capex":     ["Приобретение седельного тягача", "Покупка портального крана",
                  "Строительство складского корпуса, 2 этап", "Капитальные затраты: оборудование"],
    "lease":     ["Ежемесячная аренда причальной инфраструктуры", "Лизинговый платёж за погрузчики"],
    "payroll":   ["Расходы на оплату труда персонала", "Заработная плата за месяц"],
    "utilities": ["Коммунальные расходы: электроэнергия", "Водоснабжение и водоотведение"],
    "tax":       ["Уплата корпоративного подоходного налога", "Земельный налог за 2025 год"],
    "interest":  ["Процентные расходы по старшему займу", "Проценты по кредитной линии"],
    "insurance": ["Страховые премии по договору страхования"],
    "financing": ["Поступление транша по кредитной линии", "Финансирование от банка"],
}
COUNTERPARTIES = {
    "revenue": "ContainerLine Co", "opex": "Office World", "capex": "StroyMontazh LLP",
    "lease": "TerminalProperty LLP", "payroll": "Internal payroll", "utilities": "KEGOC JSC",
    "tax": "State Revenue Committee", "interest": "Halyk Bank",
    "insurance": "Jusan Insurance JSC", "financing": "Development Bank of KZ",
}
INFLOW = {"revenue", "financing"}
# rough per-category magnitudes so the covenants land in a plausible range
SIZE = {"revenue": 900_000, "opex": 120_000, "capex": 200_000, "lease": 60_000,
        "payroll": 150_000, "utilities": 50_000, "tax": 70_000, "interest": 90_000,
        "insurance": 30_000, "financing": 400_000}


def build(seed: int = 7) -> tuple[Path, Path]:
    rnd = random.Random(seed)
    dm = docmap.build(save=False)
    rows = []
    for sc, acc in config.SCENARIO_TO_ACC.items():
        rps = sorted(reclass.related_parties(acc, dm))
        unres = sorted(reclass.unrestricted_subsidiaries(acc))
        rcs = reclass.for_account(acc, dm)
        n = 1
        for cat, base in SIZE.items():
            for _ in range(3):
                amt = round(base * rnd.uniform(0.6, 1.4), 2)
                if cat not in INFLOW:
                    amt = -amt
                rows.append([f"TXN-{sc}-{n:04d}", acc, "2025-06-01", f"{amt:.2f}", "USD",
                             COUNTERPARTIES[cat], rnd.choice(DESCRIPTIONS[cat])])
                n += 1
        # a refund and a credit note: these must NET against their category, not add
        rows.append([f"TXN-{sc}-{n:04d}", acc, "2025-07-01", "40000.00", "USD",
                     COUNTERPARTIES["capex"], "Возврат аванса поставщиком (капитальные затраты)"])
        n += 1
        # related-party payments, by IDENTITY (the description says nothing about it)
        for rp in rps or ["Unknown Affiliate LLP"]:
            rows.append([f"TXN-{sc}-{n:04d}", acc, "2025-08-01", "-95000.00", "KZT",
                         rp, "Оплата по договору оказания услуг"])
            n += 1
        # a transfer to an unrestricted subsidiary (identity-based too)
        for u in unres:
            rows.append([f"TXN-{sc}-{n:04d}", acc, "2025-08-15", "-180000.00", "USD",
                         u, "Передача активов дочерней организации"])
            n += 1
        # the real reclassified transactions, so the evidence path is exercised
        for r in rcs:
            rows.append([r.txn_id, acc, "2025-09-01", "-260000.00", "USD",
                         "Advisory LLP", "Консультационные услуги по проекту"])

    OUT_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_LEDGER, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["txn_id", "account_id", "date", "amount", "currency",
                    "counterparty", "description"])
        w.writerows(rows)
    OUT_FX.write_text("currency,rate\nKZT,0.002\n", encoding="utf-8")
    return OUT_LEDGER, OUT_FX


if __name__ == "__main__":
    led, fx = build()
    n = sum(1 for _ in open(led, encoding="utf-8")) - 1
    print(f"Wrote {led} ({n} transactions) and {fx}\n")
    from . import solve as solvemod
    sub = solvemod.solve(str(led), str(fx), classifier_mode="keyword")
    cells = [c for s in sub["answers"].values() for c in s.values()]
    filled = [c for c in cells if c["status"] in ("COMPLIANT", "BREACH")]
    with_ev = [c for c in filled if c["evidence_txn_id"]]
    print(f"\n{'='*66}")
    print(f"cells computed        : {len(filled)}/36")
    print(f"  COMPLIANT / BREACH  : {sum(c['status']=='COMPLIANT' for c in filled)}"
          f" / {sum(c['status']=='BREACH' for c in filled)}")
    print(f"evidence ids emitted  : {len(with_ev)}")
    print(f"non-numeric actuals   : {sum(not isinstance(c['actual'], (int, float)) for c in filled)}")
    if len(filled) < 36:
        for sc, covs in sub["answers"].items():
            for cid, c in covs.items():
                if c["status"] not in ("COMPLIANT", "BREACH"):
                    print(f"  !! {sc} {cid} EMPTY")
