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
from services.ai.observability import ai_observability
from services.ai.core.gemini_service import is_rotatable_model_error, without_retired

logger = logging.getLogger(__name__)

# The images a post shows: its main image(s) (e.g. carousel slides) and extra ones such as a cover.
IMAGE_KINDS = ("MAIN", "IMAGE")
# A claim older than this is treated as abandoned (e.g. the server restarted mid-read).
STALE_MINUTES = 10
# On start, a read marked "reading" for this long is taken to have been cut off by the restart.
RESUME_AFTER_MINUTES = 2
MAX_CONCURRENT = int(os.getenv("IMAGE_TEXT_MAX_CONCURRENT", "3"))
MAX_TEXT_CHARS = 5000
# Reading text doesn't need a strong model, so this uses light models with their own daily allowance (on Gemini's
# free tier each model has its own), leaving the main models' allowance for sentiment and transcription. Tried in
# order; the next one is used when a model's limit is reached or it isn't available.
MODELS = without_retired([m.strip() for m in os.getenv(
    "IMAGE_TEXT_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite"
).split(",") if m.strip()])
# The most one read may take before the next model is tried.
TIMEOUT_SEC = int(os.getenv("IMAGE_TEXT_TIMEOUT_SEC", "60"))

_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
_running: Set[UUID] = set()
_tasks: Set[asyncio.Task] = set()
_client: Optional[genai.Client] = None

PROMPT = """This image is part of a social media post that is screened for compliance, so we need every word it shows.
Transcribe all readable text in the image exactly as written: headlines, body text, figures and amounts, dates,
labels, logos' wording, and fine print or disclaimers (fine print matters). Also transcribe text that is part of a
photo rather than the design: licence plates, signs, stickers, clothing, packaging and product labels, screens,
vehicles, posters, graffiti and tattoos, even when it is small, in the background or at an angle (an offensive word
on a licence plate or a T-shirt matters as much as the headline). Keep the reading order, one line per line of text,
with text in the photo after the design's own text. Do not describe the image, translate, correct or summarise
anything. Skip only text that is too blurred to read. If there is no readable text, return an empty string. Also say whether
one or more real people are visibly present (a face, body, or person shown in a photo or illustration); do not identify anyone.
For every visible face, return its rectangle as [top, left, bottom, right] in a 0-to-1000 coordinate grid. Do not
name people or infer anything about them. Return an empty faces list when no face is visible."""

