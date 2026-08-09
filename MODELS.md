# Running this pipeline on a different model

Everything the LLM touches in this repo goes through **one function**, `gemini.generate()`.
That is the whole seam. Swapping models — another Gemini, another provider, or a local
server — means changing an env var or writing one adapter, then re-running a fixed list of
checks to prove nothing moved that shouldn't have.

Read this together with `HANDOFF.md` §5 (the event-day runbook) and `README.md`.

---

## 0. What the model is actually responsible for

Less than it looks. The model is an **enrichment layer**, never the arithmetic:

| stage | who decides | model's role |
|---|---|---|
| document typing / routing (`docmap`) | regex + magic bytes | none |
| retrieval (`retrieval`) | BM25, stdlib | none |
| thresholds & operators (`covenants.parse_threshold`) | regex, exact | none — it may only add `name`/`metric`/`carve_outs` |
| metric definitions (`engine`) | hand-read clause wording, pinned by `test_engine` | none |
| reclassifications (`reclass`) | regex over audit reports | none |
| related parties | ownership test against the KYC threshold | none |
| **transaction categories** (`classifier`) | keyword table first | **decides the ~24% of rows no rule matched** |
| image transcription (`cli ocr`) | — | **the only reader** (no OCR in the stdlib) |

So a model swap can only move two things: the categories of undecided ledger rows, and image
transcriptions. Every covenant threshold, every formula, and every verdict stays exactly where
it was. If a model swap changes a number outside those two channels, something is wrong —
that is a bug, not a model difference.

A degraded or missing model is a supported state: the circuit breaker in
`classifier.classify_hybrid` stops calling after 2 consecutive failures, and the run falls
back to the deterministic table and prints `!! DEGRADED RUN`.

---

## 1. Another Gemini model — env only, no code

```bash
# .env
GEMINI_MODEL_FLASH=gemini-flash-lite-latest   # the workhorse: classification, covenants, OCR
GEMINI_MODEL_PRO=gemini-flash-latest          # reported in submission.json's "model" field
GEMINI_MIN_INTERVAL=4                         # seconds between calls; raise if you see 429s
```

Then:

```bash
python -m pipeline.cli check          # 1 call: proves auth + that the model id exists
```

Notes learned the hard way on this key:

- `gemini-2.5-*` is **gated**; the `-latest` aliases work. `cli check` tells you in one call.
- Free-tier request pools are **per-model**. When one 429s, another alias may still have quota.
- `gemini-flash-lite-latest` has the highest free limits and is the right default for
  classification. `gemini-flash-latest` / `gemini-2.0-flash` share a much smaller pool.
- The cache key is `(model, system, temperature, json_out, prompt[, images])`. Changing the
  model **invalidates nothing** — old replies stay on disk under `cache/gemini/`, and the new
  model simply gets fresh entries. Nothing needs clearing.

---

## 2. A different provider — one adapter

`gemini.generate()` is the only thing to reimplement. Its contract:

```python
def generate(prompt: str, *, model: str | None = None, system: str | None = None,
             temperature: float = 0.0, json_out: bool = False,
             use_cache: bool = True, max_retries: int = 4,
             images: list[bytes] | None = None) -> str
```

- **returns**: the model's text, nothing else. Callers parse JSON out of it themselves
  (`classifier._parse`, `covenants._parse_json`) and both already tolerate ```json fences.
- **`json_out=True`**: ask for JSON if the provider supports it. If it doesn't, ignore the
  flag — the parsers cope. Keep it in the cache key regardless.
- **`system`**: a system prompt. If the provider has no such concept, prepend it to the user
  message.
- **`images`**: raw PNG bytes. Only `cli ocr` passes these. If the model has no vision, raise —
  `pdfimages.transcribe` catches it and the hand-verified `image_facts.json` still wins.
- **`temperature=0.0`** everywhere. Do not raise it; every call site wants a determinate answer.
- **raises** `RuntimeError` on failure. Callers depend on this: the circuit breaker counts
  exceptions, and every LLM stage degrades to a deterministic answer instead of failing the run.
- **must redact the key** from every message it raises. `gemini.redact()` exists because the
  key travels as a query parameter and urllib puts it verbatim into tracebacks that get pasted
  into terminals.
- **should cache** to `config.GEMINI_CACHE`. On a free tier this is the difference between one
  paid run and twelve.

The cleanest way in is to keep `gemini.py`'s cache/throttle/backoff wrapper and replace only
`_request()` — that is ~15 lines and you inherit the retry, the 429 handling, and the redaction.

### Anthropic sketch

```python
BASE = "https://api.anthropic.com/v1/messages"

