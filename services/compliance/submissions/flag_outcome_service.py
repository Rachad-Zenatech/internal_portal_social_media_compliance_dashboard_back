"""Audit display only: historical decisions never authorize a newer check."""
from models.compliance.flag_review_model import REASON_LABELS
from models.compliance.flag_outcome_model import FlagOutcome
from postgresql_db.database import fetch_all


async def list_for(submission_id, user_id):
    from . import submission_service
    await submission_service._get_owned_row(submission_id, user_id, manager_can_read=True)
    rows = await fetch_all("""
        SELECT f.rule_id, f.rule_number, f.version_number,
               CASE WHEN r.status='APPROVED' THEN 'APPROVED' ELSE 'DISMISSED' END AS outcome,
               f.reason, f.note, u.full_name AS reviewer_name, f.decided_at
        FROM flag_reviews f
        LEFT JOIN users u ON u.id=f.decided_by
        LEFT JOIN creator_flag_dismissal_requests r ON r.submission_id=f.submission_id
          AND r.version_number=f.version_number AND r.rule_id=f.rule_id
        WHERE f.submission_id=$1
        UNION ALL
        SELECT r.rule_id, r.rule_number, r.version_number, 'REJECTED',
               r.reason, r.review_note, u.full_name, r.reviewed_at
        FROM creator_flag_dismissal_requests r
        LEFT JOIN users u ON u.id=r.reviewed_by
        WHERE r.submission_id=$1 AND r.status='REJECTED'
        ORDER BY version_number DESC, decided_at DESC NULLS LAST
    """, submission_id)
    return [FlagOutcome(**{**dict(row), "reason": REASON_LABELS.get(row["reason"], row["reason"])}) for row in rows]
