"""Scan an image for source citations (URLs or named title + date references), locate the source on the web, and scrape/verify it."""

import asyncio
import io
import json
import logging
import os
import shutil
import sys
from typing import Dict, List, Optional, Set, Tuple
from uuid import UUID, uuid4

from fastapi import HTTPException
from google.genai import types

from models.compliance.source_link_model import (
    ImageSourceCitation,
    ImageSourceScanJobResponse,
    ImageSourceScanResponse,
)
from postgresql_db.database import fetch_all
from services.ai.core.gemini_service import is_rotatable_model_error
from services.ai.observability import ai_observability
from services.compliance.evaluation import citation_finder
from services.compliance.media import image_text_service
from services.compliance.submissions import image_source_checks, source_link_service
# The web search lives in its own module; these names stay importable from here.
from services.compliance.submissions.image_source_search import (  # noqa: F401
    _extract_grounding_urls,
    _google_news_rss_candidates,
    _playwright_search_urls,
    _search_api_urls,
    find_source_url_on_web,
    find_source_urls_on_web,
)

logger = logging.getLogger(__name__)

MAX_JOBS = 200
_jobs: Dict[str, ImageSourceScanJobResponse] = {}
_job_tasks: Set[asyncio.Task] = set()

_EXTRACT_SOURCE_PROMPT = (
    "Look at this image and extract any cited source, reference, attribution, or publication info "
    "(including title, author/agency/publisher, month, day, year, and any URL if present). "
    "Return JSON with `source_text` (the exact citation string, or empty string if none) and "
    "`url` (the explicit URL if one is printed, or empty string)."
)
_EXTRACT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "source_text": {"type": "STRING"},
        "url": {"type": "STRING"},
    },
    "required": ["source_text", "url"],
}


def _tesseract_ocr(data: bytes) -> str:
    """Run local Tesseract OCR (matching admin's pdf_extraction_service) when pytesseract + binary are available."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    tess_cmd = (
        os.getenv("TESSERACT_CMD")
        or shutil.which("tesseract")
        or ("/usr/bin/tesseract" if sys.platform != "win32" else None)
    )
    if not tess_cmd or not os.path.isfile(tess_cmd):
        return ""
    pytesseract.pytesseract.tesseract_cmd = tess_cmd
    try:
        with Image.open(io.BytesIO(data)) as img:
            return image_text_service._clean(pytesseract.image_to_string(img))
    except Exception as e:
        logger.debug("Tesseract OCR failed, falling back to Gemini Vision: %s", e)
        return ""


async def _read_image_text(data: bytes, mime_type: str) -> str:
    """Read all visible text from an image using image_text_service's Gemini Vision setup."""
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    response = await image_text_service._generate(
        contents=[part, image_text_service.PROMPT],
        feature="IMAGE_SOURCE_OCR",
    )
    return image_text_service._clean(json.loads(response.text or "{}").get("text", ""))


async def _extract_fallback_source(data: bytes, mime_type: str) -> List[ImageSourceCitation]:
    """Fallback Gemini Vision pass when pattern matching finds no explicit 'Source:' prefix."""
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=_EXTRACT_SCHEMA,
        temperature=0,
        http_options=types.HttpOptions(timeout=image_text_service.TIMEOUT_SEC * 1000),
    )
    client = image_text_service._get_client()
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    for model in image_text_service.MODELS:
        try:
            resp = await ai_observability.generate(
                client, model=model, contents=[part, _EXTRACT_SOURCE_PROMPT],
                config=config, feature="IMAGE_SOURCE_EXTRACT",
            )
            parsed = json.loads(resp.text or "{}")
            out: List[ImageSourceCitation] = []
            url = (parsed.get("url") or "").strip()
            src = (parsed.get("source_text") or "").strip()
            if url:
                out.append(ImageSourceCitation(kind="URL", reference=url, cue="image"))
            if src:
                out.append(ImageSourceCitation(kind="NAMED", reference=src, cue="image"))
            return out
        except Exception as e:
            if not is_rotatable_model_error(e):
                logger.warning("Fallback image source extraction failed: %s", e)
                return []
    return []


