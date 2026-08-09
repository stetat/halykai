"""One command for event day: preflight, baseline, hybrid, diff, validate, ship.

    python -m pipeline.cli eventday --ledger master_ledger_2025.csv --fx FX.csv

The runbook in HANDOFF.md §5 is four phases and about a dozen checks, and every one of them
exists because something failed silently once. A checklist you have to remember at 9am on
event day is a checklist you will half-execute, so this runs the whole thing and ends on one
line: GO or NO-GO.

What it does, in the order that catches failures earliest:

  1. PREFLIGHT   — did the documents load at all? This is first because it is the only failure
                   that costs every cell while printing no error: if the archive extracted into
                   `documents/`, discovery used to find one file, route zero borrowers and write
                   36 empty cells. Also checks contracts, related parties, untranscribed images.
  2. BASELINE    — the keyword solve. No quota, never blank, and the floor everything else has
                   to beat. Written to submission_keyword.json and KEPT.
  3. HYBRID      — the same solve with the model on undecided rows only. Skipped cleanly if
                   there is no key, no quota or no network.
  4. DIFF        — every cell where the two disagree. On the rehearsal ledger the model improved
                   four cells and destroyed two, so this is the step that turns "the LLM ran"
                   into a decision you can defend.
  5. VALIDATE    — the shipped file against the rubric: every cell filled, status spelled
                   exactly, actual numeric and positive, envelope complete.

It ships the KEYWORD baseline by default. Hybrid is an improvement you verify, not a default
you trust — pass `--ship hybrid` once you have read the diff.
"""
from __future__ import annotations
import json
import shutil
from pathlib import Path

from . import config, docmap, reclass, pdfimages, solve as solvemod

BASELINE = "submission_keyword.json"
HYBRID = "submission_hybrid.json"
FINAL = "submission.json"

_OK, _WARN, _FAIL = "ok  ", "WARN", "FAIL"


class Report:
    """Collects checks so the run can end on a single verdict rather than a wall of text."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def add(self, level: str, msg: str) -> None:
        self.rows.append((level, msg))
        print(f"  {level} {msg}")

    def ok(self, msg: str) -> None:
        self.add(_OK, msg)

    def warn(self, msg: str) -> None:
        self.add(_WARN, msg)

    def fail(self, msg: str) -> None:
        self.add(_FAIL, msg)

    @property
    def failures(self) -> list[str]:
        return [m for lv, m in self.rows if lv == _FAIL]

    @property
    def warnings(self) -> list[str]:
        return [m for lv, m in self.rows if lv == _WARN]


# --- 1. preflight ----------------------------------------------------------------------
def adopt_ledger_map(rep: Report, ledger: str | None) -> None:
    """Let the LEDGER name the borrowers before any document is classified.

    Order matters and it is not obvious. `docmap` routes a document by the account ids it
    mentions, and the pattern it matches with is built from the current scenario map — so on a
    dataset whose accounts are not the built-in ones, classifying first means routing against
    the wrong ids. The real dataset makes this concrete: scenario KC sits on `TELE-4471`, which
    no ACC-#### pattern can find, so its documents would be filed under no borrower at all."""
    if not ledger:
        return
    try:
        from . import ledger as ledgermod
        txns = ledgermod.load(ledger)
        allowed = set(config.submission_template()) or None
        found = ledgermod.discover_scenario_map(txns, allowed=allowed)
    except Exception as e:
        rep.warn(f"could not pre-read the ledger for the scenario map ({str(e)[:70]})")
        return
    if not found:
        rep.fail("no scenario<->account pair could be read from the ledger — check "
                 "ledger.TXN_RE against the real txn_id format")
        return
    config.set_scenario_map(found)
    from . import ledger as ledgermod2
    ledgermod2.refresh_known_scenarios()
    wanted = set(config.submission_template())
    missing = sorted(wanted - set(found))
    if missing:
        rep.fail(f"{len(missing)} template scenario(s) have NO rows in the ledger: {missing} "
                 f"— every one of their cells will be a guess")
    else:
        rep.ok(f"ledger names {len(found)} borrowers, matching the template exactly")


