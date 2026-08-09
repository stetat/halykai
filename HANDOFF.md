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
| Non-circular end-to-end vs the key | **31 cells exercised, 0 status disagreements** (24 at 1.000, 7 lose only evidence) | `python -m pipeline.test_e2e` |
| Docs-only floor (no ledger at all) | 10.011/36 = 27.8% | `solve` with no `--ledger` |

Everything else — `reconstruct`'s 36/36, `validate`'s 34/36, `cli score` with no file,
`make_ledger`'s 8.5/36 — is in-sample or anti-sample and is **not accuracy**. Do not put any
of them in a slide.

`test_e2e` prints `TOTAL 29.600 / 36 = 0.8222`; that denominator includes the 5 cells the
harness cannot materialise, scored 0. The status-agreement rate on what it *can* test is 31/31.

Nothing is uncommitted. The circuit breaker in `classifier.classify_hybrid` (2 consecutive API
failures → stop calling) is committed and tested.

**Added since:** a retrieval layer (`retrieval.py`, `test_retrieval.py`) and `MODELS.md`. See
§8 below — the honest numbers above are unchanged by it, which is the point.

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

**Five** of 36 are unexercised by the only non-circular harness (was seven). Two are genuinely
undecidable; the rest are harness-vocabulary limits, not engine doubts:

| cell | why | worth fixing? |
|---|---|---|
| P4 6.3, P8 6.3 | key `actual` equals its own threshold within rounding — the key's rounding, not the engine, sets the verdict | **No.** Undecidable by construction. |
| P5 6.1 | cannot materialise `group_capex`; the engine's `group_capex / __ebitda__` correctly matches «капитальных затрат Группы к EBITDA Заёмщика» — it is the *harness* vocabulary that has no Group scope | **Not a bug.** Harness limitation only. |
| P4 6.1 | EBITDA add-back of $824,153 moves actual 0.33 → 1.15; undecidable at synthetic revenue scale | Low. |
| P9 6.1 | `expected_semantics` records it as NOT EXPRESSIBLE: the numerator is a counterparty-filtered SUBSET of capex, not a category | **Only by a cold agent.** See below. |

**Do not write P9 6.1's formula yourself.** The harness is worth having for exactly one reason:
`expected_semantics.EXPECTED` was written by an agent that never opened `engine.py`. Adding
`unrestricted_assets` to the harness vocabulary and writing the formula after reading the
engine converts the repo's only non-circular check into a partially circular one, and it will
still read green — which is precisely the failure mode 13 of 36 cells once hid in.

**P3 6.1 and P7 6.1 are now exercised**, and getting there fixed three ways the harness lied:

- `ebitda` is materialisable after all. The independent reading says so itself in both cells
  («sibling contracts P5 and B1 define it as «Выручка за вычетом Операционных расходов», so
  revenue - opex is the natural fill-in») and writes that expansion out by hand for P4 6.1 and
  P5 6.1, so substituting it stays inside the independent reading.
- **Springing covenants must be constructed above their own trigger.** P3 6.1 applies «ТОЛЬКО
  ПРИ УСЛОВИИ, что совокупные поступления по финансированию превышают $4,000,000.00»; the
  bisection solved for a 1.71x ratio at $1.4M of financing, the engine correctly declined to
  spring, and the cell read as a metric disagreement. Ratio constructions are now scaled past
  the trigger, which leaves `actual` untouched.
- **A construction with a negative category total is not a ledger.** The bisection returned
  tax = −994,160, `totals_to_txns` took `abs()`, and the materialised rows no longer satisfied
  the formula they were built from — P7 6.1 read 2.26 against the key's 0.36. Negative totals
  are now rejected, and the free variable is chosen by trying each candidate, because for
  «(tax + utilities) / (revenue - opex)» at 0.36x solving for `tax` needs a negative numerator
  at every scale while solving for `revenue` lands everything positive.
- A ratio is a **hyperbola** in its denominator. A global bisection walks through the pole and
  converges on nothing, and the pole straddles the target without equalling it — so the search
  scans a grid, collects *every* straddling interval, and bisects inside each.

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

**Cleared.** `README.md`'s two stale claims (no OCR in this environment; set `solve.TEAM`) are
fixed. `MODELS.md` now covers running the pipeline on a different model.

---

## 8. The retrieval layer — what it changed, and what it deliberately did not

`retrieval.py` is BM25 over 1,309 passages from 189 documents, stdlib only, scoped by borrower
and document kind. It exists because nothing in this pipeline retrieved anything: the
classifier's LLM prompt was a category list *we* wrote, judged against Kazakh narrations, with
the borrower's contract unread on disk. The prompt now carries the top passages from that
borrower's own contract and audit report, each tagged with its source filename.

**Nothing in the honest numbers moved**, and that is correct — retrieval only grounds the LLM
path, and the LLM path only touches rows the keyword table could not decide. 113/149, 33/36 and
the e2e agreement are unchanged. `make_ledger`'s dress rehearsal still computes 36/36
cells with 12/12 scenarios resolved.

Three guards are tested, not assumed, because each is a silent poisoning channel:

- the **spec and answer key are never indexed** (both name ACC-7801 in a worked example, so an
  account-based retriever hands the answer key back as context);
