"""Video Transcription Service
Generates timestamped local speech and Bedrock on-screen text for submitted videos.

Status lifecycle on content_submission_files.transcript_status:
PENDING (queued) -> PROCESSING -> COMPLETED | FAILED
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable, List, Optional, Set
from uuid import UUID

from postgresql_db.database import fetch_one, get_pool
from services.compliance.media import video_transcription_provider

logger = logging.getLogger(__name__)

STORAGE_ROOT = Path(os.getenv("SUBMISSION_STORAGE_DIR", "data/uploads")).resolve()
# A claim older than this is treated as abandoned (e.g. the server restarted mid-transcription).
STALE_MINUTES = 15
MAX_CONCURRENT = int(os.getenv("TRANSCRIPTION_MAX_CONCURRENT", "2"))

_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
_running: Set[UUID] = set()
_tasks: Set[asyncio.Task] = set()

PROMPT = """This video is screened for compliance, so we need every word it says and every word it shows.
1. Transcribe every word spoken (voiceover, narration and dialogue) with source "speech".
2. Transcribe only text intentionally added to the video, such as captions, titles, lower thirds or graphics,
   with source "on_screen". Give each distinct overlay once, timed from when it appears until it disappears.
   Omit text captured by the camera, including readable signs, banners, badges, logos, posters and screens
   in the scene. Omit watermarks and platform interface text.
Split into segments of one sentence or phrase, each no longer than about 10 seconds. For each segment give
start_sec and end_sec as the number of seconds from the start of the video (decimals allowed), the exact text
and its source. Keep segments in chronological order by start_sec.
Do not describe music, sound effects or visuals in your own words.
Names that often appear in these videos (spell them exactly like this):
ZenaTech, ZenaDrone, ZenaDrone 1000, ZooOffice, Zena Studios, DaaS (Drone as a Service), B2i Digital, ZENA (Nasdaq ticker).
If the video has no speech and no readable text, return an empty array."""

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "start_sec": {"type": "NUMBER"},
            "end_sec": {"type": "NUMBER"},
            "text": {"type": "STRING"},
            "source": {"type": "STRING", "enum": ["speech", "on_screen"]},
        },
        "required": ["start_sec", "end_sec", "text", "source"],
    },
}


def _to_seconds(value: Any) -> float:
    """Accept numbers or "MM:SS(.s)" / "HH:MM:SS" strings, which models sometimes return anyway."""
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text = str(value).strip()
    if re.fullmatch(r"\d+(?::\d+){1,2}(?:\.\d+)?", text):
        seconds = 0.0
        for part in text.split(":"):
            seconds = seconds * 60 + float(part)
        return seconds
    return max(0.0, float(text))


def _normalize(raw: Any) -> List[dict]:
    segments = []
    for item in raw if isinstance(raw, list) else []:
        try:
            text = str(item["text"]).strip()
            start = _to_seconds(item["start_sec"])
            end = _to_seconds(item["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if not text:
            continue
        source = "on_screen" if str(item.get("source", "")).lower() == "on_screen" else "speech"
        segments.append({"start_sec": round(start, 2), "end_sec": round(max(end, start), 2), "text": text, "source": source})
    segments.sort(key=lambda s: s["start_sec"])
    return segments


async def _claim(file_id: UUID) -> Optional[dict]:
    """Atomically move a video to PROCESSING unless another worker is already transcribing it."""
    return await fetch_one(
        f"""
        UPDATE content_submission_files
        SET transcript_status = 'PROCESSING', transcript_error = NULL, transcript_updated_at = NOW(),
            transcript_started_at = NOW()
        WHERE id = $1 AND deleted_at IS NULL
          AND (transcript_status IS DISTINCT FROM 'PROCESSING'
               OR transcript_updated_at < NOW() - INTERVAL '{STALE_MINUTES} minutes')
        RETURNING storage_path, mime_type, original_name
        """,
        file_id,
    )


async def _save(file_id: UUID, status: str, transcript: Optional[List[dict]] = None, error: Optional[str] = None) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE content_submission_files
            SET transcript_status = $2, transcript = $3::jsonb, transcript_error = $4, transcript_updated_at = NOW()
            WHERE id = $1
            """,
            file_id,
            status,
            json.dumps(transcript) if transcript is not None else None,
            error,
        )


