"""Read-only details of a script already sent for review."""
from postgresql_db.database import fetch_all


async def assigned_reviewer_names(row):
    ids = list(dict.fromkeys(row.get("reviewer_ids") or []))
    if not ids:
        # No individually named recipients means the same brand review team used by review notifications.
        from services.compliance.submissions import submission_service
        ids = await submission_service._review_recipients(row, exclude=row.get("created_by"))
    if not ids:
        return []
    reviewers = await fetch_all(
        "SELECT coalesce(nullif(full_name, ''), email) AS name FROM users "
        "WHERE id = ANY($1::uuid[]) ORDER BY array_position($1::uuid[], id)", ids,
    )
    return [reviewer["name"] for reviewer in reviewers if reviewer["name"]]
