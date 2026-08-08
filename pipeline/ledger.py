"""Ledger loader for event day.

The real `master_ledger_2025.csv` is a single table for ALL borrowers, with:
  - signed amounts (expenses negative, income positive),
  - multiple currencies, NO category column (categorisation is inferred, not given),
  - txn_id whose prefix encodes the scenario (TXN-<scenario>-####).

We do not get to see the file before it ships, so every dialect assumption here is a
guess that must degrade instead of crash. Concretely, the loader sniffs its encoding and
delimiter, resolves columns by fuzzy (RU/EN) header matching, and resolves each row's
scenario from TWO independent keys (txn_id prefix, then account_id) so that one of them
being in an unexpected format costs nothing.
"""
from __future__ import annotations
import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .config import ACC_TO_SCENARIO, SCENARIO_TO_ACC

TXN_RE = re.compile(r"TXN-([A-Za-z0-9]+)-\d+", re.I)
ACC_RE = re.compile(r"ACC-?\d+", re.I)
_KNOWN_SCENARIOS = {s.upper(): s for s in SCENARIO_TO_ACC}

# Encodings to try, in order. utf-8 is tried first because cp1251 text almost always
# fails a strict utf-8 decode, so a success here is real rather than mojibake.
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251", "cp1252", "koi8-r", "latin-1")
_DELIMITERS = (",", ";", "\t", "|")

# candidate header names -> normalised field. Compared against normalised headers
# (casefolded, punctuation/space -> "_"), so "Transaction ID" matches "transaction_id".
_ALIASES = {
    "txn_id": ["txn_id", "txn", "transaction_id", "transaction", "id", "txnid", "trans_id",
               "operation_id", "doc_id", "document_id", "ref", "reference",
               "номер_транзакции", "id_транзакции", "идентификатор", "транзакция",
               "номер_операции", "id_операции", "операция", "номер_документа", "номер"],
    "account_id": ["account_id", "account", "acc", "acc_id", "accountid", "account_no",
                   "account_number", "client_id", "borrower", "borrower_id",
                   "счет", "счёт", "номер_счета", "номер_счёта", "лицевой_счет",
                   "аккаунт", "клиент", "заемщик", "заёмщик", "контрагент_счет"],
    "date": ["date", "value_date", "posting_date", "txn_date", "timestamp", "op_date",
             "дата", "дата_операции", "дата_проводки", "дата_платежа", "дата_документа"],
    "amount": ["amount", "amt", "value", "sum", "signed_amount", "total", "oborot",
               "сумма", "сумма_операции", "сумма_платежа", "оборот", "значение", "итого"],
    "amount_usd": ["amount_usd", "usd", "usd_amount", "sum_usd", "amount_in_usd",
                   "сумма_usd", "сумма_в_usd", "сумма_в_долларах", "usd_экв"],
    "debit": ["debit", "dt", "debet", "дебет", "расход", "списание"],
    "credit": ["credit", "ct", "kt", "кредит", "приход", "поступление", "зачисление"],
    "currency": ["currency", "ccy", "cur", "curr", "валюта", "код_валюты"],
    "counterparty": ["counterparty", "party", "beneficiary", "payee", "vendor",
                     "contractor", "counterpart", "name", "supplier", "merchant",
                     "контрагент", "получатель", "плательщик", "бенефициар",
                     "поставщик", "наименование", "наименование_контрагента"],
    "description": ["description", "desc", "narrative", "memo", "details", "purpose",
                    "comment", "note", "particulars",
                    "описание", "назначение", "назначение_платежа", "комментарий",
                    "примечание", "детали", "содержание_операции"],
}
# Fields where a loose substring match is safe. "id"/"amount" are excluded from loose
# matching because they collide with too much ("paid", "id_valuta", ...).
_LOOSE_OK = ("account_id", "date", "currency", "counterparty", "description",
             "amount_usd", "debit", "credit")


@dataclass
class Txn:
    txn_id: str
    account_id: str
    date: str
    amount: float          # signed, in original currency
    currency: str
    counterparty: str
    description: str
    scenario: str          # resolved from txn_id prefix, else account_id
    amount_usd: float | None = None   # filled by FX conversion (or a native USD column)


