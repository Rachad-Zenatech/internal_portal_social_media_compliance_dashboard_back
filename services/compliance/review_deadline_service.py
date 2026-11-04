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
from zoneinfo import ZoneInfo

from fastapi import HTTPException

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
# Timelines a reviewer can give when sending content back, in working days (weekends don't count).
REVISION_DAY_CHOICES = (1, 2, 3, 5)
# Where "working day" is counted, so a Friday send-back with 1 day is due Monday, not Saturday.
WORK_TZ = ZoneInfo(os.getenv("REVIEW_TIMEZONE", "America/Toronto"))

KIND_STATUS = {"REVIEW": "IN_REVIEW", "REVISION": "CHANGES_REQUESTED"}


def add_working_days(start: datetime, days: int) -> datetime:
    """The same time of day, this many working days later (Saturdays and Sundays are skipped)."""
    local = start.astimezone(WORK_TZ)
    added = 0
    while added < days:
        local += timedelta(days=1)
        if local.weekday() < 5:
            added += 1
    return local.astimezone(timezone.utc)


def due_date(kind: str, publish_at: Optional[datetime], now: Optional[datetime] = None,
             days: Optional[int] = None, requested: Optional[datetime] = None, exact: bool = False) -> datetime:
    """When the next step is due: the date asked for (a creator's "need approval by"), else the working days a
    reviewer chose, else the normal window; pulled in if the publish time is close. exact: a date a reviewer picked
    by hand is kept as it is, not pulled in."""
    now = now or datetime.now(timezone.utc)
    window, before_publish = (
        (REVIEW_HOURS, REVIEW_BEFORE_PUBLISH_HOURS) if kind == "REVIEW" else (REVISION_HOURS, REVISION_BEFORE_PUBLISH_HOURS)
    )
    if requested:
        due = requested
    else:
        due = add_working_days(now, days) if days else now + timedelta(hours=window)
    if exact and requested:
        return requested
    if publish_at:
        due = min(due, publish_at - timedelta(hours=before_publish))
    return max(due, now + timedelta(hours=MIN_HOURS))


def require_future(when: Optional[datetime]) -> None:
    """A deadline a reviewer picked has to still be ahead."""
    if when is not None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when <= datetime.now(timezone.utc):
            raise HTTPException(status_code=422, detail="Pick a deadline that's still ahead")


def revision_due(publish_at: Optional[datetime], days: Optional[int], at: Optional[datetime]) -> datetime:
    """When the creator has to have the requested changes done: the date the reviewer picked, else their working days
    (pulled in if the publish time is close), else the normal window."""
    require_future(at)
    return due_date("REVISION", publish_at, days=days, requested=at, exact=True)


def video_due(publish_at: Optional[datetime], days: Optional[int], at: Optional[datetime]) -> Optional[datetime]:
    """When the video is due once its script is approved: the date picked, or working days from now; None for no deadline."""
    require_future(at)
    if at:
        return due_date("REVISION", publish_at, requested=at, exact=True)
    return due_date("REVISION", publish_at, days=days) if days else None


def _when(dt: datetime) -> str:
    """e.g. "Tue Oct 27, 3:00 PM EDT", in the team's time zone."""
    local = dt.astimezone(WORK_TZ)
    return f"{local:%a %b} {local.day}, {local:%I:%M %p %Z}".replace(", 0", ", ")


async def _recipients(row: Dict[str, Any]) -> List[UUID]:
    """Reviews: every reviewer except the creator. Revisions: the creator, and for overdue ones also whoever
    asked for the changes."""
    from services.compliance import submission_service

    if row["due_kind"] == "REVIEW":
        return await submission_service._reviewer_ids(exclude=row["created_by"], brand_codes=row.get("brand_codes") or ([row["brand_code"]] if row.get("brand_code") else []))
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
    if row["due_kind"] == "VIDEO":
        if stage == "soon":
            return {"title": f"{ref} video due soon",
                    "message": f"The script for \"{title}\" is approved. Upload the video by {_when(due)}."}
        return {"title": f"{ref} video is overdue",
                "message": f"The video for \"{title}\" was due {_when(due)}. Upload it so it can be reviewed in time."}
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
               OR (s.due_kind = 'REVISION' AND s.status = 'CHANGES_REQUESTED')
               -- A script sent back, or approved and waiting for its video
               OR (s.due_kind = 'REVISION' AND s.status = 'DRAFT' AND s.script_status IN ('CHANGES_REQUESTED', 'EDITS_PROPOSED'))
               OR (s.due_kind = 'VIDEO' AND s.status = 'DRAFT' AND s.script_status = 'APPROVED'
                   AND NOT EXISTS (SELECT 1 FROM content_submission_files f
                                   WHERE f.submission_id = s.id AND f.kind = 'MAIN' AND f.deleted_at IS NULL)))
        RETURNING s.id, s.reference_code, s.title, s.created_by, s.due_at, s.due_kind, s.current_version, s.brand_code, s.brand_codes,
                  (SELECT j.decided_by FROM content_review_jobs j
                    WHERE j.submission_id = s.id AND j.version_number = s.current_version
                    ORDER BY j.created_at DESC LIMIT 1) AS requested_by
        """,
        *params,
    )
    return [dict(r) | {"stage": stage} for r in rows]


async def send_due_notices() -> int:
    """One pass: "due soon" reminders, then overdue notices. Returns how many deadlines were notified."""
    from services.compliance import submission_service

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
