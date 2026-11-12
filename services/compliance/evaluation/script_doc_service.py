"""Script documents.

A creator writes a script from a template, sends it to at least one reviewer, and the reviewers edit it together live
(see script_doc_realtime). The flow:

    DRAFT / CHANGES_REQUESTED  creator edits and sends it (reviewers are required)
    IN_REVIEW                  reviewers edit, note and highlight together; each checks in when finished (APPROVE, CHANGES,
                               or DONE for a suggesting reviewer). Any change after a check-in clears the check-ins, so
                               everyone looks again. When everyone has checked in, an approving reviewer chooses: pass it
                               to the Content Manager for level 2 approval, or approve it and return it to the creator.
                               Any CHANGES vote sends it back to the creator instead.
    LEVEL2_REVIEW              the Content Manager decides: approve (back to the creator) or send back.
    APPROVED                   decided; the creator takes the next steps.

Content Managers see every script and can make the final decision at any point. Reviewers with VIEW capacity, and
reviewers without the review permissions, can read and add notes but not edit or approve.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException

from models.compliance.script_batch_model import BatchDetails
from models.compliance.script_doc_model import (
    BulkFailure, BulkResult, BulkSubmit, CheckIn, DocAccess, DocCreate, DocDetail, DocEvent, DocHistory, DocPage, DocPost, DocReviewer, DocSetSummary, DocSubmit, DocSummary, DocUpdate,
    DocVersion, Finalize, ManagerDecision, ScriptTemplate, UploadedPages,
)
from models.compliance.submission_model import SubmissionCreate, SubmissionUpdate
from postgresql_db.database import execute, fetch_all, fetch_one, get_pool
from services.compliance.evaluation import script_doc_realtime as realtime
from services.compliance.evaluation import script_batch_service as batches
from services.compliance.evaluation import script_review_service as scripts
from services.compliance.evaluation.script_doc_split import split_document
from services.compliance.evaluation.script_split import split_scripts
from services.compliance.evaluation.script_doc_templates import TEMPLATES, empty_script_html, get_template, template_html, text_to_html
from services.compliance.submissions import submission_service as subs
from services.shared.core.audit_service import log_audit_event

logger = logging.getLogger(__name__)

# One colour per person in the editor (cursor, name flag, notes). The creator is first, then the reviewers in the order
# they were picked; anyone else (a manager looking in) gets one by their id.
PALETTE = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed", "#db2777", "#0891b2", "#65a30d", "#ea580c", "#4f46e5"]

EDITING = ("DRAFT", "CHANGES_REQUESTED")
IN_REVIEW_STATES = ("IN_REVIEW", "LEVEL2_REVIEW")
_EDIT_EVENT_GAP_SEC = 60
_last_edit_event: Dict[tuple, float] = {}


def templates() -> List[ScriptTemplate]:
    return TEMPLATES


def _color(user_id: UUID, order: List[UUID]) -> str:
    if user_id in order:
        return PALETTE[order.index(user_id) % len(PALETTE)]
    return PALETTE[(len(order) + user_id.int % 4) % len(PALETTE)]


async def _name(user_id: Optional[UUID]) -> Optional[str]:
    if not user_id:
        return None
    r = await fetch_one("SELECT coalesce(nullif(full_name, ''), email) AS n FROM users WHERE id = $1", user_id)
    return r["n"] if r else None


async def _event(doc_id: UUID, round_: int, user_id: Optional[UUID], kind: str, detail: Optional[Dict[str, Any]] = None) -> None:
    await execute(
        "INSERT INTO script_document_events (document_id, round, user_id, user_name, kind, detail) "
        "VALUES ($1, $2, $3, (SELECT coalesce(nullif(full_name, ''), email) FROM users WHERE id = $3), $4, $5::jsonb) RETURNING id",
        doc_id, round_, user_id, kind, json.dumps(detail or {}),
    )


async def _notify(user_ids: List[UUID], title: str, message: str, doc_id: UUID) -> None:
    if not user_ids:
        return
    try:
        from models.notifications.notification_model import NotificationCreate
        from services.notifications.notification_service import create_notification
        await asyncio.gather(*(
            create_notification(NotificationCreate(
                user_id=str(u), type="SUBMISSION", title=title, message=message, link_url=f"/script-docs/{doc_id}",
                entity_type="script_document", entity_id=str(doc_id),
            )) for u in set(user_ids)
        ))
    except Exception:
        logger.warning("Could not send script document notifications for %s", doc_id, exc_info=True)


async def _managers() -> List[UUID]:
    """Everyone who can make the final decision: super admins and roles with "Manage Team Drafts"."""
    return await subs._reviewer_ids(group="CONTENT_MANAGE_DRAFTS", include_super=True)


async def _doc(doc_id: UUID) -> Dict[str, Any]:
    row = await fetch_one(
        "SELECT d.*, coalesce(nullif(u.full_name, ''), u.email) AS creator_name FROM script_documents d "
        "JOIN users u ON u.id = d.created_by WHERE d.id = $1 AND d.deleted_at IS NULL", doc_id)
    if not row:
        raise HTTPException(status_code=404, detail="Script not found")
    return row


async def _reviewers(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    return await fetch_all(
        """
        SELECT r.user_id, r.capacity, r.position, coalesce(nullif(u.full_name, ''), u.email) AS name,
               c.vote, c.note, c.checked_at
        FROM script_document_reviewers r JOIN users u ON u.id = r.user_id
        LEFT JOIN script_document_checkins c ON c.document_id = r.document_id AND c.user_id = r.user_id AND c.round = $2
        WHERE r.document_id = $1 ORDER BY r.position, u.full_name
        """,
        doc["id"], doc["round"],
    )


async def _access(doc: Dict[str, Any], reviewers: List[Dict[str, Any]], user_id: UUID) -> Optional[DocAccess]:
    """What the user may do with the script, or None when they may not see it."""
    status = doc["status"]
    is_creator = doc["created_by"] == user_id
    manager = await subs._can_manage_team_content(user_id)
    mine = next((r for r in reviewers if r["user_id"] == user_id), None)
    if not (is_creator or manager or mine):
        return None
    sup = await subs._is_super_admin(user_id)
    can_review = await subs._can_review(user_id)
    can_suggest = await subs._can_suggest(user_id)
    editor = bool(mine and mine["capacity"] == "EDIT" and can_suggest)
    # Nobody approves their own script but a super admin.
    own = is_creator and not sup
    approver = editor and can_review and not own and status == "IN_REVIEW"
    deciding = manager and not own and status in IN_REVIEW_STATES
    can_edit = (
        (is_creator and status in EDITING)
        or (editor and status == "IN_REVIEW")
        or (manager and not own and status in IN_REVIEW_STATES)
    )
    can_note = can_edit or bool(mine and status in IN_REVIEW_STATES) or (is_creator and status != "DRAFT") or (manager and status != "DRAFT")
    if is_creator:
        role = "CREATOR"
    elif manager:
        role = "MANAGER"
    elif mine and editor and can_review:
        role = "REVIEWER"
    elif mine and editor:
        role = "SUGGESTER"
    else:
        role = "VIEWER"
    return DocAccess(
        role=role, can_edit=can_edit, can_note=can_note,
        can_submit=is_creator and status in EDITING,
        can_check_in=editor and status == "IN_REVIEW" and not own,
        can_finalize=approver or (manager and not own and status == "IN_REVIEW"),
        can_decide=deciding,
        can_delete=(is_creator or manager) and status == "DRAFT",
    )


def _all_checked(doc: Dict[str, Any], reviewers: List[Dict[str, Any]]) -> bool:
    required = [r for r in reviewers if r["capacity"] == "EDIT"]
    return doc["status"] == "IN_REVIEW" and bool(required) and all(r["vote"] for r in required)


def _state(doc: Dict[str, Any], reviewers: List[Dict[str, Any]], changed_by: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What every open editor is told when the review's state moves."""
    return {
        "type": "state", "status": doc["status"], "round": doc["round"], "all_checked": _all_checked(doc, reviewers),
        "checkins": [{"user_id": str(r["user_id"]), "name": r["name"], "vote": r["vote"]} for r in reviewers if r["vote"]],
        "changed_by": changed_by,
    }


