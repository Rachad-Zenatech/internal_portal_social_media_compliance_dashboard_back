"""Creator dismissal requests that must follow a submission into review."""

from typing import Any
from uuid import UUID

from postgresql_db.database import get_pool


async def carry_pending_requests(conn: Any, submission_id: UUID, from_version: int, to_version: int) -> None:
    """Move draft requests to the review version created when the post is sent."""
    await conn.execute(
        """
        UPDATE creator_flag_dismissal_requests
        SET version_number = $2
        WHERE submission_id = $1 AND version_number = $3 AND status = 'PENDING'
        """,
        submission_id, to_version, from_version,
    )


async def repair_pending_review_requests(submission_id: UUID, current_version: int) -> None:
    """Repair requests created before draft requests were carried into review."""
    async with get_pool().acquire() as conn:
        await carry_pending_requests(conn, submission_id, current_version - 1, current_version)
