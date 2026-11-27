"""Recheck creator write access while holding the row lock used by submission."""
from fastapi import HTTPException


async def lock_editable(conn, submission_id):
    row = await conn.fetchrow(
        "SELECT status FROM content_submissions WHERE id=$1 AND deleted_at IS NULL FOR UPDATE",
        submission_id,
    )
    if row is None:
        raise HTTPException(404, "Submission not found")
    if row["status"] not in ("DRAFT", "CHANGES_REQUESTED"):
        raise HTTPException(409, "This post is locked while in review or approved. Refresh to see its current status.")
