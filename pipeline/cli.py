"""Entry point.  Run from the project root:

  python -m pipeline.cli check      # verify Gemini key/auth (1 API call)
  python -m pipeline.cli map        # build docmap.json + print current contracts
  python -m pipeline.cli specs      # build covenant_specs.json (regex + Gemini)
  python -m pipeline.cli specs --no-llm   # regex-only, no API calls
  python -m pipeline.cli validate   # bracket-check extraction vs answer key (no API)
  python -m pipeline.cli solve [--ledger master_ledger_2025.csv]  # -> submission.json
  python -m pipeline.cli score [submission.json]   # score vs answer key
"""
from __future__ import annotations
import sys
from . import config


def cmd_check(_args):
    from . import gemini
    print(f"auth mode: {config.GEMINI_AUTH_MODE} | key: {config.GEMINI_API_KEY[:6]}…"
          f"{config.GEMINI_API_KEY[-4:]} | model: {config.MODEL_FLASH}")
    try:
        print("response:", gemini.check())
        print("OK — Gemini reachable.")
    except Exception as e:
        print("FAILED:", e)
        print("\nIf this is a 400/401/403: the key is likely not an AI Studio API key.")
        print("Grab one that starts with 'AIza' at https://aistudio.google.com/apikey")
        print("or set GEMINI_AUTH_MODE=bearer in .env if it is an OAuth token.")


def cmd_map(_args):
    from . import docmap
    r = docmap.build()
    print(f"Classified {len(r['docs'])} files across {len(r['by_acc'])} accounts.\n")
    print(f"{'scenario':>8}  {'account':>9}  current contract")
    for sc, acc in config.SCENARIO_TO_ACC.items():
        print(f"{sc:>8}  {acc:>9}  {r['current_contract'].get(acc)}")
    dead = [n for n, d in r["docs"].items() if d["outdated"] and d["has_covenants"]]
    print(f"\nOutdated contracts quarantined: {len(dead)}  ->  {sorted(dead)}")


def cmd_specs(args):
    from . import covenants
    use_llm = "--no-llm" not in args
    r = covenants.build(use_llm=use_llm)
    for sc, e in r.items():
        covs = e.get("covenants", {})
        print(f"\n{sc} ({e['account']}) <- {e['contract']}")
        for cid, spec in covs.items():
            if use_llm:
                print(f"  {cid}: {spec.get('name')} {spec.get('operator')} "
                      f"{spec.get('threshold')} [{spec.get('unit')}] "
                      f"needs_ledger={spec.get('needs_ledger')}")
            else:
                print(f"  {cid}: {spec['raw_text'][:90]}…")
    print(f"\nWrote covenant_specs.json ({'with LLM' if use_llm else 'regex only'}).")


def cmd_score(args):
    from . import scorer
    files = [a for a in args if not a.startswith("-")]
    if files:
        scorer.score_file(files[0])
    else:
        print("No submission given — scoring the answer key against itself (sanity = 1.0):\n")
        import json
        data = json.loads(config.ANSWER_KEY.read_text(encoding="utf-8"))
        scorer.score_submission(
            {"answers": {sc: v["covenants"] for sc, v in data["scenarios"].items()}})


def cmd_validate(args):
    from . import validate
    validate.run(use_llm="--llm" in args)


def cmd_solve(args):
    from . import solve
    opts = {"--ledger": None, "--fx": None, "--classifier": "keyword"}
    for i, a in enumerate(args):
        if a in opts and i + 1 < len(args):
            opts[a] = args[i + 1]
    sub = solve.solve(opts["--ledger"], opts["--fx"],
                      classifier_mode=opts["--classifier"])
    filled = sum(1 for sc in sub["answers"].values()
                 for c in sc.values() if c["status"] in ("COMPLIANT", "BREACH"))
    print(f"Wrote submission.json — {filled}/36 cells computed "
          f"({'ledger supplied' if opts['--ledger'] else 'no ledger: skeleton only'}).")
    if opts["--ledger"] and filled < 36:
        print(f"!! {36 - filled} cells are EMPTY and score 0 — investigate before submitting.")


COMMANDS = {"check": cmd_check, "map": cmd_map, "specs": cmd_specs,
            "validate": cmd_validate, "solve": cmd_solve, "score": cmd_score}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
