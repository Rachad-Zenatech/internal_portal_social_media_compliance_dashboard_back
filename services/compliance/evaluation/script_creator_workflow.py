"""Creator script revisions and the handoff to filming."""


async def can_revise(row):
    from services.compliance.submissions import submission_service as subs
    if row.get("script_status") in ("EDITS_PROPOSED", "CHANGES_REQUESTED"):
        return True
    if row.get("script_status") != "APPROVED":
        return False
    video = await subs.fetch_one(
        "SELECT 1 FROM content_submission_files WHERE submission_id = $1 AND kind = 'MAIN' "
        "AND deleted_at IS NULL AND mime_type LIKE 'video/%' LIMIT 1", row["id"],
    )
    return not video
