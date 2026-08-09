"""One command that measures everything measurable on the practice release.

    python -m pipeline.cli scorecard

Free, offline, no quota. It runs every harness in a fresh process and prints ONE table.

The reason this file exists is not convenience. It is that **most numbers in this repo are not
accuracy**, and they all look like accuracy when you read them one terminal at a time.
`reconstruct` prints 36.000/36 = 1.0000; that is a synthesised ledger built to satisfy whatever
formula the engine already uses, so an engine bounding the WRONG QUANTITY scores the same 36/36
— thirteen cells once did, while every harness read green. `validate` prints 34/36; it
bracket-checks thresholds and never evaluates a metric. Quoting either as accuracy overstates
what is known by a wide margin.

So the table is split, and the split is the point:

  HONEST      out-of-sample. The only numbers to put in a slide.
  IN-SAMPLE   self-consistency and wiring proofs. Real value, not accuracy, labelled as such.
  GATES       pass/fail correctness harnesses.

Each row names the harness that produced it, so any number can be re-derived on its own.
"""
from __future__ import annotations
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config

PY = sys.executable


class Row:
    def __init__(self, label: str, value: str, harness: str, note: str = ""):
        self.label, self.value, self.harness, self.note = label, value, harness, note


def _run(module: str, args: list[str] | None = None, timeout: int = 600) -> tuple[str, int]:
    """Run a harness in a FRESH process.

    Fresh matters: several of these mutate module-level state (the scenario map, the retrieval
    index, the circuit breaker) and `test_docs` does its work at import time. Importing them
    into one process would let an earlier harness change a later one's answer, which is exactly
    the kind of silent coupling this table is supposed to rule out."""
    cmd = [PY, "-m", f"pipeline.{module}"] + (args or [])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=str(config.ROOT))
        return (p.stdout or "") + (p.stderr or ""), p.returncode
    except subprocess.TimeoutExpired:
        return f"TIMEOUT after {timeout}s", 1


def _grab(text: str, pattern: str, group: int = 0) -> str | None:
    m = re.search(pattern, text)
    return m.group(group) if m else None


def collect(verbose: bool = False) -> tuple[list[Row], list[Row], list[tuple[str, bool, str]]]:
    honest: list[Row] = []
    insample: list[Row] = []
    gates: list[tuple[str, bool, str]] = []

    def say(msg: str) -> None:
        print(f"  … {msg}", flush=True)

    # --- HONEST -------------------------------------------------------------------------
    say("test_holdout — classifier on held-out narrations")
    out, _ = _run("test_holdout")
    v = _grab(out, r"POOLED FIRST-CONTACT ACCURACY\s*:\s*(\S+\s*=\s*\S+)", 1)
    honest.append(Row("classifier, first contact on held-out narrations",
                      v or "UNPARSED", "test_holdout",
                      "the one component whose event-day input is genuinely unseen"))
    v = _grab(out, r"metric rules corroborated\s*:\s*(\S+\s*=\s*\S+)", 1)
    honest.append(Row("metric rules corroborated by >=2 borrowers", v or "UNPARSED",
                      "test_holdout",
                      "a rule firing for one borrower is indistinguishable from a patch"))

    say("test_e2e — non-circular end-to-end vs the answer key")
    out, _ = _run("test_e2e")
    exercised = disagree = 0
    for ln in out.splitlines():
        m = re.match(r"\s*([BP]\d+)\s+(6\.\d)\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+"
                     r"(\S+) / (\S+)\s*$", ln)
        if not m:
            continue
        if m.group(4) == "—":
            continue
        exercised += 1
        if m.group(3) != m.group(4):
            disagree += 1
    total = _grab(out, r"TOTAL\s+([\d.]+\s*/\s*\d+)", 1)
    honest.append(Row("non-circular e2e: status agreement",
                      f"{exercised - disagree}/{exercised} cells" if exercised else "UNPARSED",
                      "test_e2e",
                      f"{disagree} disagreement(s); {36 - exercised} cells not exercisable"))
    honest.append(Row("non-circular e2e: scored total (unexercised count as 0)",
                      total or "UNPARSED", "test_e2e",
                      "understates — the 5 unreachable cells score 0 by construction"))

    say("docs-only floor — solve with no ledger at all")
    _run("cli", ["solve"])
    out, _ = _run("cli", ["score", "submission.json"])
    v = _grab(out, r"TOTAL\s+([\d.]+\s*/\s*\d+\s*=\s*[\d.]+)", 1)
    honest.append(Row("docs-only floor (no ledger supplied)", v or "UNPARSED",
                      "cli solve && cli score",
                      "what the document half alone is worth"))

    # --- IN-SAMPLE ----------------------------------------------------------------------
    say("reconstruct — synthesised ledger (in-sample by construction)")
    out, _ = _run("reconstruct")
    v = _grab(out, r"Score across all 36 cells[^:]*:\s*([\d.]+\s*/\s*36\s*=\s*[\d.]+)", 1)
    insample.append(Row("reconstruct: ledger->status->actual->evidence->score",
                        v or "UNPARSED", "reconstruct",
                        "NOT ACCURACY: builds inputs satisfying whatever formula the engine "
                        "uses, so a wrong metric still scores 36/36"))

    say("cli validate — bracket-check vs the key")
    out, _ = _run("cli", ["validate"])
    v = _grab(out, r"(bracket OK=\d+\s+MISMATCH=\d+\s+n/a[^=]*=\d+\s*/\s*36)", 1)
    insample.append(Row("validate: thresholds/operators bracket-check",
                        v or "UNPARSED", "cli validate",
                        "NOT ACCURACY: never evaluates a metric, only brackets a threshold"))

    say("test_roundtrip — hostile-dialect ingestion")
    out, code = _run("test_roundtrip")
    v = _grab(out, r"Score after hostile-dialect round trip:\s*([\d.]+\s*/\s*36\s*=\s*[\d.]+)", 1)
    ev = _grab(out, r"Evidence transactions preserved:\s*(\d+/\d+)", 1)
    insample.append(Row("hostile-dialect round trip (cp1251/';'/KZT+FX)",
                        f"{v or '?'}, evidence {ev or '?'}", "test_roundtrip",
                        "proves ingestion loses nothing; the 36/36 is the same in-sample set"))
    gates.append(("test_roundtrip", code == 0, "ingestion preserves every cell"))

    # --- GATES --------------------------------------------------------------------------
    for mod, what in (("test_engine", "metric definitions vs real clause wording"),
                      ("test_classifier", "vocabulary, related-party matching, sign guard"),
                      ("test_ledger", "8 CSV dialects, FX, number parsing"),
                      ("test_docs", "real PDFs vs the key, incl. nested archive"),
                      ("test_retrieval", "index, scoping, spec-leakage guard"),
                      ("test_eventday", "the shipping gate: envelope, blank cells, diff")):
        say(f"{mod}")
        out, code = _run(mod)
        gates.append((mod, code == 0, what))
        if verbose and code != 0:
            print(out[-2000:])
    return honest, insample, gates


