"""Persist manual stops and cancel sentiment work, including work on another API worker."""
import asyncio
from contextlib import suppress
from uuid import UUID

from fastapi import HTTPException
from postgresql_db.database import fetch_one

STOPPED_MESSAGE = "Stopped by user."


async def run_cancellable(analysis_id: UUID, work):
    """Watch the persisted status while the AI request is running."""
    task = asyncio.ensure_future(work)

    async def watch():
        while True:
            await asyncio.sleep(1)
            row = await fetch_one("SELECT status FROM content_sentiment_analyses WHERE id = $1", analysis_id)
            if not row or row["status"] != "PROCESSING":
                return

    watcher = asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return await task
        await watcher
        raise asyncio.CancelledError()
    finally:
        task.cancel()
        watcher.cancel()
        await asyncio.gather(task, watcher, return_exceptions=True)


async def stop_analysis(submission_id: UUID, analysis_id: UUID, user_id: UUID):
    """Only the owner or a super admin may stop a draft's analysis; completed results stay intact."""
    from services.compliance.submissions import submission_service
    from services.compliance.sentiment import jobs
    from services.compliance.sentiment.config import _tasks

    row = await submission_service._get_owned_row(submission_id, user_id)
    submission_service._require_editable(row)
    saved = await fetch_one(
        """UPDATE content_sentiment_analyses
           SET status = 'FAILED', error = $3, result = NULL, retry_at = NULL, updated_at = NOW()
           WHERE id = $1 AND submission_id = $2
             AND status IN ('WAITING_TRANSCRIPT', 'PENDING', 'PROCESSING', 'SCHEDULED')
           RETURNING id""",
        analysis_id, submission_id, STOPPED_MESSAGE,
    )
    if saved:
        for task in tuple(_tasks):
            if task.get_name() == f"sentiment-{analysis_id}":
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    else:
        existing = await fetch_one(
            "SELECT id FROM content_sentiment_analyses WHERE id = $1 AND submission_id = $2",
            analysis_id, submission_id,
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Analysis not found")
    return await jobs.get_analysis(analysis_id, viewer=user_id)
