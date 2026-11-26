"""Recognize downloaded PDF sources independently of filename or server MIME mistakes."""
import logging
import shutil
from urllib.parse import unquote, urlsplit

from services.compliance.media import document_layout
from . import source_pdf_local, source_pdf_layout

clean = source_pdf_layout.clean
has_text = source_pdf_layout.has_text

logger = logging.getLogger(__name__)


def is_pdf(data: bytes, content_type: str, url: str) -> bool:
    if b"%PDF-" in data[:1024]:
        return True
    # Some publishers return an HTML access wall at a .pdf URL with HTTP 200.
    start = data.lstrip()[:256].lower()
    if start.startswith((b"<!doctype html", b"<html")):
        return False
    return content_type == "application/pdf" or urlsplit(url).path.lower().endswith(".pdf")


def read(data: bytes) -> str:
    if shutil.which("pdftotext") and shutil.which("pdfinfo"):
        return source_pdf_local.read(data)
    try:
        native = document_layout.pdf_to_text(data) or ""
    except Exception:
        logger.info("Could not extract readable text from a PDF source", exc_info=True)
        native = ""
    pages = source_pdf_layout.native_pages(native)
    return clean(native) if pages and all(source_pdf_layout.readable(page) for page in pages) else ""


def title(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "PDF source"