async def _check_supporting_document(submission_id: UUID, reference: str, post_material: str):
    """Check if an attached SUPPORTING document on the submission matches the cited named source."""
    from models.compliance.compliance_model import ProvidedSource, SourceCheck
    from services.compliance.evaluation import source_check_service, source_passages

    rows = await fetch_all(
        """
        SELECT original_name, doc_text
        FROM content_submission_files
        WHERE submission_id = $1 AND deleted_at IS NULL
          AND kind = 'SUPPORTING' AND doc_text_status = 'COMPLETED' AND COALESCE(doc_text, '') <> ''
        ORDER BY created_at
        """,
        submission_id,
    )
    docs = [ProvidedSource(name=r["original_name"], text=r["doc_text"]) for r in rows]
    match = next((d for d in docs if source_check_service._matches(reference, d)), None)
    if not match:
        return None
    results, model = await source_check_service._compare(
        post_material, match.name, match.name, None, None, match.text,
    )
    if results is None:
        return None
    claims = await source_passages.checked_claims(results, match.text)
    return SourceCheck(
        reference=reference,
        kind="DOCUMENT",
        where="attached",
        cue="Supporting document",
        status="READ",
        title=match.name,
        claims=claims,
        model=model,
        message=None if claims else "No claim in the post turned out to depend on this supporting document.",
    )


async def _load_submission_image(
    submission_id: UUID,
    file_id: Optional[UUID] = None,
) -> Tuple[Optional[str], Optional[bytes], Optional[str]]:
    """Load already-read image_text or raw image bytes from the submission's attached image."""
    from services.compliance.analytics.sentiment_service import _image_bytes
    from services.compliance.media.transcription_service import STORAGE_ROOT

    if file_id:
        rows = await fetch_all(
            """
            SELECT storage_path, mime_type, image_text, image_text_status
            FROM content_submission_files
            WHERE submission_id = $1 AND id = $2 AND deleted_at IS NULL
              AND mime_type LIKE 'image/%'
            """,
            submission_id,
            file_id,
        )
    else:
        rows = await fetch_all(
            """
            SELECT storage_path, mime_type, image_text, image_text_status
            FROM content_submission_files
            WHERE submission_id = $1 AND deleted_at IS NULL
              AND (kind = ANY($2::text[]) OR mime_type LIKE 'image/%')
            ORDER BY CASE WHEN kind = 'MAIN' THEN 0 WHEN kind = 'IMAGE' THEN 1 ELSE 2 END, created_at
            """,
            submission_id,
            list(image_text_service.IMAGE_KINDS),
        )
    for r in rows:
        if r["image_text_status"] == "COMPLETED" and (r["image_text"] or "").strip():
            return r["image_text"], None, None
        path = (STORAGE_ROOT / r["storage_path"]).resolve()
        if STORAGE_ROOT in path.parents and path.is_file():
            data, mime = await asyncio.to_thread(_image_bytes, path, r["mime_type"])
            return None, data, mime
    return None, None, None


async def _vision_text(data: bytes, mime_type: str) -> str:
    """Gemini Vision's reading of the image, or "" when no model could read it."""
    try:
        return await _read_image_text(data, mime_type)
    except Exception as e:
        logger.info("Gemini Vision couldn't read the image: %s", str(e)[:160])
        return ""


async def _read_image(
    submission_id: UUID,
    data: Optional[bytes],
    mime_type: str,
    file_id: Optional[UUID] = None,
) -> Tuple[str, Optional[bytes], str, bool]:
    """(the image's text, its bytes, its type, whether there is an image at all).

    Tesseract is quick and good enough to spot a "Source:" line, but on designed graphics it drops large headlines and
    stat labels ("Congress", "OCT 2027", "30 DAYS"), which are the claims to check. So the text the post is compared on
    is Gemini Vision's reading whenever it can be had, with Tesseract's only as the fallback."""
    if not data:
        cached_text, data, loaded_mime = await _load_submission_image(submission_id, file_id=file_id)
        if cached_text:
            return cached_text, None, mime_type, True  # read before by image_text_service (Gemini Vision)
        if not data:
            return "", None, mime_type, False
        mime_type = loaded_mime or mime_type
    vision = await _vision_text(data, mime_type)
    if vision:
        return vision, data, mime_type, True
    return await asyncio.to_thread(_tesseract_ocr, data), data, mime_type, True


async def _find_citations(text: str, data: Optional[bytes], mime_type: str) -> List[ImageSourceCitation]:
    found = citation_finder.find(text) if text else []
    if not found and data:
        # The model's reading may have lost the small print a plain OCR pass keeps.
        ocr = await asyncio.to_thread(_tesseract_ocr, data)
        found = citation_finder.find(ocr) if ocr else []
    citations = [ImageSourceCitation(kind=c.kind, reference=c.reference, cue=c.cue) for c in found]
    if not citations and data:
        citations = await _extract_fallback_source(data, mime_type)
    return citations


