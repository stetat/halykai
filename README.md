# Halyk AI Challenge — covenant compliance pipeline

An agent that reads 306 deliberately obfuscated financial PDFs plus a 2,355-row transaction
ledger and decides, for every borrower × covenant clause: `status`, `actual`, `evidence_txn_id`.

---

# ➜ `submission.json` is the deliverable

**[`submission.json`](submission.json) at the repo root is the file to submit.** Everything
else in this repository exists to produce and justify it.

| | |
|---|---|
| **cells** | 84/84 — all 27 borrowers, every clause the template asks for |
| **valid** | every cell a `COMPLIANT`/`BREACH` and a numeric `actual`; none blank |
| **verdicts** | 6 BREACH, 78 COMPLIANT, 2 evidence transactions |
| **produced by** | the deterministic keyword classifier — **no LLM**, byte-for-byte reproducible |
| **order** | matches `submission_template.json` exactly |

Two other files sit beside it as the evidence behind that choice:

- `submission_keyword.json` — the deterministic baseline (identical to what ships)
- `submission_hybrid.json` — the same run with Gemini on undecided rows (14 BREACH, 11 evidence)

They differ on **32 of 84 cells**. Which one ships and why is in [`REPRODUCE.md` §7](REPRODUCE.md).

---

## Reproducing `submission.json`

### Requirements

- **Python 3.9+** — the pipeline is **stdlib only**. No `pip install`, nothing to pin.
- **`pdftotext`** (from poppler) — the single external binary. `brew install poppler`.
- A Gemini API key in `.env` — **only if** you want the hybrid run. The shipped file needs none.
  Copy [`.env.example`](.env.example) to `.env` and fill it in; `.env` itself is gitignored.

### The one command

```bash
cd <repo root>
export DATASET_DIR="dataset/real/agentic-bank-hidden"     # ← REQUIRED

python -m pipeline.cli eventday \
    --ledger dataset/real/agentic-bank-hidden/master_ledger_2025.csv
```

That runs the whole flow — preflight → keyword baseline → hybrid → diff → validate — and ends
on **GO** or **NO-GO**. It writes `submission.json`, and keeps both variants next to it.

To reproduce the shipped file exactly, with no API calls at all:

```bash
DATASET_DIR=dataset/real/agentic-bank-hidden python -m pipeline.cli eventday \
    --ledger dataset/real/agentic-bank-hidden/master_ledger_2025.csv --no-llm
```

> ### `DATASET_DIR` is not optional
> Unset, the pipeline reads the **practice release** in `dataset/` — 12 borrowers, 36 cells —
> and writes a perfectly valid submission for the wrong contest, with no error. If a run looks
> strange, check this first.

### What a correct run prints

```
  ok   ledger names 27 borrowers, matching the template exactly
  ok   310 dataset files found (nested under ['documents'])
  ok   310 documents classified; all 27 borrowers have documents
  ok   live contract selected for all 27 borrowers
  ok   27 outdated contract(s) quarantined (the version trap)
  WARN 1 cell(s) bound related-party payments but their borrower has an empty list …
  WARN 10 image document(s) nobody has transcribed …
FX: 7 rate disclosure(s) read from documents; 7 borrower(s) covered directly.
Ledger: 2355 rows, 2 currencies, 27 scenarios resolved / 27
  ok   keyword: 84/84 cells computed -> submission_keyword.json
  ok   all 84 cells have a valid status and a numeric actual
GO, with 2 thing(s) to read first
```

Any `FAIL` means a whole class of cells is wrong and no later number is worth reading. The two
`WARN`s above are known and documented, not regressions.

### Proving the run is deterministic

```bash
DATASET_DIR=dataset/real/agentic-bank-hidden python -m pipeline.cli eventday \
    --ledger dataset/real/agentic-bank-hidden/master_ledger_2025.csv --no-llm
cp submission.json /tmp/a.json
DATASET_DIR=dataset/real/agentic-bank-hidden python -m pipeline.cli eventday \
    --ledger dataset/real/agentic-bank-hidden/master_ledger_2025.csv --no-llm
diff /tmp/a.json submission.json && echo IDENTICAL
```

Full conditions for reproducibility — and what to check when a number moves — are in
[`REPRODUCE.md`](REPRODUCE.md).

### Verifying the pipeline itself

Free, offline, no quota. All nine must pass:

```bash
python -m pipeline.test_engine       # metric definitions vs real clause wording
python -m pipeline.test_ledger       # 8 CSV dialects, FX, number parsing
python -m pipeline.test_classifier   # vocabulary, related-party matching, sign guard
python -m pipeline.test_retrieval    # index, borrower scoping, spec-leakage guard
python -m pipeline.test_docs         # real PDFs vs ground truth, incl. nested archive
python -m pipeline.test_eventday     # the shipping gate
python -m pipeline.test_roundtrip    # 36/36 through a hostile cp1251/';'/KZT file
python -m pipeline.test_e2e          # non-circular end-to-end vs the practice key
python -m pipeline.test_holdout      # the two honest accuracy numbers
```

`python -m pipeline.cli scorecard` runs them all and prints one table, separating
out-of-sample numbers from in-sample ones.

---

## Other commands

```bash
python -m pipeline.cli map                       # what got classified, and from where
python -m pipeline.cli specs --no-llm            # covenant specs, regex only
python -m pipeline.cli retrieve "<query>" --acc ACC-7001   # what the RAG layer serves
python -m pipeline.cli definitions               # what each contract defines its categories to mean
python -m pipeline.cli ocr                       # transcribe images nobody has read (uses quota)
python -m pipeline.fx                            # every FX rate found, with its source document
python -m pipeline.cli score submission.json     # score vs a key, where one exists
```

