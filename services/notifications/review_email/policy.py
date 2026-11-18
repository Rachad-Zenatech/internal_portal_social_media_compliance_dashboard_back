"""Select actionable work, respecting assignments, check-ins and the team's calendar."""
import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from models.notifications.review_email_model import ReviewEmailItem
from services.compliance.submissions import review_deadline_service as deadlines
from services.notifications.review_email.config import team_timezone


def date_value(value) -> datetime | None:
    if not value:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def details_of(row: dict) -> dict:
    value = row.get("details") or row.get("detail") or {}
    return json.loads(value) if isinstance(value, str) else value


def item_from_row(row: dict) -> ReviewEmailItem:
    document = row["source"] == "document"
    details = details_of(row) if document else row
    review = row["status"] in ("IN_REVIEW", "LEVEL2_REVIEW") or (row["status"] == "DRAFT" and row.get("script_status") == "IN_REVIEW")
    stage = "LEVEL2" if row["status"] == "LEVEL2_REVIEW" else "REVIEW" if review else "REVISION"
    sent_at = date_value(row.get("sent_at"))
    due = date_value(row.get("submission_due_at") if document else row.get("due_at"))
    due_kind = row.get("submission_due_kind") if document else row.get("due_kind")
    if due_kind != ("REVIEW" if review else "REVISION"):
        due = None
    requested = date_value(details.get("approval_needed_by") or row.get("submission_approval_needed_by")) if review else None
    if not due and requested:
        due = requested
    if not due and review and sent_at:
        due = deadlines.due_date("REVIEW", date_value(details.get("publish_at")), now=sent_at)
    # Live document changes follow the same standard revision window as uploaded scripts and posts.
    if not due and not review and row.get("decided_at"):
        due = deadlines.due_date("REVISION", date_value(details.get("publish_at")), now=date_value(row["decided_at"]))
    entity_id = str(row["id"])
    token = row.get("sent_event_id") or row.get("current_version") or row.get("round") or 0
    path = f"/script-docs/{entity_id}" if document else f"/submissions/{entity_id}" + ("/script" if row["item_kind"] == "Script" else "")
    return ReviewEmailItem(
        key=f"{row['source']}:{entity_id}:{stage}:{token}", entity_id=entity_id, kind=row["item_kind"],
        title=row["title"], reference=row.get("reference_code") or "Script", path=path,
        creator_id=str(row["created_by"]), stage=stage, due_at=due, sent_at=sent_at,
        resubmitted=bool(row.get("resubmitted")), reviewer_ids=[str(u) for u in row.get("reviewer_ids") or []],
        checked_ids=[str(u) for u in row.get("checked_ids") or []],
        brands=list(details.get("brand_codes") or ([details["brand_code"]] if details.get("brand_code") else [])),
        audiences=list(details.get("audiences") or ([details["audience"]] if details.get("audience") else [])),
        rule_sets=list(details.get("rule_sets") or []), document=document, ceo_account=bool(row.get("ceo_account")),
    )


def reviewers_for(item: ReviewEmailItem, users: dict[str, dict]) -> list[str]:
    recipients = []
    for uid, user in users.items():
        if uid == item.creator_id:
            continue
        groups = set(user.get("groups") or [])
        super_admin = user.get("is_super_admin") or user.get("super_role")
        reviewer = super_admin or "CONTENT_REVIEW_PUBLISH" in groups
        manager = super_admin or "CONTENT_MANAGE_DRAFTS" in groups
        suggester = "CONTENT_REVIEW_SUGGEST" in groups
        brand_ok = super_admin or not item.brands or not user.get("brands") or bool(set(item.brands) & set(user["brands"]))
        if not item.document and not brand_ok:
            continue
        if item.stage == "LEVEL2":
            allowed = manager
        elif item.document:
            allowed = (reviewer or suggester) and uid in item.reviewer_ids and uid not in item.checked_ids
        else:
            allowed = reviewer and (not item.reviewer_ids or uid in item.reviewer_ids)
            allowed = allowed or ((reviewer or suggester) and uid in item.reviewer_ids) or (reviewer and uid in item.approver_ids)
        if allowed:
            recipients.append(uid)
    return recipients


def digest_window(now: datetime, time_zone: str | None = None) -> tuple[datetime, datetime] | None:
    zone = ZoneInfo(time_zone) if time_zone else team_timezone()
    local = now.astimezone(zone)
    if local.time() < time(9):
        return None
    end = datetime.combine(local.date(), time.min, zone)
    start = datetime.combine(local.date() - timedelta(days=1), time.min, zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def in_digest(item: ReviewEmailItem, now: datetime, time_zone: str | None = None) -> bool:
    window = digest_window(now, time_zone)
    return bool(window and item.stage in ("REVIEW", "LEVEL2") and item.sent_at and window[0] <= item.sent_at < window[1])


def due_soon(item: ReviewEmailItem, now: datetime) -> bool:
    return bool(item.due_at and now < item.due_at <= now + timedelta(hours=1))


def outcome_label(row: dict) -> str:
    detail = details_of(row)
    kind = row["event_kind"]
    changes = kind in ("CHANGES_REQUESTED", "SCRIPT_CHANGES_REQUESTED", "APPROVAL_REOPENED") or detail.get("decision") == "REQUEST_CHANGES"
    if kind == "SCRIPT_EDITED":
        return "Your reviewer updated your script"
    if changes:
        return "Make changes, then publish" if detail.get("publish_after_changes") else "Changes requested"
    return "Script approved - ready to film" if row["item_kind"] == "Script" else "Post approved"
