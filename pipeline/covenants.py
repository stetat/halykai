"""Stage A (ledger-independent): parse each borrower's CURRENT contract into a
structured covenant spec. This is the backbone that Stage B (event day, with the
real ledger) turns into status/actual/evidence.

Approach: slice out the Article-6 clause text with regex (numbers/thresholds
extract cleanly), then use Gemini to normalise each clause into JSON."""
from __future__ import annotations
import json
import re
from . import config, docmap, pdftext, gemini

# grab clause 6.x text up to the next clause / article heading
CLAUSE_RE = re.compile(
    r"(Пункт\s+6\.[123]\b.*?)(?=Пункт\s+6\.[123]\b|Стать[яи]\s+7|\Z)",
    re.S,
)

SYSTEM = (
    "Ты финансовый аналитик. Тебе дают текст одного пункта кредитного ковенанта "
    "(на русском). Верни СТРОГО JSON без пояснений."
)

PROMPT_TMPL = """Проанализируй пункт кредитного ковенанта и верни JSON вида:
{{
  "clause": "6.x",
  "name": "<короткое имя метрики>",
  "metric": "<что именно измеряется>",
  "operator": "<= | >= | < | >",
  "threshold": <число без валюты/суффикса, напр. 0.42 или 300000.00>,
  "unit": "ratio" | "usd",
  "ledger_category": "<какие транзакции леджера входят в метрику, если применимо, иначе null>",
  "carve_outs": "<оговорки/исключения, при которых превышение допустимо, иначе null>",
  "needs_ledger": true|false
}}

Текст пункта:
---
{clause}
---
Только JSON."""


# --- deterministic operator/threshold extraction (no LLM, exact) ----------------------
_DOLLAR_NUM = r"\$\s*([0-9][0-9\s.,]*[0-9])"
_RATIO_X = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*x\b")   # 0.42x / 1.70x / 9.00x
# floor covenants (metric must stay >= T): "не менее", "снижения ... ниже", "не ниже"
_FLOOR = re.compile(r"не\s+менее|не\s+ниже|не\s+меньше|снижени\w*|ниже\s+велич|минимальн", re.I)
# ceiling covenants (metric must stay <= T): "не превышал", "составили более", "не более"
_CEIL = re.compile(r"превыш\w*|не\s+более|не\s+выше|составил\w*\s+более|максим\w*|более\s+0", re.I)


def _to_number(s: str) -> float | None:
    s = s.strip().replace(" ", "").rstrip("x")
    if s.count(",") and s.count("."):
        s = s.replace(",", "")
    elif s.count(","):
        s = s.replace(",", "." if re.search(r",\d{1,2}$", s) else "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_threshold(clause: str) -> dict:
    """Return {operator, threshold, unit, springing_trigger_usd} from clause text.

    Operator: 'ниже/снижения/не менее' => '>=' (floor); 'превышал/более' => '<=' (ceiling).
    Threshold: prefer the 'N.NNx' ratio form (covenant limit) over any '$' amount, because
    springing tests state both a ratio limit AND a '$' activation trigger."""
    out: dict = {"operator": None, "threshold": None, "unit": None,
                 "springing_trigger_usd": None}
    # floor is checked first: 'ниже' is a strong floor signal even if 'превышал' co-occurs
    if _FLOOR.search(clause):
        out["operator"] = ">="
    elif _CEIL.search(clause):
        out["operator"] = "<="

    ratio_m = _RATIO_X.search(clause)
    dollar_ms = re.findall(_DOLLAR_NUM, clause)
    if ratio_m:
        out["threshold"], out["unit"] = _to_number(ratio_m.group(1)), "ratio"
        if dollar_ms:  # a $ amount alongside a ratio limit = springing activation trigger
            out["springing_trigger_usd"] = _to_number(dollar_ms[0])
    elif dollar_ms:
        out["threshold"], out["unit"] = _to_number(dollar_ms[0]), "usd"
    return out


def clause_texts(contract_name: str) -> dict[str, str]:
    path = config.dataset_path(contract_name)
    text = pdftext.extract_text(path)
    out: dict[str, str] = {}
    for chunk in CLAUSE_RE.findall(text):
        m = re.match(r"Пункт\s+(6\.[123])", chunk)
        if m:
            out[m.group(1)] = re.sub(r"\s+", " ", chunk).strip()
    return out


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else {"error": "unparseable", "raw": raw[:200]}


def build(scenarios: list[str] | None = None, use_llm: bool = True, save: bool = True) -> dict:
    dm = docmap.build(save=False)
    current = dm["current_contract"]
    scenarios = scenarios or list(config.SCENARIO_TO_ACC.keys())
    result: dict[str, dict] = {}

    for sc in scenarios:
        acc = config.SCENARIO_TO_ACC[sc]
        contract = current.get(acc)
        entry = {"scenario": sc, "account": acc, "contract": contract, "covenants": {}}
        if not contract:
            entry["error"] = "no current contract found"
            result[sc] = entry
            continue
        for cid, ctext in clause_texts(contract).items():
            # Keep the WHOLE clause: engine.classify_kind / ratio_formula / spec_categories
            # all read raw_text, and the metric-defining sentence is often the last one
            # ("Соблюдение проверяется по наибольшей из указанных сумм"). Three clauses
            # already exceed 600 chars, so truncating here silently changes the metric.
            spec = {"clause": cid, "raw_text": ctext}
            spec.update(parse_threshold(ctext))   # deterministic, free, exact
            if use_llm:
                try:
                    raw = gemini.generate(
                        PROMPT_TMPL.format(clause=ctext),
                        model=config.MODEL_FLASH, system=SYSTEM,
                        json_out=True, temperature=0.0,
                    )
                    llm = _parse_json(raw)
                    # keep deterministic operator/threshold; let LLM add name/metric/etc.
                    for k, v in llm.items():
                        spec.setdefault(k, v)
                    for k in ("name", "metric", "ledger_category", "carve_outs", "needs_ledger"):
                        if k in llm:
                            spec[k] = llm[k]
                except Exception as e:
                    spec["llm_error"] = str(e)[:120]
            entry["covenants"][cid] = spec
        result[sc] = entry

    if save:
        (config.ROOT / "covenant_specs.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    r = build(use_llm=False)  # regex-only preview (no API needed)
    for sc, e in r.items():
        print(f"{sc} ({e['account']}) <- {e['contract']}: clauses {list(e['covenants'])}")