def preflight(rep: Report) -> dict:
    print("\n" + "=" * 78)
    print("1. PREFLIGHT — did the documents actually load?")
    print("=" * 78)

    files = config.dataset_files()
    nested = sorted({p.parent.name for p in files if p.parent != config.DATASET})
    if not files:
        rep.fail(f"{config.DATASET} contains no files — nothing can be computed")
        return {}
    if len(files) < 20:
        rep.fail(f"only {len(files)} dataset file(s) found — the archive is probably nested "
                 f"somewhere discovery did not look, or was not extracted")
    else:
        rep.ok(f"{len(files)} dataset files found"
               + (f" (nested under {nested})" if nested else " (flat)"))

    dm = docmap.build(save=True)
    # Report on the BORROWERS, not on every account id a document happens to mention. The real
    # corpus names ~550 counterparty accounts (ACC-9xxx) in passing; counting those as
    # "borrowers routed" buries the number that matters under two screens of noise.
    borrowers = [a for a in config.SCENARIO_TO_ACC.values() if a]
    n_docs = len(dm["docs"])
    routed = [a for a in borrowers if a in dm["by_acc"]]
    if not routed:
        rep.fail(f"{n_docs} documents classified but ZERO borrowers routed — check the archive")
        return dm
    if len(routed) < len(borrowers):
        rep.fail(f"{len(borrowers) - len(routed)} borrower(s) have NO documents at all: "
                 f"{sorted(set(borrowers) - set(routed))}")
    else:
        rep.ok(f"{n_docs} documents classified; all {len(borrowers)} borrowers have documents")

    missing = dm["accounts_without_contract"]
    if missing:
        rep.fail(f"{len(missing)} borrower(s) with NO live contract: {missing} "
                 f"— 3 cells each will be empty")
    else:
        rep.ok(f"live contract selected for all {len(borrowers)} borrowers")

    quarantined = sum(1 for d in dm["docs"].values() if d["outdated"] and d["has_covenants"])
    rep.ok(f"{quarantined} outdated contract(s) quarantined (the version trap)")

    no_rp = [a for a in borrowers if not reclass.related_parties(a, dm)]
    if no_rp:
        rep.warn(f"{len(no_rp)}/{len(borrowers)} borrower(s) with no related-party list: "
                 f"{sorted(no_rp)} — any related-party cell of theirs reports $0")
    else:
        rep.ok(f"related parties resolve for all {len(borrowers)} borrowers")

    try:
        untranscribed = pdfimages.untranscribed_image_docs()
        if untranscribed:
            rep.warn(f"{len(untranscribed)} image document(s) nobody has transcribed: "
                     f"{[n for n, _ in untranscribed]} — run `cli ocr`")
        else:
            rep.ok("every image-bearing document is transcribed")
    except Exception as e:                       # never let a preflight probe kill the run
        rep.warn(f"image check skipped ({str(e)[:60]})")
    return dm


# --- 2/3. solves -----------------------------------------------------------------------
def run_solve(rep: Report, label: str, out: str, ledger: str | None, fx: str | None,
              mode: str) -> dict | None:
    print("\n" + "=" * 78)
    print(f"{'2' if mode == 'keyword' else '3'}. {label.upper()} SOLVE (--classifier {mode})")
    print("=" * 78)
    try:
        sub = solvemod.solve(ledger, fx, classifier_mode=mode, write=False)
    except Exception as e:
        (rep.fail if mode == "keyword" else rep.warn)(
            f"{label} solve failed: {str(e)[:120]}")
        return None
    Path(out).write_text(json.dumps(sub, ensure_ascii=False, indent=2), encoding="utf-8")
    cells = [c for v in sub["answers"].values() for c in v.values()]
    filled = sum(1 for c in cells if c.get("status") in ("COMPLIANT", "BREACH"))
    if filled < len(cells):
        rep.fail(f"{label}: {len(cells) - filled} of {len(cells)} cells EMPTY — they score 0")
    else:
        rep.ok(f"{label}: {filled}/{len(cells)} cells computed -> {out}")
    return sub


