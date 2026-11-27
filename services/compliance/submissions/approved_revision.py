"""Approval returns a post to its creator for final edits and fresh checks."""
from fastapi import HTTPException
from postgresql_db.database import fetch_one
from .manual_check_service import timestamp

BASICS = ("title", "content_type", "brand_code", "brand_codes", "audience", "audiences",
          "region", "platforms", "account_id", "account_ids", "rule_sets", "script_required",
          "approval_needed_by", "reviewer_ids", "reviewer_route")


def comparable(value):
    if isinstance(value, (list, tuple)):
        return sorted(str(item) for item in value)
    return str(value) if value is not None else None


def require_locked_basics(row, fields):
    if row["status"] != "CHANGES_REQUESTED":
        return
    baseline = dict(row)
    for plural, singular in (("brand_codes", "brand_code"), ("account_ids", "account_id"), ("audiences", "audience")):
        if not baseline.get(plural):
            baseline[plural] = [row[singular]] if row.get(singular) else []
    changed = [key for key in BASICS if key in fields and comparable(fields[key]) != comparable(baseline.get(key))]
    if changed:
        raise HTTPException(422, "The reviewed post's basics are locked. Update its text, media or supporting sources instead.")


def approval_outcome(row, request, now):
    from .post_approval_suggestions import approval_timing
    if request.publish_after_changes:
        return row.get("publish_at"), "CHANGES_REQUESTED", "Approved for creator updates and publishing"
    if request.publish_at is None and not request.publish_now:
        return row.get("publish_at"), "APPROVED", "Approved without changes; creator chooses publishing time"
    publish_at, label = approval_timing(row, request, now)
    return publish_at, "APPROVED", label


async def require_fresh_checks(row, check):
    """Publishing permission does not substitute for checking the creator's final content."""
    if row["status"] != "CHANGES_REQUESTED" or not row.get("publish_after_changes"):
        return
    state = await fetch_one("""SELECT max(created_at) AS sentiment_at
        FROM content_sentiment_analyses WHERE submission_id=$1 AND status='COMPLETED'""", row["id"])
    # Approval and scheduling do not change the content that was checked.
    markers = [timestamp(value) for value in (row.get("content_edited_at"), row.get("media_edited_at")) if value]
    cutoff = max(markers) if markers else None
    checked = timestamp(check.get("checked_at"))
    sentiment = timestamp(state["sentiment_at"])
    if not checked or (cutoff and checked < cutoff):
        raise HTTPException(422, "Run compliance on the latest saved content before publishing.")
    if row.get("content_type") != "TEXT_POST" and (not sentiment or (cutoff and sentiment < cutoff)):
        raise HTTPException(422, "Run sentiment on the latest saved content before publishing.")
