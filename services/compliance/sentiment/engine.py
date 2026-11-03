"""Gemini execution and model failover chain for sentiment analysis."""

import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types
from pydantic import ValidationError

from models.compliance.sentiment_model import Precedent, SentimentResult
from postgresql_db.database import fetch_all, fetch_one
from services.ai import ai_observability
from services.ai.ai_guardrails import screen_input
from services.ai.gemini_service import is_rotatable_model_error
from services.compliance import review_memory_service
from services.compliance.sentiment.config import (
    QuotaExhausted,
    ModelUnavailable,
    FRAME_WIDTH,
    GEMINI_IMAGE_TYPES,
    GENERATE_TIMEOUT_SEC,
    IMAGE_INLINE_MAX_BYTES,
    IMAGE_MAX_WIDTH,
    MODEL_CHAIN,
    QUOTA_TZ,
    VIDEO_READY_TIMEOUT_SEC,
    _RESPONSE_SCHEMA,
    _get_client,
)
from services.compliance.sentiment.input_builder import _model_for, _prompt
from services.compliance.sentiment.evidence import _verify

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

async def _upload_video(client: genai.Client, file_id: str) -> Optional[types.File]:
    """Upload the submission's video to Gemini so the analysis can watch it. None if it can't be attached
    (missing file, upload or processing failure); the analysis then goes ahead on the text alone."""
    from services.compliance.transcription_service import STORAGE_ROOT  # local import, as transcription imports this module

    row = await fetch_one(
        "SELECT storage_path, mime_type FROM content_submission_files WHERE id = $1 AND deleted_at IS NULL",
        UUID(file_id),
    )
    if not row:
        return None
    path = (STORAGE_ROOT / row["storage_path"]).resolve()
    if STORAGE_ROOT not in path.parents or not path.is_file():
        logger.warning("Video %s for sentiment analysis is missing from storage", file_id)
        return None
    uploaded = None
    try:
        uploaded = await asyncio.to_thread(
            client.files.upload, file=str(path), config=types.UploadFileConfig(mime_type=row["mime_type"])
        )
        waited = 0
        while uploaded.state and uploaded.state.name == "PROCESSING" and waited < VIDEO_READY_TIMEOUT_SEC:
            await asyncio.sleep(3)
            waited += 3
            uploaded = await asyncio.to_thread(client.files.get, name=uploaded.name)
        if uploaded.state and uploaded.state.name != "ACTIVE":
            raise RuntimeError(f"video is {uploaded.state.name.lower()} after {waited}s")
        return uploaded
    except Exception as e:
        logger.warning("Couldn't attach video %s to sentiment analysis: %s", file_id, e)
        if uploaded:
            await _delete_upload(client, uploaded)
        return None


async def _delete_upload(client: genai.Client, uploaded: types.File) -> None:
    try:
        await asyncio.to_thread(client.files.delete, name=uploaded.name)
    except Exception:
        pass


def _image_bytes(path: Any, mime_type: str) -> Tuple[bytes, str]:
    """An image ready to send: as is when Gemini reads it and it's small, otherwise a smaller JPEG (first frame of
    a GIF)."""
    from services.compliance.canva_mock_render import _ffmpeg
    import subprocess

    if mime_type in GEMINI_IMAGE_TYPES and path.stat().st_size <= IMAGE_INLINE_MAX_BYTES:
        return path.read_bytes(), mime_type
    done = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-frames:v", "1",
         "-vf", f"scale='min({IMAGE_MAX_WIDTH},iw)':-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "3", "pipe:1"],
        capture_output=True, timeout=60, check=False,
    )
    if done.returncode != 0 or not done.stdout:
        raise RuntimeError(done.stderr.decode("utf-8", "replace")[:200] or "conversion failed")
    return done.stdout, "image/jpeg"


