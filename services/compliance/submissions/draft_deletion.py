"""Delete draft posts without leaving script-to-post foreign keys dangling."""

from fastapi import HTTPException


async def delete_record(conn, submission_id, user_id):
    """Called in the deletion transaction; return whether post review history was kept."""
    from services.compliance.submissions import submission_service as subs

    current = await conn.fetchrow(
        "SELECT status, current_version, script_status, script_send FROM content_submissions "
        "WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", submission_id,
    )
    if not current or current["status"] != "DRAFT":
        raise HTTPException(status_code=409, detail="This draft was just submitted or deleted")
    if subs._script_with_reviewer(dict(current)):
        raise HTTPException(status_code=409, detail="Its script is with a reviewer, so it can't be deleted until they reply.")
    if current["current_version"] == 0:
        # Keep the source script, but remove its reference to the post being deleted.
        # These nullable references do not cascade on delete in existing databases.
        await conn.execute("UPDATE script_document_posts SET submission_id = NULL WHERE submission_id = $1", submission_id)
        await conn.execute("UPDATE content_script_batch_items SET submission_id = NULL WHERE submission_id = $1", submission_id)
        await conn.execute("DELETE FROM content_submissions WHERE id = $1", submission_id)
        return False
    await conn.execute(
        "UPDATE content_submissions SET deleted_at = NOW(), deleted_by = $2, updated_at = NOW() WHERE id = $1",
        submission_id, user_id,
    )
    await subs._add_event(conn, submission_id, "DELETED", "Draft deleted", user_id, None)
    return True
