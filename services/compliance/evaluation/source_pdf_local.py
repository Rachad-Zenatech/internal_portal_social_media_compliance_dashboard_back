"""Bounded source PDF extraction with Poppler, table structure and local OCR."""
import logging
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import source_pdf_layout as layout, source_pdf_ocr

logger = logging.getLogger(__name__)
_slots = threading.BoundedSemaphore(2)
MAX_PAGES = 100
MAX_OCR_PAGES = 12


def _run(args, timeout=20, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=timeout, check=False, **kwargs)
    if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
        raise ValueError("Local PDF tool could not return a bounded, readable result")
    return result.stdout


def _read(data, native):
    deadline = time.monotonic() + 60
    def run(args, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Local PDF extraction exceeded its time budget")
        return _run(args, timeout=min(20, remaining), **kwargs)
    with tempfile.TemporaryDirectory(prefix="source-pdf-local-") as folder:
        source = Path(folder) / "source.pdf"
        source.write_bytes(data)
        info = run(["pdfinfo", str(source)]).decode("utf-8", "replace")
        match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
        count = int(match.group(1)) if match else 0
        if not 0 < count <= MAX_PAGES:
            return ""
        raw = run(["pdftotext", "-layout", "-enc", "UTF-8", str(source), "-"]).decode("utf-8", "replace")
        pages = raw.split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        if len(pages) != count:
            return ""  # Never silently treat an incomplete document as fully read.
        native_parts = layout.native_pages(native)
        if len(native_parts) == count:
            pages = [native_parts[i] if layout.readable(native_parts[i]) else text for i, text in enumerate(pages)]
        scanned = source_pdf_ocr.image_pages(source, info, run)
        missing = [i for i, text in enumerate(pages) if not layout.readable(text) or i + 1 in scanned]
        if len(missing) > MAX_OCR_PAGES:
            return ""
        for index in missing:
            text = source_pdf_ocr.read_page(source, index + 1, folder, run)
            if text is None:
                return ""  # Leave incomplete or uncertain OCR to the existing AI fallback.
            pages[index] = text
        return layout.join_pages([layout.format_page(text) for text in pages])


def read(data, native=""):
    if not _slots.acquire(timeout=10):
        return ""
    try:
        return _read(data, native)
    except Exception:
        logger.info("Local PDF extraction could not finish; retaining AI fallback", exc_info=True)
        return ""
    finally:
        _slots.release()
