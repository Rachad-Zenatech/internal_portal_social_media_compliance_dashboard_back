"""Creator dismissal requests that must follow a submission into review."""

from typing import Any
from uuid import UUID

from postgresql_db.database import get_pool
from services.compliance.submissions.caption_review_service import feedback_is_current_sql


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


def pending_send_score(check: dict, requests: list) -> int | None:
    """Potential routing score; no request changes the actual score or approves a flag."""
    from services.compliance.submissions.flag_review_service import score_after_dismissals

    if not check or check.get("analysis_pending") or check.get("ai_unavailable"):
        return None
    score = check.get("score")
    findings = check.get("violations") or []
    if score is None or any(v.get("penalty") is None for v in findings):
        return score
    requested = {
        str(request["rule_id"]) for request in requests
        if request.get("status") == "PENDING" and any(
            str(v.get("rule_id")) == str(request["rule_id"])
            and sorted(v.get("flagged_terms") or []) == sorted(request.get("flagged_terms") or [])
            for v in findings
        )
    }
    if not requested:
        return score
    potential = score_after_dismissals(check, requested)
    refundable = sum(int(v.get("penalty") or 0) for v in findings if str(v.get("rule_id")) in requested)
    return min(potential, score + refundable) if potential is not None else score


async def send_score(row: dict, check: dict) -> int | None:
    """Only this creator's pending requests on the current version can unlock review routing."""
    from services.compliance.submissions.flag_review_service import MIN_APPROVAL_SCORE

    if check.get("score") is None or check["score"] >= MIN_APPROVAL_SCORE:
        return check.get("score")
    async with get_pool().acquire() as conn:
        requests = await conn.fetch(
            """SELECT rule_id, flagged_terms, status FROM creator_flag_dismissal_requests r
               WHERE submission_id = $1 AND version_number = $2
                 AND requested_by = $3 AND status = 'PENDING' AND """ + feedback_is_current_sql('r', 'requested_at'),
            row["id"], row["current_version"], row["created_by"],
        )
    return pending_send_score(check, requests)
