"""Entry point.  Run from the project root:

  python -m pipeline.cli check      # verify Gemini key/auth (1 API call)
  python -m pipeline.cli map        # build docmap.json + print current contracts
  python -m pipeline.cli specs      # build covenant_specs.json (regex + Gemini)
  python -m pipeline.cli specs --no-llm   # regex-only, no API calls
  python -m pipeline.cli validate   # bracket-check extraction vs answer key (no API)
  python -m pipeline.cli solve [--ledger master_ledger_2025.csv]  # -> submission.json
      --classifier keyword   deterministic only (default; free, no quota)
      --classifier hybrid    keyword + ask Gemini ONLY about rows no rule decided (~28%)
      --classifier gemini    ask Gemini about every row (most quota, 429s soonest)
  python -m pipeline.cli score [submission.json]   # score vs answer key
  python -m pipeline.cli ocr [file.pdf ...]  # read images nobody has transcribed (uses quota)
  python -m pipeline.cli retrieve "<query>" [--acc ACC-7801] [-k 5]   # inspect what RAG serves
  python -m pipeline.cli definitions   # what the contracts define each category to mean
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


def cmd_ocr(args):
    """Transcribe embedded images with the model's vision.

    Only for images nobody has read by eye — the hand-checked entries in image_facts.json stay
    authoritative, because a transcription verified against the picture beats one that was not.
    Writes cache/image_facts_ocr.json rather than editing image_facts.json in place, so model
    output can never quietly overwrite a verified fact."""
    import json
    from . import pdfimages
    targets = [a for a in args if not a.startswith("-")]
    todo = ([(t, 0) for t in targets] if targets
            else pdfimages.untranscribed_image_docs())
    if not todo:
        print("Every document carrying a sizeable image is already transcribed in "
              "image_facts.json. Nothing to do.")
        print("Pass a filename to re-transcribe one anyway.")
        return
    out_path = config.CACHE / "image_facts_ocr.json"
    out = {}
    if out_path.exists():
        out = json.loads(out_path.read_text(encoding="utf-8"))
    for name, _ in todo:
        print(f"reading {name} ...")
        try:
            got = pdfimages.transcribe(name)
        except Exception as e:
            print(f"!! {name}: {e}")
            continue
        if got:
            out[name] = got
            print(json.dumps(got, ensure_ascii=False, indent=2)[:900])
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}. UNVERIFIED — check each value against the PNG in "
          f"cache/images/ before copying it into image_facts.json.")


def cmd_retrieve(args):
    """Ask the corpus a question and see which passages answer it, with provenance.

    The point is inspection: the same retrieval that grounds the classifier prompt, shown to a
    human. If a borrower's cell looks wrong on event day, run the narration through here — the
    passages the model saw are the passages printed."""
    from . import retrieval
    acc = None
    if "--acc" in args:
        i = args.index("--acc")
        if i + 1 < len(args):
            acc = args[i + 1]
    k = 5
    if "-k" in args:
        i = args.index("-k")
        if i + 1 < len(args):
            k = int(args[i + 1])
    skip = {"--acc", acc, "-k", str(k)}
    query = " ".join(a for a in args if a not in skip and not a.startswith("-"))
    if not query:
        print('usage: python -m pipeline.cli retrieve "<query>" [--acc ACC-7801] [-k 5]')
        return
    idx = retrieval.index()
    print(f"{idx.n} passages indexed | query={query!r} | acc={acc or 'ALL'}\n")
    hits = idx.search(query, k=k, acc=acc)
    if not hits:
        print("no passages matched.")
        return
    for score, p in hits:
        print(f"[{score:6.2f}] {p.doc} · {p.kind} · {','.join(p.accs) or '-'}")
        print(f"         {p.text[:400]}\n")


def cmd_definitions(_args):
    """Every sentence in the corpus that DEFINES a covenant category, grouped by category.

    Read this on event day before touching `classifier._RULES`. The contracts state what they
    mean by «Коммунальные расходы»; this is that statement, quoted, with the document name. It
    is deliberately NOT wired into the classifier — measured on the held-out narrations the
    mined terms fire once and get it wrong, because these contracts define categories
    procedurally rather than by membership. A new corpus may not, and this is where you find
    out."""
    from collections import defaultdict
    from . import retrieval
    mined = retrieval.mine_definitions()
    vocab = retrieval.mined_vocabulary()
    by_cat: dict[str, list] = defaultdict(list)
    for m in mined:
        by_cat[m.category].append(m)
    sentences = {}
    for m in mined:
        sentences.setdefault((m.category, m.sentence[:120]), m)
    print(f"{len(sentences)} definitional sentence(s); "
          f"{len(vocab)} term(s) admitted after the idf and collision filters.\n")
    for (cat, _), m in sorted(sentences.items()):
        print(f"[{cat}] {m.doc}")
        print(f"   {m.sentence}\n")
    print("terms the corpus would contribute (NOT auto-applied — read, then decide):")
    terms_by_cat: dict[str, list[str]] = defaultdict(list)
    for t, c in sorted(vocab.items()):
        terms_by_cat[c].append(t)
    for c, ts in sorted(terms_by_cat.items()):
        print(f"  {c:<12} {', '.join(ts)}")


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
    # not /36: the borrower set now comes from the ledger, so the cell count is whatever the
    # data says it is (12 borrowers x 3 clauses here, but a 13th would make it 39)
    cells = [c for sc in sub["answers"].values() for c in sc.values()]
    filled = sum(1 for c in cells if c["status"] in ("COMPLIANT", "BREACH"))
    print(f"Wrote submission.json — {filled}/{len(cells)} cells computed "
          f"({'ledger supplied' if opts['--ledger'] else 'no ledger: skeleton only'}).")
    if filled < len(cells):
        print(f"!! {len(cells) - filled} cells are EMPTY and score 0 — investigate before "
              f"submitting.")


COMMANDS = {"check": cmd_check, "map": cmd_map, "specs": cmd_specs,
            "validate": cmd_validate, "solve": cmd_solve, "score": cmd_score,
            "ocr": cmd_ocr, "retrieve": cmd_retrieve, "definitions": cmd_definitions}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
