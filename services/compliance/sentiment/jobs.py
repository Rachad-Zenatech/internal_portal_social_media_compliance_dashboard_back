"""Job lifecycle, background processing, scheduling and retrieval for sentiment analyses."""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID
from fastapi import HTTPException, status

from models.compliance.sentiment_model import (
    SentimentAnalysis,
    SentimentComparison,
    SentimentFinding,
    SentimentResult,
)
from postgresql_db.database import fetch_all, fetch_one, get_pool
from services.ai.observability import ai_usage_service
from services.ai.observability.ai_usage_service import AiLimitExceeded
from services.compliance.sentiment.config import (
    QuotaExhausted,
    ModelUnavailable,
    MAX_ATTEMPTS,
    MODEL,
    PROMPT_VERSION,
    SCHEDULE_CHECK_SEC,
    STALE_MINUTES,
    _running,
    _semaphore,
    _tasks,
)
from services.compliance.sentiment.input_builder import (
    _build_input,
    _hash,
    _json,
    _load_submission,
    _model_for,
    _transcript_pending,
)
from services.compliance.sentiment.evidence import _norm, finding_id
from services.compliance.sentiment.engine import analyse
from services.compliance.sentiment.feedback import _feedback_map

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

async def _claim(analysis_id: UUID) -> Optional[Dict[str, Any]]:
    """Atomically move an analysis to PROCESSING unless another worker already has it."""
    return await fetch_one(
        f"""
        UPDATE content_sentiment_analyses
        SET status = 'PROCESSING', attempts = attempts + 1, error = NULL, updated_at = NOW()
        WHERE id = $1
          AND (status = 'PENDING'
               OR (status = 'PROCESSING' AND updated_at < NOW() - INTERVAL '{STALE_MINUTES} minutes'))
        RETURNING submission_id, input, attempts, requested_by
        """,
        analysis_id,
    )


