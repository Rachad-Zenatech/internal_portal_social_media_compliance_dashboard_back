"""NDJSON streaming helper for compliance check progress with 15s keepalive."""

import asyncio
import json
import logging
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from services.ai.observability import ai_observability
from services.compliance.submissions import submission_service as svc
from services.compliance.submissions.submission_cancellation import run_until_disconnected

logger = logging.getLogger(__name__)


def stream_initial_check(submission_id: UUID, user_id: UUID, request: Request, body: Optional[Any]) -> StreamingResponse:
    """Stream AI check attempts as NDJSON and send 15s keepalives under CloudFront's 30s timeout."""
    queue: asyncio.Queue = asyncio.Queue()
    open_ = {"value": True}

    def listen(event: Dict) -> None:
        if open_["value"]:
            queue.put_nowait(event)

    async def work() -> None:
        ai_observability.attempt_listener.set(listen)
        try:
            sub = await run_until_disconnected(
                request,
                svc.run_initial_check(
                    submission_id,
                    user_id,
                    confirm_repost=bool(body and body.confirm_repost),
                    refresh_sources=bool(body and body.refresh_sources),
                ),
            )
            queue.put_nowait({"event": "done", "submission": sub.model_dump(mode="json")})
        except asyncio.CancelledError:
            queue.put_nowait({"event": "failed", "status": 499, "detail": "Compliance check stopped"})
            raise
        except HTTPException as e:
            queue.put_nowait({"event": "failed", "status": e.status_code, "detail": e.detail})
        except Exception:
            logger.exception("Streamed compliance check failed for %s", submission_id)
            queue.put_nowait({"event": "failed", "status": 500, "detail": "The compliance check couldn't run"})

    task = asyncio.create_task(work(), name=f"check-stream-{submission_id}")

    async def lines():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield "\n"
                    continue
                yield json.dumps(event, default=str) + "\n"
                if event["event"] in ("done", "failed"):
                    break
        finally:
            open_["value"] = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
