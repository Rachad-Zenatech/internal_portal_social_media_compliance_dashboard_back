"""Save a review outcome now; hand the post to its creator after suggestions close."""
from datetime import datetime, timezone

from fastapi import HTTPException
from postgresql_db.database import fetch_all, fetch_one


def deadline(row):
    value = row.get("approval_needed_by")
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def should_hold(row, now=None):
    until = deadline(row)
    return bool(until and (now or datetime.now(timezone.utc)) < until)


async def decisions(rows, conn=None):
    ids = [row["id"] for row in rows if row["status"] == "IN_REVIEW"]
    if not ids:
        return {}
    read = conn.fetch if conn else fetch_all
    jobs = await read("""
        SELECT DISTINCT ON (j.submission_id) j.submission_id, j.decided_by, j.decision
        FROM content_review_jobs j JOIN content_submissions s ON s.id = j.submission_id
        WHERE j.submission_id = ANY($1::uuid[]) AND j.version_number = s.current_version
          AND j.decision IN ('APPROVE', 'REQUEST_CHANGES') AND j.decided_by IS NOT NULL
        ORDER BY j.submission_id, j.decided_at DESC NULLS LAST, j.id DESC
    """, ids)
    return {job["submission_id"]: job for job in jobs}


async def is_pending(row, conn=None):
    return row["id"] in await decisions([row], conn)


async def require_undecided(conn, row):
    if await is_pending(row, conn):
        raise HTTPException(409, "A reviewer already decided this post. Leave suggestions until the approval deadline.")


def revision_deadline(row, request, publish_at):
    from services.compliance.submissions import review_deadline_service as deadlines
    if not should_hold(row):
        return deadlines.revision_due(publish_at, request.revision_days, request.revision_due_at)
    at = request.revision_due_at
    if at and (at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at) <= deadline(row):
        raise HTTPException(422, "The creator's changes deadline must be after the approval/suggestion deadline.")
    return deadlines.due_date("REVISION", None, now=deadline(row), days=request.revision_days, requested=at, exact=True)


async def save_outcome(conn, row, request, user_id, final_status, publish_at):
    revision_due = revision_deadline(row, request, publish_at) if final_status == "CHANGES_REQUESTED" else None
    held = should_hold(row)
    await conn.execute("""
        UPDATE content_submissions SET status = $2, publish_at = $3,
            publish_after_changes = $4, updated_at = NOW(), updated_by = $5,
            due_at = $6, due_kind = $7, due_reminded_at = NULL, due_overdue_notified_at = NULL
        WHERE id = $1
    """, row["id"], "IN_REVIEW" if held else final_status, publish_at,
        bool(request.publish_after_changes) if final_status == "CHANGES_REQUESTED" else False,
        user_id, deadline(row) if held else revision_due, "REVIEW" if held else "REVISION" if revision_due else None)
    return revision_due, held


async def notify_now(row, user_id, request, notes, revision_due, label, held):
    if held or row["created_by"] == user_id:
        return
    from services.compliance.submissions import submission_service as subs
    from services.compliance.submissions.review_deadline_service import _when
    reviewer = await fetch_one("SELECT full_name FROM users WHERE id = $1", user_id)
    who = reviewer["full_name"] if reviewer else "A reviewer"
    if request.decision == "REQUEST_CHANGES":
        title = f"{row['reference_code']} needs changes"
        message = f"{who} asked for changes to \"{row['title']}\". {notes or 'See the comments.'} Please resubmit by {_when(revision_due)}."
        link = f"/submissions/{row['id']}"
    else:
        title, message, link = f"{row['reference_code']} was approved", f"{who} approved \"{row['title']}\". {label}.", "/content-calendar"
    await subs._notify([row["created_by"]], title, message, link, row["id"])
