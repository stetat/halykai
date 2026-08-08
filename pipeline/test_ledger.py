"""Dialect torture-test for the event-day ledger loader.

We do not get to see `master_ledger_2025.csv` before it ships. So instead of testing one
assumed format, we write the SAME logical ledger in every dialect the organisers could
plausibly hand us and assert all of them parse to identical transactions. Anything that
fails here is a run that would have scored 0 on event day.

    python -m pipeline.test_ledger
"""
from __future__ import annotations
import tempfile
from pathlib import Path

from . import ledger as L

FAILURES: list[str] = []
TMP = Path(tempfile.mkdtemp(prefix="halyk_ledger_"))


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok ' if cond else 'FAIL'} {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def write(name: str, text: str, encoding: str = "utf-8") -> Path:
    p = TMP / name
    p.write_bytes(text.encode(encoding))
    return p


# The canonical expectation every dialect must reproduce.
EXPECT = [
    # (txn_id, scenario, amount, currency, has_text)
    ("TXN-P1-0001", "P1", -1234.56, "USD"),
    ("TXN-P1-0002", "P1", 250000.00, "KZT"),
    ("TXN-B4-0007", "B4", -9876.54, "USD"),
]


def assert_canonical(label: str, rows: list[L.Txn], *, check_text: bool = True) -> None:
    check(f"{label}: row count", len(rows) == 3, f"got {len(rows)}")
    if len(rows) != 3:
        return
    for got, (tid, sc, amt, ccy) in zip(rows, EXPECT):
        check(f"{label}: {tid} scenario", got.scenario == sc, f"got {got.scenario!r}")
        check(f"{label}: {tid} amount", abs(got.amount - amt) < 0.005,
              f"got {got.amount!r} want {amt}")
        check(f"{label}: {tid} currency", got.currency == ccy, f"got {got.currency!r}")
    if check_text:
        check(f"{label}: text columns resolved",
              all(r.counterparty and r.description for r in rows),
              f"got {[(r.counterparty, r.description) for r in rows]}")


# --- dialect 1: the assumed baseline ---------------------------------------------------
BASE = (
    "txn_id,account_id,date,amount,currency,counterparty,description\n"
    "TXN-P1-0001,ACC-7801,2025-01-05,-1234.56,USD,ТОО Альфа,операционные расходы\n"
    "TXN-P1-0002,ACC-7801,2025-01-06,250000.00,KZT,ТОО Бета,выручка от реализации\n"
    "TXN-B4-0007,ACC-7204,2025-02-11,-9876.54,USD,ТОО Гамма,капитальные затраты\n"
)
assert_canonical("baseline utf-8 comma", L.load(write("base.csv", BASE)))

# --- dialect 2: cp1251 + semicolons (RU Excel export) ----------------------------------
assert_canonical("cp1251 + semicolon",
                 L.load(write("cp1251.csv", BASE.replace(",", ";").replace(
                     "-1234.56", "-1234.56"), encoding="cp1251")))

# --- dialect 3: Cyrillic headers, RU decimal comma, NBSP thousands ---------------------
RU = (
    "Номер транзакции;Счёт;Дата операции;Сумма;Валюта;Контрагент;Назначение платежа\n"
    "TXN-P1-0001;ACC-7801;05.01.2025;-1 234,56;USD;ТОО Альфа;операционные расходы\n"
    "TXN-P1-0002;ACC-7801;06.01.2025;250 000,00;KZT;ТОО Бета;выручка от реализации\n"
    "TXN-B4-0007;ACC-7204;11.02.2025;-9 876,54;USD;ТОО Гамма;капитальные затраты\n"
).replace(" ", "\xa0")     # NBSP thousands separators, as RU exports really emit
assert_canonical("cyrillic headers + RU decimals", L.load(write("ru.csv", RU, "cp1251")))

# --- dialect 4: tab-separated, "Transaction ID"-style spaced English headers ------------
TAB = (
    "Transaction ID\tAccount Number\tValue Date\tAmount\tCCY\tBeneficiary\tPurpose\n"
    "TXN-P1-0001\tACC-7801\t2025-01-05\t(1,234.56)\tUSD\tAlfa LLP\topex\n"
    "TXN-P1-0002\tACC-7801\t2025-01-06\t250,000.00\tKZT\tBeta LLP\trevenue\n"
    "TXN-B4-0007\tACC-7204\t2025-02-11\t(9,876.54)\tUSD\tGamma LLP\tcapex\n"
)
assert_canonical("tab + spaced headers + paren negatives", L.load(write("tab.csv", TAB)))

# --- dialect 5: debit/credit pair instead of a signed amount ---------------------------
DC = (
    "txn_id,account_id,date,debit,credit,currency,counterparty,description\n"
    "TXN-P1-0001,ACC-7801,2025-01-05,1234.56,,USD,Alfa,opex\n"
    "TXN-P1-0002,ACC-7801,2025-01-06,,250000.00,KZT,Beta,revenue\n"
    "TXN-B4-0007,ACC-7204,2025-02-11,9876.54,,USD,Gamma,capex\n"
)
assert_canonical("debit/credit pair", L.load(write("dc.csv", DC)))

