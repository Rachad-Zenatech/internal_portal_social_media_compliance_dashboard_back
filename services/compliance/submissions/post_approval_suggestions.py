"""Allow suggestions until the deadline without changing the first approval."""
from datetime import datetime, timezone

from fastapi import HTTPException

from postgresql_db.database import fetch_all


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def access(row, user_id, can_suggest, approved_by, now=None):
    now = now or datetime.now(timezone.utc)
    approved = row["status"] == "APPROVED"
    deadline = utc(row.get("approval_needed_by")) if approved else None
    other = bool(approved_by and user_id != approved_by and user_id != row.get("created_by"))
    return {"suggestions_until": deadline,
            "can_suggest_after_approval": bool(approved and other and can_suggest and deadline and now < deadline),
            "may_manage": bool(approved and approved_by == user_id)}


async def approvals(rows, conn=None):
    ids = [r["id"] for r in rows if r["status"] == "APPROVED"]
    if not ids:
        return {}
    read = conn.fetch if conn else fetch_all
    decisions = await read("""
        SELECT DISTINCT ON (j.submission_id) j.submission_id, j.decided_by
        FROM content_review_jobs j JOIN content_submissions s ON s.id = j.submission_id
        WHERE j.submission_id = ANY($1::uuid[]) AND j.version_number = s.current_version
          AND j.decision = 'APPROVE' AND j.decided_by IS NOT NULL
        ORDER BY j.submission_id, j.decided_at DESC NULLS LAST, j.id DESC
    """, ids)
    return {r["submission_id"]: r["decided_by"] for r in decisions}


async def enrich(items, rows, user_id):
    from services.compliance.submissions import submission_service as subs

    owners = await approvals(rows)
    from services.compliance.submissions import review_handoff
    pending = await review_handoff.decisions(rows)
    can_suggest = await subs._can_suggest(user_id) if owners or pending else False
    now = datetime.now(timezone.utc)
    for item, row in zip(items, rows):
        if row["id"] in pending:
            item.review_decision_pending = True
            item.suggestions_until = review_handoff.deadline(row)
            item.can_suggest_after_approval = bool(can_suggest and row["created_by"] != user_id and review_handoff.should_hold(row, now))
            for name in ("can_review", "can_approve", "can_delegate", "can_manage_approval"):
                if hasattr(item, name):
                    setattr(item, name, False)
            continue
        state = access(row, user_id, can_suggest, owners.get(row["id"]), now)
        item.suggestions_until = state["suggestions_until"]
        item.can_suggest_after_approval = state["can_suggest_after_approval"]
        if row["status"] == "APPROVED":
            item.can_manage_approval = item.can_manage_approval and state["may_manage"]
    return items


async def require_manager(row, user_id):
    if (await approvals([row])).get(row["id"]) != user_id:
        raise HTTPException(status_code=403, detail="This post is already approved. Other reviewers can only leave suggestions before the approval deadline.")


async def require_comment(row, user_id, conn=None):
    from services.compliance.submissions import submission_service as subs
    from services.compliance.submissions import review_handoff

    if row["status"] == "IN_REVIEW" and row["created_by"] != user_id and await review_handoff.is_pending(row, conn):
        if not review_handoff.should_hold(row) or not await subs._can_suggest(user_id):
            raise HTTPException(403, "The suggestion deadline has passed. Wait for the creator handoff.")
        return

    if row["status"] != "APPROVED" or row["created_by"] == user_id:
        return
    owner = (await approvals([row], conn)).get(row["id"])
    if owner == user_id:
        return
    if not access(row, user_id, await subs._can_suggest(user_id), owner)["can_suggest_after_approval"]:
        raise HTTPException(status_code=403, detail="The suggestion window is closed. Comments on this approved post are read-only.")


def publishing_access(row, now=None):
    from models.compliance.post_approval_model import PublishingAccess

    now = now or datetime.now(timezone.utc)
    deadline = utc(row.get("approval_needed_by")) if row["status"] == "APPROVED" else None
    allowed = row["status"] == "APPROVED" and (deadline is None or now >= deadline)
    message = None if allowed else (
        "Publishing is available after the suggestion deadline." if deadline else "The post must be approved before publishing.")
    return PublishingAccess(can_publish=allowed, suggestions_until=deadline, message=message)


async def publishing_for_viewer(submission_id, user_id):
    from services.compliance.submissions import submission_service as subs

    row = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    if row["created_by"] != user_id and not await subs._is_super_admin(user_id):
        from models.compliance.post_approval_model import PublishingAccess

        return PublishingAccess(can_publish=False, suggestions_until=utc(row.get("approval_needed_by")),
                                message="Only the post's creator can choose its final publishing schedule.")
    return publishing_access(row)


def approval_timing(row, request, now):
    deadline = utc(row.get("approval_needed_by"))
    if deadline and now < deadline:
        return utc(request.publish_at or row["publish_at"]), "Approved; suggestions remain open until the approval deadline"
    if request.publish_now:
        return now, "Approved to publish now"
    planned = utc(request.publish_at or row["publish_at"])
    if not planned:
        raise HTTPException(status_code=422, detail="Pick a date and time to schedule it")
    if planned < now:
        raise HTTPException(status_code=422, detail="Pick a publish time in the future, or publish it now")
    return planned, f"Approved and scheduled for {planned:%b %d, %H:%M} UTC"