`--classifier` selects how much the LLM sees: `keyword` (default, free, offline), `hybrid`
(1 call/borrower, only rows no rule decided), `gemini` (every row). See [`MODELS.md`](MODELS.md)
to run any of it on a different model.

---

## What the real archive is

Established by inspection, not assumed — each of these broke something before it was handled:

- **310 files**, documents nested inside `documents/`.
- **27 borrowers, 84 cells.** 24 × {6.1, 6.2, 6.3}, plus **X1/X2/X3 with a 6.4**, plus **J4
  numbered 5.1/5.2/5.3**. A hardcoded triple both invents and omits cells.
- **The template is blank** — no answer key ships, so accuracy is not measurable on this data.
- **Scenario ids are `S1 B2 F1 G1 H1 J1 KC X1 …`**, read from the ledger by majority vote and
  constrained to the template's list.
- **`KC` sits on account `TELE-4471`**, not `ACC-####`, and numbers its rows `TXN-KC-CAP-29` —
  three segments where every other borrower uses two.
- **800 of 2,355 rows are counterparty noise** on ~550 `ACC-9xxx` accounts; only 1,555 are
  borrower rows.
- **J4's documents are entirely in English** — Dutch auditor, `CREDIT AGREEMENT`, `Section 5.1`,
  `SUPERSEDED … NOT OPERATIVE`. Every Russian-only pattern missed it.
- **No FX file ships**, and 25 of 27 borrowers hold EUR. See below.
- **The transaction ledger is data, not a document.** It names every account, so routing it by
  account id filed a 310 KB CSV under all 27 borrowers — and `reclass` then found six
  *fabricated* reclassifications inside the ledger's own rows.

## The traps that decide cells

**The version trap.** Every borrower has an outdated prior-year contract stamped
«НЕДЕЙСТВУЮЩАЯ … НЕ ПРИМЕНЯЕТСЯ» (or `SUPERSEDED … NOT OPERATIVE`) alongside the live one.
Using the wrong one poisons every number for that borrower. 27/27 quarantined.

**Related parties are an ownership test, not a name match.** The contracts are explicit:
*«Отнесение контрагента к аффилированным лицам определяется … **а не назначением платежа**»*.
Dossiers state membership either as an ownership table with a per-borrower threshold, or
declaratively («классифицирован как АФФИЛИРОВАННОЕ ЛИЦО»), and are seeded with near-miss decoys
and a footnote trap where a 48% stake is really 27.3% of voting rights.

**FX rates are in the documents, and differ per borrower.** No `--fx` file ships. Two borrowers
state a rate outright («1 EUR = $1.08»); five more imply it through a worked example — and those
quote the payment **net of a bank fee** explicitly excluded from the converted amount. Adding
the fee back turns 1.1029 into exactly 1.1200 and 1.0764 into 1.0850: every disclosed rate is
round once restored, none are without it. See [`pipeline/fx.py`](pipeline/fx.py).

**Covenant data hidden inside images.** `pdftotext` returns nothing for an image, so a document
whose ownership table lives in a picture reads as ordinary and the determination is silently
dropped. `pipeline/pdfimages.py` extracts them (stdlib only); `cli ocr` transcribes them with
model vision, and hand-verified values in `image_facts.json` always win. **10 image documents in
the real archive are still untranscribed** — the largest open lead.

---

## Layout

```
pipeline/
  config.py       paths, .env, dataset discovery, scenario<->account map, template reader
  pdftext.py      magic-byte typing + cached `pdftotext -raw` extraction
  docmap.py       classify every file, quarantine outdated contracts, pick the current one
  retrieval.py    BM25 passage index (RU/KZ morphology folded in), scoped per borrower;
                  grounds the classifier prompt in that borrower's own contract
  covenants.py    contract -> covenant spec. Thresholds/operators parsed DETERMINISTICALLY
  reclass.py      auditor reclassifications, related parties, period cut-offs
  fx.py           per-borrower FX rates read out of the documents
  classifier.py   transaction categoriser: keyword table first, LLM only on undecided rows
  ledger.py       ledger loader: sniffs encoding/delimiter, fuzzy RU/EN headers, FX
  engine.py       spec + ledger -> status/actual/evidence (leave-one-out evidence)
  eventday.py     the one command: preflight -> baseline -> hybrid -> diff -> validate
  scorecard.py    every measurable number in one table
  solve.py        end-to-end -> submission.json
  cli.py          entry point
```

Docs: [`REPRODUCE.md`](REPRODUCE.md) (reproducibility + the ship decision) ·
[`MODELS.md`](MODELS.md) (running on a different model) ·
[`HANDOFF.md`](HANDOFF.md) (what is measured, what is left, and every trap found).

## Honest numbers

Measured on the **practice release**, which ships a filled answer key. The real archive's
template is blank, so nothing can be scored against it — these are what the pipeline is worth,
not what this submission scored:

| measure | value |
|---|---|
| transaction classifier, first-contact on held-out narrations | **113/149 = 75.8%** |
| metric rules corroborated by ≥2 borrowers | **33/36 = 91.7%** |
| non-circular end-to-end vs the key | **31 cells exercised, 0 status disagreements** |
| hostile-dialect ingestion round trip | 36.000/36, evidence 9/9 |

`reconstruct`'s 36/36 and `validate`'s 34/36 are **in-sample** and are not accuracy.