def report_llm_health(rep: Report) -> None:
    """Was the hybrid run a real model run, or the keyword table wearing its badge?"""
    from . import classifier
    st = getattr(classifier.classify_hybrid, "last_stats", {}) or {}
    if st.get("errors"):
        rep.warn(f"hybrid hit API errors ({st['errors'][0][:70]}) — cells fell back to "
                 f"keywords; this is a DEGRADED run, not a model score")
    if st.get("sign_rejected"):
        rep.warn(f"{st['sign_rejected']} model answer(s) contradicted the ledger's sign and "
                 f"were rejected — read a few of those rows")


# --- 4. diff ---------------------------------------------------------------------------
def diff(rep: Report, a: dict | None, b: dict | None, skipped: bool = False) -> int:
    print("\n" + "=" * 78)
    print("4. DIFF — where the model overruled the keyword table")
    print("=" * 78)
    if not a or not b:
        if skipped:
            print("  (nothing to compare — the model pass was not run)")
        else:
            rep.warn("no diff: only one solve produced a result")
        return 0
    A, B = a["answers"], b["answers"]
    changed = 0
    for sc in sorted(A):
        for cid in sorted(A[sc]):
            x, y = A[sc][cid], B.get(sc, {}).get(cid, {})
            if x.get("status") != y.get("status") or x.get("actual") != y.get("actual"):
                changed += 1
                flip = "  <-- STATUS FLIP" if x.get("status") != y.get("status") else ""
                print(f"  {sc:>4} {cid}   keyword {str(x.get('status')):<9} "
                      f"{_num(x.get('actual')):>16}   ->   hybrid {str(y.get('status')):<9} "
                      f"{_num(y.get('actual')):>16}{flip}")
    if not changed:
        rep.ok("the model changed nothing — the keyword table already decided every cell")
    else:
        rep.warn(f"{changed} cell(s) differ — READ THESE before shipping hybrid. A model that "
                 f"empties a category is worse than one that never ran.")
    return changed


def _num(v) -> str:
    return f"{v:,.2f}" if isinstance(v, (int, float)) else str(v)


