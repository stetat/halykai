"""Central config: paths, env loading, model names, scenario<->account map."""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "dataset"
CACHE = ROOT / "cache"
TXT_CACHE = CACHE / "txt"
GEMINI_CACHE = CACHE / "gemini"
for d in (CACHE, TXT_CACHE, GEMINI_CACHE):
    d.mkdir(parents=True, exist_ok=True)


def _load_env(path: Path = ROOT / ".env") -> None:
    """Tiny .env loader (no dependency on python-dotenv)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_AUTH_MODE = os.environ.get("GEMINI_AUTH_MODE", "key").lower()
MODEL_FLASH = os.environ.get("GEMINI_MODEL_FLASH", "gemini-2.5-flash")
MODEL_PRO = os.environ.get("GEMINI_MODEL_PRO", "gemini-2.5-pro")
MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_INTERVAL", "4"))

# The 12 borrowers <-> 12 scenarios. Mapping is a HYPOTHESIS derived from structure
# (P1..P10 <-> ACC-7801..7810, B1/B4 <-> ACC-7201/7204). The authoritative link is the
# ledger (txn_id prefix == scenario_id); verify once the real ledger is available.
SCENARIO_TO_ACC = {
    "P1": "ACC-7801", "P2": "ACC-7802", "P3": "ACC-7803", "P4": "ACC-7804",
    "P5": "ACC-7805", "P6": "ACC-7806", "P7": "ACC-7807", "P8": "ACC-7808",
    "P9": "ACC-7809", "P10": "ACC-7810",
    "B1": "ACC-7201", "B4": "ACC-7204",
}
ACC_TO_SCENARIO = {v: k for k, v in SCENARIO_TO_ACC.items()}

# The pre-filled template doubles as the answer key for this practice release.
ANSWER_KEY = DATASET / "submission_template.json"