def _request(model: str, payload: dict) -> dict:
    req = urllib.request.Request(BASE, data=json.dumps(payload).encode(), method="POST",
        headers={"content-type": "application/json",
                 "x-api-key": config.API_KEY,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())
```

with the payload shaped `{"model": ..., "max_tokens": 4096, "system": system,
"messages": [{"role": "user", "content": [...]}]}`, text pulled from
`resp["content"][0]["text"]`, and images sent as
`{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}`.
Current model ids: `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5-20251001`.
For a classification workload, the cheapest tier is the right default — this task is
vocabulary matching, not reasoning.

### OpenAI-compatible / local (Ollama, vLLM, LM Studio)

Same shape against `/v1/chat/completions`, text at
`resp["choices"][0]["message"]["content"]`, `json_out` → `response_format={"type":"json_object"}`.
A local server removes the rate limit entirely, which changes the calculus in §4: with no
quota, prefer `--classifier gemini` (every row) over `hybrid`.

---

## 3. Verification protocol — run this after ANY model change

Free and offline. **None of these call the API**, so they must all still pass; a model change
that moves any of them has broken something deterministic.

```bash
python -m pipeline.test_classifier    # vocabulary, the alias pin, hybrid routing
python -m pipeline.test_engine        # metric definitions, quoting real clauses
python -m pipeline.test_ledger        # 8 dialects, FX, number parsing
python -m pipeline.test_roundtrip     # 36/36 through a hostile cp1251/';'/KZT file
python -m pipeline.test_docs          # real PDFs vs the key
python -m pipeline.test_retrieval     # index, scoping, spec-leakage guard, grounded prompt
python -m pipeline.test_eventday      # the shipping gate: envelope, blank cells, the diff
python -m pipeline.test_e2e           # non-circular end-to-end vs the key
python -m pipeline.test_holdout       # the two honest numbers
```

Expected, and **not allowed to move on a model swap**:

| check | value |
|---|---|
| classifier first-contact accuracy | 113/149 = 75.8% |
| metric rules corroborated by ≥2 borrowers | 33/36 = 91.7% |
| non-circular e2e vs the key | 31 cells exercised, 0 status disagreements |
| hostile-dialect round trip | 36.000/36 = 1.0000, evidence 9/9 |

Then the paid checks, in this order — each costs quota, so stop at the first failure:

```bash
python -m pipeline.cli check                        # 1 call: auth + model id
python -m pipeline.test_classifier --gemini         # reproduces the hybrid experiment
python -m pipeline.cli solve --ledger L.csv --classifier hybrid
python -m pipeline.cli score submission.json
```

Finally the whole-program dress rehearsal, which writes a realistic multi-borrower CSV and
drives the real CLI against it:

```bash
python -m pipeline.make_ledger
```

---

## 4. Choosing the classifier mode for your model

`--classifier` decides how much of the ledger the model sees:

| mode | calls | when |
|---|---|---|
| `keyword` | 0 | default. Free, offline, and the only thing running when quota is gone. |
| `hybrid` | 1 per borrower | **the right choice on a metered key.** Deterministic table first; the model is asked only about rows no rule decided (~24%) or where two categories' vocabulary collided. |
| `gemini` | 1 per borrower, every row | a local/unmetered model, where there is no reason to withhold rows. |

The measured case for `hybrid`, against 149 narrations the LLM had never seen: on rows whose
truth *is* revenue or opex, the sign fallback wins narrowly (35/35 vs 34/35); on rows whose
truth it **cannot express** — capex, tax, lease, interest — the LLM scores 110/110 against
0/110. Hybrid pays as soon as more than ~3% of unmatched rows are neither revenue nor opex,
which any real ledger clears.

---

## 5. What the model sees (and how to inspect it)

Since the retrieval layer landed, the classifier prompt is **grounded in the borrower's own
documents**: the top passages from that borrower's contract and audit report, retrieved
against its own narrations, each tagged with its source filename. The point is that the
contract states what it means by «Коммунальные расходы» and the model should read that
sentence rather than reconstruct it.

To see exactly what a model was given:

```bash
python -m pipeline.cli retrieve "оплата за электроэнергию" --acc ACC-7801 -k 5
python -m pipeline.cli definitions        # what each contract defines its categories to mean
```

Retrieval is **scoped to one borrower**. An unscoped retriever is worse than none here:
another borrower's clause reads as authoritative and names a different threshold. Two things
are never indexed, and `test_retrieval` fails if they are — the challenge spec / answer key
(both name ACC-7801 in a worked example, so an account-based router would serve the key back
as context) and the superseded 2024 contracts.

If a model swap produces a surprising category, `cli retrieve` on that narration shows the
passages that produced it. That is why every snippet carries its document name.

---

## 6. Recording which model produced a submission

`solve.MODEL` (currently `config.MODEL_PRO`) is written into `submission.json`'s `model`
field. Set `GEMINI_MODEL_PRO` to the model that actually did the work, or edit `solve.MODEL`
directly if you adapt a non-Gemini provider — a submission that names the wrong model is a
reproducibility failure even when every number in it is right.

---

## 7. Running on a new *dataset* (not a new model)

Different question, answered in `HANDOFF.md` §5. The short version: the borrower set comes
from the ledger (`ledger.discover_scenario_map`), not from a constant, so a 13th borrower
just works. The two data-dependent knobs are the classifier vocabulary and the
applied/rejected reclassification read. Retrieval reindexes itself — the index fingerprint is
over file **contents**, not names, because event day ships a new archive that reuses these
hashed filenames and a name-keyed cache would serve the practice release's text forever.
