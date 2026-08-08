"""Thin Gemini REST client: stdlib-only, on-disk cache, throttle, backoff.

Supports both auth styles because the supplied key may be an AI Studio API key
(?key=...) or an OAuth/session token (Authorization: Bearer ...)."""
from __future__ import annotations
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from . import config


def _retry_after(body: str) -> float | None:
    """Parse the server-suggested wait from a 429 body ('Please retry in 59.5s' / retryDelay)."""
    m = re.search(r"retry in ([0-9.]+)s", body) or re.search(r'retryDelay"?:\s*"?([0-9.]+)s', body)
    return float(m.group(1)) if m else None

BASE = "https://generativelanguage.googleapis.com/v1beta"
_last_call = [0.0]


def redact(s: str) -> str:
    """Strip the API key from anything that may be printed or raised.

    The key travels as a query parameter, so urllib failures (InvalidURL, URLError, socket
    errors) put it verbatim into their message and traceback — and these get pasted into
    terminals and bug reports."""
    key = config.GEMINI_API_KEY
    out = s.replace(key, "<REDACTED_KEY>") if key else s
    return re.sub(r"([?&]key=)[^&\s'\"]+", r"\1<REDACTED_KEY>", out)


def _cache_path(model: str, prompt: str, system: str | None,
                temperature: float = 0.0, json_out: bool = False) -> "config.Path":
    # temperature/json_out are part of the request, so they must be part of the key:
    # otherwise a JSON-mode reply gets served for a plain-text call with the same prompt.
    key = hashlib.sha256(
        f"{model}\x00{system}\x00{temperature}\x00{json_out}\x00{prompt}".encode()
    ).hexdigest()[:24]
    return config.GEMINI_CACHE / f"{key}.json"


def _request(model: str, payload: dict) -> dict:
    url = f"{BASE}/models/{model}:generateContent"
    headers = {"Content-Type": "application/json"}
    if config.GEMINI_AUTH_MODE == "bearer":
        headers["Authorization"] = f"Bearer {config.GEMINI_API_KEY}"
    else:  # key / query
        url += f"?key={config.GEMINI_API_KEY}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise                                  # handled (and redacted) by the caller
    except Exception as e:
        raise RuntimeError(f"{type(e).__name__}: {redact(str(e))}") from None


def generate(prompt: str, *, model: str | None = None, system: str | None = None,
             temperature: float = 0.0, json_out: bool = False,
             use_cache: bool = True, max_retries: int = 4) -> str:
    """Return model text for a prompt. Caches by (model, system, prompt)."""
    model = model or config.MODEL_FLASH
    cp = _cache_path(model, prompt, system, temperature, json_out)
    if use_cache and cp.exists():
        return json.loads(cp.read_text(encoding="utf-8"))["text"]

    gen_cfg: dict = {"temperature": temperature}
    if json_out:
        gen_cfg["responseMimeType"] = "application/json"
    payload: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    for attempt in range(max_retries):
        wait = config.MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            resp = _request(model, payload)
            _last_call[0] = time.time()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 503) and attempt < max_retries - 1:
                # honour the server's suggested wait (per-minute window); else exponential.
                wait_s = _retry_after(body) if e.code == 429 else None
                wait_s = min((wait_s + 2) if wait_s else 2 ** attempt * 5, 90)
                time.sleep(wait_s)
                continue
            raise RuntimeError(f"Gemini HTTP {e.code}: {redact(body[:500])}") from None
        except RuntimeError as e:
            # transient network/socket failure (already redacted by _request): retry rather
            # than abandoning a whole borrower's classification on one blip.
            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt * 5, 60))
                continue
            raise
        if "error" in resp:
            raise RuntimeError(f"Gemini error: {json.dumps(resp['error'])[:500]}")
        try:
            cand = resp["candidates"][0]
            parts = cand.get("content", {}).get("parts", [])
            # thinking models may emit a thought-only part first; keep text parts.
            text = "".join(p["text"] for p in parts if "text" in p)
            if not text:
                fr = cand.get("finishReason", "?")
                raise RuntimeError(f"No text in response (finishReason={fr}): "
                                   f"{json.dumps(resp)[:400]}")
        except (KeyError, IndexError):
            raise RuntimeError(f"Unexpected Gemini response: {json.dumps(resp)[:500]}")
        if use_cache:
            cp.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
        return text
    raise RuntimeError("Gemini: exhausted retries")


def check() -> str:
    """One-shot connectivity/auth test. Bypasses cache."""
    return generate("Reply with exactly: OK", use_cache=False)
