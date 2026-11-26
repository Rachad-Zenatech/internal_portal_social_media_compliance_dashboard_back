"""Advance analysis revisions only when saved inputs to the analyses actually change."""
from datetime import datetime
import json
from uuid import UUID

NON_CONTENT_FIELDS = frozenset({"publish_at", "approval_needed_by", "reviewer_ids", "reviewer_route"})


def same_value(new, old):
    if isinstance(new, list) and isinstance(old, list):
        return [str(value) for value in new] == [str(value) for value in old]
    if isinstance(old, datetime) and isinstance(new, str):
        try:
            parsed = datetime.fromisoformat(new.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=old.tzinfo)
        return parsed == old
    if isinstance(new, UUID) or isinstance(old, UUID):
        return str(new) == str(old)
    if isinstance(new, dict):
        if isinstance(old, str):
            try:
                old = json.loads(old)
            except (ValueError, TypeError):
                old = None
        return new == (old or {})
    return new == old


def content_changed(row, fields):
    """Scheduling and reviewer routing never invalidate compliance or sentiment."""
    return any(key not in NON_CONTENT_FIELDS and not same_value(value, row.get(key)) for key, value in fields.items())