# --- 5. validate -----------------------------------------------------------------------
def validate_submission(rep: Report, path: str) -> None:
    """The shipped file against the rubric. Cheap, and the only check on the ENVELOPE.

    A blank cell and a wrong cell score the same, so anything missing here is free points
    thrown away — «Пустая и неверная ячейка стоят одинаково, поэтому заполняйте все ячейки»."""
    print("\n" + "=" * 78)
    print(f"5. VALIDATE — {path} against the rubric")
    print("=" * 78)
    try:
        sub = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        rep.fail(f"{path} is not readable JSON: {str(e)[:80]}")
        return

    for field in ("team", "contact_email", "model", "answers"):
        if not sub.get(field):
            rep.fail(f"envelope is missing `{field}`")
    if sub.get("team") in ("your-team-name", "", None):
        rep.fail("`team` is still the spec's placeholder")
    else:
        rep.ok(f"envelope: team={sub.get('team')!r} model={sub.get('model')!r}")

    bad_status, bad_actual, blank = [], [], []
    n = 0
    for sc, covs in (sub.get("answers") or {}).items():
        for cid, cell in covs.items():
            n += 1
            st, act = cell.get("status"), cell.get("actual")
            if st not in ("COMPLIANT", "BREACH"):
                (blank if st in (None, "") else bad_status).append(f"{sc} {cid}={st!r}")
            if not isinstance(act, (int, float)):
                blank.append(f"{sc} {cid} actual={act!r}")
            elif act < 0:
                # «`actual` — положительное число до 2 знаков после запятой»
                bad_actual.append(f"{sc} {cid}={act}")
    if blank:
        rep.fail(f"{len(blank)} cell(s) blank or non-numeric — each scores 0: {blank[:4]}")
    if bad_status:
        rep.fail(f"{len(bad_status)} cell(s) with a status the rubric rejects: {bad_status[:4]}")
    if bad_actual:
        rep.warn(f"{len(bad_actual)} cell(s) with a NEGATIVE actual; the rubric asks for a "
                 f"positive number: {bad_actual[:4]}")
    if not (blank or bad_status):
        rep.ok(f"all {n} cells have a valid status and a numeric actual")

    # Only the practice release ships a key; on event day this is expected to be absent.
    try:
        if config.ANSWER_KEY.exists():
            key = json.loads(config.ANSWER_KEY.read_text(encoding="utf-8")).get("scenarios")
            if key:
                want = {(sc, cid) for sc, v in key.items() for cid in v["covenants"]}
                got = {(sc, cid) for sc, v in sub["answers"].items() for cid in v}
                if want - got:
                    rep.fail(f"{len(want - got)} cell(s) in the template are missing from the "
                             f"submission: {sorted(want - got)[:4]}")
                elif got - want:
                    rep.warn(f"{len(got - want)} cell(s) not in the template: "
                             f"{sorted(got - want)[:4]}")
                else:
                    rep.ok(f"cell set matches the template exactly ({len(want)} cells)")
    except Exception as e:
        rep.warn(f"template comparison skipped ({str(e)[:60]})")


# --- driver ----------------------------------------------------------------------------
def run(ledger: str | None = None, fx: str | None = None, ship: str = "keyword",
        no_llm: bool = False) -> int:
    rep = Report()
    print("=" * 78)
    print("EVENT-DAY DRY RUN")
    print("=" * 78)
    print(f"ledger={ledger or '(none — skeleton only)'}  fx={fx or '(none)'}  ship={ship}")

    adopt_ledger_map(rep, ledger)
    preflight(rep)
    if rep.failures:
        print("\n!! preflight failed — fix this before spending any quota. "
              "Every later number would describe the wrong documents.")

    base = run_solve(rep, "keyword", BASELINE, ledger, fx, "keyword")

    hyb = None
    if no_llm:
        print("\n3. HYBRID SOLVE — skipped (--no-llm)")
    elif not ledger:
        print("\n3. HYBRID SOLVE — skipped (no ledger, nothing to classify)")
    else:
        hyb = run_solve(rep, "hybrid", HYBRID, ledger, fx, "hybrid")
        if hyb:
            report_llm_health(rep)

    diff(rep, base, hyb, skipped=(no_llm or not ledger))

    chosen = HYBRID if (ship == "hybrid" and hyb) else BASELINE
    if ship == "hybrid" and not hyb:
        rep.warn("--ship hybrid asked for, but the hybrid solve produced nothing; "
                 "shipping the keyword baseline")
    if Path(chosen).exists():
        shutil.copyfile(chosen, FINAL)
        print(f"\n-> {FINAL} written from {chosen}")
    validate_submission(rep, FINAL)

    print("\n" + "=" * 78)
    if rep.failures:
        print(f"NO-GO — {len(rep.failures)} blocking issue(s):")
        for m in rep.failures:
            print(f"   FAIL  {m}")
        if rep.warnings:
            print(f"   ({len(rep.warnings)} warning(s) as well)")
        print("=" * 78)
        return 1
    if rep.warnings:
        print(f"GO, with {len(rep.warnings)} thing(s) to read first:")
        for m in rep.warnings:
            print(f"   WARN  {m}")
    else:
        print("GO — no blocking issues and nothing to read.")
    print(f"\nShipping {FINAL} (from {chosen}). Baseline kept at {BASELINE}.")
    print("=" * 78)
    return 0