async def _candidate_urls(citations: List[ImageSourceCitation]) -> List[str]:
    url_citation = next((c for c in citations if c.kind == "URL"), None)
    if url_citation:
        return [url_citation.reference]
    named = next((c for c in citations if c.kind == "NAMED"), None)
    return await find_source_urls_on_web(named.reference) if named else []


async def scan_image_and_check_source(
    submission_id: UUID,
    user_id: UUID,
    image_bytes: Optional[bytes] = None,
    mime_type: Optional[str] = None,
    file_id: Optional[UUID] = None,
) -> ImageSourceScanResponse:
    """Scan an uploaded (or attached) image for source citations, find the source online, and check the image's claims
    against up to two readable sources, the most authoritative first."""
    from services.compliance.submissions import submission_service

    row = await submission_service._get_owned_row(submission_id, user_id, manager_can_read=True)
    text, data, mime, has_image = await _read_image(row["id"], image_bytes, mime_type or "image/jpeg", file_id=file_id)
    if not has_image:
        return ImageSourceScanResponse(message="No image was uploaded or attached to this post.")
    citations = await _find_citations(text, data, mime)
    if not citations:
        return ImageSourceScanResponse(extracted_text=text, message="No source citation was found in the image.")
    reply = dict(extracted_text=text, citations=citations)

    candidate_urls = await _candidate_urls(citations)
    merged, read = (None, [])
    if candidate_urls:
        merged, read = await image_source_checks.check_candidates(submission_id, candidate_urls, user_id, text)
    if read:
        return ImageSourceScanResponse(**reply, resolved_url=read[0].url, check=merged, source_checks=read)

    # No page could be read (bot walls, nothing found): an attached Supporting Document may be the source.
    doc_check = await _check_supporting_document(submission_id, citations[0].reference, text)
    if doc_check:
        return ImageSourceScanResponse(**reply, resolved_url=merged.url if merged else None, check=doc_check)
    if not candidate_urls:
        return ImageSourceScanResponse(
            **reply, message=f"Found citation \"{citations[0].reference}\", but couldn't locate a public URL on the web. "
                             "Paste the article's text to check against it instead.",
        )
    return ImageSourceScanResponse(
        **reply, resolved_url=merged.url if merged else None, check=merged,
        message=(merged.message if merged and merged.message else "None of the sources found could be read.")
        + " You can paste the article's text to check against it instead.",
    )


async def _run_scan_job(
    job_id: str,
    submission_id: UUID,
    user_id: UUID,
    image_bytes: Optional[bytes],
    mime_type: Optional[str],
    file_id: Optional[UUID] = None,
) -> None:
    """Execute image OCR, web search, and scraping in the background and record the job result."""
    try:
        res = await scan_image_and_check_source(
            submission_id=submission_id,
            user_id=user_id,
            image_bytes=image_bytes,
            mime_type=mime_type,
            file_id=file_id,
        )
        _jobs[job_id] = ImageSourceScanJobResponse(
            job_id=job_id,
            status="COMPLETED",
            **res.model_dump(),
        )
    except Exception as e:
        logger.warning("Background image source scan job %s failed: %s", job_id, e)
        _jobs[job_id] = ImageSourceScanJobResponse(
            job_id=job_id,
            status="FAILED",
            message=str(e)[:500] or "Image source scan failed.",
        )


async def start_scan_job(
    submission_id: UUID,
    user_id: UUID,
    image_bytes: Optional[bytes] = None,
    mime_type: Optional[str] = None,
    file_id: Optional[UUID] = None,
) -> ImageSourceScanJobResponse:
    """Queue image source scanning + web scraping as a background task so the request returns immediately."""
    from services.compliance.submissions import submission_service

    await submission_service._get_owned_row(submission_id, user_id, manager_can_read=True)
    if len(_jobs) >= MAX_JOBS:
        oldest_key = next(iter(_jobs))
        _jobs.pop(oldest_key, None)

    job_id = str(uuid4())
    job = ImageSourceScanJobResponse(
        job_id=job_id,
        status="PROCESSING",
        message="Scanning image and scraping source in the background...",
    )
    _jobs[job_id] = job
    task = asyncio.create_task(
        _run_scan_job(job_id, submission_id, user_id, image_bytes, mime_type, file_id=file_id),
        name=f"image-source-scan-{job_id}",
    )
    _job_tasks.add(task)
    task.add_done_callback(_job_tasks.discard)
    return job


async def get_scan_job(submission_id: UUID, job_id: str, user_id: UUID) -> ImageSourceScanJobResponse:
    """Return the current status and result of a background image source scan job."""
    from services.compliance.submissions import submission_service

    await submission_service._get_owned_row(submission_id, user_id, manager_can_read=True)
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Image source scan job not found.")
    return job

