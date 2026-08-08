"""PDF true-typing + text extraction.

Filenames/extensions in this dataset are deliberate misdirection, so we type files
by magic bytes, not extension, and extract with `pdftotext -raw -enc UTF-8`
(the -layout mode destroys the Cyrillic; -raw recovers it)."""
from __future__ import annotations
import subprocess
from pathlib import Path
from . import config


def magic(path: Path, n: int = 8) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)


def is_pdf(path: Path) -> bool:
    return magic(path, 4) == b"%PDF"


def true_type(path: Path) -> str:
    m = magic(path, 16)
    if m[:4] == b"%PDF":
        return "pdf"
    if m[:4] == b"\x00\x00\x01\x00":
        return "ico-stub"
    if m[:1] in (b"{", b"["):
        return "json"
    if m[:3] == b"\xef\xbb\xbf":
        return "utf8-text"
    try:
        head = m.decode("utf-8")
        if head.startswith("#") or "," in head:
            return "text/csv"
    except UnicodeDecodeError:
        pass
    return "other"


def extract_text(path: Path, use_cache: bool = True) -> str:
    """Extract text from a (possibly mis-extensioned) PDF. Cached by filename."""
    cache_file = config.TXT_CACHE / (path.name + ".txt")
    if use_cache and cache_file.exists():
        return cache_file.read_text(encoding="utf-8", errors="replace")
    if not is_pdf(path):
        # Not a PDF: return raw text as-is (handles the disguised CSV/MD/RU files).
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
    try:
        out = subprocess.run(
            ["pdftotext", "-raw", "-enc", "UTF-8", str(path), "-"],
            capture_output=True, timeout=120,
        )
        text = out.stdout.decode("utf-8", errors="replace")
    except FileNotFoundError:
        raise RuntimeError("pdftotext not found. Install poppler (it provides pdftotext).")
    if use_cache:
        cache_file.write_text(text, encoding="utf-8")
    return text