# --- dialect sniffing -----------------------------------------------------------------
def _decode(raw: bytes) -> str:
    """Decode ledger bytes, trying the encodings this dataset plausibly ships in."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters="".join(_DELIMITERS)).delimiter
    except csv.Error:
        pass
    # Fall back to whichever candidate splits the header line into the most fields.
    header = sample.splitlines()[0] if sample.splitlines() else ""
    best, best_n = ",", 0
    for d in _DELIMITERS:
        n = header.count(d)
        if n > best_n:
            best, best_n = d, n
    return best


def _norm(h: str) -> str:
    """Normalise a header: strip accents/case/punctuation so 'Transaction ID' == 'transaction_id'."""
    h = unicodedata.normalize("NFKC", (h or "")).strip().casefold()
    h = re.sub(r"[^0-9a-zа-яё]+", "_", h)
    return h.strip("_")


def _resolve_headers(fieldnames: list[str]) -> dict[str, str]:
    norm = {}
    for fn in fieldnames:
        if fn:
            norm.setdefault(_norm(fn), fn)
    resolved: dict[str, str] = {}
    taken: set[str] = set()
    # pass 1: exact normalised match (most reliable)
    for field, names in _ALIASES.items():
        for cand in names:
            col = norm.get(_norm(cand))
            if col and col not in taken:
                resolved[field] = col
                taken.add(col)
                break
    # pass 2: substring match for the fields where that is unambiguous
    for field in _LOOSE_OK:
        if field in resolved:
            continue
        for cand in _ALIASES[field]:
            c = _norm(cand)
            hit = next((orig for n, orig in norm.items()
                        if orig not in taken and c and c in n), None)
            if hit:
                resolved[field] = hit
                taken.add(hit)
                break
    return resolved


# --- value parsing --------------------------------------------------------------------
_NUM_JUNK = re.compile(r"[^\d,.\-+()]")


def _to_float(s: str) -> float:
    """Parse a money cell. Handles RU/EN thousands separators, NBSP, unicode minus,
    parenthesised negatives, and trailing currency codes."""
    if s is None:
        return 0.0
    s = unicodedata.normalize("NFKC", str(s)).strip()
    if not s:
        return 0.0
    s = s.replace("−", "-").replace("–", "-")     # unicode minus / en-dash
    s = s.replace("\xa0", "").replace(" ", "").replace(" ", "")
    s = _NUM_JUNK.sub("", s)                                 # drop $, KZT, letters
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if s.count(",") and s.count("."):                        # 1,234.56 or 1.234,56
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") \
            else s.replace(".", "").replace(",", ".")
    elif s.count(",") == 1:                                  # 1234,56 vs 1,234
        head, tail = s.split(",")
        # Exactly 3 trailing digits is the only ambiguous case, and even then a "0"
        # integer part means a decimal ("0,002" is a rate, never 2). Rates carry 3+
        # decimals, so anything other than a clean 3-digit group is a decimal comma.
        thousands = len(tail) == 3 and tail.isdigit() and head.lstrip("-+") not in ("", "0") \
            and not head.lstrip("-+").startswith("0")
        s = head + ("" if thousands else ".") + tail
    elif s.count(",") > 1:                                   # 1,234,567 thousands
        s = s.replace(",", "")
    elif s.count(".") > 1:                                   # 1.234.567 thousands
        s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return -v if neg else v


def _scenario_of(txn_id: str, account_id: str) -> str:
    """Resolve the scenario from either key. txn_id prefix wins; account_id is the
    independent fallback so an unexpected txn_id format cannot silently zero the run."""
    m = TXN_RE.search(txn_id or "")
    if m:
        sc = _KNOWN_SCENARIOS.get(m.group(1).upper())
        if sc:
            return sc
    for blob in (account_id or "", txn_id or ""):
        a = ACC_RE.search(blob)
        if a:
            acc = a.group(0).upper().replace("ACC", "ACC-").replace("--", "-")
            sc = ACC_TO_SCENARIO.get(acc)
            if sc:
                return sc
    # last resort: a bare scenario token anywhere in the id ("P10-0001", "B1_0007")
    for tok in re.split(r"[^A-Za-z0-9]+", (txn_id or "").upper()):
        if tok in _KNOWN_SCENARIOS:
            return _KNOWN_SCENARIOS[tok]
    return ""


def load(path: str | Path) -> list[Txn]:
    path = Path(path)
    text = _decode(path.read_bytes())
    delim = _sniff_delimiter(text[:8192])
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    cols = _resolve_headers(reader.fieldnames or [])
    has_amount = "amount" in cols or "amount_usd" in cols or \
                 ("debit" in cols or "credit" in cols)
    if not has_amount:
        raise ValueError(
            f"Could not resolve an amount column from {reader.fieldnames} "
            f"(delimiter={delim!r}). Add the real header to ledger._ALIASES.")

    rows: list[Txn] = []
    for r in reader:
        def cell(field: str) -> str:
            return (r.get(cols.get(field, "")) or "").strip()

        if "amount" in cols:
            amount = _to_float(r.get(cols["amount"]))
        else:                                    # debit/credit pair -> signed amount
            amount = _to_float(r.get(cols.get("credit", ""))) - \
                     _to_float(r.get(cols.get("debit", "")))
        txn_id, account_id = cell("txn_id"), cell("account_id")
        usd = _to_float(r.get(cols["amount_usd"])) if "amount_usd" in cols else None
        rows.append(Txn(
            txn_id=txn_id,
            account_id=account_id,
            date=cell("date"),
            amount=amount,
            currency=cell("currency").upper() or "USD",
            counterparty=cell("counterparty"),
            description=cell("description"),
            scenario=_scenario_of(txn_id, account_id),
            amount_usd=usd if usd else None,
        ))
    return rows


# --- FX -------------------------------------------------------------------------------
_FX_CCY = ["currency", "ccy", "cur", "code", "валюта", "код_валюты"]
_FX_RATE = ["rate", "usd_rate", "rate_to_usd", "fx_rate", "multiplier", "value", "price",
            "курс", "курс_к_usd", "курс_доллара", "значение"]
_FX_DATE = ["date", "дата", "as_of", "on_date", "дата_курса"]
# Rate tables sometimes quote "units of local per 1 USD" instead of "USD per unit".
# "per/за <n> usd" always means inverted; bare "to_usd" is the DIRECT multiplier, so only
# invert on "to" when an explicit unit count is present ("rate_to_1_usd").
_FX_INVERTED_RE = re.compile(r"(?:per|за)_(?:\d+_)?(?:usd|доллар\w*)|to_\d+_usd")


def load_fx(path: str | Path) -> dict:
    """Load an FX table into {currency: rate} / {(currency, date): rate}, where rate is a
    USD multiplier (amount * rate = USD). Auto-inverts 'local per USD' quoting."""
    path = Path(path)
    text = _decode(path.read_bytes())
    delim = _sniff_delimiter(text[:8192])
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    names = reader.fieldnames or []
    norm = {_norm(n): n for n in names if n}

    def pick(cands):
        for c in cands:
            if _norm(c) in norm:
                return norm[_norm(c)]
        for c in cands:                                   # loose
            hit = next((o for n, o in norm.items() if _norm(c) in n), None)
            if hit:
                return hit
        return None

    ccy_col, rate_col, date_col = pick(_FX_CCY), pick(_FX_RATE), pick(_FX_DATE)
    if not ccy_col or not rate_col:
        raise ValueError(f"Could not resolve currency/rate columns from {names}")
    inverted = bool(_FX_INVERTED_RE.search(_norm(rate_col)))

    rates: dict = {}
    for r in reader:
        ccy = (r.get(ccy_col) or "").strip().upper()
        rate = _to_float(r.get(rate_col))
        if not ccy or not rate:
            continue
        if inverted:
            rate = 1.0 / rate
        d = (r.get(date_col) or "").strip() if date_col else ""
        if d:
            rates[(ccy, d)] = rate
        rates.setdefault(ccy, rate)
    return rates


def convert_fx(txns: list[Txn], rates: dict | None = None) -> list[str]:
    """Fill amount_usd. `rates` maps currency (or (currency,date)) -> USD multiplier.
    Returns the currencies that had no rate (treated 1:1) so the caller can warn."""
    rates = rates or {}
    missing: set[str] = set()
    for t in txns:
        if t.amount_usd is not None:            # ledger already carried a USD column
            continue
        ccy = (t.currency or "USD").upper()
        if ccy == "USD":
            t.amount_usd = t.amount
            continue
        rate = rates.get((ccy, t.date)) or rates.get(ccy)
        if rate:
            t.amount_usd = t.amount * rate
        else:
            t.amount_usd = t.amount
            missing.add(ccy)
    return sorted(missing)


def by_scenario(txns: list[Txn]) -> dict[str, list[Txn]]:
    out: dict[str, list[Txn]] = {}
    for t in txns:
        out.setdefault(t.scenario, []).append(t)
    return out