async def transcribe_file(file_id: UUID) -> None:
    async with _semaphore:
        row = await _claim(file_id)
        if not row:
            return
        try:
            path = (STORAGE_ROOT / row["storage_path"]).resolve()
            if STORAGE_ROOT not in path.parents or not path.is_file():
                raise FileNotFoundError("The video file is missing from storage.")
            logger.info("Transcribing %s (%s)", row["original_name"], file_id)
            raw = await video_transcription_provider.transcribe(path, row["mime_type"], PROMPT, RESPONSE_SCHEMA)
            segments = _normalize(raw)
            if raw and not segments:
                raise ValueError("The transcription response had no valid timestamped segments")
            await _save(file_id, "COMPLETED", transcript=segments)
            logger.info("Transcribed %s into %d segments", file_id, len(segments))
        except Exception as e:
            logger.warning("Transcription failed for %s: %s", file_id, e)
            await _save(file_id, "FAILED", transcript=getattr(e, "segments", None),
                        error=str(e)[:500] or e.__class__.__name__)
        # Sentiment analyses wait for the transcript; either outcome lets them run.
        from services.compliance.analytics import sentiment_service
        from services.compliance.submissions import submission_service
        await submission_service.recheck_after_transcript(file_id)
        await sentiment_service.on_transcript_finished(file_id)


async def _run(file_id: UUID) -> None:
    try:
        await transcribe_file(file_id)
    finally:
        _running.discard(file_id)


def schedule(file_ids: Iterable[UUID]) -> None:
    """Start background transcription for each video not already being transcribed by this process."""
    for file_id in file_ids:
        if file_id in _running:
            continue
        _running.add(file_id)
        task = asyncio.create_task(_run(file_id), name=f"transcribe-{file_id}")
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def schedule_missing(file_ids: List[UUID], *, retry_failed: bool = False) -> None:
    """Queue videos that were never transcribed, or whose previous attempt was abandoned."""
    if not file_ids:
        return
    from services.ai.observability import ai_usage_service
    pool = get_pool()
    async with pool.acquire() as conn:
        due = await conn.fetch(
            f"""
            SELECT id, submission_id FROM content_submission_files
            WHERE id = ANY($1::uuid[]) AND deleted_at IS NULL
              AND (transcript_status IS NULL
                   OR ($2::boolean AND transcript_status = 'FAILED')
                   OR (transcript_status IN ('PENDING', 'PROCESSING')
                       AND transcript_updated_at < NOW() - INTERVAL '{STALE_MINUTES} minutes'))
            """,
            file_ids, retry_failed,
        )
    allowed = []
    for f in due:
        try:
            # System-started, so only the company-wide daily cap applies (no person to charge).
            await ai_usage_service.reserve("TRANSCRIPTION", None, f["submission_id"])
        except ai_usage_service.AiLimitExceeded:
            logger.info("Skipping catch-up transcription of %s: daily AI limit reached", f["id"])
            break
        allowed.append(f["id"])
    if not allowed:
        return
    file_ids = allowed
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            UPDATE content_submission_files
            SET transcript_status = 'PENDING', transcript_updated_at = NOW()
            WHERE id = ANY($1::uuid[]) AND deleted_at IS NULL
              AND (transcript_status IS NULL
                   OR ($2::boolean AND transcript_status = 'FAILED')
                   OR (transcript_status IN ('PENDING', 'PROCESSING')
                       AND transcript_updated_at < NOW() - INTERVAL '{STALE_MINUTES} minutes'))
            RETURNING id
            """,
            file_ids, retry_failed,
        )
    schedule(r["id"] for r in rows)


async def retry_failed_videos(submission_id: UUID) -> None:
    """Only explicit compliance reruns retry failed transcripts; background polling does not."""
    from postgresql_db.database import fetch_all
    rows = await fetch_all("SELECT id FROM content_submission_files WHERE submission_id=$1 AND deleted_at IS NULL "
                           "AND mime_type LIKE 'video/%' AND transcript_status='FAILED'", submission_id)
    await schedule_missing([row["id"] for row in rows], retry_failed=True)
