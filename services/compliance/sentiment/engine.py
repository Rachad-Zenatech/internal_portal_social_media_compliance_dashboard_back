"""Gemini execution and model failover chain for sentiment analysis."""

import asyncio
import io
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

from services.ai.core import ai_types as types
from pydantic import ValidationError

from models.compliance.sentiment_model import Precedent, SentimentResult
from postgresql_db.database import fetch_all, fetch_one
from services.ai.observability import ai_observability
from services.ai.observability.ai_guardrails import screen_input
from services.ai.core.gemini_service import is_rotatable_model_error
from services.compliance.sentiment.response_format import OutputLimitReached, parse_response
from services.compliance.submissions import review_memory_service
from services.compliance.sentiment.config import (
    API_RETRIES,
    MAX_OUTPUT_TOKENS,
    MAX_IMAGES,
    QuotaExhausted,
    ModelUnavailable,
    next_quota_reset,
    FRAME_WIDTH,
    GEMINI_IMAGE_TYPES,
    GENERATE_TIMEOUT_SEC,
    IMAGE_INLINE_MAX_BYTES,
    IMAGE_MAX_WIDTH,
    MODEL_CHAIN,
    QUOTA_TZ,
    VIDEO_READY_TIMEOUT_SEC,
    RESPONSE_SCHEMA,
    _get_client,
)
from services.compliance.sentiment.input_builder import _model_for, _precedent_label, _prompt
from services.compliance.sentiment.evidence import _verify

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

async def _upload_video(client: Any, file_id: str) -> Optional[types.File]:
    """Video upload stub for compatibility; Bedrock inspects sampled frames directly."""
    return None


async def _delete_upload(client: Any, uploaded: types.File) -> None:
    return None


def _image_bytes(path: Any, mime_type: str) -> Tuple[bytes, str]:
    """An image ready to send: as is when Gemini reads it and it's small, otherwise a smaller JPEG (first frame of
    a GIF)."""
    from services.compliance.media.canva_mock_render import _ffmpeg
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
    from services.compliance.media.transcription_service import STORAGE_ROOT  # local import, as transcription imports this module

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
    """Use the cheapest model for routine posts, and stronger models first when high_stakes=True."""
    first = _model_for(inp)
    if not fallback:
        return [first]
    if os.getenv("SENTIMENT_MODEL", "").strip() or os.getenv("SENTIMENT_MODELS", "").strip():
        return [first] + [m for m in MODEL_CHAIN if m != first]
    from services.ai.core import model_router
    routed = model_router.route("SENTIMENT", high_stakes=bool(inp.get("high_stakes"))).models
    if first and first not in ("default", "") and first not in routed:
        routed = routed + [first]
    return routed or ([first] if first else list(MODEL_CHAIN))


async def _generate(client: Any, inp: Dict[str, Any], video: Optional[types.File],
                    images: Optional[List[types.Part]] = None,
                    precedents: Optional[List[Dict[str, Any]]] = None, fallback: bool = True) -> SentimentResult:
    """Analyse with the first model in the chain that has allowance left and still exists. Only when every model's
    daily limit is used up does it raise QuotaExhausted (so the run can be scheduled for after the reset)."""
    from services.ai.core.gemini_service import model_is_available

    models = _models_to_try(inp, fallback)
    models = [m for m in models if model_is_available(m)] or models
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


async def _generate_with(client: Any, model: str, inp: Dict[str, Any], video: Optional[types.File],
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
        max_output_tokens=MAX_OUTPUT_TOKENS,
        retry_invalid_json=False,
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
            result = parse_response(response, config.max_output_tokens)
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
        except OutputLimitReached as e:
            last_error = e
            if attempt >= 1:
                break
            config.max_output_tokens = min(config.max_output_tokens * 2, max(config.max_output_tokens, 32768))
            logger.warning("Sentiment output limit reached for %s; retrying with %d tokens", model, config.max_output_tokens)
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
    if isinstance(last_error, OutputLimitReached):
        raise RuntimeError("The sentiment assessment exceeded the response limit. Retry the analysis or increase SENTIMENT_MAX_OUTPUT_TOKENS.") from last_error
    raise RuntimeError("The AI returned an incomplete sentiment assessment. Please retry the analysis.") from last_error