async def _image_parts(images: List[Dict[str, Any]]) -> List[types.Part]:
    """The post's images as prompt parts, in order. Stops at the first one that can't be read, so the numbers
    Gemini sees (IMAGE 1, 2, ...) always match the post's order."""
    from services.compliance.transcription_service import STORAGE_ROOT  # local import, as transcription imports this module

    if not images:
        return []
    # Not filtered on deleted_at: a saved test case re-runs with the images it was analysed with.
    rows = await fetch_all(
        "SELECT id, storage_path, mime_type FROM content_submission_files WHERE id = ANY($1::uuid[])",
        [UUID(i["file_id"]) for i in images],
    )
    by_id = {str(r["id"]): r for r in rows}
    parts: List[types.Part] = []
    for image in images:
        row = by_id.get(image["file_id"])
        path = (STORAGE_ROOT / row["storage_path"]).resolve() if row else None
        if not path or STORAGE_ROOT not in path.parents or not path.is_file():
            logger.warning("Image %s for sentiment analysis is missing from storage", image["file_id"])
            break
        try:
            data, mime_type = await asyncio.to_thread(_image_bytes, path, row["mime_type"])
        except Exception as e:
            logger.warning("Couldn't attach image %s to sentiment analysis: %s", image["file_id"], e)
            break
        parts.append(types.Part.from_bytes(data=data, mime_type=mime_type))
    return parts


async def analyse(inp: Dict[str, Any], submission_id: Optional[UUID] = None,
                  exclude_own_reviews: bool = False, fallback: bool = True) -> SentimentResult:
    """Run the analysis for an input built by _build_input, with the video and images attached when there are any,
    and the most similar past reviews as precedent (see review_memory_service). Retries a model on brief rate
    limits/outages and once on a malformed response; moves down the model chain when a model's daily limit is used
    up or it's unavailable (fallback=False tests exactly the input's model)."""
    client = _get_client()
    images = await _image_parts(inp.get("images") or [])
    try:
        precedents = await review_memory_service.similar(inp, submission_id, exclude_submission=exclude_own_reviews)
    except Exception as e:
        logger.warning("Couldn't look up similar past reviews (going ahead without them): %s", e)
        precedents = []
    video = await _upload_video(client, inp["video_file_id"]) if inp.get("video_file_id") else None
    try:
        result = await _generate(client, inp, video, images, precedents, fallback=fallback)
    finally:
        if video:
            await _delete_upload(client, video)
    return _with_precedents(result, precedents)


def _with_precedents(result: SentimentResult, precedents: List[Dict[str, Any]]) -> SentimentResult:
    """Record which past reviews the analysis was given, and turn findings' P1/P2 labels into those reviews'
    names (dropping any label that doesn't match one)."""
    labels = {f"P{n}": _precedent_label(p) for n, p in enumerate(precedents, start=1)}
    for f in result.findings:
        key = (f.precedent or "").strip().strip("[]").upper()
        f.precedent = labels.get(key) or (f.precedent if f.precedent in labels.values() else None)
    result.precedents = [
        Precedent(
            label=_precedent_label(p),
            submission_id=p["meta"].get("submission_id"),
            reference_code=p["meta"].get("reference_code"),
            version=p["meta"].get("version"),
            title=p.get("title"),
            decision=p["meta"].get("decision") or "",
            notes=p["meta"].get("notes"),
            decided_at=p["meta"].get("decided_at"),
            similarity=p["score"],
            same_post=p.get("same_post", False),
        )
        for p in precedents
    ]
    return result


def _is_daily_quota(e: Exception) -> bool:
    text = str(e)
    return "PerDay" in text or "free_tier" in text


def _is_timeout(e: Exception) -> bool:
    import httpx

    msg = str(e).lower()
    return (
        isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError))
        or "timed out" in msg
        or "deadline_exceeded" in msg
        or "504" in msg
        or "deadline expired" in msg
    )


def _is_unavailable(e: Exception) -> bool:
    text = str(e).lower()
    return "404" in text or "not_found" in text or "no longer available" in text or "is not found" in text


def _models_to_try(inp: Dict[str, Any], fallback: bool = True) -> List[str]:
    """The input's own model first, then the rest of the chain (unless fallback is off, e.g. to test one model)."""
    first = _model_for(inp)
    return [first] + ([m for m in MODEL_CHAIN if m != first] if fallback else [])


def next_quota_reset(now: Optional[datetime] = None) -> datetime:
    """The next midnight Pacific time, when Gemini's daily quotas reset (in UTC)."""
    local = (now or datetime.now(timezone.utc)).astimezone(QUOTA_TZ)
    midnight = datetime.combine((local + timedelta(days=1)).date(), datetime.min.time(), tzinfo=QUOTA_TZ)
    return midnight.astimezone(timezone.utc)