async def _broadcast_state(doc_id: UUID, changed_by: Optional[Dict[str, Any]] = None) -> None:
    doc = await _doc(doc_id)
    await realtime.publish(doc_id, _state(doc, await _reviewers(doc), changed_by))


async def get_access(doc_id: UUID, user_id: UUID) -> tuple:
    """For the live connection: the script, and what the user may do with it."""
    doc = await _doc(doc_id)
    reviewers = await _reviewers(doc)
    access = await _access(doc, reviewers, user_id)
    if not access:
        raise HTTPException(status_code=403, detail="You don't have access to this script")
    return doc, reviewers, access


def _detail(doc: Dict[str, Any], reviewers: List[Dict[str, Any]], access: DocAccess, user_id: UUID, decided_by: Optional[str],
            posts: Optional[List[DocPost]] = None, pages: Optional[List[DocPage]] = None, set_title: Optional[str] = None) -> DocDetail:
    order = [doc["created_by"], *[r["user_id"] for r in reviewers]]
    return DocDetail(
        id=doc["id"], title=doc["title"], template_key=doc["template_key"], status=doc["status"], round=doc["round"],
        creator_id=doc["created_by"], creator_name=doc["creator_name"], creator_color=_color(doc["created_by"], order),
        body_html=doc["body_html"], body_text=doc["body_text"], details=subs._json(doc["details"]) or {},
        send_note=doc["send_note"], decision_note=doc["decision_note"], decided_by_name=decided_by, decided_at=doc["decided_at"],
        reviewers=[
            DocReviewer(user_id=r["user_id"], name=r["name"], capacity=r["capacity"], can_approve=r.get("can_approve", False),
                        color=_color(r["user_id"], order), checked_in=bool(r["vote"]), vote=r["vote"], note=r["note"], checked_at=r["checked_at"])
            for r in reviewers
        ],
        # The reviewers are picked when the script is sent, so they are not one of the details still to fill in.
        missing_details=[m for m in batches.missing_details({**(subs._json(doc["details"]) or {}), "reviewer_ids": ["-"]})
                         if m != "Send to reviewers"],
        set_id=doc.get("set_id"), set_title=set_title, pages=pages or [], posts=posts or [],
        all_checked=_all_checked(doc, reviewers), access=access, my_color=_color(user_id, order),
        updated_at=doc["updated_at"], created_at=doc["created_at"],
    )


