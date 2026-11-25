"""Show complete brand manuals directly from the uploaded PDF without AI summaries or guesses."""
import asyncio
from collections import Counter
from statistics import median

from fastapi import HTTPException

from models.compliance.brand_voice_model import BrandManualSection
from services.compliance.evaluation import brand_voice_service as voices
from services.compliance.media.pdf_process import isolated


def _normalise(text):
    return " ".join(text.split()).casefold()


def _page_lines(page):
    import pymupdf

    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    blocks = page.get_text("dict", flags=flags, sort=True)["blocks"]
    return [line for block in blocks if block.get("type") == 0 for line in block["lines"]]


def _line_text(line):
    return "".join(span["text"] for span in line["spans"]).strip()


def _title(page, lines, repeated):
    sizes = [span["size"] for line in lines for span in line["spans"] if span["text"].strip()]
    body_size = median(sizes) if sizes else 0
    for line in lines:
        text = _line_text(line)
        if not text or text.isdecimal() or _normalise(text) in repeated or len(text) > 160:
            continue
        large = any(span["size"] >= body_size * 1.2 for span in line["spans"])
        bold = any(span["flags"] & 16 for span in line["spans"])
        if large or bold or (text.isupper() and any(char.isalpha() for char in text)):
            return text
    return f"Page {page.number + 1}"


@isolated("manual_sections")
def read_sections(data: bytes):
    """Keep every page and all its text, including image-only pages and diagrams."""
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("The PDF is password protected")
        pages = [_page_lines(page) for page in document]
        counts = Counter(text for lines in pages for text in {_normalise(_line_text(line)) for line in lines})
        repeated = {text for text, count in counts.items() if count >= max(2, len(document) / 2)}
        return [BrandManualSection(page=page.number + 1, title=_title(page, pages[page.number], repeated),
                                   text=page.get_text(sort=True), width=page.rect.width, height=page.rect.height)
                for page in document]


@isolated("manual_page")
def render_page(data: bytes, number: int):
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as document:
        if number < 1 or number > len(document):
            raise HTTPException(status_code=404, detail="Manual page not found")
        page = document[number - 1]
        scale = min(2, 1600 / max(page.rect.width, page.rect.height))
        return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")


async def _manual(brand_code, user_id):
    await voices.get_voice(brand_code, user_id)
    path, _, _ = await voices.file_path(brand_code, "manual")
    return await asyncio.to_thread(path.read_bytes)


async def sections(brand_code, user_id):
    data = await _manual(brand_code, user_id)
    try:
        return await asyncio.to_thread(read_sections, data)
    except Exception as error:
        raise HTTPException(status_code=422, detail="Couldn't read this PDF. Download it or upload an unlocked PDF.") from error


async def page_image(brand_code, number, user_id):
    data = await _manual(brand_code, user_id)
    try:
        return await asyncio.to_thread(render_page, data, number)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=422, detail="Couldn't render this manual page.") from error
