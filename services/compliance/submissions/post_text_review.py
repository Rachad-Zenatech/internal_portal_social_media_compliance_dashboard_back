"""Direct reviewer text edits, serialized with decisions and attributed in the existing event log."""
import json
from html import escape

from fastapi import HTTPException

from models.compliance.post_text_review_model import PostTextEdit, PostTextReview
from postgresql_db.database import get_pool


def require_text_post(row):
    if row["content_type"] != "TEXT_POST":
        raise HTTPException(422, "Direct text review is available for Text Post submissions.")


async def may_edit(row, user_id):
    from services.compliance.submissions import submission_service as subs
    from services.compliance.submissions.review_handoff import is_pending
    return (row["status"] == "IN_REVIEW" and await subs._can_suggest(user_id)
            and not await is_pending(row)
            and (row["created_by"] != user_id or await subs._is_super_admin(user_id)))


async def edits_for(conn, submission_id):
    rows = await conn.fetch(
        """SELECT e.id::text AS id, e.actor_user_id AS reviewer_id, e.created_at, e.data,
                  coalesce(e.data->>'reviewer_name', u.full_name, 'Former reviewer') AS reviewer_name
           FROM content_submission_events e LEFT JOIN users u ON u.id = e.actor_user_id
           WHERE e.submission_id = $1 AND e.event_type IN ('POST_TEXT_EDITED', 'CAPTION_EDITED')
             AND e.data->>'platform' IS NULL ORDER BY e.id""",
        submission_id,
    )
    edits = []
    for row in rows:
        data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        edits.append(PostTextEdit(
            id=row["id"], reviewer_id=row["reviewer_id"], reviewer_name=row["reviewer_name"],
            created_at=row["created_at"], version=data["version"],
            before_text=data.get("before_text", data.get("before", "")),
            after_text=data.get("after_text", data.get("after", "")),
        ))
    return edits


def state_for(row, edits, can_edit):
    return PostTextReview(text=row.get("caption_text") or "", revision=edits[-1].id if edits else None,
                          can_edit=can_edit, edits=edits)


def require_base(row, edits, data):
    revision = edits[-1].id if edits else None
    if data.base_text != (row.get("caption_text") or "") or data.base_revision != revision:
        raise HTTPException(409, "Another reviewer changed this post. Copy your draft, load the latest text, and apply your changes again.")


def require_same_assignment(before, after):
    if not after:
        raise HTTPException(404, "Submission not found")
    if any(before.get(key) != after.get(key) for key in ("reviewer_ids", "brand_code", "brand_codes", "created_by")):
        raise HTTPException(409, "The post assignment changed. Refresh before continuing.")


async def get_review(submission_id, user_id):
    from services.compliance.submissions import submission_service as subs
    visible = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    require_text_post(visible)
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM content_submissions WHERE id = $1 AND deleted_at IS NULL FOR SHARE", submission_id)
            require_same_assignment(visible, row)
            require_text_post(row)
            return state_for(row, await edits_for(conn, submission_id), await may_edit(row, user_id))


async def save_review(submission_id, user_id, data):
    from services.compliance.submissions import submission_service as subs
    visible = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    require_text_post(visible)
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM content_submissions WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", submission_id)
            require_same_assignment(visible, row)
            require_text_post(row)
            from services.compliance.submissions.review_handoff import require_undecided
            await require_undecided(conn, row)
            if not await may_edit(row, user_id):
                raise HTTPException(403, "Only other reviewers can edit post text while it is in review.")
            edits = await edits_for(conn, submission_id)
            require_base(row, edits, data)
            if data.text == (row.get("caption_text") or ""):
                return state_for(row, edits, True)
            await write_edit(conn, row, user_id, data.text)
            updated = dict(row, caption_text=data.text)
            return state_for(updated, await edits_for(conn, submission_id), True)


async def write_edit(conn, row, user_id, text):
    from services.compliance.submissions import submission_service as subs
    name = await conn.fetchval("SELECT full_name FROM users WHERE id = $1", user_id)
    html = "<p>" + escape(text).replace("\n", "<br>") + "</p>"
    await conn.execute(
        """UPDATE content_submissions SET caption_text = $2, caption_html = $3,
                  latest_check = NULL, content_edited_at = clock_timestamp(),
                  updated_at = clock_timestamp(), updated_by = $4 WHERE id = $1""",
        row["id"], text, html, user_id,
    )
    await subs._add_event(conn, row["id"], "CAPTION_EDITED", "Reviewer edited post text", user_id, {
        "version": row["current_version"], "before_text": row.get("caption_text") or "",
        "after_text": text, "reviewer_name": name or "Reviewer",
        "before": row.get("caption_text") or "", "after": text, "platform": None,
    })


async def lock_decision(conn, row):
    """Do not let approval based on an earlier text/check race with an edit."""
    current = await conn.fetchrow(
        "SELECT status, caption_text, content_edited_at, latest_check FROM content_submissions WHERE id = $1 FOR UPDATE",
        row["id"],
    )
    if row.get("content_type") == "TEXT_POST" and current and any(
        current.get(key) != row.get(key) for key in ("caption_text", "content_edited_at", "latest_check")
    ):
        raise HTTPException(409, "The post text or its check changed. Refresh and check the latest text before deciding.")
    return current["status"] if current else None


def require_no_files(content_type):
    if content_type == "TEXT_POST":
        raise HTTPException(422, "Text posts use only a caption and source links; attachments are not allowed.")


async def lock_upload(conn, submission_id):
    row = await conn.fetchrow("SELECT content_type FROM content_submissions WHERE id = $1 FOR UPDATE", submission_id)
    require_no_files(row["content_type"])


async def require_empty_attachments(conn, row, payload):
    if payload.get("content_type") == "TEXT_POST" and await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM content_submission_files WHERE submission_id = $1 AND deleted_at IS NULL)", row["id"]
    ):
        raise HTTPException(422, "Remove attached files before changing to a Text Post.")


async def check_row(submission_id, user_id):
    """Reviewers may recheck their text edits through the existing compliance endpoint."""
    from services.compliance.submissions import submission_service as subs
    row = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    if row["content_type"] == "TEXT_POST" and await may_edit(row, user_id):
        return row
    return await subs._get_owned_row(submission_id, user_id)


async def store_check(conn, row, check):
    from datetime import datetime
    from services.compliance.submissions import media_freshness
    if row["content_type"] == "TEXT_POST" and not await media_freshness.check_still_current(
        conn, row, datetime.fromisoformat(check["checked_at"])
    ):
        raise HTTPException(409, "The post changed during the compliance check. Run it again on the latest text.")
    await conn.execute("UPDATE content_submissions SET latest_check = $2::jsonb, updated_at = NOW() WHERE id = $1",
                       row["id"], json.dumps(check))