async def _generate(client: genai.Client, inp: Dict[str, Any], video: Optional[types.File],
                    images: Optional[List[types.Part]] = None,
                    precedents: Optional[List[Dict[str, Any]]] = None, fallback: bool = True) -> SentimentResult:
    """Analyse with the first model in the chain that has allowance left and still exists. Only when every model's
    daily limit is used up does it raise QuotaExhausted (so the run can be scheduled for after the reset)."""
    models = _models_to_try(inp, fallback)
    skipped: List[str] = []
    out_of_quota: List[str] = []
    for model in models:
        try:
            result = await _generate_with(client, model, inp, video, images, precedents)
        except QuotaExhausted:
            out_of_quota.append(model)
            skipped.append(f"{model}'s daily limit was used up")
            logger.info("Sentiment: daily limit for %s used up, trying the next model", model)
            continue
        except ModelUnavailable as e:
            skipped.append(str(e))
            logger.warning("Sentiment: %s, trying the next model", e)
            continue
        result.model_used = model
        if skipped:
            result.model_note = f"Analysed with {model} because " + "; ".join(skipped) + "."
        return result
    if out_of_quota:
        raise QuotaExhausted(", ".join(out_of_quota))
    raise RuntimeError("None of the sentiment models are available: " + ", ".join(models))


async def _generate_with(client: genai.Client, model: str, inp: Dict[str, Any], video: Optional[types.File],
                         images: Optional[List[types.Part]] = None,
                         precedents: Optional[List[Dict[str, Any]]] = None) -> SentimentResult:
    """One model: retries brief rate limits and outages, and once on malformed output."""
    images = images or []
    prompt = _prompt(inp, video_attached=video is not None, images_attached=len(images), precedents=precedents)
    contents: Any = prompt
    if images or video:
        contents = []
        for n, part in enumerate(images, start=1):
            contents += [f"IMAGE {n}:", part]
        contents += ([video] if video else []) + [prompt]
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        temperature=0,
        # Without a limit, a request queued behind "high demand" can hang indefinitely.
        http_options=types.HttpOptions(timeout=GENERATE_TIMEOUT_SEC * 1000),
    )
    # The post's own words are untrusted input: an injection attempt is recorded with the call (and the analysis
    # still runs, since a post that tries to steer the review is itself a concern for reviewers).
    transcript = inp.get("transcript")
    guard = screen_input("\n".join([
        inp.get("title") or "", inp.get("caption") or "",
        *(seg.get("text") or "" for seg in transcript if isinstance(seg, dict)),
    ] if isinstance(transcript, list) else [inp.get("title") or "", inp.get("caption") or ""]))
    last_error: Optional[Exception] = None
    for attempt in range(API_RETRIES):
        try:
            response = await ai_observability.generate(client, model=model, contents=contents, config=config,
                                                        feature="SENTIMENT", guard=guard, attempt=attempt + 1)
            result = SentimentResult.model_validate(json.loads(response.text or "{}"))
            if inp.get("video_file_id") and not video:
                result.information_gaps.append("The video couldn't be attached, so only its text and transcript were assessed.")
            expected = len(inp.get("images") or [])
            if len(images) < expected:
                result.information_gaps.append(
                    f"Only {len(images)} of the post's {expected} images could be attached, so the rest weren't looked at."
                )
            if inp.get("images_left_out"):
                result.information_gaps.append(
                    f"Only the first {MAX_IMAGES} images were looked at; {inp['images_left_out']} more weren't."
                )
            return _verify(result, inp, video_attached=video is not None, images_attached=len(images))
        except (ValueError, ValidationError) as e:
            # Malformed or schema-invalid output: one more try, then give up.
            last_error = e
            if attempt >= 1:
                break
        except Exception as e:
            last_error = e
            if _is_timeout(e):
                # It already waited the full limit; retrying the same busy model would only wait again.
                raise ModelUnavailable(model, "timed out") from e
            if not is_rotatable_model_error(e):
                raise
            if _is_daily_quota(e):
                # A daily quota won't recover within a retry: move on to the next model.
                raise QuotaExhausted(model) from e
            if _is_unavailable(e):
                # A retired or unknown model won't come back either.
                raise ModelUnavailable(model, "isn't available") from e
            await asyncio.sleep(2 ** (attempt + 1))
    if last_error is not None and is_rotatable_model_error(last_error):
        # Still overloaded or rate-limited after every retry: let the next model take over.
        raise ModelUnavailable(model, "was too busy") from last_error
    raise RuntimeError(f"Gemini didn't return a valid assessment: {last_error}")