async def get(doc_id: UUID, user_id: UUID) -> DocDetail:
    doc, reviewers, access = await get_access(doc_id, user_id)
    for r in reviewers:
        r["can_approve"] = r["capacity"] == "EDIT" and await subs._can_review(r["user_id"])
    by = await _name(doc["decided_by"])
    rows = await fetch_all(
        "SELECT p.position, p.title, p.submission_id, s.reference_code FROM script_document_posts p "
        "LEFT JOIN content_submissions s ON s.id = p.submission_id WHERE p.document_id = $1 ORDER BY p.position", doc_id)
    pages, set_title = [], None
    if doc.get("set_id") and access.role in ("CREATOR", "MANAGER"):
        pages = await _pages_module()._pages(doc["set_id"])
        set_title = (await fetch_one("SELECT title FROM script_sets WHERE id = $1", doc["set_id"]) or {}).get("title")
    return _detail(doc, reviewers, access, user_id, by, [DocPost(**r) for r in rows], pages, set_title)


async def create(user_id: UUID, req: DocCreate) -> DocDetail:
    """A new document from a template: the document and its first script (page)."""
    t = get_template(req.template_key)
    title = req.title.strip()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            set_id = await conn.fetchval("INSERT INTO script_sets (title, created_by) VALUES ($1, $2) RETURNING id", title, user_id)
            doc_id = await conn.fetchval(
                "INSERT INTO script_documents (title, template_key, created_by, body_html, body_text, set_id, position) "
                "VALUES ($1, $2, $3, $4, $5, $6, 1) RETURNING id",
                title, t.key, user_id, template_html(t, title), "", set_id)
    await _event(doc_id, 0, user_id, "CREATED", {"template": t.key})
    await log_audit_event("script_document", str(doc_id), "CREATE", user_id, {"template": t.key})
    return await get(doc_id, user_id)


async def update(doc_id: UUID, user_id: UUID, req: DocUpdate) -> DocDetail:
    doc, reviewers, access = await get_access(doc_id, user_id)
    if not access.can_submit:
        raise HTTPException(status_code=409, detail="The details can only be changed while the script is a draft or sent back")
    if req.title is not None:
        await execute("UPDATE script_documents SET title = $2, updated_at = NOW() WHERE id = $1 RETURNING id", doc_id, req.title.strip())
    if req.details is not None:
        details = json.dumps(req.details.model_dump(mode="json", exclude_none=True))
        await execute("UPDATE script_documents SET details = $2::jsonb, updated_at = NOW() WHERE id = $1 RETURNING id", doc_id, details)
        if doc.get("set_id"):
            # The details are the document's: every page that can still change gets them.
            await execute("UPDATE script_sets SET details = $2::jsonb, updated_at = NOW() WHERE id = $1 RETURNING id", doc["set_id"], details)
            await fetch_all(
                "UPDATE script_documents SET details = $2::jsonb WHERE set_id = $1 AND deleted_at IS NULL "
                "AND status IN ('DRAFT', 'CHANGES_REQUESTED') RETURNING id", doc["set_id"], details)
    return await get(doc_id, user_id)


