"""Manual compliance checks: reuse current results and only finish explicitly requested runs."""
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from models.compliance.compliance_model import ComplianceCheckResponse


def saved_check(row):
    value = row.get("latest_check")
    return json.loads(value) if isinstance(value, str) else (value or {})


def timestamp(value):
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError, AttributeError):
        return None


def content_is_current(row, check):
    if "checked_content_edited_at" in check:
        return timestamp(check["checked_content_edited_at"]) == timestamp(row.get("content_edited_at"))
    checked, edited = timestamp(check.get("checked_at")), timestamp(row.get("content_edited_at"))
    return checked is not None and (edited is None or checked >= edited)


def may_finish_check(row):
    check = saved_check(row)
    return bool(check.get("manual_requested_at") and check.get("analysis_pending") and content_is_current(row, check))


def require_current_check(row, rulebook_version):
    check = saved_check(row)
    if not check or not content_is_current(row, check) or check.get("rulebook_version") != rulebook_version:
        raise HTTPException(422, "Run the compliance check on the Compliance page before sending or approving this post.")
    if check.get("analysis_pending") or check.get("ai_unavailable"):
        raise HTTPException(422, "The compliance check has no completed score. Run it on the Compliance page and wait for it to finish.")
    return ComplianceCheckResponse.model_validate(check), check