- the **2024 contracts are never indexed** (the version trap, at retrieval scale);
- retrieval is **scoped to one borrower** — unscoped is worse than none, because another
  borrower's clause reads as authoritative and names a different threshold.

Writing `test_retrieval` found two live stemmer bugs, both the repo's signature inflection
class: Kazakh two-letter suffixes were eating Russian words (`оплате` → `опла` via KZ `те`,
while `оплату` → `оплат`, so two inflections of one word stopped sharing a stem), and `-ых/-их`
was missing from the Russian ending table entirely. Kazakh endings are now gated on a
Kazakh-specific letter and get the second stripping pass agglutination requires.

**Definition mining is not in the decision path, and must not be autowired.** `cli definitions`
extracts every sentence where a contract defines a category. Measured against the 149 held-out
narrations, the mined terms fire on **1 of the 35** rows the keyword table cannot decide, and
that one is wrong: it reads «услуги по подбору ПЕРСОНАЛА» as payroll where the table
deliberately says opex. Net −1. The cause is a property of *this* corpus — the contracts define
categories procedurally («суммы, отнесённые к данной статье»), not by membership — so there is
nothing to mine. A new corpus may define membership, which is exactly what `cli definitions` is
for on event day: **read it, then decide by hand.** `test_retrieval` fails if someone wires it
in without re-measuring.

Event-day use: when a cell looks wrong, run its narration through
`python -m pipeline.cli retrieve "<narration>" --acc ACC-78xx` — the passages printed are the
passages the model saw.

## 9. Related-party name matching (fixed — do not revert to substring)

`classifier._is_related` used to test `kyc_name in counterparty.lower()`. That required the
ledger to reproduce the dossier's punctuation exactly, and the dossiers do not even agree with
each other: «Aral Capital Partners, LLP», «Atyrau Holding Group L.L.P», «Ertis Capital, LLP».
A ledger writing «ARAL CAPITAL PARTNERS LLP» missed on the comma. **4 of every 5 realistic
renderings failed**, and the failure is silent and points the wrong way — a missed related
party reports $0 and reads COMPLIANT. This decides every 6.3 plus P6 6.1: **13 of 36 cells**.

Now compared as identifying words with legal forms and punctuation stripped. Two guards, both
tested, stop the looser matcher from the opposite error: extra words the ledger adds must be
non-identifying (so «Aktau Holdings Trading House LLP» ≠ «Aktau Holdings LLP»), and the match
must rest on a distinctive word (every party here is a «<Place> Capital/Holding Partners LLP»,
so without this «Aktau Holdings LLP» matches the borrower itself, «Aktau Port Services JSC»).

Measured: **108/108** recall on ledger renderings, **0 false positives in 228** non-related
pairings, pinned by `run_related_party_matching` in `test_classifier`. On event day, watch for
Cyrillic transliteration of a Latin name («Сарыарка» for «Saryarka») — that is the one class
this matcher still cannot bridge, and it would need the real ledger to confirm it happens.

---

## 10. The archive may not extract flat — the single largest risk that was still open

The spec's dataset table says the PDFs arrive inside a folder: «`documents/` — **Одна папка**
со всеми PDF-документами датасета». **This practice release extracts flat**, and every
discovery path in the pipeline was `DATASET.iterdir()` filtered by `is_file()`, which skips a
subdirectory in silence.

Measured against a nested copy of this exact corpus, before the fix: **1 document classified,
0 accounts resolved, 0 contracts selected, 36 empty cells.** No exception, no traceback, and
not one `!!` line naming the cause. The entire score, lost to a folder, in a run that looks
like it worked.

Discovery now walks the tree through `config.dataset_files()`, and — just as important —
bare filenames resolve through `config.dataset_path()`. Documents are keyed by bare filename
everywhere (`docmap` stores `d.name`; four modules re-join it against `DATASET`), and that
join is the thing that breaks; it now happens in exactly one place. `docmap` prints which
shape it found, because "1 document classified" is otherwise indistinguishable from "the
archive is empty".

`test_docs` builds a nested copy of the real corpus in a temp directory and asserts the whole
document layer comes out identical: 206 documents, 12 borrowers routed, 12/12 live contracts,
12/12 related parties, retrieval index intact.

**On event day, read `docmap`'s first line before anything else.** If the archive is nested it
will say so; if it says nothing and the document count looks low, stop and look at the folder
before trusting a single cell.

---

## Command reference

```
python -m pipeline.cli {check,map,validate,solve,score,ocr,retrieve,definitions}
python -m pipeline.cli solve --ledger L.csv --fx FX.csv --classifier {keyword,gemini,hybrid}

# free / offline — run all of these after any change
python -m pipeline.test_classifier     # vocabulary + the alias pin + hybrid routing
python -m pipeline.test_engine         # metric definitions, quoting real clauses
python -m pipeline.test_ledger         # 8 dialects, FX, number parsing
python -m pipeline.test_roundtrip      # 36/36 through a hostile file
python -m pipeline.test_docs           # real PDFs vs the key — the generalisation harness
python -m pipeline.test_e2e            # non-circular end-to-end vs the key
python -m pipeline.test_retrieval      # index, scoping, spec-leakage guard, grounded prompt
python -m pipeline.test_holdout        # the two honest numbers

python -m pipeline.test_classifier --gemini   # spends quota; reproduces the hybrid experiment
```
