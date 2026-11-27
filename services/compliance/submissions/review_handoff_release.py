"""Restart-safe, atomic delivery of decided posts once their suggestion deadline passes."""
import json
import logging
import asyncio
from datetime import datetime, timezone

from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)


async def release_one(conn, row, now):
    from services.compliance.submissions import submission_service as subs
    from services.compliance.submissions import review_deadline_service as deadlines
    data = row["outcome"] or {}
    data = json.loads(data) if isinstance(data, str) else data
    legacy = data.get("request")
    if legacy:
        data = {**legacy, "revision_due": legacy.get("revision_due_at")}
    planned = datetime.fromisoformat(legacy["publish_at"]) if legacy and legacy.get("publish_at") else row["publish_at"]
    changes = row["decision"] == "REQUEST_CHANGES" or bool(data.get("publish_after_changes"))
    due = datetime.fromisoformat(data["revision_due"]) if changes and data.get("revision_due") else None
    if changes and (due is None or due <= now):
        due = deadlines.due_date("REVISION", None, now=now, days=data.get("revision_days"))
    status = "CHANGES_REQUESTED" if changes else "APPROVED"
    await conn.execute("""UPDATE content_submissions SET status = $2, due_at = $3, due_kind = $4,
        publish_after_changes = $5, publish_at = $6,
        due_reminded_at = NULL, due_overdue_notified_at = NULL, updated_at = NOW() WHERE id = $1""",
        row["id"], status, due, "REVISION" if due else None,
        bool(data.get("publish_after_changes", row["publish_after_changes"])) if changes else False, planned)
    await conn.execute("""UPDATE content_review_jobs SET status = $3, completed_at = NOW()
        WHERE submission_id = $1 AND version_number = $2 AND status = 'AWAITING_REVIEW'
          AND decision IN ('APPROVE', 'REQUEST_CHANGES')""", row["id"], row["current_version"], status)
    await subs._add_event(conn, row["id"], "REVIEW_HANDOFF_RELEASED", "Suggestion deadline reached; sent to the creator",
                          row["decided_by"], {**data, "version": row["current_version"], "status": status,
                                              "decision": row["decision"], "handoff_pending": False})
    if row["created_by"] == row["decided_by"]:
        return None
    title = f"{row['reference_code']} needs changes" if changes and row["decision"] == "REQUEST_CHANGES" else f"{row['reference_code']} was approved"
    message = (f"The suggestion window for \"{row['title']}\" is complete. "
               + ("Review the suggestions, update your post and rerun checks before publishing." if changes
                  else "Review the suggestions and choose the final publishing schedule."))
    return await conn.fetchrow("""INSERT INTO notifications
        (user_id, type, title, message, link_url, entity_type, entity_id, is_read, created_at)
        VALUES ($1, 'SUBMISSION', $2, $3, $4, 'content_submission', $5, false, NOW()) RETURNING *""",
        str(row["created_by"]), title, message, f"/submissions/{row['id']}", str(row["id"]))


async def release_due():
    notices = []
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch("""
                SELECT s.*, j.decision, j.decided_by, e.data AS outcome
                FROM content_submissions s JOIN LATERAL (
                    SELECT decision, decided_by FROM content_review_jobs
                    WHERE submission_id = s.id AND version_number = s.current_version
                      AND decision IN ('APPROVE', 'REQUEST_CHANGES') AND decided_by IS NOT NULL
                    ORDER BY decided_at DESC NULLS LAST, id DESC LIMIT 1
                ) j ON true
                LEFT JOIN LATERAL (SELECT data FROM content_submission_events
                    WHERE submission_id = s.id AND event_type IN ('REVIEW_DECISION', 'REVIEW_DECISION_HELD')
                      AND data->>'version' = s.current_version::text ORDER BY id DESC LIMIT 1) e ON true
                WHERE s.status = 'IN_REVIEW' AND s.deleted_at IS NULL AND s.approval_needed_by <= NOW()
                ORDER BY s.approval_needed_by, s.id LIMIT 50 FOR UPDATE OF s SKIP LOCKED
            """)
            for row in rows:
                notice = await release_one(conn, row, datetime.now(timezone.utc))
                if notice:
                    notices.append(notice)
    if rows:
        from services.compliance.submissions import review_memory_service
        for row in rows:
            review_memory_service.remember_soon(row["id"], row["current_version"])
    if notices:
        from services.notifications.notification_service import broadcaster
        from models.notifications.notification_model import NotificationResponse
        for notice in notices:
            try:
                broadcaster.broadcast(NotificationResponse(**dict(notice)))
            except Exception:
                logger.warning("Live review-handoff notice delivery failed", exc_info=True)
    return len(rows)


async def handoff_loop():
    """Release due decisions promptly and recover them after a server restart."""
    while True:
        try:
            await release_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Review handoff release failed; retrying")
        await asyncio.sleep(5)