def render(honest, insample, gates) -> int:
    W = 78
    print("\n" + "=" * W)
    print("ACCURACY SCORECARD — practice release")
    print("=" * W)

    print("\nHONEST (out-of-sample) — the only numbers to quote")
    print("-" * W)
    for r in honest:
        print(f"  {r.label}")
        print(f"      {r.value:<34} [{r.harness}]")
        if r.note:
            print(f"      {r.note}")
    print("\nIN-SAMPLE — self-consistency and wiring proofs. NOT accuracy.")
    print("-" * W)
    for r in insample:
        print(f"  {r.label}")
        print(f"      {r.value:<34} [{r.harness}]")
        if r.note:
            print(f"      {r.note}")

    print("\nCORRECTNESS GATES")
    print("-" * W)
    for name, ok, what in gates:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<18} {what}")

    failed = [n for n, ok, _ in gates if not ok]
    unparsed = [r.label for r in honest + insample if r.value == "UNPARSED"]
    print("\n" + "=" * W)
    if failed:
        print(f"GATES FAILED: {', '.join(failed)} — the numbers above describe a broken build.")
        print("=" * W)
        return 1
    if unparsed:
        print(f"All gates pass, but {len(unparsed)} number(s) could not be parsed:")
        for u in unparsed:
            print(f"   - {u}")
        print("A harness changed its output format; fix the parse before quoting the table.")
        print("=" * W)
        return 1
    print("All gates pass.")
    print("\nIf you quote one number, quote the classifier's first-contact accuracy: it is the")
    print("only measurement of the component that will actually see unseen data on event day.")
    print("The `actual` VALUES cannot be measured at all here — the real ledger is withheld.")
    print("=" * W)
    return 0


def run(verbose: bool = False) -> int:
    t0 = time.time()
    print("Running every harness in a fresh process. Free, offline, no quota.\n")
    honest, insample, gates = collect(verbose=verbose)
    code = render(honest, insample, gates)
    print(f"({time.time() - t0:.0f}s)")
    return code


if __name__ == "__main__":
    raise SystemExit(run(verbose="-v" in sys.argv))
