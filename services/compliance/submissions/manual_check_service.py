"""Manual compliance checks: reuse current results and only finish explicitly requested runs."""
import json
import os
from datetime import datetime, timezone

from fastapi import HTTPException
from models.compliance.compliance_model import ComplianceCheckResponse


def saved_check(row):
    value = row.get("latest_check")
    return json.loads(value) if isinstance(value, str) else (value or {})


def allow_scriptless_video_compliance():
    """Permit local video fixtures to check their uploaded media without a script."""
    return (
        os.getenv("APP_ENV", "development").strip().lower() != "production"
        and os.getenv("DEV_ALLOW_SCRIPTLESS_VIDEO_COMPLIANCE", "").strip().lower() in {"1", "true", "yes"}
    )


def timestamp(value):
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError, AttributeError):
        return None


def content_is_current(row, check):
    if "media_edited_at" in row:
        from services.compliance.submissions.media_freshness import is_current
        return is_current(row, check)
    if "checked_content_edited_at" in check:
        return timestamp(check["checked_content_edited_at"]) == timestamp(row.get("content_edited_at"))
    checked, edited = timestamp(check.get("checked_at")), timestamp(row.get("content_edited_at"))
    # Legacy checks predate the freshness timestamps. They remain usable when the row also has no edit marker;
    # any row with a known edit time must be checked again.
    if checked is None:
        return edited is None
    return checked is not None and (edited is None or checked >= edited)


def may_finish_check(row):
    check = saved_check(row)
    return bool(check.get("manual_requested_at") and check.get("analysis_pending") and content_is_current(row, check))


def require_current_check(row, rulebook_version, *, allow_incomplete=False, validate=True):
    check = saved_check(row)
    if not check or not content_is_current(row, check) or check.get("rulebook_version") != rulebook_version:
        raise HTTPException(422, "Run the compliance check on the Compliance page before sending or approving this post.")
    if not allow_incomplete and (check.get("analysis_pending") or check.get("ai_unavailable")):
        raise HTTPException(422, "The compliance check has no completed score. Run it on the Compliance page and wait for it to finish.")
    result = ComplianceCheckResponse.model_validate(check) if validate else None
    return result, check


def require_submission_fields(missing: list[str]) -> None:
    if missing:
        raise HTTPException(status_code=422, detail={
            "message": "Some required information is missing.",
            "missing": sorted(set(missing)),
        })


def require_send_score(score: int, *, pending_score: int | None = None) -> None:
    from services.compliance.submissions.flag_review_service import MIN_APPROVAL_SCORE

    if score < MIN_APPROVAL_SCORE and (pending_score is None or pending_score < MIN_APPROVAL_SCORE):
        raise HTTPException(
            422, f"The compliance score is {score}/100. Sending for review requires at least {MIN_APPROVAL_SCORE}, "
            "or pending dismissal requests that cover enough disputed deductions to reach it."
        )


async def analysis_details_missing(row):
    from services.compliance.submissions import submission_service as subs
    missing = [m for m in await subs._missing_for_submit(row) if m not in subs._SEND_ONLY_FIELDS]
    if row.get("content_type") != "VIDEO" or row.get("script_status") == "APPROVED":
        return missing
    script = await readable_script(row["id"])
    if subs.script_stage_on(row) or script:
        missing = [m for m in missing if m not in {"main_file", "caption", "script_approval"}]
        if not script:
            missing.append("script")
    return missing


async def readable_script(submission_id):
    from services.compliance.submissions import submission_service as subs
    return await subs.fetch_one(
        "SELECT 1 FROM content_submission_files WHERE submission_id = $1 AND kind = 'SCRIPT' "
        "AND deleted_at IS NULL AND doc_text_status = 'COMPLETED' AND btrim(coalesce(doc_text, '')) <> '' "
        "UNION ALL SELECT 1 FROM content_script_versions WHERE submission_id = $1 "
        "AND btrim(coalesce(body, '')) <> '' LIMIT 1", submission_id,
    )


async def require_sentiment_ready(submission_id, user_id):
    """Video sentiment starts only after a real video is attached; scripts do not require it."""
    from services.compliance.submissions import submission_service as subs
    row = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    if row.get("content_type") == "VIDEO":
        video = await subs.fetch_one(
            "SELECT 1 FROM content_submission_files WHERE submission_id = $1 AND kind = 'MAIN' "
            "AND deleted_at IS NULL AND mime_type LIKE 'video/%' LIMIT 1", submission_id,
        )
        if not video:
            raise HTTPException(422, "Sentiment analysis is available after the actual video is uploaded. Scripts only need compliance review.")
    await subs.require_details_complete(row, "running the sentiment analysis")


async def require_compliance_ready(row):
    """Check the script while no actual video exists, including after script approval."""
    from services.compliance.submissions import submission_service as subs
    if row.get("content_type") == "VIDEO":
        video = await subs.fetch_one(
            "SELECT 1 FROM content_submission_files WHERE submission_id = $1 AND kind = 'MAIN' "
            "AND deleted_at IS NULL AND mime_type LIKE 'video/%' LIMIT 1", row["id"],
        )
        if video and allow_scriptless_video_compliance():
            missing = [key for key in await subs._missing_for_submit(row)
                       if key not in subs._SEND_ONLY_FIELDS | {"main_file", "caption", "script_approval"}]
            if missing:
                raise HTTPException(422, "Fill in the post details before running the compliance check. Still needed: "
                                    + subs.post_steps_service.missing_words(missing) + ".")
            return
        if not video:
            if not await readable_script(row["id"]):
                raise HTTPException(422, "Attach a script and wait for its text to finish reading before running script compliance.")
            missing = [key for key in await subs._missing_for_submit(row)
                       if key not in subs._SEND_ONLY_FIELDS | {"main_file", "caption", "script_approval"}]
            if missing:
                raise HTTPException(422, "Fill in the script details before running compliance. Still needed: "
                                    + subs.post_steps_service.missing_words(missing) + ".")
            return
    await subs.require_details_complete(row, "running the compliance check")
