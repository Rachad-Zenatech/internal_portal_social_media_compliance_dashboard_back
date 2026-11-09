"""Document Text Service
Reads the text of a submission's script, and of a blog's article file, once per file and in the background, so the
rulebook check screens it like the caption. Without it, a script sent for approval on its own is checked on its
title alone.

How each format is read:
- .txt / .md: decoded as text.
- .docx: the paragraphs in the document's XML, in order (no extra library needed; a .docx is a zip of XML).
- .pdf: read by Gemini's light models, the same ones that read text in images, so scanned PDFs work too.
- .doc (Word 97-2003): can't be read reliably; the creator is asked to save it as .docx or PDF.

Status lifecycle on content_submission_files.doc_text_status:
NULL (not read yet) -> PROCESSING -> COMPLETED | FAILED
A finished read (either way) re-runs the check of an open submission, as a finished video transcript does.
"""

import asyncio
import io
import json
import logging
import os
import zipfile
from pathlib import Path
from typing import Iterable, List, Optional, Set
from uuid import UUID
from xml.etree import ElementTree

from postgresql_db.database import fetch_all, fetch_one, get_pool
from services.compliance.media import document_layout

logger = logging.getLogger(__name__)

# Files whose text is read: the script, and a blog's main file when it's a document.
DOC_EXT = {".txt", ".md", ".docx", ".pdf", ".doc"}
STALE_MINUTES = 10
# On start, a read marked "reading" for this long is taken to have been cut off by the restart.
RESUME_AFTER_MINUTES = 2
MAX_CONCURRENT = int(os.getenv("DOC_TEXT_MAX_CONCURRENT", "2"))
MAX_TEXT_CHARS = int(os.getenv("DOC_TEXT_MAX_CHARS", "30000"))
# Gemini takes inline files up to 20 MB; leave room for the request around it.
MAX_PDF_BYTES = 18 * 1024 * 1024

PDF_PROMPT = """This PDF is a script or article for a social media post that is screened for compliance, so we need
every word it contains. Transcribe all of its text exactly as written, in reading order: headings, body, speaker
lines, on-screen text notes, captions, figures and amounts, footnotes and fine print. One paragraph per line. Do not
translate, correct or summarise anything. Keep the structure with these marks: start each page with a line
"--- Page N ---" (N is the page number); put "# " before a title or heading; write each table row as one line, "| cell
| cell |", with no separator row; wrap bold wording in **double asterisks**. If there is no readable text, return an
empty string."""

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
_running: Set[UUID] = set()
_tasks: Set[asyncio.Task] = set()


class UnreadableDocument(Exception):
    """A document we can't read, with a message the creator can act on."""


def is_document_file(kind: str, name_or_path: str) -> bool:
    """Whether this file's text is read: any script, or a main file that's a document (a blog's article)."""
    ext = Path(name_or_path or "").suffix.lower()
    return ext in DOC_EXT and kind in ("SCRIPT", "MAIN")


# --- Reading each format -----------------------------------------------------------------------------------------

def _decode(data: bytes) -> str:
    """Text file bytes as text: UTF-16 when it starts with that byte-order mark, else UTF-8, else Windows-1252."""
    encodings = ("utf-16",) if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_docx(data: bytes) -> str:
    """The text of a .docx with its headings, bold wording, tables (one row per line) and page breaks marked, so it
    shows like the document (see document_layout)."""
    try:
        return document_layout.docx_to_text(data)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as e:
        raise UnreadableDocument("This file isn't a valid Word document. Save it again as .docx or PDF.") from e


def clean(text: str) -> str:
    """Whitespace tidied, runs of blank lines cut to one (so a script's scenes stay apart when it's shown and edited),
    capped at MAX_TEXT_CHARS."""
    kept: List[str] = []
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if line or (kept and kept[-1]):
            kept.append(line)
    return "\n".join(kept).strip()[:MAX_TEXT_CHARS]


def read_local(path: Path) -> Optional[str]:
    """The text of a document that's read without AI, or None for a PDF (which needs a model to read)."""
    ext = path.suffix.lower()
    if ext == ".doc":
        raise UnreadableDocument("Older Word files (.doc) can't be read. Save it as .docx or PDF and upload it again.")
    if ext == ".pdf":
        return None
    data = path.read_bytes()
    if ext == ".docx":
        return clean(read_docx(data))
    return clean(_decode(data))


