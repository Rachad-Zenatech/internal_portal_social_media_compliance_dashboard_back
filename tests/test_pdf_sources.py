"""Downloaded PDFs feed the same source reader used by quick and full checks."""
import asyncio
from unittest.mock import AsyncMock, patch

import pymupdf
import pytest

from services.compliance.evaluation import source_fetch_service as sf, source_pdf_reader as pdf


def document():
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_textbox((40, 40, 550, 750), "Public report: drones support inspection and monitoring. " * 15)
        return doc.tobytes()


@pytest.mark.parametrize("url,kind", [
    ("https://example.com/report.pdf", "application/pdf"),
    ("https://example.com/download?id=1", "application/octet-stream"),
    ("https://example.com/report.PDF?download=1#page=2", "application/octet-stream"),
    ("https://example.com/download", "text/html"),
])
def test_pdf_text_is_read_from_downloaded_bytes(url, kind):
    with patch.object(sf, "_download", AsyncMock(return_value=(document(), kind, url))):
        result = asyncio.run(sf._fetch(url))
    assert result.ok
    assert "drones support inspection" in result.text
    assert result.content_hash and result.title


def test_html_access_page_at_pdf_url_is_not_parsed_as_pdf():
    data = b"<!doctype html><html><body>Access denied</body></html>"
    assert not pdf.is_pdf(data, "text/html", "https://example.com/report.pdf")
    with patch.object(sf, "_download", AsyncMock(return_value=(data, "text/html", "https://example.com/report.pdf"))):
        result = asyncio.run(sf._fetch("https://example.com/report.pdf"))
    assert not result.ok
    assert result.status == "EMPTY"


def test_malformed_pdf_returns_actionable_result():
    with patch.object(sf, "_download", AsyncMock(return_value=(b"%PDF-invalid", "application/pdf", "https://example.com/a"))):
        result = asyncio.run(sf._fetch("https://example.com/a"))
    assert not result.ok
    assert result.status == "UNSUPPORTED"


def test_parser_errors_do_not_crash_source_check():
    with patch.object(pdf.document_layout, "pdf_to_text", side_effect=ValueError("bad PDF")):
        assert pdf.read(b"%PDF-invalid") == ""
