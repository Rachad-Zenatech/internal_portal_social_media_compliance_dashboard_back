"""Check freshness follows media revisions, independently of post detail edits."""
import json
from datetime import datetime

# Extraction/transcription updates do not replace media. Deleted media still counts as a revision.
REVISION_SQL = """greatest(s.image_provenance_edited_at,
    (SELECT max(greatest(mf.created_at, mf.deleted_at)) FROM content_submission_files mf
     WHERE mf.submission_id = s.id AND mf.kind IN ('MAIN', 'IMAGE', 'SCRIPT')),
    (SELECT max(sv.created_at) FROM content_script_versions sv WHERE sv.submission_id = s.id))"""


def is_current(row, check):
    from services.compliance.submissions.manual_check_service import timestamp
    edited = timestamp(row.get("media_edited_at"))
    if "checked_media_edited_at" in check:
        return timestamp(check["checked_media_edited_at"]) == edited
    checked = timestamp(check.get("checked_at"))
    return edited is None or (checked is not None and checked >= edited)


async def check_still_current(conn, row, started):
    """Lock the post and reject results superseded by media, workflow or newer checks."""
    cur = await conn.fetchrow(
        "SELECT s.status, s.current_version, s.content_edited_at, s.latest_check, " + REVISION_SQL +
        " AS media_edited_at FROM content_submissions s WHERE s.id = $1 AND s.deleted_at IS NULL FOR UPDATE",
        row["id"],
    )
    if not cur:
        return False
    marker = "media_edited_at" if "media_edited_at" in row else "content_edited_at"
    if any(cur.get(key) != row.get(key) for key in ("status", "current_version", marker, "content_edited_at")):
        return False
    saved = cur.get("latest_check") or {}
    saved = json.loads(saved) if isinstance(saved, str) else saved
    try:
        return not saved.get("checked_at") or datetime.fromisoformat(saved["checked_at"]) <= started
    except (TypeError, ValueError):
        return True
