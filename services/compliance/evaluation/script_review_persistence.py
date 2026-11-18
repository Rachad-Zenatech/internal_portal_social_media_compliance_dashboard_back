"""Script-stage version persistence and locking, shared by uploaded-script send paths."""

import json
from typing import Any, Dict, Optional
from uuid import UUID
from fastapi import HTTPException


async def _adopt_check(run, submission_id: UUID, check: Dict[str, Any]) -> None:
    """While a script is with the reviewer, the post's own score is the script's check, so the creator and the reviewer
    never see two different scores for the same wording. `run` is conn.execute or the pool's execute."""
    await run(
        "UPDATE content_submissions SET latest_check = coalesce(latest_check, '{}'::jsonb) || $2::jsonb WHERE id = $1",
        submission_id, json.dumps({**check, "analysis_pending": False, "documents_pending": 0}),
    )

async def _add_version(conn, row: Dict[str, Any], body: str, source: str, user_id: UUID,
                       check: Dict[str, Any], file_id: Optional[UUID] = None) -> int:
    number = await conn.fetchval(
        "SELECT coalesce(max(version_number), 0) + 1 FROM content_script_versions WHERE submission_id = $1", row["id"]
    )
    await conn.execute(
        """
        INSERT INTO content_script_versions (submission_id, version_number, body, source, file_id, check_result, created_by)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
        """,
        row["id"], number, body, source, file_id, json.dumps(check), user_id,
    )
    await _adopt_check(conn.execute, row["id"], check)
    return number

async def _lock_status(conn, submission_id: UUID, allowed: tuple) -> None:
    """Lock the post and make sure the script is still where the caller saw it (two people can act at once)."""
    current = await conn.fetchval("SELECT script_status FROM content_submissions WHERE id = $1 FOR UPDATE", submission_id)
    linked = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM script_document_posts p JOIN script_documents d ON d.id = p.document_id "
        "WHERE p.submission_id = $1 AND d.deleted_at IS NULL)", submission_id)
    if linked:
        raise HTTPException(409, "Continue this review in Script documents")
    if (current or "DRAFT") not in allowed:
        raise HTTPException(status_code=409, detail="The script's status just changed; refresh to see it")