async def delete(doc_id: UUID, user_id: UUID) -> None:
    doc, reviewers, access = await get_access(doc_id, user_id)
    if not access.can_delete:
        raise HTTPException(status_code=409, detail="Only a script that hasn't been sent for review can be deleted")
    await execute("UPDATE script_documents SET deleted_at = NOW() WHERE id = $1 RETURNING id", doc_id)
    if doc.get("set_id"):
        # A document left with no scripts goes with its last one.
        await execute(
            "UPDATE script_sets SET deleted_at = NOW() WHERE id = $1 AND NOT EXISTS "
            "(SELECT 1 FROM script_documents WHERE set_id = $1 AND deleted_at IS NULL) RETURNING id", doc["set_id"])
    await log_audit_event("script_document", str(doc_id), "DELETE", user_id, {})


async def save_snapshot(doc_id: UUID, user_id: UUID, html: str, text: str) -> None:
    """An editor saves the script as it reads now (for lists, the check and history); the live editor itself is kept as updates."""
    doc, reviewers, access = await get_access(doc_id, user_id)
    if not access.can_edit:
        raise HTTPException(status_code=403, detail="You can't edit this script")
    if not (text or "").strip():
        # An editor that has not loaded yet shows an empty page; that must not wipe the script that is stored.
        return
    await execute("UPDATE script_documents SET body_html = $2, body_text = $3, updated_at = NOW() WHERE id = $1 RETURNING id",
                  doc_id, html, text)


async def reviewer_options(user_id: UUID, doc_id: UUID) -> list:
    doc = await _doc(doc_id)
    brands = (subs._json(doc["details"]) or {}).get("brand_codes") or None
    return await subs.reviewer_options(user_id, brands)


async def submit(doc_id: UUID, user_id: UUID, req: DocSubmit) -> DocDetail:
    doc, reviewers, access = await get_access(doc_id, user_id)
    if not access.can_submit:
        raise HTTPException(status_code=409, detail="This script can't be sent for review now")
    if not req.reviewers:
        raise HTTPException(status_code=400, detail="Pick at least one reviewer to send the script to")
    picks = {p.user_id: p for p in req.reviewers}
    if not any(p.capacity == "EDIT" for p in picks.values()):
        raise HTTPException(status_code=400, detail="At least one reviewer has to be able to edit, so the script can be decided")
    allowed = {o.id for o in await reviewer_options(user_id, doc_id)}
    unknown = [u for u in picks if u not in allowed]
    if unknown:
        raise HTTPException(status_code=400, detail="Some of the people picked can't review scripts")
    if scripts.only_sent_to_self(list(picks), user_id) and not await subs._is_super_admin(user_id):
        raise HTTPException(status_code=400, detail=scripts.SELF_REVIEW_MESSAGE)
    html = req.html if req.html is not None else doc["body_html"]
    text = req.text if req.text is not None else doc["body_text"]
    if not (text or "").strip() or not split_document(html, doc["title"]):
        raise HTTPException(status_code=400, detail="The script is empty")
    # The posts made from the scripts need their details, as for scripts uploaded together.
    details = subs._json(doc["details"]) or {}
    details["reviewer_ids"] = [str(u) for u in picks]
    missing = batches.missing_details(details)
    if missing:
        raise HTTPException(status_code=422, detail=f"Fill in the details first: {', '.join(missing)}.")
    round_ = doc["round"] + 1
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            locked = await conn.fetchrow("SELECT status FROM script_documents WHERE id = $1 FOR UPDATE", doc_id)
            if locked["status"] not in EDITING:
                raise HTTPException(status_code=409, detail="This script was already sent")
            await conn.execute(
                "UPDATE script_documents SET status = 'IN_REVIEW', round = $2, body_html = $3, body_text = $4, send_note = $5, "
                "decision_note = NULL, decided_by = NULL, decided_at = NULL, details = $6::jsonb, updated_at = NOW() WHERE id = $1",
                doc_id, round_, html, text, (req.note or "").strip() or None, json.dumps(details))
            await conn.execute("DELETE FROM script_document_reviewers WHERE document_id = $1", doc_id)
            for i, p in enumerate(req.reviewers):
                await conn.execute(
                    "INSERT INTO script_document_reviewers (document_id, user_id, capacity, position) VALUES ($1, $2, $3, $4) "
                    "ON CONFLICT (document_id, user_id) DO UPDATE SET capacity = EXCLUDED.capacity, position = EXCLUDED.position",
                    doc_id, p.user_id, p.capacity, i)
            await conn.execute(
                "INSERT INTO script_document_versions (document_id, round, label, body_html, body_text, created_by, created_by_name) "
                "VALUES ($1, $2, $3, $4, $5, $6, (SELECT coalesce(nullif(full_name, ''), email) FROM users WHERE id = $6))",
                doc_id, round_, f"Sent for review (round {round_})", html, text, user_id)
    await _event(doc_id, round_, user_id, "SUBMITTED", {"reviewers": len(req.reviewers), "note": req.note})
    await log_audit_event("script_document", str(doc_id), "SUBMIT", user_id, {"round": round_})
    await _notify([p.user_id for p in req.reviewers], "Script to review", f"“{doc['title']}” was sent to you for review.", doc_id)
    await _broadcast_state(doc_id)
    return await get(doc_id, user_id)


