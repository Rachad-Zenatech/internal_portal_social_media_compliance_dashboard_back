"""Review and revision deadlines.

Content in review gets a due date for the reviewer; content sent back for changes gets a due date for its creator
to resubmit. Both are pulled earlier when the planned publish time is close, so there's still time to act. A
background loop sends one "due soon" reminder and one "overdue" notice per deadline.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from postgresql_db.database import execute, fetch_all

logger = logging.getLogger(__name__)


def _hours(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, default)))
    except ValueError:
        return default


# How long reviewers have, and how long creators have to resubmit after changes are requested.
REVIEW_HOURS = _hours("REVIEW_DUE_HOURS", 24)
REVISION_HOURS = _hours("REVISION_DUE_HOURS", 24)
# Reviews finish this long before the publish time; resubmissions come early enough to be reviewed again.
REVIEW_BEFORE_PUBLISH_HOURS = _hours("REVIEW_BEFORE_PUBLISH_HOURS", 2)
REVISION_BEFORE_PUBLISH_HOURS = _hours("REVISION_BEFORE_PUBLISH_HOURS", 6)
# A "due soon" reminder goes out this long before the deadline.
REMIND_BEFORE_HOURS = _hours("DUE_REMINDER_HOURS", 4)
CHECK_EVERY_SEC = max(30.0, _hours("DUE_CHECK_MINUTES", 10) * 60)
# Never give someone less than this, even when the publish time is very close.
MIN_HOURS = 1

KIND_STATUS = {"REVIEW": "IN_REVIEW", "REVISION": "CHANGES_REQUESTED"}


def due_date(kind: str, publish_at: Optional[datetime], now: Optional[datetime] = None) -> datetime:
    """When the next step is due: the normal window, pulled in if the publish time is close."""
    now = now or datetime.now(timezone.utc)
    window, before_publish = (
        (REVIEW_HOURS, REVIEW_BEFORE_PUBLISH_HOURS) if kind == "REVIEW" else (REVISION_HOURS, REVISION_BEFORE_PUBLISH_HOURS)
    )
    due = now + timedelta(hours=window)
    if publish_at:
        due = min(due, publish_at - timedelta(hours=before_publish))
    return max(due, now + timedelta(hours=MIN_HOURS))


def _when(dt: datetime) -> str:
    return f"{dt.astimezone(timezone.utc):%b %d, %H:%M} UTC"


async def _recipients(row: Dict[str, Any]) -> List[UUID]:
    """Reviews: every reviewer except the creator. Revisions: the creator, and for overdue ones also whoever
    asked for the changes."""
    from services import submission_service

    if row["due_kind"] == "REVIEW":
        return await submission_service._reviewer_ids(exclude=row["created_by"], brand_code=row.get("brand_code"))
    ids = [row["created_by"]]
    if row.get("stage") == "overdue" and row.get("requested_by") and row["requested_by"] != row["created_by"]:
        ids.append(row["requested_by"])
    return ids


def _message(row: Dict[str, Any], stage: str) -> Dict[str, str]:
    ref, title, due = row["reference_code"], row["title"], row["due_at"]
    if row["due_kind"] == "REVIEW":
        if stage == "soon":
            return {"title": f"{ref} review due soon",
                    "message": f"\"{title}\" is waiting for review, due {_when(due)}."}
        return {"title": f"{ref} review is overdue",
                "message": f"\"{title}\" was due for review {_when(due)} and is still waiting."}
    if stage == "soon":
        return {"title": f"{ref} changes due soon",
                "message": f"Please make the requested changes to \"{title}\" and resubmit by {_when(due)}."}
    return {"title": f"{ref} changes are overdue",
            "message": f"The requested changes to \"{title}\" were due {_when(due)}. Resubmit it so it can be reviewed in time."}


async def _claim(stage: str) -> List[Dict[str, Any]]:
    """Mark deadlines that need a notice as notified and return them, in one statement, so a notice goes out
    once even if several API instances run this loop."""
    if stage == "soon":
        cond = "s.due_reminded_at IS NULL AND s.due_at > NOW() AND s.due_at <= NOW() + make_interval(secs => $1)"
        col, params = "due_reminded_at", [REMIND_BEFORE_HOURS * 3600]
    else:
        cond = "s.due_overdue_notified_at IS NULL AND s.due_at <= NOW()"
        col, params = "due_overdue_notified_at", []
    rows = await fetch_all(
        f"""
        UPDATE content_submissions s SET {col} = NOW()
        WHERE s.deleted_at IS NULL AND s.due_at IS NOT NULL AND {cond}
          AND ((s.due_kind = 'REVIEW' AND s.status = 'IN_REVIEW')
               OR (s.due_kind = 'REVISION' AND s.status = 'CHANGES_REQUESTED'))
        RETURNING s.id, s.reference_code, s.title, s.created_by, s.due_at, s.due_kind, s.current_version, s.brand_code,
                  (SELECT j.decided_by FROM content_review_jobs j
                    WHERE j.submission_id = s.id AND j.version_number = s.current_version
                    ORDER BY j.created_at DESC LIMIT 1) AS requested_by
        """,
        *params,
    )
    return [dict(r) | {"stage": stage} for r in rows]


async def send_due_notices() -> int:
    """One pass: "due soon" reminders, then overdue notices. Returns how many deadlines were notified."""
    from services import submission_service

    sent = 0
    for stage in ("soon", "overdue"):
        for row in await _claim(stage):
            text = _message(row, stage)
            await submission_service._notify(
                await _recipients(row), text["title"], text["message"], f"/submissions/{row['id']}", row["id"]
            )
            sent += 1
    return sent


async def backfill_missing() -> int:
    """Give content that was already waiting when deadlines were introduced a deadline from now, as if it had just
    arrived at its step. Runs once at start-up; does nothing once every waiting item has one."""
    rows = await fetch_all(
        """
        SELECT id, status, publish_at FROM content_submissions
        WHERE deleted_at IS NULL AND due_at IS NULL AND status IN ('IN_REVIEW', 'CHANGES_REQUESTED')
        """
    )
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    kinds = ["REVIEW" if r["status"] == "IN_REVIEW" else "REVISION" for r in rows]
    await execute(
        """
        UPDATE content_submissions s
        SET due_at = v.due_at, due_kind = v.due_kind, due_reminded_at = NULL, due_overdue_notified_at = NULL
        FROM unnest($1::uuid[], $2::timestamptz[], $3::text[]) AS v(id, due_at, due_kind)
        WHERE s.id = v.id AND s.due_at IS NULL
        """,
        [r["id"] for r in rows],
        [due_date(k, r["publish_at"], now) for k, r in zip(kinds, rows)],
        kinds,
    )
    return len(rows)


async def deadline_loop() -> None:
    logger.info("Review deadline reminders running every %.0f s", CHECK_EVERY_SEC)
    try:
        filled = await backfill_missing()
        if filled:
            logger.info("Gave %d waiting submission(s) a deadline", filled)
    except Exception:
        logger.exception("Couldn't backfill review deadlines")
    while True:
        try:
            sent = await send_due_notices()
            if sent:
                logger.info("Sent %d review deadline notice(s)", sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Review deadline check failed")
        await asyncio.sleep(CHECK_EVERY_SEC)