# --- dialect 6: unknown txn_id format -> scenario must fall back to account_id ---------
NOPREFIX = (
    "txn_id,account_id,date,amount,currency,counterparty,description\n"
    "OP/2025/0001,ACC-7801,2025-01-05,-1234.56,USD,Alfa,opex\n"
    "OP/2025/0002,ACC-7801,2025-01-06,250000.00,KZT,Beta,revenue\n"
    "OP/2025/0007,ACC-7204,2025-02-11,-9876.54,USD,Gamma,capex\n"
)
rows = L.load(write("noprefix.csv", NOPREFIX))
check("txn_id unparseable -> scenario from account_id",
      [r.scenario for r in rows] == ["P1", "P1", "B4"],
      f"got {[r.scenario for r in rows]}")

# --- dialect 7: BOM + CRLF + quoted fields containing the delimiter --------------------
QUOTED = (
    '﻿txn_id,account_id,date,amount,currency,counterparty,description\r\n'
    'TXN-P1-0001,ACC-7801,2025-01-05,-1234.56,USD,"Alfa, LLP","opex, monthly"\r\n'
    'TXN-P1-0002,ACC-7801,2025-01-06,250000.00,KZT,"Beta, LLP","revenue, sales"\r\n'
    'TXN-B4-0007,ACC-7204,2025-02-11,-9876.54,USD,"Gamma, LLP","capex, plant"\r\n'
)
assert_canonical("BOM + CRLF + quoted delimiters", L.load(write("quoted.csv", QUOTED)))

# --- dialect 8: native USD column present (no FX needed) -------------------------------
USDCOL = (
    "txn_id,account_id,date,amount,amount_usd,currency,counterparty,description\n"
    "TXN-P1-0001,ACC-7801,2025-01-05,-1234.56,-1234.56,USD,Alfa,opex\n"
    "TXN-P1-0002,ACC-7801,2025-01-06,250000.00,500.00,KZT,Beta,revenue\n"
    "TXN-B4-0007,ACC-7204,2025-02-11,-9876.54,-9876.54,USD,Gamma,capex\n"
)
rows = L.load(write("usdcol.csv", USDCOL))
L.convert_fx(rows, None)
check("native amount_usd column is preserved (not overwritten 1:1)",
      abs(rows[1].amount_usd - 500.00) < 0.005, f"got {rows[1].amount_usd}")

# --- FX ---------------------------------------------------------------------------------
FX_DIRECT = "currency,rate\nKZT,0.002\nEUR,1.09\n"
rates = L.load_fx(write("fx1.csv", FX_DIRECT))
rows = L.load(write("base2.csv", BASE))
missing = L.convert_fx(rows, rates)
check("FX: USD multiplier applied", abs(rows[1].amount_usd - 500.00) < 0.005,
      f"got {rows[1].amount_usd}")
check("FX: no missing currencies reported", missing == [], f"got {missing}")

FX_INVERTED = "Валюта,Курс за 1 USD\nKZT,500\nEUR,0.917\n"
rates = L.load_fx(write("fx2.csv", FX_INVERTED, "cp1251"))
rows = L.load(write("base3.csv", BASE))
L.convert_fx(rows, rates)
check("FX: 'units per USD' quoting is auto-inverted",
      abs(rows[1].amount_usd - 500.00) < 0.005, f"got {rows[1].amount_usd}")

# "rate_to_usd" is the DIRECT multiplier and must NOT be inverted (contrast with "за 1 USD").
rates = L.load_fx(write("fx3.csv", "currency,rate_to_usd\nKZT,0.002\n"))
rows = L.load(write("base5.csv", BASE))
L.convert_fx(rows, rates)
check("FX: bare 'rate_to_usd' is treated as direct, not inverted",
      abs(rows[1].amount_usd - 500.00) < 0.005, f"got {rows[1].amount_usd}")

rows = L.load(write("base4.csv", BASE))
missing = L.convert_fx(rows, {})
check("FX: missing rate is reported, not silently 1:1", missing == ["KZT"], f"got {missing}")

# --- number parsing edge cases ----------------------------------------------------------
for raw, want in [
    ("1 234,56", 1234.56), ("1\xa0234,56", 1234.56), ("1,234.56", 1234.56),
    ("1.234,56", 1234.56), ("1.234.567", 1234567.0), ("(1,234.56)", -1234.56),
    ("−1234.56", -1234.56), ("1234.56 KZT", 1234.56), ("$1,234.56", 1234.56),
    ("", 0.0), ("n/a", 0.0), ("-", 0.0),
    # FX rates: a comma with 3+ decimals is a DECIMAL comma, not a thousands group.
    # "0,002" must never become 2 — that silently scales every converted amount by 1000x.
    ("0,002", 0.002), ("0,9175", 0.9175), ("1,0925", 1.0925), ("500,00", 500.0),
    ("1,234", 1234.0), ("1,234,567", 1234567.0), ("-0,002", -0.002),
]:
    check(f"_to_float({raw!r})", abs(L._to_float(raw) - want) < 0.005,
          f"got {L._to_float(raw)} want {want}")

print()
if FAILURES:
    print(f"{len(FAILURES)} LEDGER TEST(S) FAILED:")
    for f in FAILURES:
        print("   -", f)
    raise SystemExit(1)
print("ALL LEDGER DIALECT TESTS PASSED")
