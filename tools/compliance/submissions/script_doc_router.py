"""Script Document Router
Scripts written from a template and reviewed live by several reviewers: the hub's list, the document, the review flow
(send, check in, finalize, level 2) and the live editing connection.
"""

import asyncio
import json
import logging
from typing import Annotated, Any, Dict, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, WebSocket, status

from models.compliance.script_doc_model import (
    BulkResult, BulkSubmit, CheckIn, DocCreate, DocSetSummary, UploadedPages, DocDetail, DocHistory, DocSnapshot, DocSubmit, DocSummary, DocUpdate, Finalize, ManagerDecision,
    ScriptTemplate,
)
from models.compliance.submission_model import ReviewerOption
from postgresql_db.database import fetch_one
from services.compliance.evaluation import script_doc_realtime as realtime
from services.compliance.evaluation import script_doc_pages as pages
from services.compliance.evaluation import script_doc_reads as reads
from services.compliance.evaluation import script_doc_service as svc
from services.compliance.evaluation import shared_script_review as sharedrev
from tools.compliance.submissions.script_directory_router import router as directory_router
from services.shared.auth.auth_service import AUTH_COOKIE_NAME, get_current_user_id_dependency, verify_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/script-docs", tags=["Script Documents"])
router.include_router(directory_router)  # /script-docs/service/script-users, for the Share Document portal (declared first so no /{doc_id} route takes it)
CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.get("/templates", response_model=List[ScriptTemplate])
async def list_templates(user_id: CurrentUser):
    """The script templates a creator can start from."""
    return svc.templates()


@router.post("/upload", response_model=UploadedPages)
async def attach_script(user_id: CurrentUser, file: UploadFile = File(...)):
    """Attach a script: a new document made from an uploaded file, with a page for each script in it."""
    return await pages.create_from_upload(user_id, file.filename or "script", await file.read())


@router.delete("/sets/{set_id}", status_code=204)
async def delete_set(set_id: UUID, user_id: CurrentUser):
    """Delete a document and its scripts, only while every one is still a draft."""
    await pages.delete_set(set_id, user_id)
    return Response(status_code=204)


@router.get("/sets", response_model=List[DocSetSummary])
async def list_sets(user_id: CurrentUser):
    """The creator's documents (each with its scripts as pages)."""
    return await pages.list_sets(user_id)


@router.get("", response_model=List[DocSummary])
async def list_docs(user_id: CurrentUser, scope: Literal["mine", "review", "all"] = Query("mine")):
    """mine: scripts the user wrote; review: scripts sent to them; all: every script (Content Managers)."""
    return await reads.list_docs(user_id, scope)


@router.post("", response_model=DocDetail)
async def create_doc(req: DocCreate, user_id: CurrentUser):
    return await svc.create(user_id, req)


@router.get("/{doc_id}/shared-editor")
async def get_shared_editor(doc_id: UUID, user_id: CurrentUser):
    """Where the Share Document server's editor for this script is, and a 15-minute sign-in for it (for the iframe)."""
    doc, _, _ = await svc.get_access(doc_id, user_id)
    return await sharedrev.editor_session(doc, user_id)


@router.get("/{doc_id}", response_model=DocDetail)
async def get_doc(doc_id: UUID, user_id: CurrentUser):
    return await svc.get(doc_id, user_id)


@router.patch("/{doc_id}", response_model=DocDetail)
async def update_doc(doc_id: UUID, req: DocUpdate, user_id: CurrentUser):
    return await svc.update(doc_id, user_id, req)


@router.delete("/{doc_id}", status_code=204)
async def delete_doc(doc_id: UUID, user_id: CurrentUser):
    await svc.delete(doc_id, user_id)
    return Response(status_code=204)


@router.put("/{doc_id}/snapshot", status_code=204)
async def save_snapshot(doc_id: UUID, req: DocSnapshot, user_id: CurrentUser):
    """An editor saves the script as it reads now."""
    await svc.save_snapshot(doc_id, user_id, req.html, req.text)
    return Response(status_code=204)


@router.get("/{doc_id}/reviewers", response_model=List[ReviewerOption])
async def reviewer_options(doc_id: UUID, user_id: CurrentUser):
    """The people the creator can send this script to."""
    return await svc.reviewer_options(user_id, doc_id)


