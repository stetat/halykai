"""Event-day rehearsal: does the proven 36/36 still hold through a HOSTILE file?

`reconstruct.py` proves the covenant math is right, but it feeds the engine in-memory
Txn objects. On event day the transactions arrive as a file, in a dialect nobody has
shown us. This harness takes the exact same 36 cells and forces them through the full
disk chain first:

    Txn -> cp1251 CSV, ';'-separated, Cyrillic headers, RU decimals, NBSP thousands,
           every other row denominated in KZT
        -> ledger.load() -> load_fx() -> convert_fx() -> engine.evaluate() -> scorer

The KZT rows are written at 500x with an FX table of 0.002 USD/KZT, so a correct FX
pipeline reproduces the original USD figures exactly and the score must stay 1.0000.
Any drop here is a bug in ingestion, not in the covenant math.

    python -m pipeline.test_roundtrip
"""
from __future__ import annotations
import tempfile
from pathlib import Path

from . import config, covenants, engine, scorer, reconstruct
from . import ledger as L
from .engine import Categorizer

TMP = Path(tempfile.mkdtemp(prefix="halyk_rt_"))
KZT_RATE = 0.002          # USD per KZT
HEADER = ("Номер транзакции;Счёт;Дата операции;Сумма;Валюта;Контрагент;Назначение платежа")


def _ru_amount(v: float) -> str:
    """Render like a RU export: NBSP thousands, decimal comma."""
    s = f"{abs(v):,.2f}".replace(",", "\xa0").replace(".", ",")
    return f"-{s}" if v < 0 else s


def write_nasty(txns, path: Path) -> Path:
    """Write these transactions in the least convenient plausible dialect."""
    lines = [HEADER]
    for i, t in enumerate(txns):
        kzt = (i % 2 == 1)                     # alternate currencies
        amt = t.amount / KZT_RATE if kzt else t.amount
        lines.append(";".join([
            t.txn_id, t.account_id or "ACC-7801", t.date or "01.06.2025",
            _ru_amount(amt), "KZT" if kzt else "USD",
            t.counterparty or "cp", t.description or "",
        ]))
    path.write_bytes(("\n".join(lines) + "\n").encode("cp1251"))
    return path


FX = (TMP / "fx.csv")
FX.write_bytes("Валюта;Курс\nKZT;0,002\n".encode("cp1251"))


def run() -> None:
    specs = covenants.build(use_llm=False, save=False)
    key = scorer.load_key()
    rates = L.load_fx(FX)
    assert abs(rates["KZT"] - KZT_RATE) < 1e-9, f"FX table misparsed: {rates}"

    answers: dict[str, dict] = {}
    covered = 0
    ev_checked = ev_ok = 0
    dropped: list[str] = []

    for sc in config.SCENARIO_TO_ACC:
        answers[sc] = {}
        for cid in ("6.1", "6.2", "6.3"):
            spec = specs[sc]["covenants"].get(cid, {})
            kcell = key[sc][cid]
            built = reconstruct.build_cell(sc, cid, spec, kcell)
            if built is None:
                answers[sc][cid] = {"status": None, "actual": None, "evidence_txn_id": None}
                continue
            txns, _catf, reclasses, extras = built

            # --- the part under test: through the file and back ---
            f = write_nasty(txns, TMP / f"{sc}_{cid.replace('.', '')}.csv")
            reloaded = L.load(f)
            missing = L.convert_fx(reloaded, rates)
            if missing:
                dropped.append(f"{sc} {cid}: no FX for {missing}")
            if len(reloaded) != len(txns):
                dropped.append(f"{sc} {cid}: {len(txns)} rows in, {len(reloaded)} back")

            # same synthetic base classifier (category tag lives in `description`)
            catf = Categorizer(reconstruct._base, reclasses)
            res = engine.evaluate(spec, reloaded, catf, reclasses, extras)
            answers[sc][cid] = {"status": res.status, "actual": res.actual,
                                "evidence_txn_id": res.evidence_txn_id}
            covered += 1
            if kcell.get("evidence_txn_id"):
                ev_checked += 1
                ev_ok += int(res.evidence_txn_id == kcell["evidence_txn_id"])

    total = 0.0
    worst: list[tuple[float, str]] = []
    for sc in config.SCENARIO_TO_ACC:
        for cid in ("6.1", "6.2", "6.3"):
            if answers[sc][cid]["status"] is not None:
                s, _ = scorer.score_cell(answers[sc][cid], key[sc][cid])
                total += s
                worst.append((s, f"{sc} {cid}"))

    print(f"Round-tripped {covered} cells through cp1251/';'/Cyrillic/KZT + FX.")
    print(f"Evidence transactions preserved: {ev_ok}/{ev_checked}")
    for d in dropped:
        print("  !!", d)
    worst.sort()
    for s, name in worst[:5]:
        if s < 1.0:
            print(f"  !! {name} scored {s:.3f}")
    print(f"\nScore after hostile-dialect round trip: "
          f"{total:.3f}/{covered} = {total/covered:.4f}")

    ok = (not dropped) and total / covered > 0.9999 and ev_ok == ev_checked
    print("\n" + ("ROUND-TRIP CLEAN — ingestion preserves the 36/36."
                  if ok else "ROUND-TRIP REGRESSION — ingestion is losing information."))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