RESPONSE_SCHEMA = {"type": "OBJECT", "properties": {"text": {"type": "STRING"}, "person_detected": {"type": "BOOLEAN"}, "faces": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"box": {"type": "ARRAY", "items": {"type": "INTEGER"}, "minItems": 4, "maxItems": 4}}, "required": ["box"]}}}, "required": ["text", "person_detected", "faces"]}


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
        SET image_text_status = 'PROCESSING', image_text_error = NULL, image_text_updated_at = NOW(),
            image_text_started_at = NOW()
        WHERE id = $1 AND deleted_at IS NULL AND mime_type LIKE 'image/%'
          AND (image_text_status IS DISTINCT FROM 'PROCESSING'
               OR image_text_updated_at < NOW() - INTERVAL '{STALE_MINUTES} minutes')
        RETURNING storage_path, mime_type, original_name
        """,
        file_id,
    )


async def _save(file_id: UUID, status: str, text: Optional[str] = None, error: Optional[str] = None, person_detected: Optional[bool] = None, face_boxes: Optional[List[List[int]]] = None) -> None:
    await get_pool().execute(
        """
        UPDATE content_submission_files
        SET image_text_status = $2::varchar, image_text = $3, image_text_error = $4, person_detected = $5,
            face_boxes = COALESCE($6::jsonb, face_boxes),
            face_detection_version = CASE WHEN $2::varchar = 'COMPLETED' THEN 1 ELSE face_detection_version END,
            image_text_updated_at = NOW()
        WHERE id = $1
        """,
        file_id, status, text, error, person_detected, json.dumps(face_boxes) if face_boxes is not None else None,
    )


async def _generate(contents, feature: str = "IMAGE_TEXT", response_schema=None) -> object:
    """Ask the first model in MODELS that answers; quota, rate-limit and unavailable-model errors move on to the next.
    Also used to read PDF scripts (see document_text_service), with its own feature label."""
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=response_schema or RESPONSE_SCHEMA, temperature=0,
        # A request stuck behind "high demand" moves on to the next model instead of hanging.
        http_options=types.HttpOptions(timeout=TIMEOUT_SEC * 1000),
    )
    client = _get_client()
    last_error: Optional[Exception] = None
    for model in MODELS:
        try:
            return await ai_observability.generate(client, model=model, contents=contents, config=config, feature=feature)
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


def _face_boxes(faces: object) -> List[List[int]]:
    """Keep only valid, bounded face rectangles; these are anonymous locations, never identities."""
    boxes: List[List[int]] = []
    for face in faces if isinstance(faces, list) else []:
        box = face.get("box") if isinstance(face, dict) else None
        if not isinstance(box, list) or len(box) != 4:
            continue
        try:
            top, left, bottom, right = [max(0, min(1000, int(value))) for value in box]
        except (TypeError, ValueError):
            continue
        if bottom > top and right > left:
            boxes.append([top, left, bottom, right])
        if len(boxes) == 20:
            break
    return boxes


async def read_file(file_id: UUID) -> None:
    """Read one image's text and save it, then re-check its submission if it's still open."""
    from services.compliance.analytics.sentiment_service import _image_bytes  # local imports: those modules import this one's callers
    from services.compliance.media import face_detection_service
    from services.compliance.media.transcription_service import STORAGE_ROOT
    from services.shared.storage.s3_storage_service import ensure_local_file

    async with _semaphore:
        row = await _claim(file_id)
        if not row:
            return
        try:
            path = (STORAGE_ROOT / row["storage_path"]).resolve()
            if STORAGE_ROOT not in path.parents:
                raise FileNotFoundError("The image file is missing from storage.")
            await asyncio.to_thread(ensure_local_file, path, "submissions", row["storage_path"])
            if not path.is_file():
                raise FileNotFoundError("The image file is missing from storage.")
            data, mime_type = await asyncio.to_thread(_image_bytes, path, row["mime_type"])
            local_boxes = await asyncio.to_thread(face_detection_service.detect_faces, data)
            response = await _generate(contents=[types.Part.from_bytes(data=data, mime_type=mime_type), PROMPT])
            result = json.loads(response.text or "{}")
            text = _clean(result.get("text", ""))
            face_boxes = local_boxes if local_boxes else _face_boxes(result.get("faces"))
            await _save(file_id, "COMPLETED", text=text, person_detected=bool(result.get("person_detected")) or bool(face_boxes), face_boxes=face_boxes)
            try:
                await get_pool().execute("""
                    UPDATE content_submissions s SET people_in_images_detected = (
                        SELECT BOOL_OR(person_detected) FROM content_submission_files f
                        WHERE f.submission_id = s.id AND f.deleted_at IS NULL AND f.person_detected IS NOT NULL
                    ) WHERE s.id = (SELECT submission_id FROM content_submission_files WHERE id = $1)
                """, file_id)
            except Exception as e:
                logger.warning("Couldn't refresh person detection for image %s: %s", file_id, e)
            logger.info("Read %d characters of text in image %s", len(text), file_id)
        except Exception as e:
            logger.warning("Couldn't read the text in image %s: %s", file_id, e)
            await _save(file_id, "FAILED", error=str(e)[:500] or e.__class__.__name__)
    from services.compliance.submissions import submission_service

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


async def resume_interrupted() -> int:
    """A read runs inside the server, so a restart leaves its image marked "reading" with nothing doing it. On start,
    queue those again. One that began in the last couple of minutes may still be running in another process, so it's
    left alone; the reset happens in one statement, so only one process picks each image up."""
    rows = await fetch_all(
        f"""
        UPDATE content_submission_files SET image_text_status = NULL
        WHERE image_text_status = 'PROCESSING' AND deleted_at IS NULL AND kind = ANY($1::text[])
          AND image_text_updated_at < NOW() - INTERVAL '{RESUME_AFTER_MINUTES} minutes'
        RETURNING id
        """,
        list(IMAGE_KINDS),
    )
    schedule(r["id"] for r in rows)
    return len(rows)


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
               OR (face_detection_version < 1 AND image_text_status = 'COMPLETED')
               OR (image_text_status = 'PROCESSING' AND image_text_updated_at < NOW() - INTERVAL '{STALE_MINUTES} minutes'))
        """,
        list(file_ids), list(IMAGE_KINDS),
    )
    schedule(r["id"] for r in due)