async def _finish(analysis_id: UUID, submission_id: UUID, actor: Optional[UUID],
                  result: Optional[SentimentResult], error: Optional[str], model: str = MODEL,
                  retry_at: Optional[datetime] = None) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE content_sentiment_analyses
                SET status = $2::varchar, result = $3::jsonb, error = $4, model = $5, retry_at = $6, updated_at = NOW(),
                    completed_at = CASE WHEN $2::varchar = 'COMPLETED' THEN NOW() ELSE NULL END
                WHERE id = $1
                """,
                analysis_id,
                "COMPLETED" if result else "FAILED",
                result.model_dump_json() if result else None,
                error,
                model,
                retry_at,
            )
            if result:
                label = result.overall_assessment.replace("_", " ").lower()
                await conn.execute(
                    """
                    INSERT INTO content_submission_events (submission_id, event_type, message, data, actor_user_id)
                    VALUES ($1, 'SENTIMENT_ANALYZED', $2, $3::jsonb, $4)
                    """,
                    submission_id,
                    f"Sentiment analysis: {label}, {len(result.findings)} finding(s)",
                    json.dumps({"analysis_id": str(analysis_id), "overall_assessment": result.overall_assessment}),
                    actor,
                )


async def run_analysis(analysis_id: UUID) -> None:
    async with _semaphore:
        claimed = await _claim(analysis_id)
        if not claimed:
            return
        submission_id = claimed["submission_id"]
        inp = _json(claimed["input"])
        model = _model_for(inp)
        try:
            result = await analyse(inp, submission_id)
            await _finish(analysis_id, submission_id, claimed["requested_by"], result, None, result.model_used or model)
            logger.info("Sentiment analysis %s: %s", analysis_id, result.overall_assessment)
        except QuotaExhausted as e:
            logger.warning("Sentiment analysis %s: daily quota for %s used up until %s", analysis_id, e.model, e.reset_at)
            await _finish(analysis_id, submission_id, claimed["requested_by"], None, str(e), model, retry_at=e.reset_at)
        except Exception as e:
            logger.warning("Sentiment analysis %s failed (attempt %s): %s", analysis_id, claimed["attempts"], e)
            await _finish(analysis_id, submission_id, claimed["requested_by"], None, str(e)[:500] or e.__class__.__name__, model)


async def _run(analysis_id: UUID) -> None:
    try:
        await run_analysis(analysis_id)
    except Exception:
        # Saving the outcome failed; the row stays claimed and resume_unfinished retries it once stale.
        logger.exception("Sentiment analysis %s couldn't be saved", analysis_id)
    finally:
        _running.discard(analysis_id)


def schedule(analysis_ids: Iterable[UUID]) -> None:
    """Start each analysis in the background unless this process is already running it."""
    for analysis_id in analysis_ids:
        if analysis_id in _running:
            continue
        _running.add(analysis_id)
        task = asyncio.create_task(_run(analysis_id), name=f"sentiment-{analysis_id}")
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def _queue_for(row: Dict[str, Any], trigger: str, requested_by: Optional[UUID],
                     analysis_id: Optional[UUID] = None) -> UUID:
    """Create (or, for a waiting row, fill in) a PENDING analysis, reusing a matching earlier result."""
    inp = await _build_input(row)
    content_hash = _hash(inp)
    existing = await fetch_one(
        """
        SELECT id FROM content_sentiment_analyses
        WHERE submission_id = $1 AND content_hash = $2 AND status IN ('PENDING', 'PROCESSING', 'COMPLETED')
          AND ($3::uuid IS NULL OR id <> $3)
        ORDER BY created_at DESC LIMIT 1
        """,
        row["id"], content_hash, analysis_id,
    )
    if existing and not analysis_id:
        return existing["id"]
    if existing and analysis_id:
        # The transcript finished and the content matches an earlier run: drop the placeholder.
        await fetch_one("DELETE FROM content_sentiment_analyses WHERE id = $1 RETURNING id", analysis_id)
        return existing["id"]
    if not analysis_id:
        # A new Gemini call (waiting rows were already counted when they were created).
        await ai_usage_service.reserve("SENTIMENT", requested_by, row["id"])
    saved = await fetch_one(
        """
        INSERT INTO content_sentiment_analyses
            (id, submission_id, version_number, trigger, status, content_hash, input, prompt_version, requested_by)
        VALUES (COALESCE($1, gen_random_uuid()), $2, $3, $4, 'PENDING', $5, $6::jsonb, $7, $8)
        ON CONFLICT (id) DO UPDATE SET
            status = 'PENDING', content_hash = EXCLUDED.content_hash, input = EXCLUDED.input,
            error = NULL, updated_at = NOW()
        RETURNING id
        """,
        analysis_id, row["id"], row["current_version"], trigger, content_hash,
        json.dumps(inp, default=str), PROMPT_VERSION, requested_by,
    )
    schedule([saved["id"]])
    return saved["id"]


async def request_analysis(submission_id: UUID, user_id: Optional[UUID], trigger: str = "MANUAL") -> UUID:
    """Start an analysis of the submission's current content (or return the matching one already done or running).
    Videos wait for their transcript first. Over the AI usage limit, a manual request raises AiLimitExceeded (429);
    any other trigger records a failed analysis instead, so reviewers can see why there isn't one."""
    try:
        return await _request(submission_id, user_id, trigger)
    except AiLimitExceeded as e:
        if trigger == "MANUAL":
            raise
        saved = await fetch_one(
            """
            INSERT INTO content_sentiment_analyses
                (submission_id, version_number, trigger, status, prompt_version, error, requested_by)
            SELECT id, current_version, $2, 'FAILED', $3, $4, $5 FROM content_submissions WHERE id = $1
            RETURNING id
            """,
            submission_id, trigger, PROMPT_VERSION, f"Not run: {e.detail}", user_id,
        )
        return saved["id"]


async def _request(submission_id: UUID, user_id: Optional[UUID], trigger: str) -> UUID:
    row = await _load_submission(submission_id)
    if not row:
        raise ValueError("Submission not found")
    if _transcript_pending(row):
        waiting = await fetch_one(
            "SELECT id FROM content_sentiment_analyses WHERE submission_id = $1 AND status = 'WAITING_TRANSCRIPT' LIMIT 1",
            submission_id,
        )
        if waiting:
            return waiting["id"]
        await ai_usage_service.reserve("SENTIMENT", user_id, submission_id)
        saved = await fetch_one(
            """
            INSERT INTO content_sentiment_analyses (submission_id, version_number, trigger, status, prompt_version, requested_by)
            VALUES ($1, $2, $3, 'WAITING_TRANSCRIPT', $4, $5)
            RETURNING id
            """,
            submission_id, row["current_version"], trigger, PROMPT_VERSION, user_id,
        )
        from services.compliance.media import transcription_service
        await transcription_service.schedule_missing([row["video_id"]])
        return saved["id"]
    return await _queue_for(row, trigger, user_id)


async def on_transcript_finished(file_id: UUID) -> None:
    """Called by the transcription job (completed or failed): start any analysis that was waiting for it."""
    try:
        waiting = await fetch_all(
            """
            SELECT a.id, a.trigger, a.requested_by FROM content_sentiment_analyses a
            JOIN content_submission_files f ON f.submission_id = a.submission_id
            WHERE f.id = $1 AND a.status = 'WAITING_TRANSCRIPT'
            """,
            file_id,
        )
        for w in waiting:
            await _resume_waiting(w)
    except Exception:
        logger.exception("Couldn't start waiting sentiment analyses for file %s", file_id)


