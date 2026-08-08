# Halyk AI Challenge — pipeline

Agent that reads the (deliberately obfuscated) financial PDFs + transaction ledger and
decides, per borrower × covenant (clauses 6.1/6.2/6.3), `status` / `actual` / `evidence_txn_id`.

## What the dataset really is
Filenames and extensions are misdirection. Verified:
- `master_ledger_2025.csv` is a **4-byte stub**; `ground_truth.json` is a **PDF trap**;
  `submission_template.json` is **pre-filled = the answer key** for this practice release.
- 200 real PDFs → **12 borrowers** (`ACC-7201,7204,7801–7810`) ↔ 12 scenarios (`B1,B4,P1–P10`),
  confirmed: audit reports cite `TXN-<scenario>-####` for their account.
- Every borrower has an **outdated 2024 contract** ("НЕДЕЙСТВУЮЩАЯ … НЕ ПРИМЕНЯЕТСЯ") and a
  **live 2025 contract**. Use only the live one. There are also **interim/draft** audit
  worksheets ("ПРОМЕЖУТОЧНАЯ … предварительн") that rank below the final audit report.
- `pdftotext -raw -enc UTF-8` recovers the Cyrillic (`-layout` does not).

## The practice release cannot be fully "solved"
The real ledger + FX table are withheld: **exact `actual`s and 7/9 `evidence_txn_id`s are not
computable from these files.** This release exists to build & validate the *document-reading*
half (done: see below) and to build + unit-test the *compute engine* so it's correct the moment
the real ledger ships on event day.

## Layout
```
pipeline/
  config.py       paths, .env loader, scenario<->account map
  pdftext.py      magic-byte typing + cached `pdftotext -raw` extraction
  docmap.py       classify all files, quarantine outdated contracts, pick current one
  covenants.py    Stage A: current contract -> covenant spec. Thresholds/operators are
                  parsed DETERMINISTICALLY (regex, exact, free); Gemini only enriches.
  reclass.py      auditor reclassifications (applied vs the rejected-trap) + related parties
  classifier.py   Gemini transaction categoriser: batched 1 call/borrower, cached, with a
                  deterministic related-party override + keyword fallback
  ledger.py       event-day ledger loader: sniffs encoding+delimiter, fuzzy RU/EN headers,
                  dual scenario resolution (txn_id prefix, then account_id), FX table
  engine.py       Stage B: spec + ledger -> status/actual/evidence (leave-one-out evidence)
  gemini.py       stdlib REST client: dual auth, disk cache, throttle, backoff
  scorer.py       exact CASE.ru.md rubric
  validate.py     bracket-check extraction vs the answer key (no API)
  solve.py        end-to-end -> submission.json
  test_engine.py  synthetic-ledger correctness tests for the engine
  test_ledger.py  dialect torture-test: the same ledger in 8 plausible formats
  test_roundtrip.py  the 36/36 forced through a hostile file (cp1251/';'/Cyrillic/KZT+FX)
  cli.py          entry point
```

## Event-day ingestion hardening
The case states the ledger is **one file for all borrowers, multi-currency, expenses
negative, no category column** — but not its encoding, delimiter, or header names. Those
unknowns are the only way to score 0 on cells whose math is already proven, so the loader
degrades instead of crashing:

- **encoding** sniffed (utf-8/BOM/utf-16/cp1251/cp1252/koi8-r) — a cp1251 ledger used to
  raise `UnicodeDecodeError` and produce no submission file at all;
- **delimiter** sniffed (`,` `;` tab `|`) — RU Excel exports default to `;`;
- **headers** matched on normalised RU+EN aliases, so `Сумма`, `Transaction ID`, and
  `назначение_платежа` all resolve; a `debit`/`credit` pair substitutes for `amount`;
- **scenario resolved from two independent keys** — `txn_id` prefix first, then
  `account_id`. Previously an unexpected `txn_id` format sent every row to bucket `""`
  and emitted **0/36 silently**;
- **numbers**: NBSP thousands, RU decimal commas, parenthesised negatives, unicode minus,
  trailing currency codes. Note `0,002` is a *rate*, not `2` — getting that wrong scales
  every converted amount by 1000x (caught by `test_roundtrip`);
- **FX** is real now (`--fx`), and auto-inverts "units per 1 USD" quoting. It was
  previously a `TODO` that silently counted non-USD rows 1:1;
- **fail-safe**: ledger/classifier/engine errors are caught per stage and per cell, so a
  bad borrower costs its own cells and never the whole run, and `submission.json` is
  always written. `solve` shouts about unmapped rows, empty scenarios, missing FX, and
  any cell left empty.

