# Handoff — what is left to perfect the model

Written 2026-08-09. Read this together with `README.md` (what the dataset is and how the
pipeline works) and the answer to *"is it done?"* — the document half is done; the
**transaction-classification half is the whole remaining risk**.

---

## 0. Where the model actually stands

Only two numbers in this repo are out-of-sample. Quote these and no others:

| measure | value | harness |
|---|---|---|
| **Transaction classifier, first-contact accuracy** | **113/149 = 75.8%** | `python -m pipeline.test_holdout` |
| **Metric rules corroborated by ≥2 borrowers** | **33/36 = 91.7%** | same |
| Non-circular end-to-end vs the key | **29 cells exercised, 0 status disagreements** (22 at 1.000, 7 lose only evidence) | `python -m pipeline.test_e2e` |
| Docs-only floor (no ledger at all) | 10.011/36 = 27.8% | `solve` with no `--ledger` |

Everything else — `reconstruct`'s 36/36, `validate`'s 34/36, `cli score` with no file,
`make_ledger`'s 8.5/36 — is in-sample or anti-sample and is **not accuracy**. Do not put any
of them in a slide.

`test_e2e` prints `TOTAL 27.600 / 36 = 0.7667`; that denominator includes the 7 cells the
harness cannot materialise, scored 0. The status-agreement rate on what it *can* test is 29/29.

**Uncommitted right now:** a circuit breaker in `classifier.classify_hybrid` (2 consecutive API
failures → stop calling) plus its test and a clearer `solve` message. Tested and passing —
commit it before anything else.

---

## 1. The one thing that most improves the score: classifier generalisation

The engine, the covenant math, ingestion, evidence, and the image channel are all closed. The
score on event day will be decided by **whether each ledger row is booked into the right
category**, because every `actual` is a signed sum of categories.

Current state of that layer:

- 75.8% first-contact on held-out narrations.
- **~27–28% of held-out rows match no vocabulary at all** and fall through to the sign of the
  amount — a fallback that can only emit `revenue` or `opex`. A capex or tax row landing there
  becomes opex, and opex is a *denominator* in CAPEX_INTENSITY and P6 6.1, where inflating it
  flips BREACH → COMPLIANT. `solve` shouts the fallback rate; read that line every run.
- **Sets A, B, C, D are all burned.** Each was written before the fix round it motivated, and
  each fix was fitted to it. In-sample score after patching runs ~100% every round while the
  next fresh set lands back near 80%. That gap *is* the finding: keyword patching buys the
  strings you saw, not the concept.

### What to do, in order

1. **Get an honest set E.** Spawn a *cold* agent, explicitly forbidden from opening
   `classifier.py` or `test_holdout.py`, and have it write 60+ RU/KZ/EN narrations with true
   labels from the contracts' own vocabulary. Set D was written that way and scored 70.0% —
   nine points below the sets written by someone who had read the table. **That nine points is
   the measure of self-bias; a set you write yourself will lie to you by about that much.**
2. **Fix what E finds structurally, not lexically.** Set D's misses were not near-misses, they
   were inverted answers from structural holes: substring matching with no word boundaries
   (`EXCAVATOR` → tax via `exca-VAT-or`), `БЕСПРОЦЕНТНЫЙ` (interest-*free*) → interest, `БЕЗ НДС`
   → a tax payment. When a miss appears, ask what *class* of wording it represents before adding
   a token.
3. **Keep the boundary rules as they are unless a test forces a change.** ASCII tokens close at
   both ends (Latin abbreviations hide *inside* words). Cyrillic stems stay open at the front,
   because Russian's prefixes mostly preserve meaning (`СУБаренда` must still match `аренд`) —
   except the negating ones, so `без/бес/не/анти/контр` immediately in front blocks the match.
   Both sides are pinned by tests; breaking one to fix the other is the classic regression here.
4. **Always match the STEM.** RU inflection has broken this table three times
   (`оплат труда` vs `оплатУ труда`, `основны средств` vs `основныХ средств`,
   `строительств` vs `строительнО-монтажные` — the fix there is the shared stem `строитель`,
   not a replacement). Re-run `python -m pipeline.test_classifier` after every table edit.