async def _resume_waiting(waiting: Dict[str, Any]) -> None:
    sub = await fetch_one("SELECT submission_id FROM content_sentiment_analyses WHERE id = $1", waiting["id"])
    if not sub:
        return
    row = await _load_submission(sub["submission_id"])
    if row and not _transcript_pending(row):
        await _queue_for(row, waiting["trigger"], waiting["requested_by"], analysis_id=waiting["id"])


async def resume_unfinished(submission_id: UUID) -> None:
    """Restart work a restart may have interrupted: stale PENDING/PROCESSING rows, and waiting rows whose
    transcript is already done. Analyses that keep failing stop after MAX_ATTEMPTS."""
    rows = await fetch_all(
        f"""
        SELECT id, status, trigger, requested_by, attempts FROM content_sentiment_analyses
        WHERE submission_id = $1
          AND (status = 'WAITING_TRANSCRIPT'
               OR (status IN ('PENDING', 'PROCESSING') AND updated_at < NOW() - INTERVAL '{STALE_MINUTES} minutes')
               OR (status = 'PENDING' AND attempts = 0))
        """,
        submission_id,
    )
    for r in rows:
        if r["status"] == "WAITING_TRANSCRIPT":
            await _resume_waiting(r)
        elif r["attempts"] >= MAX_ATTEMPTS:
            await fetch_one(
                """
                UPDATE content_sentiment_analyses SET status = 'FAILED', updated_at = NOW(),
                       error = COALESCE(error, 'Stopped after repeated interruptions.')
                WHERE id = $1 RETURNING id
                """,
                r["id"],
            )
        else:
            if r["status"] == "PROCESSING":
                await fetch_one("UPDATE content_sentiment_analyses SET status = 'PENDING' WHERE id = $1 RETURNING id", r["id"])
            schedule([r["id"]])


def _result(row: Dict[str, Any]) -> Optional[SentimentResult]:
    """The stored result, with finding ids filled in for analyses saved before findings had them."""
    raw = _json(row["result"])
    if not raw:
        return None
    result = SentimentResult.model_validate(raw)
    for f in result.findings:
        f.id = f.id or finding_id(f)
    return result


