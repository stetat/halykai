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
  pdfimages.py    stdlib PDF image extraction — the dataset hides tables in images
  test_docs.py    document reading vs REAL ground truth (evidence ids, KYC thresholds)
  cli.py          entry point
```

## Related parties are an ownership test, not a name match
The contracts are explicit: *"Отнесение контрагента к аффилированным лицам определяется …
**а не назначением платежа**"*. Each KYC dossier carries an ownership table and a threshold
that **differs per borrower** (seen: 20%–38%):

> Организации, в которых Группа владеет **36.0% и более** голосующих прав, признаются
> связанными сторонами для целей Договора.

Only holders at or above that bar qualify. The dossiers are seeded with near-miss decoys
(`Saryarka Terminal Properties LLP` at 33.5% against a 36.0% bar) and one **footnote trap**:
a stake shown as 48.0% in the table is held indirectly, with the Group's real voting rights
disclosed further down as 27.3% — below that dossier's 30.0% bar, so it does **not** qualify.
Previously `related_parties()` returned every company name in the file, which inflates all
12 related-party cells. All 12 borrowers now resolve — but **two of them only via images**
(see below).

## The deepest trap: covenant data hidden inside IMAGES
`pdftotext` returns nothing for an image, so a text-only pipeline reads these documents as
ordinary and **silently drops the determination**. Four documents do this, and each one
changes an answer:

| document | account | what only the image says |
|---|---|---|
| `f5e315b390df.pdf` | ACC-7806 (P6) | the **entire KYC dossier**, scanned — no text layer at all |
| `6686c0493014.pdf` | ACC-7802 (P2) | the ownership section of an otherwise-text dossier |
| `2fe3878667db.pdf` | ACC-7804 (P4) | one-off items **added back to EBITDA**, with a $300k floor |
| `abe2474bd443.pdf` | ACC-7809 (P9) | which subsidiaries are **unrestricted** (<50% pledged) |

`python -m pipeline.pdfimages` finds every PDF carrying a sizeable image and writes them to
`cache/images/` as PNGs — stdlib only, no poppler or Pillow (the streams are FlateDecode
with a PNG predictor, so the inflated bytes are already PNG's IDAT payload). The values
read off those PNGs live in **`image_facts.json`**, each with its source document so it can
be re-checked. They are used only where the text layer yields nothing for that account.

Each image carries the same **threshold + near-miss decoy** structure as the text dossiers:
P2's bar is 25.0% with a 23.4% decoy; P6's is 40.0% with a 38.1% decoy; P4's add-back floor
is $300k with a $251k item that must **not** be added; P9's pledge bar is 50.0%.

**Event day:** re-run `python -m pipeline.pdfimages`, *look at* the PNGs, update
`image_facts.json`. There is no OCR in this environment, so this step is deliberately human.

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
The classifier has two layers: a **deterministic** keyword/related-party layer (free, always
available, and the only thing running when the free tier 429s) and **Gemini** for ambiguous
rows. Because the free tier is small, treat the deterministic layer as the primary classifier
and Gemini as the enrichment. Test it with:
```
python -m pipeline.test_classifier             # deterministic path only — free, no API calls
python -m pipeline.test_classifier --gemini    # additionally spends quota on the LLM path
```
`solve.base_classifier` **is** `classifier.keyword_category` — one implementation, aliased,
never copied. A stale copy previously lived in `solve.py` and had silently diverged: it could
emit only 4 of the 13 categories, so every ratio covenant dividing by interest/tax/utilities/
insurance/financing hit `den == 0` → `UNKNOWN` → empty cell → 0 points. It scored **23%** where
the shared implementation scores **100%**. `test_classifier` now pins the alias and asserts every
formula category is reachable offline, so the two paths cannot drift apart again.
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
- Deterministic classifier: **31/31 = 100%**, no API calls, with all 10 formula categories
  reachable offline (was 23% before the stale-copy fix). 9 of the 31 rows are built from
  vocabulary **mined out of the actual PDFs** — the contracts' own category labels and real
  counterparty names — rather than invented by us; the other 22 are hand-written, so treat
  this as a calibration signal, not a guarantee.
- Engine metric-definition tests: **9/9**, each written against real mined clause wording.
- Related-party lists resolve for **12/12** borrowers (2 only via `image_facts.json`).
- Document ground-truth tests (`python -m pipeline.test_docs`): **all pass**. Evidence
  transactions the documents actually name are recovered **2/2** as applied
  reclassifications (was 0/2 — the parser's sentence terminator broke on the `.` inside
  `($418,204.37)`, so every reclassification carrying an amount silently became
  "not applied" and no cell could ever produce evidence). The remaining **7/9 evidence ids
  appear in no document** and are only derivable once the ledger ships.
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
| revenue_cover_payroll_util | Revenue / (Payroll+Utilities) | P6 6.2 |

Three covenants are **not** category sums and were being computed wrong until the clause
text was read properly (all found by mining the PDFs, all now pinned by `test_engine`):

| cell | clause says | correct metric | the trap |
|---|---|---|---|
| B1 6.2 | "по отдельности, а **не в совокупности** … по **наибольшей** из указанных сумм; их сумма **не является** показателем" | `max(payroll, utilities)` | summing them turns a COMPLIANT cell into a BREACH |
| P10 6.2 | "Выручка **за вычетом наибольшей** из величин Расходов на оплату труда и Налогов" | `revenue − max(payroll, tax)` | also contains "наибольш", but is a different covenant from B1 6.2 |
| P6 6.1 | "превышал 0.08x **Операционных расходов**" | `related / opex` | every other related-party ratio divides by revenue |

`reconstruct.py` is an integration fixture: it synthesises a ledger that reproduces the key's
actuals, then proves the ledger→status/actual/evidence→score chain has no wiring/arithmetic/
rounding/evidence bugs. It is not a generalisation score.

**It also cannot catch a wrong metric definition** — it builds inputs that satisfy whatever
formula the engine uses, so a wrong formula still scores 36/36. `validate.py` can't either;
it only bracket-checks thresholds. 13 of 36 cells were computing the wrong quantity while
both harnesses read green. Only the metric-definition tests in `test_engine.py`, written
against clause wording mined from the PDFs, pin this down. **When in doubt, read the clause.**

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