## Usage (run from project root; prefix `PYTHONUTF8=1` on Windows for console Cyrillic)
```
python -m pipeline.cli check                    # verify Gemini key (1 call)
python -m pipeline.cli map                       # docmap.json + current-contract table
python -m pipeline.cli validate                  # bracket-check vs answer key (no API)
python -m pipeline.test_engine                    # engine correctness tests
python -m pipeline.cli solve --ledger LEDGER.csv  # -> submission.json (keyword classifier)
python -m pipeline.solve --ledger LEDGER.csv --classifier gemini   # Gemini categoriser
python -m pipeline.cli score submission.json      # score vs answer key
```
The classifier has two layers: a strong **deterministic** keyword/related-party layer (free,
always available) and **Gemini** for ambiguous rows. Test it with:
```
python -m pipeline.test_classifier   # labelled mini-ledger; prints accuracy + Gemini-vs-fallback source
```
Free tier is **~20 requests/minute**. If you burn the minute, `classify_batch` catches the 429
and falls back to keywords, and the harness prints `!! DEGRADED RUN` — so a rate-limited run
is never mistaken for a real Gemini score. Re-run after ~60s for a clean Gemini number. The
deterministic layer alone passes the current fixture; the real-data value of Gemini is rows the
keywords don't cover, so recalibrate both against the actual ledger on event day.

## Current results (this practice release)
- Version trap beaten: **12/12** live contracts selected, **12/12** outdated quarantined.
- Extraction validation: **34 bracket-OK, 0 MISMATCH, 2 n/a** (boundary-rounding) / 36 —
  thresholds, operators, and borrower mapping are correct.
- Engine: all correctness tests pass (ratio math, reclassification flips, rejected-trap,
  aggregate → null evidence).
- Reconstructed-ledger E2E (`python -m pipeline.reconstruct`): **all 36 cells** score
  **36.000/36 = 1.0000** graded by the real scorer against the real key, with **9/9** evidence
  transactions recovered via leave-one-out.
- Scorer self-test = **1.0000**.
- Ledger dialect torture-test (`python -m pipeline.test_ledger`): **all pass** — 8 formats
  parse identically, plus FX and number-parsing edge cases.
- Hostile-dialect round trip (`python -m pipeline.test_roundtrip`): the same 36 cells pushed
  through a cp1251 / `;` / Cyrillic-header / NBSP / half-KZT file **+ FX table** still score
  **36.000/36 = 1.0000** with **9/9** evidence — ingestion loses nothing.

### Covenant coverage (36/36 computable from ledger)
The leverage/cover ratios are all **signed sums of ledger categories** (EBITDA = Revenue−Opex),
handled by a general ratio engine (`engine.ratio_formula`):

| formula id | definition | example |
|---|---|---|
| interest_cover | EBITDA / Interest | B1 6.1 |
| cover_sources | (Revenue+Financing) / (Opex+Capex) | P2 6.1 |
| springing_leverage | Financing / EBITDA, active only if Financing > trigger $ | P3 6.1 |
| ebitda_margin | EBITDA / Revenue | P4 6.1 |
| group_capex_ebitda | GroupCapex / EBITDA | P5 6.1 |
| tax_util_ebitda | (Taxes+Utilities) / EBITDA | P7 6.1 |
| unrestricted_assets | AssetsToUnrestrictedSubs / Capex | P9 6.1 |
| insurance_cover | Insurance / (Lease+Utilities) | P10 6.1 |

`reconstruct.py` is an integration fixture: it synthesises a ledger that reproduces the key's
actuals, then proves the ledger→status/actual/evidence→score chain has no wiring/arithmetic/
rounding/evidence bugs. It is not a generalisation score.

## Gemini notes
- Key authenticates as a query-param key. `gemini-2.5-*` is gated on it; use the `-latest`
  aliases. Workhorse is **`gemini-flash-lite-latest`** (highest free limits, ideal for
  classification); `gemini-flash-latest`/`gemini-2.0-flash` have a much smaller shared pool and
  exhaust quickly.
- Free-tier request pools are small and **per-model**; when one 429s, another `-latest` alias may
  still have quota. The client parses the server's `retry in Xs` and waits it out, does the heavy
  lifting in deterministic code, and caches every reply under `cache/gemini/`.
- Clean Gemini classifier run (flash-lite) on the labelled fixture: **22/22 = 100%** with 0 keyword
  fallback (indicative — the fixture is hand-built; recalibrate on the real ledger).

## Event-day checklist (when the real ledger arrives)
0. **Set `solve.TEAM`** — it ships as the spec's placeholder `your-team-name`.
1. Confirm `SCENARIO_TO_ACC` via `txn_id` prefix ↔ `account_id` in the ledger.
2. `python -m pipeline.cli solve --ledger master_ledger_2025.csv` (add `--fx` if provided).
   Read the ingestion report it prints *before* trusting the cells: rows loaded, currencies,
   `scenarios resolved / 12`, and any `!!` line. `!!` means a whole class of cells is wrong.
3. Tune the two data-dependent knobs against the ledger: the base transaction **classifier**
   (`solve.base_classifier` — maps each counterparty/description to a category like capex,
   opex, financing, interest, tax, utilities, insurance, related-party) and the
   reclassification **applied/rejected** read (`reclass.py`). Spend Gemini budget here, on the
   ~12 audit reports. The covenant math (all 8 formulas + springing + evidence) is already done.
4. `python -m pipeline.cli score submission.json` and iterate.
```
