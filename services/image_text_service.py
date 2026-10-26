"""Image Text Service
Reads the text shown in a submission's images (a headline on a graphic, figures, labels, fine print) with Gemini,
once per file and in the background, so the rulebook check screens it like the caption. Without it, a post whose
message is in the image is checked on its title alone.

Status lifecycle on content_submission_files.image_text_status:
NULL (not read yet) -> PROCESSING -> COMPLETED | FAILED
A finished read (either way) re-runs the check of an open submission, as a finished video transcript does.
"""

import asyncio
import json
import logging
import os
from typing import Iterable, List, Optional, Set
from uuid import UUID

import httpx
from google import genai
from google.genai import types

from postgresql_db.database import fetch_all, fetch_one, get_pool
from services import ai_observability
from services.gemini_service import is_rotatable_model_error

logger = logging.getLogger(__name__)

# The images a post shows: its main image(s) (e.g. carousel slides) and extra ones such as a cover.
IMAGE_KINDS = ("MAIN", "IMAGE")
# A claim older than this is treated as abandoned (e.g. the server restarted mid-read).
STALE_MINUTES = 10
MAX_CONCURRENT = int(os.getenv("IMAGE_TEXT_MAX_CONCURRENT", "3"))
MAX_TEXT_CHARS = 5000
# Reading text doesn't need a strong model, so this uses light models with their own daily allowance (on Gemini's
# free tier each model has its own), leaving the main models' allowance for sentiment and transcription. Tried in
# order; the next one is used when a model's limit is reached or it isn't available.
MODELS = [m.strip() for m in os.getenv(
    "IMAGE_TEXT_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite"
).split(",") if m.strip()]
# The most one read may take before the next model is tried.
TIMEOUT_SEC = int(os.getenv("IMAGE_TEXT_TIMEOUT_SEC", "60"))

_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
_running: Set[UUID] = set()
_tasks: Set[asyncio.Task] = set()
_client: Optional[genai.Client] = None

PROMPT = """This image is part of a social media post that is screened for compliance, so we need every word it shows.
Transcribe all readable text in the image exactly as written: headlines, body text, figures and amounts, dates,
labels, logos' wording, and fine print or disclaimers (fine print matters). Keep the reading order, one line per
line of text. Do not describe the image, translate, correct or summarise anything. Skip text too small or
blurred to read. If there is no readable text, return an empty string."""

RESPONSE_SCHEMA = {"type": "OBJECT", "properties": {"text": {"type": "STRING"}}, "required": ["text"]}


def is_image_file(kind: str, mime_type: str) -> bool:
    return kind in IMAGE_KINDS and (mime_type or "").startswith("image/")


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured, so text in images can't be read.")
        _client = genai.Client(api_key=api_key)
    return _client


async def _claim(file_id: UUID) -> Optional[dict]:
    """Atomically mark an image as being read unless another worker already is."""
    return await fetch_one(
        f"""
        UPDATE content_submission_files
        SET image_text_status = 'PROCESSING', image_text_error = NULL, image_text_updated_at = NOW()
        WHERE id = $1 AND deleted_at IS NULL AND mime_type LIKE 'image/%'
          AND (image_text_status IS DISTINCT FROM 'PROCESSING'
               OR image_text_updated_at < NOW() - INTERVAL '{STALE_MINUTES} minutes')
        RETURNING storage_path, mime_type, original_name
        """,
        file_id,
    )


async def _save(file_id: UUID, status: str, text: Optional[str] = None, error: Optional[str] = None) -> None:
    await get_pool().execute(
        """
        UPDATE content_submission_files
        SET image_text_status = $2, image_text = $3, image_text_error = $4, image_text_updated_at = NOW()
        WHERE id = $1
        """,
        file_id, status, text, error,
    )


async def _generate(contents) -> object:
    """Ask the first model in MODELS that answers; quota, rate-limit and unavailable-model errors move on to the next."""
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=RESPONSE_SCHEMA, temperature=0,
        # A request stuck behind "high demand" moves on to the next model instead of hanging.
        http_options=types.HttpOptions(timeout=TIMEOUT_SEC * 1000),
    )
    client = _get_client()
    last_error: Optional[Exception] = None
    for model in MODELS:
        try:
            return await ai_observability.generate(client, model=model, contents=contents, config=config, feature="IMAGE_TEXT")
        except Exception as e:
            timed_out = isinstance(e, (httpx.TimeoutException, TimeoutError)) or "timed out" in str(e).lower()
            if not timed_out and not is_rotatable_model_error(e):
                raise
            logger.info("Image text: %s unavailable (%s), trying the next model", model, str(e)[:120])
            last_error = e
    raise RuntimeError(f"No image-text model is available right now: {last_error}")


def _clean(text: str) -> str:
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    return "\n".join(line for line in lines if line)[:MAX_TEXT_CHARS]


async def read_file(file_id: UUID) -> None:
    """Read one image's text and save it, then re-check its submission if it's still open."""
    from services.sentiment_service import _image_bytes  # local imports: those modules import this one's callers
    from services.transcription_service import STORAGE_ROOT

    async with _semaphore:
        row = await _claim(file_id)
        if not row:
            return
        try:
            path = (STORAGE_ROOT / row["storage_path"]).resolve()
            if STORAGE_ROOT not in path.parents or not path.is_file():
                raise FileNotFoundError("The image file is missing from storage.")
            data, mime_type = await asyncio.to_thread(_image_bytes, path, row["mime_type"])
            response = await _generate(contents=[types.Part.from_bytes(data=data, mime_type=mime_type), PROMPT])
            text = _clean(json.loads(response.text or "{}").get("text", ""))
            await _save(file_id, "COMPLETED", text=text)
            logger.info("Read %d characters of text in image %s", len(text), file_id)
        except Exception as e:
            logger.warning("Couldn't read the text in image %s: %s", file_id, e)
            await _save(file_id, "FAILED", error=str(e)[:500] or e.__class__.__name__)
    from services import submission_service

    await submission_service.recheck_after_image_text(file_id)


async def _run(file_id: UUID) -> None:
    try:
        await read_file(file_id)
    finally:
        _running.discard(file_id)


def schedule(file_ids: Iterable[UUID]) -> None:
    """Read each image in the background, unless this process is already reading it."""
    for file_id in file_ids:
        if file_id in _running:
            continue
        _running.add(file_id)
        task = asyncio.create_task(_run(file_id), name=f"image-text-{file_id}")
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def schedule_missing(file_ids: List[UUID]) -> None:
    """Read the images among these that were never read, or whose read was abandoned (e.g. images uploaded before
    image text existed). Failed reads aren't retried automatically."""
    if not file_ids:
        return
    due = await fetch_all(
        f"""
        SELECT id FROM content_submission_files
        WHERE id = ANY($1::uuid[]) AND deleted_at IS NULL AND kind = ANY($2::text[]) AND mime_type LIKE 'image/%'
          AND (image_text_status IS NULL
               OR (image_text_status = 'PROCESSING' AND image_text_updated_at < NOW() - INTERVAL '{STALE_MINUTES} minutes'))
        """,
        list(file_ids), list(IMAGE_KINDS),
    )
    schedule(r["id"] for r in due)
