# Reproducing the submission, byte for byte

The point of this file: **run these commands and you get the same `submission.json` every
time.** If a number moves between two runs, one of the conditions below was different — check
them in order, they are listed most-likely-first.

---

## 0. The one thing that changes everything

```bash
export DATASET_DIR="dataset/real/agentic-bank-hidden"
```

Without it the pipeline reads the **practice release** in `dataset/` — 12 borrowers, 36 cells,
a pre-filled answer key — and produces a perfectly valid submission for the wrong contest.
Every command below assumes it is set. It is the first thing to check when output looks wrong.

The real archive and the practice release both live under `dataset/`, and discovery walks the
tree, so they would merge into one 516-file corpus with two sets of borrowers. They do not,
because a directory holding its own `submission_template.json` is treated as a separate archive
and skipped by the parent (`config._nested_archive_roots`). Verify the split any time:

```bash
python -c "from pipeline import config; print(len(config.dataset_files()), config.DATASET)"
# 206 → the practice release
DATASET_DIR=dataset/real/agentic-bank-hidden python -c "from pipeline import config; print(len(config.dataset_files()))"
# 310 → the real archive
```

---

## 1. The reproducible run

```bash
cd "<repo root>"
export DATASET_DIR="dataset/real/agentic-bank-hidden"

python -m pipeline.cli eventday \
    --ledger dataset/real/agentic-bank-hidden/master_ledger_2025.csv
```

That is the whole thing: preflight → keyword baseline → hybrid → diff → validate → GO/NO-GO.
It writes three files and keeps all of them:

| file | what it is |
|---|---|
| `submission_keyword.json` | deterministic, no API. **The reproducible artefact.** |
| `submission_hybrid.json` | keyword + Gemini on the rows no rule decided |
| `submission.json` | what ships — a copy of the keyword baseline by default |

Add `--ship hybrid` to ship the model's version instead, **after** reading the diff.
Add `--no-llm` to skip the model entirely and spend no quota.

---

## 2. What is deterministic, and what is not

**Fully deterministic — same input, same output, forever:**

- document classification, contract selection, clause parsing, thresholds
- the keyword classifier (`--classifier keyword`) — pure string matching
- FX rates read from the documents
- reclassifications, related parties, the engine, the scorer

`submission_keyword.json` is therefore reproducible on any machine with the same archive. It
is the file to submit if consistency matters more than the last few points.

**Not deterministic by nature:** the Gemini pass. Same prompt, same temperature (0.0), but a
hosted model can change under you, and a 429 silently degrades the run to keywords.

**This is why the cache matters.** Every model reply is stored under `cache/gemini/`, keyed by
`(model, system, temperature, json_out, prompt)`. As long as that directory is intact, a repeat
run reads from disk and produces *identical* output without calling the API at all.

> **Do not delete `cache/` if you want to reproduce a hybrid submission.**
> Back it up alongside the submission. Deleting it does not just cost time — it re-queries a
> model that may now answer differently.

Check whether a hybrid run actually used the model or quietly fell back:

```bash
grep -E "DEGRADED|asked Gemini|sign_rejected" /tmp/eventday.log
```

`!! DEGRADED RUN` means you are looking at keyword output wearing a Gemini label. Wait for the
rate-limit window and re-run.

---

## 3. Conditions that must match

| condition | why it matters | check |
|---|---|---|
| `DATASET_DIR` | wrong dataset entirely | `echo $DATASET_DIR` |
| archive contents | text extraction is cached by file **content**, so a changed PDF re-extracts | `find "$DATASET_DIR" -type f \| wc -l` → 310 |
| `pdftotext` (poppler) | a different version can extract Cyrillic differently | `pdftotext -v` |
| Python | 3.10+; the pipeline is stdlib-only, no pinned deps | `python -V` |
| `cache/gemini/` | hybrid reproducibility depends on it | `ls cache/gemini \| wc -l` |
| `.env` model ids | changing `GEMINI_MODEL_FLASH` re-queries everything | `grep MODEL .env` |

There are **no third-party dependencies**. Nothing to pin, nothing to break on install. The
only external binary is `pdftotext`.

---

## 4. Verifying a run before you trust it

The offline suite never touches the API and must be green regardless of dataset:

```bash
python -m pipeline.test_engine       # metric definitions vs real clause wording
python -m pipeline.test_ledger       # CSV dialects, FX, number parsing
python -m pipeline.test_classifier   # vocabulary, related-party matching, sign guard
python -m pipeline.test_retrieval    # index, borrower scoping, spec-leakage guard
python -m pipeline.test_eventday     # the shipping gate
```

Then, on the real archive, the two things worth reading in the `eventday` output:

1. **Preflight** — 310 files, 27 borrowers, 27 live contracts. Any `FAIL` here means a whole
   class of cells is wrong, and no later number is worth reading.
2. **The ingestion line** — `scenarios resolved / 27` and the sign-fallback percentage.

---

## 5. What the real archive is (established, not assumed)

Worth writing down because several of these differ from the practice release and each one
broke something:

- **310 files**, documents nested under `documents/` — the practice release was flat.
- **27 borrowers, 84 cells.** 24 borrowers × {6.1, 6.2, 6.3}, plus **X1/X2/X3 with a 6.4**, plus
  **J4 numbered 5.1/5.2/5.3**. A hardcoded triple both invents and omits cells.
- **The template is blank.** There is no answer key, so accuracy cannot be measured here —
  only the pipeline's internal checks apply.
- **Scenario ids are `S1 B2 F1 G1 H1 J1 KC X1 …`**, not the practice `P1/B1`. They come from
  the ledger, majority-voted, constrained to the template's list.
- **`KC` sits on account `TELE-4471`**, not `ACC-####`, and its rows are numbered
  `TXN-KC-CAP-29` — three segments, where every other borrower uses two. Both patterns had to
  be generalised or that borrower's 63 rows resolved to nothing.
- **800 of 2,355 ledger rows are counterparty/noise rows** on ~550 `ACC-9xxx` accounts. They
  correctly resolve to no borrower; only 1,555 rows are borrower rows.
- **J4's documents are entirely in English** — a Dutch auditor, `CREDIT AGREEMENT`,
  `Section 5.1`, `SUPERSEDED … NOT OPERATIVE`. Every Russian-only pattern missed it.
- **No FX file ships.** 25 of 27 borrowers have EUR rows and the rate is stated *in the
  documents*, per borrower — see below.

---

## 6. FX: the rate is in the documents, and it differs per borrower

There is no `--fx` file in the archive. The contracts say the rate is «раскрытый аудитором»,
and the corpus states it two ways:

- **explicitly** — «по следующим курсам: 1 EUR = $1.08»
- **implicitly** — «счёт на сумму 92,415.50 EUR урегулирован платежом … $105,353.67» → 1.1400

The implicit form carries a trap: some sentences quote the payment **net of a bank fee** that
is explicitly *not* part of the converted amount («которая не входит в пересчитываемую сумму»).
The fee must be added back first. This is checkable rather than a judgement call — restoring it
turns 1.1029 into exactly **1.1200**, 1.0764 into **1.0850**, 1.0982 into **1.1000**. Every
disclosed rate is a round number once the fee is back, and none of them are without it.

Rates found: 7 borrowers disclose one (1.08–1.14). The other 18 with EUR rows get the **median**
of the disclosed rates, and each is named in the run's FX report. That is an assumption and is
labelled as one — but 1.0 is the one value the documents rule out.

```bash
DATASET_DIR=dataset/real/agentic-bank-hidden python -m pipeline.fx   # show every rate + source
```

---

## 7. The keyword-vs-hybrid decision on THIS archive (read before shipping hybrid)

Both were run. They differ on **32 of 84 cells, 8 of them status flips** — far more than the
practice release's 4-of-36, so this is a real decision rather than a formality.

| | keyword (shipped) | hybrid |
|---|---|---|
| cells | 84/84 valid | 84/84 valid |
| BREACH verdicts | 6 | 14 |
| evidence ids | 2 | 11 |

Almost every flip is a **6.3 related-party cell** where keyword reports `0.00` and hybrid finds
$260k–$300k. That is not the model being cleverer — it is the two paths answering *different
questions*. Related-party membership is an **identity** test against the KYC dossier, and the
contracts say so in as many words: «Отнесение контрагента к аффилированным лицам определяется
… **а не назначением платежа**». The keyword path applies exactly that test and returns $0 for
the 12 borrowers whose dossier names no counterparty. The model, given no list, infers
membership from the payment description — the one signal the contract explicitly rules out.

So `submission.json` ships the **keyword** baseline. It is the contract-faithful reading, it is
deterministic, and its zeros are honest.

**That is a defensible call, not a certain one.** If a dossier exists that we failed to parse,
a $0 is as wrong as a guess. The open lead is §5's untranscribed images — three of them belong
to borrowers with no related-party list. `python -m pipeline.cli ocr` transcribes them; verified
values in `image_facts.json` always win over model output. If a real ownership list turns up
there, re-run and the keyword path will find those payments by identity, which is the answer
both paths should have been giving.

---

## 8. If a number moves between runs

Work down this list:

1. `DATASET_DIR` unset or different → **wrong dataset**. Most common cause by far.
2. You compared `submission.json` from a `--ship hybrid` run against a default run.
3. `cache/gemini/` was deleted or partly populated → the model was re-queried.
4. A `!! DEGRADED RUN` in one of the two runs → that one is keyword output.
5. The archive changed (re-download, re-extract) → text cache re-keys on content and rebuilds.
6. `pdftotext` version differs between machines.

To prove the deterministic half is stable, run the keyword path twice and diff:

```bash
DATASET_DIR=dataset/real/agentic-bank-hidden python -m pipeline.cli eventday \
    --ledger dataset/real/agentic-bank-hidden/master_ledger_2025.csv --no-llm
cp submission_keyword.json /tmp/a.json
DATASET_DIR=dataset/real/agentic-bank-hidden python -m pipeline.cli eventday \
    --ledger dataset/real/agentic-bank-hidden/master_ledger_2025.csv --no-llm
diff /tmp/a.json submission_keyword.json && echo "IDENTICAL"
```
