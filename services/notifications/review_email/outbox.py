"""Atomic deduplication, leases and retriable delivery without blocking review workflows."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from postgresql_db.database import execute, fetch_all
from services.notifications import email_service
from services.notifications.review_email import config, templates
from services.notifications.review_email.policy import due_soon, in_digest, reviewers_for

logger = logging.getLogger(__name__)


async def enqueue(messages: list[dict]) -> int:
    if not messages:
        return 0
    rows = await fetch_all("""
        INSERT INTO review_email_outbox (dedupe_key, user_id, kind, payload)
        SELECT q.key, q.user_id, q.kind, q.payload
        FROM jsonb_to_recordset($1::jsonb) AS q(key TEXT, user_id UUID, kind TEXT, payload JSONB)
        ON CONFLICT (dedupe_key) DO UPDATE
        SET payload = EXCLUDED.payload, status = 'PENDING', available_at = NOW()
        WHERE review_email_outbox.status = 'CANCELLED' AND review_email_outbox.kind = 'DIGEST'
        RETURNING id
    """, json.dumps(messages))
    return len(rows)


async def claim() -> list[dict]:
    rows = await fetch_all("""
        WITH ready AS (
            SELECT id FROM review_email_outbox WHERE status = 'PENDING' AND available_at <= NOW()
              AND (locked_until IS NULL OR locked_until < NOW())
            ORDER BY available_at, id FOR UPDATE SKIP LOCKED LIMIT 20
        ) UPDATE review_email_outbox o SET locked_until = NOW() + INTERVAL '5 minutes', attempts = attempts + 1
          FROM ready WHERE o.id = ready.id RETURNING o.*
    """)
    return [dict(r) for r in rows]


def current_payload(job: dict, items: dict, users: dict, now: datetime) -> dict | None:
    payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else dict(job["payload"])
    uid = str(job["user_id"])
    if uid not in users:
        return None
    if job["kind"] == "OUTCOME":
        return payload
    if job["kind"] == "DIGEST" and payload["date"] != str(now.astimezone(config.user_timezone(users[uid])).date()):
        return None
    visible = []
    for saved in payload["items"]:
        item = items.get(saved["key"])
        if not item:
            continue
        if job["kind"] == "REMINDER":
            if not due_soon(item, now) or saved.get("due_at") != (item.due_at.isoformat() if item.due_at else None):
                continue
            valid = uid == item.creator_id if item.stage == "REVISION" else uid in reviewers_for(item, users)
        else:
            valid = in_digest(item, now, str(config.user_timezone(users[uid]))) and uid in reviewers_for(item, users)
        if valid:
            visible.append(templates.item_payload(item))
    return {**payload, "items": visible} if visible else None


async def _deliver(job: dict, items: dict, users: dict, semaphore: asyncio.Semaphore) -> bool:
    async with semaphore:
        try:
            payload = current_payload(job, items, users, datetime.now(timezone.utc))
            if payload is None:
                await execute("UPDATE review_email_outbox SET status = 'CANCELLED', locked_until = NULL WHERE id = $1", job["id"])
                return False
            user = users[str(job["user_id"])]
            subject, html, text = templates.render(job["kind"], payload, user["name"], str(config.user_timezone(user)))
            accepted = await email_service.send_email(user["email"], subject, html, text)
            if accepted:
                await execute("UPDATE review_email_outbox SET status = 'SENT', sent_at = NOW(), locked_until = NULL WHERE id = $1", job["id"])
                return True
        except Exception:
            logger.exception("Review email delivery failed for outbox item %s", job["id"])
        await execute("""
            UPDATE review_email_outbox SET locked_until = NULL,
                   available_at = NOW() + make_interval(secs => LEAST(3600, 60 * power(2, LEAST(attempts, 6)))::int)
            WHERE id = $1
        """, job["id"])
        return False


async def deliver(items: dict, users: dict) -> int:
    semaphore = asyncio.Semaphore(4)
    return sum(await asyncio.gather(*(_deliver(job, items, users, semaphore) for job in await claim())))
