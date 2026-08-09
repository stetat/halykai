"""Central config: paths, env loading, model names, scenario<->account map."""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The event-day archive is a different directory from the practice release, and it must not be
# merged with it: discovery walks the tree, so leaving the real data inside `dataset/` would
# route two corpora's documents to one set of borrowers. Point DATASET_DIR at the archive root
# (the folder holding `documents/` and `submission_template.json`).
DATASET = Path(os.environ.get("DATASET_DIR") or (ROOT / "dataset")).expanduser()
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


_ARCHIVE_MARKER = "submission_template.json"


def _nested_archive_roots() -> list["Path"]:
    """Sub-directories that are their own dataset, and so must not be absorbed into this one.

    Keeping the event-day archive inside the practice `dataset/` is the obvious thing to do and
    it silently merges two corpora: 516 files, two sets of borrowers, and one borrower's
    documents routed to another's cells. A dataset root is identifiable — it carries its own
    `submission_template.json` — so a directory holding one is a separate archive, and this
    dataset stops at its edge. Point DATASET_DIR at it to work on it instead."""
    if not DATASET.exists():
        return []
    return [m.parent for m in DATASET.rglob(_ARCHIVE_MARKER)
            if m.is_file() and m.parent != DATASET]


def dataset_files() -> list["Path"]:
    """Every dataset file, at any depth, deterministically ordered.

    Sorted by NAME, not by path, so the same archive gives the same order whether it extracted
    flat or nested — several harnesses compare against recorded results and would otherwise
    change answer only because a folder appeared."""
    if not DATASET.exists():
        return []
    foreign = _nested_archive_roots()
    out = [p for p in DATASET.rglob("*")
           if p.is_file()
           and not p.name.startswith(".")            # .DS_Store; NB `_Thumbs.db` is a real
           and not any(d in _IGNORED_DIRS for d in p.parts)   # contract and must be kept
           and not any(root in p.parents for root in foreign)]
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


# On the practice release the template is PRE-FILLED and doubles as the answer key. On event
# day it ships blank — same file, no answers — so anything reading it must tolerate nulls.
ANSWER_KEY = dataset_path("submission_template.json")


def submission_template() -> dict:
    """The template's scenario -> [clause ids] structure: exactly the cells we owe.

    The practice release was 12 borrowers x {6.1,6.2,6.3}. The real one is 27 borrowers, three
    of which carry a 6.4 and one (J4) whose covenants are numbered under Article 5 — so a
    hardcoded ("6.1","6.2","6.3") both invents cells nobody asked for and silently omits four
    that were asked for. The template is the authority on which cells exist."""
    import json
    try:
        data = json.loads(ANSWER_KEY.read_text(encoding="utf-8"))
    except Exception:
        return {}
    answers = data.get("answers") or data.get("scenarios") or {}
    out = {}
    for sc, v in answers.items():
        covs = v.get("covenants", v) if isinstance(v, dict) else {}
        out[sc] = sorted(covs.keys())
    return out