async def _read_pdf(path: Path) -> str:
    from google.genai import types

    from services.compliance.media import image_text_service

    data = await asyncio.to_thread(path.read_bytes)
    # A PDF with a text layer is read directly, keeping its pages, headings and tables; only scans need the AI.
    laid_out = await asyncio.to_thread(document_layout.pdf_to_text, data)
    if laid_out:
        return clean(laid_out)
    if len(data) > MAX_PDF_BYTES:
        raise UnreadableDocument("This PDF is too large to read. Upload it as .docx, or split it.")
    response = await image_text_service._generate(
        contents=[types.Part.from_bytes(data=data, mime_type="application/pdf"), PDF_PROMPT],
        feature="DOC_TEXT",
    )
    return clean(json.loads(response.text or "{}").get("text", ""))


# --- Background reads, as for image text ---------------------------------------------------------------------------

async def _claim(file_id: UUID) -> Optional[dict]:
    """Atomically mark a document as being read unless another worker already is."""
    return await fetch_one(
        f"""
        UPDATE content_submission_files
        SET doc_text_status = 'PROCESSING', doc_text_error = NULL, doc_text_updated_at = NOW(),
            doc_text_started_at = NOW()
        WHERE id = $1 AND deleted_at IS NULL
          AND (doc_text_status IS DISTINCT FROM 'PROCESSING'
               OR doc_text_updated_at < NOW() - INTERVAL '{STALE_MINUTES} minutes')
        RETURNING storage_path, original_name
        """,
        file_id,
    )


async def _save(file_id: UUID, status: str, text: Optional[str] = None, error: Optional[str] = None) -> None:
    await get_pool().execute(
        """
        UPDATE content_submission_files
        SET doc_text_status = $2, doc_text = $3, doc_text_error = $4, doc_text_updated_at = NOW()
        WHERE id = $1
        """,
        file_id, status, text, error,
    )


async def read_file(file_id: UUID) -> None:
    """Read one document's text and save it, then re-check its submission if it's still open."""
    from services.compliance.media.transcription_service import STORAGE_ROOT  # local import: that module imports this one's callers

    async with _semaphore:
        row = await _claim(file_id)
        if not row:
            return
        try:
            path = (STORAGE_ROOT / row["storage_path"]).resolve()
            if STORAGE_ROOT not in path.parents or not path.is_file():
                raise UnreadableDocument("The file is missing from storage. Upload it again.")
            text = await asyncio.to_thread(read_local, path)
            if text is None:
                text = await _read_pdf(path)
            await _save(file_id, "COMPLETED", text=text)
            logger.info("Read %d characters of text in document %s", len(text), file_id)
        except UnreadableDocument as e:
            await _save(file_id, "FAILED", error=str(e))
        except Exception as e:
            logger.warning("Couldn't read the text in document %s: %s", file_id, e)
            await _save(file_id, "FAILED", error="The text couldn't be read right now. Remove the file and add it again "
                                                 "to retry, or paste the text into the caption.")
    from services.compliance.submissions import submission_service

    await submission_service.recheck_after_document_text(file_id)


async def _run(file_id: UUID) -> None:
    try:
        await read_file(file_id)
    finally:
        _running.discard(file_id)


def schedule(file_ids: Iterable[UUID]) -> None:
    """Read each document in the background, unless this process is already reading it."""
    for file_id in file_ids:
        if file_id in _running:
            continue
        _running.add(file_id)
        task = asyncio.create_task(_run(file_id), name=f"doc-text-{file_id}")
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def resume_interrupted() -> int:
    """A read runs inside the server, so a restart leaves its document marked "reading" with nothing doing it. On start,
    queue those again (see image_text_service.resume_interrupted)."""
    rows = await fetch_all(
        f"""
        UPDATE content_submission_files SET doc_text_status = NULL
        WHERE doc_text_status = 'PROCESSING' AND deleted_at IS NULL
          AND doc_text_updated_at < NOW() - INTERVAL '{RESUME_AFTER_MINUTES} minutes'
        RETURNING id
        """
    )
    schedule(r["id"] for r in rows)
    return len(rows)


async def schedule_missing(file_ids: List[UUID]) -> None:
    """Read the documents among these that were never read, or whose read was abandoned (e.g. scripts uploaded
    before scripts were read). Failed reads aren't retried automatically."""
    if not file_ids:
        return
    due = await fetch_all(
        f"""
        SELECT id FROM content_submission_files
        WHERE id = ANY($1::uuid[]) AND deleted_at IS NULL
          AND (doc_text_status IS NULL
               OR (doc_text_status = 'PROCESSING' AND doc_text_updated_at < NOW() - INTERVAL '{STALE_MINUTES} minutes'))
        """,
        list(file_ids),
    )
    schedule(r["id"] for r in due)


def paragraphs(text: Optional[str]) -> List[str]:
    """A read document's paragraphs, in order (one per line as stored)."""
    return [p for p in (text or "").split("\n") if p.strip()]