5. **Decide the hybrid question with the measured numbers, not intuition.** `--classifier hybrid`
   replaces the sign fallback with Gemini on undecided rows only. Measured against the 149
   narrations the LLM had never seen: on rows whose truth *is* revenue/opex the sign guess wins
   narrowly (35/35 vs 34/35); on rows whose truth it **cannot express** the LLM scores 110/110
   against 0/110. Hybrid pays as soon as >~3% of unmatched rows are neither revenue nor opex,
   which any real ledger clears. Use it on event day if quota allows — it is 1 call per borrower.

**Known live traps in this table** (documented so they are not re-broken):
`вознаграждение` is the KZ banking word for interest *and* a plain agent fee *and* director's
pay — mapping it unconditionally to INTEREST fixed 2 cells and broke 2 and poisoned 9 ratio
denominators; it now requires debt context (`_DEBT_CTX`). `склад` in the asset list turned
"спецодежда для персонала СКЛАДА" into capex — a location, not a purchase — hence the
`_CONSUMABLE` guard ahead of the capex test.

---

## 2. Three metric rules that rest on a single borrower

33/36 cells are corroborated by ≥2 borrowers' wording. These three are not:

- **P1 6.1** CAPEX_INTENSITY
- **P10 6.2** REVENUE_LESS_MAX
- **B1 6.2** MAX_LINE

Each rests on a rule only one borrower's phrasing triggers. Re-read those three clauses in the
live 2025 contracts and confirm the metric is what the clause says — **not** what a harness says.
Neither `reconstruct.py` nor `validate.py` can catch a wrong metric: reconstruct synthesises a
ledger satisfying whatever formula the engine already uses, and validate only bracket-checks
thresholds. 13 of 36 cells once bounded the wrong quantity while both read green.

The traps already found in this area, for calibration on what to look for:
B1 6.2 is `max(payroll, utilities)` **not** their sum ("не в совокупности … по наибольшей");
P10 6.2 is `revenue − max(payroll, tax)` (also says "наибольш", different covenant);
P6 6.1 divides related-party by **opex**, not revenue; P6 6.2 is a payroll+utilities coverage
ratio misread as related-party because "связанные С НИМИ расходы" means *associated* expenses.

---

## 3. Cells `test_e2e` cannot reach

Seven of 36 are unexercised by the only non-circular harness. Two are genuinely undecidable;
the rest would need harness work of decreasing value:

| cell | why | worth fixing? |
|---|---|---|
| P4 6.3, P8 6.3 | key `actual` equals its own threshold within rounding — the key's rounding, not the engine, sets the verdict | **No.** Undecidable by construction. |
| P3 6.1, P7 6.1 | harness cannot materialise `ebitda` as ledger rows | Maybe — needs the bisection to handle a derived variable. |
| P5 6.1 | cannot materialise `group_capex`; the engine's `group_capex / __ebitda__` correctly matches «капитальных затрат Группы к EBITDA Заёмщика» — it is the *harness* vocabulary that has no Group scope | **Not a bug.** Harness limitation only. |
| P4 6.1 | EBITDA add-back of $824,153 moves actual 0.33 → 1.15; undecidable at synthetic revenue scale | Low. |
| P9 6.1 | no independent formula was written for it | Yes, if a cold agent can write one. |

If you extend the harness: synthetic txn ids **must avoid the 0001 block** (TXN-P3-0001 is a real
applied reclassification, so a synthetic revenue row gets reclassified out of revenue), and
bisection must pick a free variable appearing **once** — a category on both sides of a `/` is
non-monotonic.

---

## 4. Evidence

- 2 of the key's 9 evidence ids are named in documents; both are recovered. The other **7 appear
  in no document at all** and are only derivable once the real ledger ships.
- The definition that matters: **evidence is the transaction whose exclusion flips the verdict**,
  not merely one that was reclassified. B1 6.1's key evidence is TXN-B1-0020 while B1's only
  reclassification is TXN-B1-0023. Leave-one-out in `engine.py` implements this.
- Pattern in the key: all 6 related-party breaches carry evidence; no MIN_REVENUE, GENERIC or
  CAPEX_INTENSITY breach does.
- **Guessing is free.** `CASE.ru.md` says that where the key holds null, the 0.20 decays on the
  `actual` scale and the id is ignored — so always emit a best guess, never null. `solve` already
  never emits a blank cell. The scorer is verified against the rubric including this rule; do not
  re-audit it.

