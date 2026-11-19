"""Keeping this portal in step with the Share Document server's script reviews.

The Share Document server publishes events on the portal's event bus (RabbitMQ, routing key share_document.script.<event>)
when a script's content, review or approval changes. This module listens, finds the page the shared document belongs to
(script_documents.share_document_id) and brings the page up to date, so the hub, the rulebook check and the post made from
an approved script all see what the shared editor holds. It never copies the editor: it reads the script's text back and
mirrors the review's status.

    content.saved / change.proposed / merge.completed   the page's text is read again
    review.submitted                                    status IN_REVIEW, its round
    review.finalized / review.decided                   status LEVEL2_REVIEW / CHANGES_REQUESTED / APPROVED, with the people
                                                        who have to act told, and the post made on approval
    post.requested                                      the post is made (again) from the approved script

Handling an event twice does nothing twice: a status is only acted on when it changes.
"""

import asyncio
import logging
from typing import Any, Dict, Optional
from uuid import UUID

from postgresql_db.database import execute, fetch_one
from services.compliance.evaluation import shared_script_service as shared

logger = logging.getLogger(__name__)

QUEUE = "social_media.share_document.script"
ROUTING_KEY = "share_document.script.#"
TEXT_EVENTS = {"content.saved", "change.proposed", "merge.completed"}
STATUS_EVENTS = {"review.submitted", "review.finalized", "review.decided"}
STATUSES = {"IN_REVIEW", "LEVEL2_REVIEW", "CHANGES_REQUESTED", "APPROVED"}


async def _page(share_id: str) -> Optional[Dict[str, Any]]:
    return await fetch_one(
        """SELECT d.id, d.title, d.status, d.created_by, d.position, u.email FROM script_documents d JOIN users u ON u.id = d.created_by
           WHERE d.share_document_id = $1 AND d.deleted_at IS NULL""", share_id)


async def refresh_text(page: Dict[str, Any], share_id: str) -> None:
    """Read the script's text back from the shared document into the page (for lists, the rulebook check and the post)."""
    got = await shared.call("GET", f"/{share_id}", ["script_documents:read"], params={"email": page["email"]})
    text = (got.get("text") or "").strip()
    if text:
        # The reviewers read the script in this portal's own (read-only) editor, drawn from body_html: keep it as current as the
        # text, and tell the people who have the page open to read it again.
        from services.compliance.evaluation import script_doc_service as core
        from services.compliance.evaluation.script_doc_templates import shared_script_html

        changed = await fetch_one(
            "UPDATE script_documents SET body_text = $2, body_html = $3, updated_at = NOW() WHERE id = $1 AND (body_text IS DISTINCT FROM $2 OR body_html IS DISTINCT FROM $3) RETURNING id",
            page["id"], text, shared_script_html(page["position"] or 1, page["title"], text))
        if changed:
            await core._broadcast_state(page["id"])


async def _mirror_checkins(page: Dict[str, Any], share_id: str) -> None:
    """A change in the shared editor clears everyone's check-in there; the same people's check-ins are cleared here."""
    state = await shared.call("GET", f"/{share_id}/review", ["script_documents:read"], params={"email": page["email"]})
    cleared = [c["email"].lower() for c in state.get("collaborators", []) if c.get("email") and not c.get("vote")]
    if cleared:
        await execute(
            """DELETE FROM script_document_checkins c USING users u, script_documents d
               WHERE d.id = $1 AND c.document_id = d.id AND c.round = d.round AND u.id = c.user_id AND lower(u.email) = ANY($2::text[])
               RETURNING c.user_id""", page["id"], cleared)


