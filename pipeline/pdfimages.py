"""Extract embedded raster images from PDFs, stdlib-only (no poppler, no Pillow).

Why this exists: this dataset hides covenant-critical determinations inside IMAGES, where
`pdftotext` sees nothing at all. Four documents do it, and each one changes an answer:

  f5e315b390df.pdf   ACC-7806  the ENTIRE KYC dossier, scanned (no text layer)
  6686c0493014.pdf   ACC-7802  the ownership section of an otherwise-text dossier
  2fe3878667db.pdf   ACC-7804  one-off items added back to EBITDA, + a $300k floor
  abe2474bd443.pdf   ACC-7809  which subsidiaries are "unrestricted" (<50% pledged)

Text-only extraction reports these documents as ordinary and silently drops the data, so
`find_image_docs()` flags any PDF carrying images and `dump()` writes them as PNGs for a
human to read. The images are FlateDecode with a PNG predictor, which means the inflated
stream is already `filter byte + row` per row — exactly PNG's IDAT payload — so it can be
repackaged into a valid PNG without decoding the filters or any image library.
"""
from __future__ import annotations
import re
import struct
import zlib
from pathlib import Path

from . import config

_IMG_RE = re.compile(rb"/Subtype\s*/Image")


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png(width: int, height: int, raw_rows: bytes, colors: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2 if colors == 3 else 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw_rows)) + _chunk(b"IEND", b""))


def images_in(pdf: Path) -> list[tuple[int, int, int, bytes]]:
    """Return [(width, height, colors, png_bytes)] for each extractable image."""
    raw = pdf.read_bytes()
    if raw[:4] != b"%PDF":
        return []
    out: list[tuple[int, int, int, bytes]] = []
    for m in _IMG_RE.finditer(raw):
        dict_start = raw.rfind(b"<<", 0, m.start())
        stream_kw = raw.find(b"stream", m.start())
        if dict_start < 0 or stream_kw < 0:
            continue
        hdr = raw[dict_start:stream_kw].decode("latin-1", "replace")
        w = re.search(r"/Width\s+(\d+)", hdr)
        h = re.search(r"/Height\s+(\d+)", hdr)
        if not (w and h) or "FlateDecode" not in hdr:
            continue
        width, height = int(w.group(1)), int(h.group(1))
        colors = 3 if "DeviceRGB" in hdr else 1
        body = raw[raw.find(b"\n", stream_kw) + 1:]
        try:
            data = zlib.decompressobj().decompress(body)
        except zlib.error:
            continue
        stride = 1 + width * colors          # 1 predictor byte per row
        if len(data) < stride * height:
            continue
        out.append((width, height, colors, _png(width, height, data[:stride * height], colors)))
    return out


def find_image_docs(min_pixels: int = 200_000) -> list[tuple[str, int]]:
    """Dataset files carrying images big enough to hold a table. [(name, n_images)]."""
    found = []
    for p in sorted(config.DATASET.iterdir()):
        if not p.is_file():
            continue
        imgs = [i for i in images_in(p) if i[0] * i[1] >= min_pixels]
        if imgs:
            found.append((p.name, len(imgs)))
    return found


def dump(out_dir: Path | None = None, min_pixels: int = 200_000) -> list[Path]:
    """Write every sizeable embedded image to PNG so it can actually be read."""
    out_dir = Path(out_dir or (config.CACHE / "images"))
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, _ in find_image_docs(min_pixels):
        for i, (w, h, _c, png) in enumerate(images_in(config.DATASET / name)):
            if w * h < min_pixels:
                continue
            p = out_dir / f"{name}.img{i}.png"
            p.write_bytes(png)
            written.append(p)
    return written


if __name__ == "__main__":
    docs = find_image_docs()
    print(f"PDFs carrying sizeable embedded images: {len(docs)}")
    for name, n in docs:
        print(f"  {name}  ({n} image(s))")
    paths = dump()
    print(f"\nWrote {len(paths)} PNG(s) to {config.CACHE / 'images'}")
    print("These can hold covenant-critical tables that pdftotext cannot see — read them.")