async def note_change(doc_id: UUID, user_id: UUID, channel: str) -> None:
    """Someone changed the script or its notes. In review, everybody's check-in is cleared (they have to look again)
    and everyone is told who changed it."""
    cleared = await fetch_all(
        """
        WITH d AS (SELECT round, status FROM script_documents WHERE id = $1)
        DELETE FROM script_document_checkins c USING d
        WHERE c.document_id = $1 AND c.round = d.round AND d.status IN ('IN_REVIEW', 'LEVEL2_REVIEW')
        RETURNING c.user_id
        """, doc_id)
    doc = await fetch_one("SELECT status, round FROM script_documents WHERE id = $1", doc_id)
    if not doc or doc["status"] not in IN_REVIEW_STATES:
        return
    now = time.monotonic()
    key = (doc_id, user_id, channel)
    if now - _last_edit_event.get(key, -1e9) > _EDIT_EVENT_GAP_SEC:
        _last_edit_event[key] = now
        await _event(doc_id, doc["round"], user_id, "CHANGE" if channel == "text" else "NOTE")
    # Told at once when check-ins were cleared; otherwise at most every few seconds, so typing doesn't flood everyone.
    gap_key = (doc_id, "state")
    if cleared or now - _last_edit_event.get(gap_key, -1e9) > 5:
        _last_edit_event[gap_key] = now
        await _broadcast_state(doc_id, {"user_id": str(user_id), "name": await _name(user_id), "channel": channel})


async def check_in(doc_id: UUID, user_id: UUID, req: CheckIn) -> DocDetail:
    doc, reviewers, access = await get_access(doc_id, user_id)
    if not access.can_check_in:
        raise HTTPException(status_code=403, detail="You can't check in on this script")
    can_approve = await subs._can_review(user_id)
    if req.vote == "APPROVE" and not can_approve:
        raise HTTPException(status_code=403, detail="Only a content reviewer can approve")
    if req.vote == "CHANGES" and not can_approve:
        raise HTTPException(status_code=403, detail="A suggesting reviewer can only mark their suggestions as done")
    if req.vote == "DONE" and can_approve:
        raise HTTPException(status_code=400, detail="Choose approve or request changes")
    if req.vote == "CHANGES" and not (req.note or "").strip():
        raise HTTPException(status_code=400, detail="Say what needs to change")
    await execute(
        "INSERT INTO script_document_checkins (document_id, round, user_id, vote, note, pass_to_manager) VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (document_id, round, user_id) DO UPDATE SET vote = EXCLUDED.vote, note = EXCLUDED.note, "
        "pass_to_manager = EXCLUDED.pass_to_manager, checked_at = NOW() RETURNING user_id",
        doc_id, doc["round"], user_id, req.vote, (req.note or "").strip() or None, bool(req.pass_to_manager))
    await _event(doc_id, doc["round"], user_id, "CHECKIN", {"vote": req.vote, "note": req.note, "pass_to_manager": req.pass_to_manager})
    await _broadcast_state(doc_id)
    return await get(doc_id, user_id)


async def withdraw_check_in(doc_id: UUID, user_id: UUID) -> DocDetail:
    doc, reviewers, access = await get_access(doc_id, user_id)
    if doc["status"] != "IN_REVIEW":
        raise HTTPException(status_code=409, detail="The script isn't in review")
    await execute("DELETE FROM script_document_checkins WHERE document_id = $1 AND round = $2 AND user_id = $3 RETURNING user_id",
                  doc_id, doc["round"], user_id)
    await _broadcast_state(doc_id)
    return await get(doc_id, user_id)