async def _mirror_reviewers(page_id: UUID, share_id: str, actor_email: str) -> None:
    """Keep script_document_reviewers in sync with Share Document's review collaborators so Sent to me lists them."""
    from postgresql_db.database import get_pool

    state = await shared.call("GET", f"/{share_id}/review", ["script_documents:read"], params={"email": actor_email})
    rows = []
    for pos, collab in enumerate(state.get("collaborators") or []):
        email = (collab.get("email") or "").strip().lower()
        if not email or collab.get("role") == "CREATOR":
            continue
        cap = "VIEW" if str(collab.get("capacity") or "").upper() == "VIEW" else "EDIT"
        rows.append((email, cap, pos))
    if not rows:
        return
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            users = await conn.fetch("SELECT id, lower(email) AS email FROM users WHERE lower(email) = ANY($1::text[])", [r[0] for r in rows])
            by_email = {u["email"]: u["id"] for u in users}
            resolved = [(by_email[e], cap, pos) for e, cap, pos in rows if e in by_email]
            if not resolved:
                return
            await conn.execute("DELETE FROM script_document_reviewers WHERE document_id = $1", page_id)
            for uid, cap, pos in resolved:
                await conn.execute(
                    "INSERT INTO script_document_reviewers (document_id, user_id, capacity, position) VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
                    page_id, uid, cap, pos,
                )


async def _mirror_status(page: Dict[str, Any], payload: Dict[str, Any]) -> None:
    from services.compliance.evaluation import script_doc_pages as pages, script_doc_service as core

    status = payload.get("status")
    if status not in STATUSES:
        return
    try:
        await _mirror_reviewers(page["id"], payload["documentId"], page["email"])
    except Exception:
        logger.debug("Could not sync collaborators for shared script %s", page["id"], exc_info=True)
    changed = await fetch_one(
        """UPDATE script_documents SET status = $2, round = GREATEST(round, $3), decision_note = coalesce($4, decision_note),
               decided_at = CASE WHEN $2 = 'IN_REVIEW' THEN NULL ELSE NOW() END, updated_at = NOW()
           WHERE id = $1 AND status IS DISTINCT FROM $2 RETURNING id""",
        page["id"], status, int(payload.get("round") or 0), (payload.get("note") or "").strip() or None)
    from services.compliance.evaluation.script_submission_sync import sync_document
    await sync_document(page["id"])
    if not changed:
        return  # already there: an event delivered twice
    doc = await core._doc(page["id"])
    if status == "CHANGES_REQUESTED":
        await core._notify([doc["created_by"]], "Changes requested on your script", f"“{doc['title']}” needs changes.", doc["id"])
    elif status == "LEVEL2_REVIEW":
        await core._notify(await core._managers(), "Script waiting for level 2 approval",
                           f"“{doc['title']}” needs the Content Manager's decision.", doc["id"])
    elif status == "APPROVED":
        await pages._approved(doc)


async def handle(payload: Dict[str, Any]) -> None:
    """One event from the Share Document server. Never raises: a bad event is logged and dropped."""
    try:
        event, share_id = payload.get("event"), payload.get("documentId")
        page = await _page(share_id) if share_id else None
        if not page:
            return  # not a script of this portal
        if event in TEXT_EVENTS and page["status"] == "IN_REVIEW":
            await _mirror_checkins(page, share_id)
        if event in TEXT_EVENTS or (event in STATUS_EVENTS and payload.get("status") in ("LEVEL2_REVIEW", "APPROVED")):
            await refresh_text(page, share_id)
        if event in STATUS_EVENTS:
            await _mirror_status(page, payload)
        elif event == "post.requested":
            from services.compliance.evaluation import script_doc_pages as pages
            await pages._make_posts(page["id"])
    except Exception:
        logger.warning("Couldn't handle the shared script event %s", payload.get("event"), exc_info=True)


async def start_consumer() -> None:
    """Background task: subscribe once the message bus is up (it connects after this is started)."""
    if not shared.enabled():
        return
    from services.notifications.rabbitmq_config import get_events_exchange
    from services.notifications.rabbitmq_service import rabbitmq_manager

    for _ in range(90):
        if rabbitmq_manager.is_connected and getattr(rabbitmq_manager, "_channel", None):
            break
        await asyncio.sleep(2)
    else:
        logger.warning("The message bus never came up: shared script events aren't followed")
        return
    queue = await rabbitmq_manager._channel.declare_queue(QUEUE, durable=True)
    await queue.bind(get_events_exchange(), routing_key=ROUTING_KEY)
    await rabbitmq_manager.consume_queue(QUEUE, handle)
    logger.info("Following the Share Document server's script events on %s", QUEUE)
