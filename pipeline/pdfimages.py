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
import json
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
    for p in config.dataset_files():
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
        for i, (w, h, _c, png) in enumerate(images_in(config.dataset_path(name))):
            if w * h < min_pixels:
                continue
            p = out_dir / f"{name}.img{i}.png"
            p.write_bytes(png)
            written.append(p)
    return written


_VISION_PROMPT = (
    "Это скан или изображение из кредитного досье казахстанского банка. Извлеки ВСЕ данные, "
    "относящиеся к проверке финансовых ковенантов, и верни СТРОГО один JSON-объект.\n"
    "Обязательно извлекай, если присутствует:\n"
    "  ownership_threshold_pct — порог владения для признания связанной стороной "
    "(«владеет N% и более … признаются связанными сторонами»)\n"
    "  holdings_pct — {название организации: доля в %} из таблицы владения. Если в сноске "
    "указано, что доля удерживается КОСВЕННО и реальные права голоса иные — верни реальные "
    "права голоса и опиши это в notes.\n"
    "  ebitda_addback_floor_usd и one_off_items_usd — {название: сумма} для разовых статей\n"
    "  pledged_threshold_pct и subsidiary_pledged_pct — {название: % активов в залоге}\n"
    "  notes — любые оговорки, сноски и условия, меняющие трактовку\n"
    "Числа возвращай как числа, без разделителей тысяч и без знака %. "
    "Не выдумывай отсутствующие поля — просто опусти их."
)


def untranscribed_image_docs(facts_path=None, min_pixels: int = 200_000) -> list[tuple[str, int]]:
    """Documents carrying a sizeable image that NOBODY has transcribed.

    This is the dataset's most dangerous failure mode and the only one that is completely
    silent: pdftotext returns nothing for an image, so a document whose ownership table or
    add-back schedule lives in a picture reads as an ordinary file with no covenant data, and
    the pipeline reports a confident wrong answer instead of an error. Four such documents were
    found by hand and live in image_facts.json. Any OTHER document with a big image is one
    nobody has looked at — worth shouting about even when no OCR is available."""
    import json as _json
    facts_path = facts_path or (config.ROOT / "image_facts.json")
    covered = ""
    if Path(facts_path).exists():
        covered = _json.dumps(_json.loads(Path(facts_path).read_text(encoding="utf-8")),
                              ensure_ascii=False)
    return [(name, n) for name, n in find_image_docs(min_pixels) if name not in covered]


def transcribe(doc: str, model: str | None = None, min_pixels: int = 200_000) -> dict:
    """Read a document's embedded images with the model's vision. Cached; costs one call.

    The hand-written entries in image_facts.json stay authoritative — they were checked against
    the pictures by eye. This exists for the images an event-day dataset carries that nobody has
    seen, where the alternative is not a worse answer but no answer at all."""
    from . import gemini
    imgs = [png for w, h, _c, png in images_in(config.dataset_path(doc))
            if w * h >= min_pixels]
    if not imgs:
        return {}
    raw = gemini.generate(_VISION_PROMPT, model=model, images=imgs,
                          json_out=True, temperature=0.0)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        out = json.loads(m.group(0)) if m else {}
    if out:
        out["source"] = f"{doc} (transcribed by model vision, NOT verified by eye)"
    return out


if __name__ == "__main__":
    docs = find_image_docs()
    print(f"PDFs carrying sizeable embedded images: {len(docs)}")
    for name, n in docs:
        print(f"  {name}  ({n} image(s))")
    paths = dump()
    print(f"\nWrote {len(paths)} PNG(s) to {config.CACHE / 'images'}")
    print("These can hold covenant-critical tables that pdftotext cannot see — read them.")