@router.post("/{doc_id}/submit", response_model=DocDetail)
async def submit(doc_id: UUID, req: DocSubmit, user_id: CurrentUser):
    """Send the script for review. At least one reviewer is required."""
    return await svc.submit(doc_id, user_id, req)


@router.post("/{doc_id}/check-in", response_model=DocDetail)
async def check_in(doc_id: UUID, req: CheckIn, user_id: CurrentUser):
    """A reviewer says they are finished (approve, request changes, or done for a suggesting reviewer)."""
    return await svc.check_in(doc_id, user_id, req)


@router.delete("/{doc_id}/check-in", response_model=DocDetail)
async def withdraw_check_in(doc_id: UUID, user_id: CurrentUser):
    return await svc.withdraw_check_in(doc_id, user_id)


@router.post("/{doc_id}/finalize", response_model=DocDetail)
async def finalize(doc_id: UUID, req: Finalize, user_id: CurrentUser):
    """Every reviewer has checked in: pass to the Content Manager (level 2) or approve back to the creator."""
    return await svc.finalize(doc_id, user_id, req)


@router.post("/{doc_id}/decision", response_model=DocDetail)
async def decide(doc_id: UUID, req: ManagerDecision, user_id: CurrentUser):
    """The Content Manager's final decision."""
    return await svc.decide(doc_id, user_id, req)


@router.post("/{doc_id}/pages", response_model=DocDetail)
async def add_page(doc_id: UUID, user_id: CurrentUser):
    """A new page (script) in the same document, with its own table."""
    return await pages.add_page(doc_id, user_id)


@router.post("/{doc_id}/pages/upload", response_model=UploadedPages)
async def upload_pages(doc_id: UUID, user_id: CurrentUser, file: UploadFile = File(...)):
    """Upload a file: it is split into scripts and each becomes a page of the document."""
    return await pages.upload_pages(doc_id, user_id, file.filename or "script", await file.read())


@router.post("/{doc_id}/pages/submit", response_model=BulkResult)
async def submit_pages(doc_id: UUID, req: BulkSubmit, user_id: CurrentUser):
    """Send the document's scripts for review together, each on its own."""
    return await pages.submit_all(doc_id, user_id, req)


@router.post("/{doc_id}/posts", response_model=DocDetail)
async def create_posts(doc_id: UUID, user_id: CurrentUser):
    """Make the posts for an approved script document that could not be made when it was approved."""
    return await pages.create_posts(doc_id, user_id)


@router.get("/{doc_id}/history", response_model=DocHistory)
async def history(doc_id: UUID, user_id: CurrentUser):
    return await reads.history(doc_id, user_id)


@router.post("/{doc_id}/check", response_model=Dict[str, Any])
async def check(doc_id: UUID, user_id: CurrentUser, body: Optional[Dict[str, Any]] = None):
    """The rulebook check of the script as it reads now (or of `text`)."""
    return await reads.check(doc_id, user_id, (body or {}).get("text"))


async def _valid_user(token: str) -> Optional[UUID]:
    payload = verify_token(token)
    if not payload or payload.get("scope") == "link" or payload.get("is_service_token"):
        return None
    try:
        uid = UUID(str(payload["sub"]))
    except (KeyError, ValueError):
        return None
    if await fetch_one("SELECT 1 FROM users WHERE id = $1 AND is_active AND deleted_at IS NULL", uid):
        return uid
    return None


async def _ws_user(ws: WebSocket) -> Optional[UUID]:
    """Who the connection is: the sign-in cookie, or a first message {"type": "auth", "token": ...}. A token is never
    taken from the URL."""
    candidates: List[str] = [t for t in (ws.cookies.get(AUTH_COOKIE_NAME),) if t]
    header = ws.headers.get("authorization", "")
    if header.startswith("Bearer "):
        candidates.append(header[7:].strip())
    for token in candidates:
        uid = await _valid_user(token)
        if uid:
            return uid
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        if first.get("type") == "auth" and isinstance(first.get("token"), str):
            return await _valid_user(first["token"])
    except Exception:
        pass
    return None


@router.websocket("/{doc_id}/live")
async def live(ws: WebSocket, doc_id: UUID, notes_only: bool = Query(False)):
    """The live connection for one script: its text and notes, or (`notes_only`, for a script edited in the Share Document
    portal) just the notes."""
    await ws.accept()
    user_id = await _ws_user(ws)
    if not user_id:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        await realtime.run(ws, doc_id, user_id, notes_only)
    except HTTPException:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
