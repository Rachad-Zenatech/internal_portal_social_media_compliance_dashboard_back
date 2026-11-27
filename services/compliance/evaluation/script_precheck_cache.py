"""Reuse script-stage checks only while their wording, sources and scoring policy remain current."""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from services.compliance.media import script_sources
from services.compliance.media.script_content import VERSION as SCORING_VERSION


def check_key(row: Dict[str, Any], text: str) -> str:
    """Keep the existing key format so current saved checks remain reusable."""
    prefix = "media-v1:sources:" if script_sources.split(text)[1] else "media-v1:"
    cleaned = (text or "").replace("\r\n", "\n").strip()
    return prefix + hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def precheck_record(row: Dict[str, Any], text: str, check: Dict[str, Any]) -> str:
    return json.dumps({
        "key": check_key(row, text), "rulebook_version": check.get("rulebook_version"), "check": check,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    })


def still_valid(stored: Any, row: Dict[str, Any], text: str, rulebook: Optional[str]) -> Optional[Dict[str, Any]]:
    try:
        stored = (json.loads(stored) if isinstance(stored, str) else stored) or {}
    except (TypeError, ValueError):
        return None
    key = stored.get("key") or ""
    legacy_current = False
    if not key.startswith("media-v1:") and "media_edited_at" in row:
        from services.compliance.submissions.manual_check_service import content_is_current
        legacy_current = content_is_current(row, stored)
    check = stored.get("check")
    if not check or check.get("script_scoring_version") != SCORING_VERSION:
        return None
    if key != check_key(row, text) and not (legacy_current and not script_sources.split(text)[1]):
        return None
    if rulebook and stored.get("rulebook_version") != rulebook:
        return None
    return check
