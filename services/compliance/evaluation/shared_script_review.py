"""A script page's review, handed to the Share Document server.

When a page has a shared document (see shared_script_service), the review of it is run there: who may edit, check-ins, routing
and decisions are decided by the shared server's rules (the same rules this portal uses), and this portal mirrors the result
(shared_script_sync). So before this portal moves its own state, the same move is made there first: if the shared server
refuses (or can't be reached), nothing changes here and the person is told why. A page without a shared document, or with
SCRIPT_EDITOR=legacy, is not touched: every function here then does nothing.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException

from postgresql_db.database import fetch_all, fetch_one
from services.compliance.evaluation import shared_script_service as shared
from services.compliance.evaluation.script_doc_templates import text_to_html

logger = logging.getLogger(__name__)


def shared_id(doc: Dict[str, Any]) -> Optional[str]:
    """The page's shared document, when the shared editor is on and the page has one."""
    return doc.get("share_document_id") if shared.enabled() else None


async def _email(user_id: UUID) -> str:
    row = await fetch_one("SELECT email FROM users WHERE id = $1", user_id)
    return row["email"] if row else ""


async def _emails(user_ids: List[UUID]) -> Dict[UUID, str]:
    rows = await fetch_all("SELECT id, email FROM users WHERE id = ANY($1::uuid[])", list(user_ids))
    return {r["id"]: r["email"] for r in rows}


async def _call(method: str, path: str, scopes: List[str], **kwargs: Any) -> Dict[str, Any]:
    try:
        return await shared.call(method, path, scopes, **kwargs)
    except shared.SharedScriptError as e:
        # The shared server's own refusals (403, 409, 422...) are the person's to read; its being down is a 502.
        status = e.status_code if 400 <= e.status_code < 500 else 502
        raise HTTPException(status_code=status, detail=str(e)) from e


async def body(doc: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(html, text) of the script as the shared editor holds it now, for the page's sending, or None for a page without one."""
    sid = shared_id(doc)
    if not sid:
        return None
    got = await _call("GET", f"/{sid}", ["script_documents:read"], params={"email": await _email(doc["created_by"])})
    text = (got.get("text") or "").strip()
    return text_to_html(doc["title"], text), text


async def send(doc: Dict[str, Any], user_id: UUID, picks: List[Any], managers: List[UUID], can_review: Dict[UUID, bool], note: Optional[str]) -> None:
    """Invite the people picked (a content reviewer if they hold the review permission, else a suggesting reviewer, with the
    capacity they were given) and the Content Managers, and send the script for review as its creator."""
    sid = shared_id(doc)
    if not sid:
        return
    emails = await _emails([*(p.user_id for p in picks), *managers])
    invitees = [{"email": emails[p.user_id], "capacity": p.capacity,
                 "role": "REVIEWER" if can_review.get(p.user_id) else "SUGGESTER"} for p in picks if p.user_id in emails]
    picked = {p.user_id for p in picks}
    # Managers see every script and can decide at any point; one who isn't a shared user yet is simply left out.
    invitees += [{"email": emails[m], "role": "MANAGER", "capacity": "EDIT", "optional": True}
                 for m in managers if m in emails and m not in picked and m != doc["created_by"]]
    await _call("POST", f"/{sid}/send", ["script_documents:write"],
                json={"actor_email": await _email(user_id), "invitees": invitees, "note": note})


async def act(doc: Dict[str, Any], user_id: UUID, action: str, **fields: Any) -> None:
    """One move of the review (check_in, withdraw, finalize, decide) as this person."""
    sid = shared_id(doc)
    if sid:
        await _call("POST", f"/{sid}/actions", ["script_documents:write"],
                    json={"actor_email": await _email(user_id), "action": action, **{k: v for k, v in fields.items() if v is not None}})


async def editor_session(doc: Dict[str, Any], user_id: UUID) -> Dict[str, Any]:
    """What the page needs to show the shared editor for this person: where it is, and a short-lived sign-in for it."""
    sid = shared_id(doc) or await shared.ensure_shared_document(doc["id"])
    if not sid:
        raise HTTPException(status_code=503, detail="The shared editor isn't available for this script right now")
    token = await _call("POST", f"/{sid}/embed-token", ["script_documents:write"], params={"email": await _email(user_id)})
    front = os.getenv("SHARE_DOCUMENT_FRONT_URL", "http://localhost:5176").rstrip("/")
    return {"share_document_id": sid, "url": f"{front}/embed/script/{sid}", "origin": front, **token}