---

## 5. Event-day runbook (the real ledger + FX table arrive)

The math is done. Event day is ingestion + the two data-dependent knobs.

1. **Commit the circuit breaker** if it is still uncommitted.
2. `python -m pipeline.cli solve --ledger master_ledger_2025.csv --fx FX.csv`
3. **Read the ingestion report before trusting any cell.** Every `!!` line means a whole class of
   cells is wrong: unresolved columns, non-USD rows with no FX table, unmapped rows, empty
   scenarios, untranscribed images, cells left empty, and the sign-fallback percentage.
4. Confirm the borrower set. `ledger.discover_scenario_map()` majority-votes the real
   scenario↔account pairs out of the rows and `config.set_scenario_map()` adopts them, **mutating
   both dicts in place** (several modules do `from .config import SCENARIO_TO_ACC`; rebinding
   leaves them on the old map). If a 13th borrower appears it should just work — verified with a
   synthetic P11/ACC-7811 — but check the count printed.
5. **Tune the two knobs against the real narrations**: the classifier vocabulary
   (`classifier.keyword_category`; `solve.base_classifier` is an *alias*, never a copy — a stale
   copy once scored 23% vs 100%) and the applied/rejected reclassification read in `reclass.py`.
   Spend the Gemini budget here, on the ~12 audit reports.
6. `python -m pipeline.cli score submission.json` and iterate.
7. If images have been swapped for event day, `python -m pipeline.cli ocr` re-reads them with
   Gemini vision into `cache/image_facts_ocr.json`; verified values in `image_facts.json` always
   win. `pdfimages.untranscribed_image_docs()` detects any new image doc and `solve` shouts.

Rate limits: free tier is ~20 requests/window and **per-model**. Workhorse is
`gemini-flash-lite-latest`; `gemini-2.5-*` is gated on this key. A 429 falls back to keywords and
prints `!! DEGRADED RUN`, so a rate-limited run is never mistaken for a real Gemini score.

---

## 6. Closed — do not spend time re-opening

- **Hidden channels.** All PDFs swept for annotations, embedded files, AcroForm, optional-content
  layers, JavaScript, XMP and `/Info`. Clean. All 206 files typed by magic bytes — **no hidden
  ledger exists** (`_Thumbs.db` is ACC-7803's 2024 decoy contract; `4a5315740e89.csv` is a PDF).
- **Images.** Exactly 4 PDFs contain images; all 4 are transcribed with provenance. Gemini vision
  OCR is validated against all 3 hand-verified docs — every threshold and amount exact.
- **Version trap** 12/12. **Related parties** resolve 12/12 (an ownership test against a
  per-dossier threshold, not a name match). **Ingestion** round-trips the 36/36 through a
  cp1251/`;`/Cyrillic/NBSP/half-KZT file plus FX. **Scorer** verified against the rubric.
- **Supersession**: the final audit report supersedes interim drafts wholesale, and both documents
  say so. Fixed via `_SUPERSEDES_RE`.
- `solve.TEAM = "darkhan"`, contact `adarhan76@gmail.com`. Envelope is
  `{team, contact_email, model, answers}`.

## 7. Documentation debt

`README.md` claims "There is no OCR in this environment, so this step is deliberately human" and
lists "Set `solve.TEAM` — it ships as the placeholder" as event-day step 0. Both are stale: OCR
works via `cli ocr`, and TEAM is set. Fix before anyone else reads it.

---

## Command reference

```
python -m pipeline.cli {check,map,validate,solve,score,ocr}
python -m pipeline.cli solve --ledger L.csv --fx FX.csv --classifier {keyword,gemini,hybrid}

# free / offline — run all of these after any change
python -m pipeline.test_classifier     # vocabulary + the alias pin + hybrid routing
python -m pipeline.test_engine         # metric definitions, quoting real clauses
python -m pipeline.test_ledger         # 8 dialects, FX, number parsing
python -m pipeline.test_roundtrip      # 36/36 through a hostile file
python -m pipeline.test_docs           # real PDFs vs the key — the generalisation harness
python -m pipeline.test_e2e            # non-circular end-to-end vs the key
python -m pipeline.test_holdout        # the two honest numbers

python -m pipeline.test_classifier --gemini   # spends quota; reproduces the hybrid experiment
```
