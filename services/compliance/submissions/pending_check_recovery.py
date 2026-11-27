"""Recover explicitly requested checks whose media finished before the pending result was saved."""
import asyncio
import json
import logging
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from postgresql_db import database as db

logger = logging.getLogger(__name__)
_queued = {}
_finishing = set()
_last_attempt = {}


def decode(value):
    return json.loads(value) if isinstance(value, str) else value or {}


def same_run(before, current):
    before, current = decode(before), decode(current)
    return bool(current.get("analysis_pending") and before.get("manual_requested_at")
                and current.get("manual_requested_at") == before.get("manual_requested_at")
                and current.get("check_snapshot_id") == before.get("check_snapshot_id"))


@contextmanager
def finishing(submission_id):
    if submission_id in _finishing:
        yield False
        return
    _finishing.add(submission_id)
    try:
        yield True
    finally:
        _finishing.discard(submission_id)


def pending_files(files):
    pending, video_seen = [], False
    for f in files:
        kind, mime = f["kind"], f["mime_type"] or ""
        status = "COMPLETED"
        if kind in ("MAIN","IMAGE") and mime.startswith("image/"):
            status = f.get("image_text_status")
        elif kind == "MAIN" and mime.startswith("video/") and not video_seen:
            video_seen = True
            status = f.get("transcript_status")
        elif kind in ("MAIN","SCRIPT") and re.search(r"\.(txt|md|docx|pdf|doc)$", f["original_name"], re.I):
            status = f.get("doc_text_status")
        if status not in ("COMPLETED","FAILED"):
            pending.append(f)
    return pending


async def files_for(submission_id):
    return await db.fetch_all("SELECT id,kind,mime_type,original_name,image_text_status,doc_text_status,transcript_status "
                              "FROM content_submission_files WHERE submission_id=$1 AND deleted_at IS NULL ORDER BY created_at DESC", submission_id)


async def media_ready(submission_id):
    return not pending_files(await files_for(submission_id))


async def recover(submission_id, *, services=None):
    files = await files_for(submission_id)
    pending = pending_files(files)
    if pending:
        images = [f["id"] for f in pending if (f["mime_type"] or "").startswith("image/")]
        videos = [f["id"] for f in pending if (f["mime_type"] or "").startswith("video/")]
        documents = [f["id"] for f in pending if not (f["mime_type"] or "").startswith(("image/","video/"))]
        if images:
            from services.compliance.media import image_text_service
            await image_text_service.schedule_missing(images)
        if videos:
            from services.compliance.media import transcription_service
            await transcription_service.schedule_missing(videos)
        if documents:
            from services.compliance.media import document_text_service
            await document_text_service.schedule_missing(documents)
    elif files:
        await finish_for_file(files[0]["id"], [files[0]["kind"]], "completed media reading", services=services)


def schedule(row):
    from .manual_check_service import may_finish_check
    key = row["id"]
    if not may_finish_check(row) or key in _queued or time.monotonic()-_last_attempt.get(key,0) < 15:
        return
    if len(_last_attempt) >= 1000:
        _last_attempt.pop(next(iter(_last_attempt)))
    _last_attempt[key] = time.monotonic()
    async def run():
        try:
            await recover(key)
        except Exception:
            logger.exception("Could not recover pending compliance check %s",key)
        finally:
            _queued.pop(key,None)
    _queued[key] = asyncio.create_task(run())


async def finish_for_file(file_id, kinds, what, *, services=None):
    """Re-run the check of the open submission a file belongs to, after something about the file was read."""
    if services is None:
        from . import submission_service as subs
    else:
        subs = services
    try:
        sub = await subs.fetch_one(
            """
            SELECT s.*, """ + subs.media_freshness.REVISION_SQL + """ AS media_edited_at FROM content_submissions s
            JOIN content_submission_files f ON f.submission_id = s.id
            WHERE f.id = $1 AND f.kind = ANY($2::text[]) AND f.deleted_at IS NULL
              AND s.status IN ('DRAFT', 'IN_REVIEW', 'CHANGES_REQUESTED') AND s.deleted_at IS NULL
            """,
            file_id, kinds,
        )
        if not sub or not subs.manual_check_service.may_finish_check(sub):
            return
        if not await media_ready(sub["id"]):
            return
        with finishing(sub["id"]) as acquired:
            if not acquired:
                return
            started = datetime.now(timezone.utc)
            from services.ai.observability.attempt_progress import capture_attempts
            request_started = time.perf_counter()
            with capture_attempts() as attempts:
                result, check = await subs._run_check(sub)
            check["models"] = {"attempts":list(attempts.values()), "duration_ms":round((time.perf_counter()-request_started)*1000)}
            previous = subs.manual_check_service.saved_check(sub)
            check.update((key, previous[key]) for key in ("manual_requested_at", "checked_content_edited_at") if key in previous)
            # Tell the creator the first time the video turns out not to match their approved script.
            was_mismatch = ((subs._json(sub["latest_check"]) or {}).get("script_match") or {}).get("status") == "MISMATCH"
            newly_mismatched = ((check.get("script_match") or {}).get("status") == "MISMATCH") and not was_mismatch
            pool = subs.get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    if not await subs._check_still_current(conn, sub, started):
                        subs.logger.info("Not saving a re-check of %s: the post changed while it ran", sub["id"])
                        return
                    current = await conn.fetchval("SELECT latest_check FROM content_submissions WHERE id=$1", sub["id"])
                    if not same_run(sub["latest_check"], current):
                        return
                    await conn.execute(
                        "UPDATE content_submissions SET latest_check = $2::jsonb WHERE id = $1",
                        sub["id"], json.dumps(check),
                    )
                    await subs._mirror_to_script_version(conn, sub["id"], check)
                    await conn.execute(
                        """
                        UPDATE content_review_jobs
                        SET automated_check = $3::jsonb, score = $4, traffic_light = $5
                        WHERE submission_id = $1 AND version_number = $2 AND status = 'AWAITING_REVIEW'
                        """,
                        sub["id"], sub["current_version"], json.dumps(check), result.score, result.traffic_light,
                    )
                    summary = (
                        f"Read {what}; still reading the rest of the post before scoring"
                        if subs.analysis_pending(check)
                        else f"Re-checked with {what}: the AI review couldn't run, so there is no score yet" if check["ai_unavailable"]
                        else f"Re-checked with {what}: {result.violations_count} issue(s), score {result.score}/100"
                    )
                    await subs._add_event(conn, sub["id"], "CHECK_COMPLETED", summary, None,
                                     {"traffic_light": result.traffic_light, "score": result.score})
            if newly_mismatched:
                await subs._notify(
                    [sub["created_by"]],
                    f"{sub['reference_code']}: the video doesn't match your approved script",
                    f"\"{sub['title']}\" says something quite different from the script a reviewer approved "
                    f"({check['script_match']['similarity']}% alike). Check you uploaded the right video before sending it for review.",
                    f"/submissions/{sub['id']}",
                    sub["id"],
                )
    except Exception:
        subs.logger.exception("Couldn't re-check submission after reading %s of %s", what, file_id)
