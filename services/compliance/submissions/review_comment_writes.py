"""Serialize comments with approval so stale tabs cannot write after the deadline."""
from fastapi import HTTPException

from postgresql_db.database import get_pool
from services.compliance.submissions.post_approval_suggestions import require_comment


async def write(submission_id, user_id, query, *args):
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, status, current_version, created_by, approval_needed_by FROM content_submissions "
                "WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", submission_id)
            if not row:
                raise HTTPException(status_code=404, detail="Post not found")
            await require_comment(row, user_id, conn)
            return await conn.fetchrow(query, *args)
