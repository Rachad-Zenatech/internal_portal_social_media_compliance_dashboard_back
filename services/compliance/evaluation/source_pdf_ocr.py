"""Local OCR for missing PDF pages, with word positions and confidence checks."""
import csv
import io
import os
import re
import statistics
from pathlib import Path

from . import source_pdf_layout as layout


def _blank(path):
    from PIL import Image
    with Image.open(path) as image:
        histogram = image.convert("L").histogram()
    return sum(histogram[:230]) / max(1, sum(histogram)) < .00005


def _words(tsv):
    words = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if row.get("level") != "5" or not text:
            continue
        words.append({"text": text, "confidence": float(row["conf"]),
                      **{key: int(row[key]) for key in ("left", "top", "width", "height")}})
    return words


def _aligned(words):
    unit = max(1., statistics.median(word["width"] / max(1, len(word["text"])) for word in words))
    origin = min(word["left"] for word in words)
    bands = []
    for word in sorted(words, key=lambda word: (word["top"], word["left"])):
        center = word["top"] + word["height"] / 2
        band = next((band for band in reversed(bands) if abs(band[0] - center) <= max(2, word["height"] * .5)), None)
        if band is None:
            bands.append([center, [word]])
        else:
            band[1].append(word)
    lines = []
    for _, group in sorted(bands, key=lambda band: band[0]):
        line, previous = "", None
        for word in sorted(group, key=lambda word: word["left"]):
            start = round((word["left"] - origin) / unit)
            ordinary_gap = previous is not None and word["left"] - previous["left"] - previous["width"] < unit * 2.5
            gap = 1 if ordinary_gap else max(1 if line else 0, start - len(line))
            line += " " * gap + word["text"]
            previous = word
        lines.append(line)
    return "\n".join(lines)


def image_pages(source, info, run):
    """A page-size scan still needs OCR when a digital footer makes its text layer nonempty."""
    size = re.search(r"^Page size:\s*([\d.]+) x ([\d.]+) pts", info, re.MULTILINE)
    if not size:
        return set()
    area = float(size.group(1)) * float(size.group(2)) / (72 * 72)
    scanned = set()
    for line in run(["pdfimages", "-list", str(source)]).decode("utf-8", "replace").splitlines():
        cells = line.split()
        if len(cells) < 14 or not cells[0].isdigit() or cells[2] != "image":
            continue
        width, height, xppi, yppi = (float(cells[i]) for i in (3, 4, 12, 13))
        if xppi > 0 and yppi > 0 and width * height / (xppi * yppi) >= area * .6:
            scanned.add(int(cells[0]))
    return scanned


def read_page(source, number, folder, run):
    prefix = Path(folder) / f"ocr-{number}"
    run(["pdftoppm", "-f", str(number), "-l", str(number), "-r", "200", "-scale-to", "2500",
         "-png", "-singlefile", str(source), str(prefix)])
    image = prefix.with_suffix(".png")
    if _blank(image):
        return ""  # A genuinely blank page is complete, not an extraction failure.
    output = run([os.getenv("TESSERACT_CMD", "tesseract"), str(image), "stdout", "-l", "eng", "--psm", "3", "tsv"],
                 env={**os.environ, "OMP_THREAD_LIMIT": "1"}).decode("utf-8", "replace")
    words = _words(output)
    if not words:
        return None
    confidence = sum(word["confidence"] * len(word["text"]) for word in words) / sum(len(word["text"]) for word in words)
    uncertain_number = any(any(char.isdigit() for char in word["text"]) and word["confidence"] < 50 for word in words)
    if confidence < 75 or uncertain_number:
        return None
    text = _aligned(words)
    return text if layout.readable(text) else None
