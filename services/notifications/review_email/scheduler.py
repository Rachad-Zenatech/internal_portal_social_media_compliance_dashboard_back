"""One 9 a.m. digest per reviewer, one-hour reminders, and creator decision emails."""
import asyncio
import logging
from datetime import datetime, timezone
from postgresql_db.database import get_pool
from services.compliance.submissions import approval_path_service
from services.notifications.review_email import config, outbox, repository, templates
from services.notifications.review_email.policy import (details_of, due_soon, in_digest, item_from_row, outcome_label, reviewers_for)

logger = logging.getLogger(__name__)


def scheduled_messages(items: list, users: dict, now: datetime) -> list[dict]:
    messages, digests = [], {}
    for item in items:
        recipients = reviewers_for(item, users) if item.stage in ("REVIEW", "LEVEL2") else [item.creator_id]
        for uid in recipients:
            if uid in users and in_digest(item, now, str(config.user_timezone(users[uid]))):
                digests.setdefault(uid, []).append(templates.item_payload(item))
        if due_soon(item, now):
            for uid in recipients:
                if uid in users:
                    messages.append({"key": f"reminder:{item.key}:{item.due_at.isoformat()}:{uid}", "user_id": uid,
                                     "kind": "REMINDER", "payload": {"stage": item.stage, "items": [templates.item_payload(item)]}})
    for uid, entries in digests.items():
        date = str(now.astimezone(config.user_timezone(users[uid])).date())
        entries.sort(key=lambda item: (item["due_at"] or "9999", item["title"].casefold()))
        messages.append({"key": f"digest:{date}:{uid}", "user_id": uid, "kind": "DIGEST", "payload": {"date": date, "items": entries}})
    return messages


def outcome_messages(rows: list[dict], users: dict, items: list | None = None) -> list[dict]:
    messages = []
    changes = {item.entity_id: item for item in items or [] if item.stage == "REVISION"}
    for row in rows:
        uid = str(row["user_id"])
        if uid not in users:
            continue
        details = details_of(row)
        label = outcome_label(row)
        note = details.get("notes") or details.get("note") or row.get("message") or "Open the script to see your reviewer's decision."
        current = changes.get(row.get("entity_id"))
        messages.append({"key": row["event_key"], "user_id": uid, "kind": "OUTCOME", "payload": {
            "label": label, "message": note, "title": row["title"], "reference": row["reference_code"],
            "kind": row["item_kind"], "path": row["path"],
            "changes_due_at": current.due_at.isoformat() if current and current.due_at else None}})
    return messages


async def load_items() -> list:
    rules = await approval_path_service.load_rules()
    items = []
    for row in await repository.pending_rows():
        item = item_from_row(row)
        if not item.document:
            ceo = item.ceo_account or approval_path_service.is_ceo_content(item.rule_sets, [])
            ids, _ = rules.approvers(item.brands, item.audiences, ceo)
            item.approver_ids = [str(uid) for uid in ids]
        items.append(item)
    return items


async def run_once(now: datetime | None = None) -> int:
    if not config.configured():
        return 0
    # Exactly one API worker builds/drains workflow emails at a time; leases protect abandoned deliveries.
    async with get_pool().acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock(8009, 91)"):
            return 0
        try:
            users = await repository.users()
            items = await load_items()
            messages = scheduled_messages(items, users, now or datetime.now(timezone.utc))
            messages += outcome_messages(await repository.outcomes(), users, items)
            await outbox.enqueue(messages)
            # Re-read immediately before delivery so completed reviews, withdrawn work and removed assignees are excluded.
            latest = {item.key: item for item in await load_items()}
            return await outbox.deliver(latest, await repository.users())
        finally:
            await conn.execute("SELECT pg_advisory_unlock(8009, 91)")


async def email_loop() -> None:
    logger.info("Review email scheduler started: 09:00 recipient-local digest and one-hour reminders")
    if not config.configured():
        logger.warning("Review email delivery is inactive: configure SENDGRID_API_KEY, FRONTEND_URL and REVIEW_EMAIL_ENABLED")
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Review email scheduler pass failed")
        await asyncio.sleep(60)
