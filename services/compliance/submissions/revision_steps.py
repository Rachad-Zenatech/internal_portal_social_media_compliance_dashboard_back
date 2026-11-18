"""Revision progress; previous checks remain valid until the media changes."""
from models.compliance.submission_model import SubmissionSteps
from postgresql_db.database import fetch_all


async def for_submissions(rows):
    ids = [row["id"] for row in rows if row["status"] == "CHANGES_REQUESTED"]
    if not ids:
        return {}
    results = await fetch_all(
        "SELECT s.id, (SELECT max(j.decided_at) FROM content_review_jobs j WHERE j.submission_id = s.id "
        "AND j.version_number = s.current_version AND j.decision = 'REQUEST_CHANGES') AS revision_at, "
        "EXISTS (SELECT 1 FROM content_review_comments c WHERE c.submission_id = s.id "
        "AND c.version_number = s.current_version AND c.deleted_at IS NULL AND c.resolved_at IS NULL) AS unresolved_comments "
        "FROM content_submissions s WHERE s.id = ANY($1::uuid[])", ids,
    )
    return {row["id"]: dict(row) for row in results}


def compute(row, missing):
    from services.compliance.submissions.post_steps_service import _aware, _tool
    returned = _aware(row.get("revision_at"))
    edited = _aware(row.get("content_edited_at"))
    changed = bool(returned and edited and edited > returned)
    cutoff = max(returned, edited) if returned and edited else returned
    def checked(at, running=False):
        if "media_edited_at" in row:
            return _tool(_aware(at), _aware(row.get("media_edited_at")), running)
        if not returned:
            return "todo"
        stamp = _aware(at)
        if not stamp or stamp <= returned:
            return "running" if running else "todo"
        return _tool(stamp, cutoff, running)
    states = {
        "suggestions": "done" if changed and not row.get("unresolved_comments", True) else "todo",
        "details": "done" if changed and not missing else "todo",
        "compliance": checked(row.get("check_at"), bool(row.get("analysis_pending"))),
        "sentiment": checked(row.get("sentiment_at"), bool(row.get("sentiment_running"))),
    }
    if row.get("ai_unavailable") and not row.get("analysis_pending"):
        states["compliance"] = "unavailable"
    script_only = row.get("script_stage") and not row.get("main_is_video")
    required = ["suggestions", "details", "compliance"] + ([] if script_only else ["sentiment"])
    next_step = next((key for key in required if states[key] != "done"), "send")
    return SubmissionSteps(**states, revision=True, mood="todo", send="todo", next=next_step,
                           ready_to_send=next_step == "send", locked=False, missing=missing)
