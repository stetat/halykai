"""Tests for the event-day gate — free, offline, no API.

`cli eventday` is the last thing between a run and a submission, so its VALIDATE step is the
one check whose false negative is unrecoverable: it is the only place that looks at the
envelope, and a blank cell scores exactly what a wrong cell scores. Every failure mode it is
supposed to catch gets a test here.

    python -m pipeline.test_eventday
"""
from __future__ import annotations
import json
import sys
import tempfile
from pathlib import Path

from . import eventday

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def _sub(**over) -> dict:
    base = {
        "team": "darkhan",
        "contact_email": "adarhan76@gmail.com",
        "model": "gemini-flash-lite-latest",
        "answers": {"P1": {"6.1": {"status": "BREACH", "actual": 1.2,
                                   "evidence_txn_id": "TXN-P1-0001"}}},
    }
    base.update(over)
    return base


def _validate(sub: dict) -> eventday.Report:
    rep = eventday.Report()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "submission.json"
        p.write_text(json.dumps(sub), encoding="utf-8")
        eventday.validate_submission(rep, str(p))
    return rep


def test_accepts_a_good_submission() -> None:
    rep = _validate(_sub())
    # the template comparison will flag the missing 35 cells; only the ENVELOPE and cell
    # checks are under test here, so look for the absence of those specific failures
    bad = [m for m in rep.failures if "envelope" in m or "blank" in m or "status" in m]
    check(not bad, f"a well-formed submission raises no envelope/cell failure ({bad})")


def test_catches_a_blank_cell() -> None:
    rep = _validate(_sub(answers={"P1": {"6.1": {"status": None, "actual": None,
                                                 "evidence_txn_id": None}}}))
    check(any("blank" in m for m in rep.failures),
          "a blank cell is a FAIL (it scores exactly what a wrong cell scores)")


def test_catches_a_misspelled_status() -> None:
    rep = _validate(_sub(answers={"P1": {"6.1": {"status": "compliant", "actual": 1.0,
                                                 "evidence_txn_id": None}}}))
    check(any("status" in m for m in rep.failures),
          "lowercase 'compliant' is a FAIL — the rubric wants it exact")


def test_catches_a_non_numeric_actual() -> None:
    rep = _validate(_sub(answers={"P1": {"6.1": {"status": "BREACH", "actual": "1.2",
                                                 "evidence_txn_id": None}}}))
    check(any("blank" in m or "non-numeric" in m for m in rep.failures),
          "a stringified actual is a FAIL")


def test_catches_the_placeholder_team() -> None:
    rep = _validate(_sub(team="your-team-name"))
    check(any("placeholder" in m for m in rep.failures),
          "the spec's placeholder team name is a FAIL")


def test_catches_a_missing_envelope_field() -> None:
    s = _sub()
    del s["contact_email"]
    rep = _validate(s)
    check(any("contact_email" in m for m in rep.failures),
          "a missing envelope field is a FAIL")


def test_warns_on_a_negative_actual() -> None:
    rep = _validate(_sub(answers={"P1": {"6.1": {"status": "BREACH", "actual": -3.0,
                                                 "evidence_txn_id": None}}}))
    check(any("NEGATIVE" in m for m in rep.warnings),
          "a negative actual is a WARN — «`actual` — положительное число»")


def test_diff_reports_status_flips() -> None:
    """The diff is what turns 'the LLM ran' into a decision, so it must not stay quiet."""
    a = {"answers": {"P5": {"6.2": {"status": "BREACH", "actual": 2_906_312.92}}}}
    b = {"answers": {"P5": {"6.2": {"status": "COMPLIANT", "actual": 0.0}}}}
    rep = eventday.Report()
    n = eventday.diff(rep, a, b)
    check(n == 1, f"the diff counts the changed cell ({n})")
    check(any("differ" in m for m in rep.warnings),
          "a changed cell is surfaced as something to read before shipping")

    same = eventday.Report()
    check(eventday.diff(same, a, a) == 0 and not same.warnings,
          "identical solves produce no diff and no warning")


def test_missing_solve_is_not_silent() -> None:
    rep = eventday.Report()
    eventday.diff(rep, {"answers": {}}, None)
    check(bool(rep.warnings), "a hybrid solve that produced nothing is reported, not ignored")

    quiet = eventday.Report()
    eventday.diff(quiet, {"answers": {}}, None, skipped=True)
    check(not quiet.warnings, "but a deliberately skipped model pass is not a warning")


def main() -> None:
    print("=" * 78)
    print("EVENT-DAY GATE TESTS (offline, no API)")
    print("=" * 78 + "\n")
    for fn in (test_accepts_a_good_submission, test_catches_a_blank_cell,
               test_catches_a_misspelled_status, test_catches_a_non_numeric_actual,
               test_catches_the_placeholder_team, test_catches_a_missing_envelope_field,
               test_warns_on_a_negative_actual, test_diff_reports_status_flips,
               test_missing_solve_is_not_silent):
        fn()
    print()
    if FAILED:
        print(f"{len(FAILED)} EVENT-DAY GATE TEST(S) FAILED")
        for m in FAILED:
            print(f"  - {m}")
        sys.exit(1)
    print("ALL EVENT-DAY GATE TESTS PASSED")


if __name__ == "__main__":
    main()