def _votes(reviewers: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {"APPROVE": [], "CHANGES": [], "DONE": []}
    for r in reviewers:
        if r["vote"] in out:
            out[r["vote"]].append(r)
    return out


async def _stamp_version(doc_id: UUID, round_: int, label: str, user_id: UUID) -> None:
    await execute(
        "INSERT INTO script_document_versions (document_id, round, label, body_html, body_text, created_by, created_by_name) "
        "SELECT id, $2, $3, body_html, body_text, $4, (SELECT coalesce(nullif(full_name, ''), email) FROM users WHERE id = $4) "
        "FROM script_documents WHERE id = $1 RETURNING id", doc_id, round_, label, user_id)


async def _close(doc: Dict[str, Any], status: str, user_id: UUID, note: Optional[str], label: str, kind: str) -> None:
    await execute(
        "UPDATE script_documents SET status = $2, decision_note = $3, decided_by = $4, decided_at = NOW(), updated_at = NOW() WHERE id = $1 RETURNING id",
        doc["id"], status, (note or "").strip() or None, user_id)
    await _stamp_version(doc["id"], doc["round"], label, user_id)
    await _event(doc["id"], doc["round"], user_id, kind, {"status": status, "note": note})
    await log_audit_event("script_document", str(doc["id"]), kind, user_id, {"status": status})
    await _broadcast_state(doc["id"])


async def finalize(doc_id: UUID, user_id: UUID, req: Finalize) -> DocDetail:
    """Everyone has checked in: send it to the Content Manager (level 2), or approve it back to the creator. Any request
    for changes sends it back to the creator instead."""
    doc, reviewers, access = await get_access(doc_id, user_id)
    if doc["status"] != "IN_REVIEW":
        raise HTTPException(status_code=409, detail="The script isn't in review")
    if not access.can_finalize:
        raise HTTPException(status_code=403, detail="Only a content reviewer who checked in can decide where the script goes")
    if not _all_checked(doc, reviewers):
        raise HTTPException(status_code=409, detail="Every reviewer has to check in first")
    votes = _votes(reviewers)
    if votes["CHANGES"]:
        notes = "; ".join(f"{r['name']}: {r['note']}" for r in votes["CHANGES"] if r["note"])
        await _close(doc, "CHANGES_REQUESTED", user_id, req.note or notes, "Changes requested", "CHANGES_REQUESTED")
        await _notify([doc["created_by"]], "Changes requested on your script", f"“{doc['title']}” needs changes.", doc_id)
    else:
        # Without a content reviewer's approval (only suggesting reviewers took part) the Content Manager has to decide.
        route = "LEVEL2" if (req.route == "LEVEL2" or not votes["APPROVE"]) else "CREATOR"
        if route == "LEVEL2":
            await _close(doc, "LEVEL2_REVIEW", user_id, req.note, "Passed to level 2", "LEVEL2")
            await _notify(await _managers(), "Script waiting for level 2 approval", f"“{doc['title']}” needs the Content Manager's decision.", doc_id)
        else:
            await _close(doc, "APPROVED", user_id, req.note, "Approved", "APPROVED")
            await _pages_module()._approved(doc)
    return await get(doc_id, user_id)


async def decide(doc_id: UUID, user_id: UUID, req: ManagerDecision) -> DocDetail:
    """The Content Manager's final decision (level 2, or earlier: they can decide at any point in review)."""
    doc, reviewers, access = await get_access(doc_id, user_id)
    if not access.can_decide:
        raise HTTPException(status_code=403, detail="Only the Content Manager decides here")
    if req.decision == "SEND_BACK" and not (req.note or "").strip():
        raise HTTPException(status_code=400, detail="Say what needs to change")
    if req.decision == "APPROVE":
        await _close(doc, "APPROVED", user_id, req.note, "Approved by Content Manager", "APPROVED")
        await _pages_module()._approved(doc)
    else:
        await _close(doc, "CHANGES_REQUESTED", user_id, req.note, "Sent back by Content Manager", "CHANGES_REQUESTED")
        await _notify([doc["created_by"]], "Changes requested on your script", f"“{doc['title']}” needs changes.", doc_id)
    return await get(doc_id, user_id)


def _pages_module():
    """The pages module (it imports this one, so it is loaded when first needed)."""
    from services.compliance.evaluation import script_doc_pages
    return script_doc_pages