def _to_model(row: Dict[str, Any], current_hash: Optional[str]) -> SentimentAnalysis:
    inp = _json(row.get("input")) or {}
    return SentimentAnalysis(
        id=row["id"],
        submission_id=row["submission_id"],
        version_number=row["version_number"],
        trigger=row["trigger"],
        status=row["status"],
        model=row["model"],
        prompt_version=row["prompt_version"],
        result=_result(row),
        error=row["error"],
        is_current=current_hash is None or row["content_hash"] is None or row["content_hash"] == current_hash,
        context_used=[e.get("title", "") for e in (inp.get("context") or [])],
        high_stakes=bool(inp.get("high_stakes")),
        has_video=bool(inp.get("video_file_id")),
        images=[{"index": n, "name": i.get("name") or f"Image {n}"} for n, i in enumerate(inp.get("images") or [], start=1)],
        retry_at=row.get("retry_at"),
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


async def _with_feedback(model: SentimentAnalysis, viewer: Optional[UUID]) -> SentimentAnalysis:
    if model.result and model.result.findings:
        model.feedback = await _feedback_map(model.id, viewer)
    return model


async def get_analysis(analysis_id: UUID, viewer: Optional[UUID] = None) -> SentimentAnalysis:
    row = await fetch_one("SELECT * FROM content_sentiment_analyses WHERE id = $1", analysis_id)
    return await _with_feedback(_to_model(row, None), viewer)


async def get_for_version(submission_id: UUID, version: int, viewer: Optional[UUID] = None) -> Optional[SentimentAnalysis]:
    """The most recent analysis of one version, as it was (looking back, so never flagged stale)."""
    row = await fetch_one(
        """
        SELECT * FROM content_sentiment_analyses
        WHERE submission_id = $1 AND version_number = $2
        ORDER BY created_at DESC LIMIT 1
        """,
        submission_id, version,
    )
    if not row:
        return None
    return await _with_comparison(await _with_feedback(_to_model(row, None), viewer))


def _same_finding(a: SentimentFinding, b: SentimentFinding) -> bool:
    """Whether two runs flagged the same thing: the same id, or (wording rewritten slightly, or a visual described
    differently) the same kind of finding where one's wording contains the other's."""
    if a.id and a.id == b.id:
        return True
    if a.basis != b.basis or a.image_index != b.image_index:
        return False
    x, y = _norm(a.quote), _norm(b.quote)
    if not (x and y and (x in y or y in x)):
        return False
    # Video moments must also roughly line up.
    return a.start_sec is None or b.start_sec is None or abs(a.start_sec - b.start_sec) <= 3


async def _with_comparison(model: SentimentAnalysis) -> SentimentAnalysis:
    """Attach how this analysis differs from the previous completed one of the same submission."""
    if model.status != "COMPLETED" or not model.result:
        return model
    prev = await fetch_one(
        """
        SELECT * FROM content_sentiment_analyses
        WHERE submission_id = $1 AND status = 'COMPLETED' AND id <> $2 AND created_at < $3 AND result IS NOT NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        model.submission_id, model.id, model.created_at,
    )
    before = _result(prev) if prev else None
    if not before:
        return model
    now = model.result.findings
    model.comparison = SentimentComparison(
        previous_analysis_id=prev["id"],
        previous_version=prev["version_number"],
        same_version=prev["version_number"] == model.version_number,
        previous_completed_at=prev["completed_at"],
        previous_assessment=before.overall_assessment,
        previous_concern=before.reputational_concern,
        resolved=[f for f in before.findings if not any(_same_finding(f, g) for g in now)],
        new_ids=[g.id for g in now if g.id and not any(_same_finding(f, g) for f in before.findings)],
        still_ids=[g.id for g in now if g.id and any(_same_finding(f, g) for f in before.findings)],
    )
    return model


async def get_latest(submission_id: UUID, viewer: Optional[UUID] = None) -> Optional[SentimentAnalysis]:
    """The most recent analysis, flagged stale if the content, scope or rules changed since it ran."""
    row = await fetch_one(
        "SELECT * FROM content_sentiment_analyses WHERE submission_id = $1 ORDER BY created_at DESC LIMIT 1",
        submission_id,
    )
    if not row:
        return None
    if row["status"] == "SCHEDULED":
        if await promote_due(submission_id):
            row = await fetch_one("SELECT * FROM content_sentiment_analyses WHERE id = $1", row["id"]) or row
    elif row["status"] in ("WAITING_TRANSCRIPT", "PENDING", "PROCESSING") and row["id"] not in _running:
        await resume_unfinished(submission_id)
        row = await fetch_one("SELECT * FROM content_sentiment_analyses WHERE id = $1", row["id"]) or row
    current_hash = None
    if row["status"] == "COMPLETED":
        sub = await _load_submission(submission_id)
        if sub and not _transcript_pending(sub):
            current_hash = _hash(await _build_input(sub))
    return await _with_comparison(await _with_feedback(_to_model(row, current_hash), viewer))


async def _analysis_row(submission_id: UUID, analysis_id: UUID) -> Dict[str, Any]:
    row = await fetch_one(
        "SELECT * FROM content_sentiment_analyses WHERE id = $1 AND submission_id = $2", analysis_id, submission_id
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sentiment analysis not found")
    return row


# ---------------------------------------------------------------------------
# Run when Gemini's daily limit resets
# ---------------------------------------------------------------------------

async def schedule_after_reset(submission_id: UUID, analysis_id: UUID) -> SentimentAnalysis:
    """An analysis that failed on Gemini's daily limit runs again once the limit resets. It counts as the same
    request (no new AI usage is reserved), since the failed attempt was already counted."""
    row = await _analysis_row(submission_id, analysis_id)
    if row["status"] != "FAILED" or not row.get("retry_at"):
        raise HTTPException(status_code=409, detail="Only an analysis stopped by Gemini's daily limit can be scheduled")
    updated = await fetch_one(
        """
        UPDATE content_sentiment_analyses SET status = 'SCHEDULED', updated_at = NOW()
        WHERE id = $1 AND status = 'FAILED' RETURNING *
        """,
        analysis_id,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="It changed in the meantime; refresh to see it")
    return _to_model(updated, None)


async def promote_due(submission_id: Optional[UUID] = None) -> int:
    """Start scheduled analyses whose quota reset time has passed (all of them, or one submission's)."""
    rows = await fetch_all(
        """
        UPDATE content_sentiment_analyses
        SET status = 'PENDING', attempts = 0, error = NULL, retry_at = NULL, updated_at = NOW()
        WHERE status = 'SCHEDULED' AND retry_at <= NOW() AND ($1::uuid IS NULL OR submission_id = $1)
        RETURNING id
        """,
        submission_id,
    )
    schedule([r["id"] for r in rows])
    return len(rows)


async def scheduled_runs_loop() -> None:
    """Background loop (started with the app): runs scheduled analyses once Gemini's daily limit has reset."""
    while True:
        try:
            started = await promote_due()
            if started:
                logger.info("Started %d scheduled sentiment analyses after the quota reset", started)
        except Exception:
            logger.exception("Couldn't start scheduled sentiment analyses")
        await asyncio.sleep(SCHEDULE_CHECK_SEC)

