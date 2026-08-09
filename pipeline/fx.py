"""Per-borrower FX rates, read out of the documents.

The practice release shipped rates as a `--fx` CSV. The real archive ships **no FX file at
all**, and 25 of its 27 borrowers carry EUR rows. The rate is in the documents, because the
contracts say it is: «Суммы в иностранной валюте пересчитываются по курсу, раскрытому
аудитором». Leaving those rows at 1:1 understates every EUR amount by 8–16% and silently
biases whichever covenant they land in.

The corpus states the rate two ways, and BOTH have to be read:

  1. **Explicitly** — a treasury memo: «пересчитываются … по следующим курсам: 1 EUR = $1.08.»

  2. **Implicitly**, by worked example — «Расчёты с контрагентом «Alpen Infrastruktur Finanz
     GmbH»: счёт на сумму 92,415.50 EUR урегулирован платежом в долларах США в размере
     $105,353.67.» The rate is the quotient, 1.1400.

The implicit form carries a trap. Some of those sentences continue: «отражённым **за вычетом**
банковской комиссии в размере $1,162.84, **которая не входит в пересчитываемую сумму**» — the
payment is quoted NET of a bank fee that is explicitly not part of the converted amount, so the
fee must be added back before dividing. It is checkable rather than a matter of taste: adding
it back turns 1.1029 into exactly 1.1200, 1.0764 into 1.0850, and 1.0982 into 1.1000. Every
disclosed rate in this corpus is a round two-to-four decimal number once the fee is restored,
and none of them are without it.

Rates are keyed per BORROWER, not globally: the corpus discloses 1.08 for one and 1.14 for
another over the same period, so a single global rate is wrong for somebody by construction.
"""
from __future__ import annotations
import re
from statistics import median

from . import config, docmap, pdftext

_NUM = r"[0-9][0-9,]*(?:\.[0-9]+)?"

# «по следующим курсам: 1 EUR = $1.08»
EXPLICIT_RE = re.compile(rf"1\s*([A-Z]{{3}})\s*=\s*\$\s*({_NUM})")
# «счёт на сумму 92,415.50 EUR урегулирован платежом в долларах США в размере $105,353.67»
IMPLIED_RE = re.compile(
    rf"на\s+сумму\s*({_NUM})\s*([A-Z]{{3}})[^.]{{0,140}}?\$\s*({_NUM})", re.I)
# «за вычетом банковской комиссии в размере $1,162.84, которая не входит в пересчитываемую сумму»
FEE_RE = re.compile(rf"комисси\w*\s*в\s*размере\s*\$\s*({_NUM})", re.I)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def disclosures(dm: dict | None = None) -> list[dict]:
    """Every FX rate the corpus states, with provenance so each one can be re-checked."""
    dm = dm or docmap.build(save=False)
    out: list[dict] = []
    for name, d in dm["docs"].items():
        if d["is_spec"] or d["outdated"]:
            continue
        try:
            text = pdftext.extract_text(config.dataset_path(name))
        except Exception:
            continue
        for m in EXPLICIT_RE.finditer(text):
            out.append({"accs": list(d["accs"]), "currency": m.group(1).upper(),
                        "rate": _num(m.group(2)), "kind": "explicit", "doc": name, "fee": 0.0})
        for m in IMPLIED_RE.finditer(text):
            fee_m = FEE_RE.search(text[m.end():m.end() + 220])
            fee = _num(fee_m.group(1)) if fee_m else 0.0
            foreign, paid = _num(m.group(1)), _num(m.group(3))
            if foreign <= 0:
                continue
            out.append({"accs": list(d["accs"]), "currency": m.group(2).upper(),
                        "rate": round((paid + fee) / foreign, 6), "kind": "implied",
                        "doc": name, "fee": fee})
    return out


def rates_by_scenario(dm: dict | None = None, verbose: bool = True) -> dict:
    """{(currency, scenario): rate} plus a {currency: fallback} default.

    Only 7 of the 27 borrowers disclose a rate, so a fallback is unavoidable. It is the MEDIAN
    of the disclosed rates rather than 1.0: 1.0 is the one value the corpus rules out — it says
    conversion is required and every disclosed rate is 1.08–1.16 — while the median is the
    least-wrong constant given what the documents actually say. Every borrower that gets the
    fallback is named in the report, because that is an assumption, not a measurement."""
    dm = dm or docmap.build(save=False)
    found = disclosures(dm)
    rates: dict = {}
    per_ccy: dict[str, list[float]] = {}

    for f in found:
        per_ccy.setdefault(f["currency"], []).append(f["rate"])
        for acc in f["accs"]:
            sc = config.ACC_TO_SCENARIO.get(acc)
            if not sc:
                continue
            key = (f["currency"], sc)
            # An explicit statement outranks one inferred from a worked example.
            if key not in rates or f["kind"] == "explicit":
                rates[key] = f["rate"]

    for ccy, vals in per_ccy.items():
        rates.setdefault(ccy, round(median(vals), 6))

    if verbose:
        _report(found, rates, per_ccy)
    return rates


def _report(found: list[dict], rates: dict, per_ccy: dict) -> None:
    # `rates` mixes per-borrower keys ("EUR", "S2") with currency-wide fallbacks ("EUR"), so
    # only the tuple keys may be unpacked.
    named = {k[1] for k in rates if isinstance(k, tuple) and k[1] in config.SCENARIO_TO_ACC}
    print(f"FX: {len(found)} rate disclosure(s) read from documents; "
          f"{len(named)} borrower(s) covered directly.")
    for f in sorted(found, key=lambda x: str(x["accs"])):
        fee = f"  (+${f['fee']:,.2f} bank fee added back)" if f["fee"] else ""
        print(f"   {','.join(f['accs']) or '-':<12} 1 {f['currency']} = ${f['rate']:<8.4f}"
              f" [{f['kind']}, {f['doc']}]{fee}")
    for ccy, vals in per_ccy.items():
        if len(vals) > 1:
            print(f"   !! {ccy} rates differ across borrowers ({sorted(set(vals))}) — a single "
                  f"global rate would be wrong for someone. Applied per borrower.")
        print(f"   fallback for borrowers with no disclosure: 1 {ccy} = ${rates[ccy]:.4f} "
              f"(median of {len(vals)} disclosed)")


if __name__ == "__main__":
    from . import ledger as L
    txns = L.load(config.dataset_path("master_ledger_2025.csv"))
    allowed = set(config.submission_template()) or None
    config.set_scenario_map(L.discover_scenario_map(txns, allowed=allowed))
    L.refresh_known_scenarios()
    rates_by_scenario()
