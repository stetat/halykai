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


def set_scenario_map(mapping: dict[str, str]) -> None:
    """Replace the scenario<->account map for this run.

    Mutates both dicts IN PLACE rather than rebinding them. Several modules do
    `from .config import SCENARIO_TO_ACC, ACC_TO_SCENARIO`, which binds the dict objects at
    import time — rebinding here would leave those modules on the old map and produce a split
    view where the ledger resolves a borrower the engine then skips.

    The map above is the practice release's 12 borrowers. On event day the ledger is the
    authority: `ledger.discover_scenario_map` reads the real pairs out of it and calls this,
    so a dataset with different accounts, more borrowers or different scenario ids works
    without a code change."""
    SCENARIO_TO_ACC.clear()
    SCENARIO_TO_ACC.update(mapping)
    ACC_TO_SCENARIO.clear()
    ACC_TO_SCENARIO.update({v: k for k, v in mapping.items()})

# --- finding the dataset's files, wherever the archive put them ------------------------
# The practice release extracts FLAT: every PDF sits directly in dataset/. The spec's own
# dataset table does not promise that — it says the archive contains
# «`documents/` — **Одна папка** со всеми PDF-документами датасета».
#
# Every discovery path here used to be `DATASET.iterdir()` filtered by `is_file()`, which skips
# a subdirectory silently. Against a nested archive that classifies ONE document, resolves ZERO
# accounts, selects ZERO contracts, and writes 36 empty cells — a total loss, with no exception
# raised and no `!!` line that names the cause. So discovery walks the tree, and every module
# resolves bare filenames through here rather than by joining DATASET itself.
_IGNORED_DIRS = {"__MACOSX"}


def dataset_files() -> list["Path"]:
    """Every dataset file, at any depth, deterministically ordered.

    Sorted by NAME, not by path, so the same archive gives the same order whether it extracted
    flat or nested — several harnesses compare against recorded results and would otherwise
    change answer only because a folder appeared."""
    if not DATASET.exists():
        return []
    out = [p for p in DATASET.rglob("*")
           if p.is_file()
           and not p.name.startswith(".")            # .DS_Store; NB `_Thumbs.db` is a real
           and not any(d in _IGNORED_DIRS for d in p.parts)]   # contract and must be kept
    return sorted(out, key=lambda p: (p.name, str(p)))


_PATH_CACHE: dict[str, "Path"] = {}


def dataset_path(name: str) -> "Path":
    """Resolve a bare dataset filename to its real location.

    Documents are keyed by bare filename throughout the pipeline (`docmap` stores `d.name`,
    everything downstream re-joins it). That join is what breaks on a nested archive, so it
    happens exactly once, here."""
    if not _PATH_CACHE or name not in _PATH_CACHE:
        _PATH_CACHE.clear()
        for p in dataset_files():
            _PATH_CACHE.setdefault(p.name, p)
    hit = _PATH_CACHE.get(name)
    if hit is not None:
        return hit
    return DATASET / name          # let the caller raise a normal FileNotFoundError


def reset_dataset_cache() -> None:
    """Forget resolved paths (tests that point DATASET somewhere else need this)."""
    _PATH_CACHE.clear()


# The pre-filled template doubles as the answer key for this practice release.
ANSWER_KEY = dataset_path("submission_template.json")
