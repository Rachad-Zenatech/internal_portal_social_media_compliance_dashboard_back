"""Script documents: the creator's document and its pages (one script each), and the post an approved script becomes.

A page is a script_documents row of its own; the document (script_sets) holds what its pages share. Pages are added empty,
or from an uploaded file split into scripts as an uploaded batch is; every page is sent for review on its own or together.
"""

import json
import logging
import re
from typing import Any, Dict, List
from uuid import UUID

from fastapi import HTTPException

from models.compliance.script_batch_model import BatchDetails
from models.compliance.script_doc_model import (
    BulkFailure, BulkResult, BulkSubmit, DocDetail, DocPage, DocSetSummary, DocSubmit, UploadedPages,
)
from models.compliance.submission_model import SubmissionCreate, SubmissionUpdate
from postgresql_db.database import execute, fetch_all, fetch_one, get_pool
from services.compliance.evaluation import script_batch_service as batches
from services.compliance.evaluation import script_doc_service as core
from services.compliance.evaluation.script_doc_templates import empty_script_html, text_to_html
from services.compliance.evaluation.script_split import split_scripts
from services.compliance.evaluation.script_doc_split import split_document
from services.compliance.submissions import submission_service as subs
from services.shared.core.audit_service import log_audit_event

logger = logging.getLogger(__name__)


async def _approved(doc: Dict[str, Any]) -> None:
    """The script was approved: it becomes a post for the creator to attach a video to."""
    made = 0
    try:
        made = await _make_posts(doc["id"])
    except Exception:
        logger.exception("Couldn't make the post for script document %s", doc["id"])
    message = (f"“{doc['title']}” was approved. A post was made: open it to attach the video." if made
               else f"“{doc['title']}” was approved.")
    await core._notify([doc["created_by"]], "Your script was approved", message, doc["id"])


async def _make_posts(doc_id: UUID) -> int:
    """The video post for an approved script, with the document's details and the approved wording as its approved
    script. Returns how many were made (0 when it already has one)."""
    doc = await core._doc(doc_id)
    creator = doc["created_by"]
    have = await fetch_one("SELECT submission_id FROM script_document_posts WHERE document_id = $1 AND position = 1", doc_id)
    if have and have["submission_id"]:
        return 0
    shared = BatchDetails(**(subs._json(doc["details"]) or {})).model_dump(exclude_none=True)
    title, text = doc["title"], doc["body_text"]
    post = await subs.create_submission(SubmissionCreate(title=title, content_type="VIDEO"), creator)
    try:
        await subs.update_submission(post.id, SubmissionUpdate(**shared), creator)
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO content_script_versions (submission_id, version_number, body, source, created_by) "
                    "VALUES ($1, 1, $2, 'UPLOAD', $3)", post.id, text, creator)
                await conn.execute(
                    "UPDATE content_submissions SET script_required = TRUE, script_status = 'APPROVED', updated_at = NOW() WHERE id = $1", post.id)
                await subs._add_event(conn, post.id, "SCRIPT_APPROVED", "Script approved in a script document", creator)
    except Exception:
        await execute("UPDATE content_submissions SET deleted_at = NOW() WHERE id = $1 RETURNING id", post.id)
        raise
    await execute(
        "INSERT INTO script_document_posts (document_id, position, title, body, submission_id) VALUES ($1, 1, $2, $3, $4) "
        "ON CONFLICT (document_id, position) DO UPDATE SET title = EXCLUDED.title, body = EXCLUDED.body, "
        "submission_id = EXCLUDED.submission_id RETURNING position", doc_id, title, text, post.id)
    return 1


async def create_posts(doc_id: UUID, user_id: UUID) -> DocDetail:
    """Make the post that couldn't be made when the script was approved (the creator retries)."""
    doc, reviewers, access = await core.get_access(doc_id, user_id)
    if doc["status"] != "APPROVED":
        raise HTTPException(status_code=409, detail="The post is made once the script is approved")
    if doc["created_by"] != user_id and not access.can_decide:
        raise HTTPException(status_code=403, detail="Only the creator can make the post")
    await _make_posts(doc_id)
    return await core.get(doc_id, user_id)


# --- The creator's document: its pages ---------------------------------------------------------------------------------

async def _pages(set_id: UUID) -> List[DocPage]:
    rows = await fetch_all(
        """
        SELECT d.id, d.title, d.status, d.position, d.round,
               (SELECT count(*) FROM script_document_reviewers r WHERE r.document_id = d.id AND r.capacity = 'EDIT') AS required,
               (SELECT count(*) FROM script_document_checkins c JOIN script_document_reviewers r
                  ON r.document_id = c.document_id AND r.user_id = c.user_id AND r.capacity = 'EDIT'
                WHERE c.document_id = d.id AND c.round = d.round) AS checked_in
        FROM script_documents d WHERE d.set_id = $1 AND d.deleted_at IS NULL ORDER BY d.position, d.created_at
        """, set_id)
    return [DocPage(**r) for r in rows]


async def _owned_set(doc_id: UUID, user_id: UUID) -> tuple:
    """The script and the id of the document it belongs to (made on the spot for a script that has none), for the creator."""
    doc = await core._doc(doc_id)
    if doc["created_by"] != user_id:
        raise HTTPException(status_code=403, detail="Only the creator can change the pages of their document")
    set_id = doc.get("set_id")
    if not set_id:
        set_id = await _make_set(doc)
    return doc, set_id


async def _make_set(doc: Dict[str, Any]) -> UUID:
    row = await fetch_one("INSERT INTO script_sets (title, created_by, details) VALUES ($1, $2, $3::jsonb) RETURNING id",
                          doc["title"], doc["created_by"], json.dumps(subs._json(doc["details"]) or {}))
    await execute("UPDATE script_documents SET set_id = $2, position = 1 WHERE id = $1 RETURNING id", doc["id"], row["id"])
    return row["id"]


async def _new_page(set_id: UUID, creator: UUID, title: str, html: str, text: str = "") -> UUID:
    set_row = await fetch_one("SELECT details FROM script_sets WHERE id = $1", set_id)
    row = await fetch_one(
        "INSERT INTO script_documents (title, template_key, created_by, body_html, body_text, set_id, details, position) "
        "VALUES ($1, 'blank', $2, $3, $4, $5, $6::jsonb, (SELECT coalesce(max(position), 0) + 1 FROM script_documents WHERE set_id = $5)) "
        "RETURNING id", title[:300], creator, html, text, set_id, json.dumps(subs._json(set_row["details"]) or {}))
    await core._event(row["id"], 0, creator, "CREATED", {"page": True})
    await execute("UPDATE script_sets SET updated_at = NOW() WHERE id = $1 RETURNING id", set_id)
    return row["id"]


async def add_page(doc_id: UUID, user_id: UUID) -> DocDetail:
    """A new page: another script in the same document, on its own page with its own table, sent for review on its own."""
    doc, set_id = await _owned_set(doc_id, user_id)
    n = len(await _pages(set_id)) + 1
    title = "New script"
    new_id = await _new_page(set_id, user_id, title, empty_script_html(n, title))
    return await core.get(new_id, user_id)


async def _add_uploaded(set_id: UUID, user_id: UUID, filename: str, data: bytes) -> List[DocPage]:
    """Read an uploaded file, split it into scripts as an uploaded batch is, and add each as a page of the document."""
    text = await batches.read_upload(filename, data)
    found = split_scripts(text)
    if not found:
        raise HTTPException(status_code=422, detail="No scripts were found in that file")
    if len(found) > batches.MAX_SCRIPTS:
        raise HTTPException(status_code=422, detail=f"That file has {len(found)} scripts; {batches.MAX_SCRIPTS} is the most at once")
    stem = filename.rsplit(".", 1)[0].strip() or "Uploaded script"
    if len(found) == 1:
        found = [(stem, found[0][1])]
    first = len(await _pages(set_id)) + 1
    ids = []
    for n, (raw_title, body) in enumerate(found, start=first):
        title = re.sub(r"^\s*\d{1,3}\s*[.):\-–]\s*", "", raw_title).strip() or raw_title
        ids.append(await _new_page(set_id, user_id, title, text_to_html(f"{n}. {title}", body), body))
    await log_audit_event("script_document", str(set_id), "UPLOAD_PAGES", user_id, {"file": filename, "scripts": len(found)})
    return [p for p in await _pages(set_id) if p.id in ids]


async def upload_pages(doc_id: UUID, user_id: UUID, filename: str, data: bytes) -> UploadedPages:
    """Upload a file into an existing document: each script in it becomes a page."""
    doc, set_id = await _owned_set(doc_id, user_id)
    return UploadedPages(created=await _add_uploaded(set_id, user_id, filename, data))


async def create_from_upload(user_id: UUID, filename: str, data: bytes) -> UploadedPages:
    """Attach a script: a new document made from an uploaded file, one page for each script in it."""
    # Read it first, so a file that can't be read doesn't leave an empty document behind.
    await batches.read_upload(filename, data)
    title = (filename.rsplit(".", 1)[0].strip() or "Uploaded script")[:300]
    set_id = await fetch_one("INSERT INTO script_sets (title, created_by) VALUES ($1, $2) RETURNING id", title, user_id)
    try:
        return UploadedPages(created=await _add_uploaded(set_id["id"], user_id, filename, data))
    except Exception:
        await execute("UPDATE script_sets SET deleted_at = NOW() WHERE id = $1 RETURNING id", set_id["id"])
        raise


async def submit_all(doc_id: UUID, user_id: UUID, req: BulkSubmit) -> BulkResult:
    """Send every page that can be sent (or the ones listed) for review at once, each as its own script. A page that can't
    be sent (its details aren't filled in, it is empty) is reported; the others still go."""
    doc, set_id = await _owned_set(doc_id, user_id)
    rows = await fetch_all(
        "SELECT id, title FROM script_documents WHERE set_id = $1 AND created_by = $2 AND deleted_at IS NULL "
        "AND status IN ('DRAFT', 'CHANGES_REQUESTED') ORDER BY position", set_id, user_id)
    if req.ids is not None:
        rows = [r for r in rows if r["id"] in set(req.ids)]
    if not rows:
        raise HTTPException(status_code=409, detail="There are no scripts to send")
    sent, failed = 0, []
    for r in rows:
        try:
            await core.submit(r["id"], user_id, DocSubmit(reviewers=req.reviewers, note=req.note))
            sent += 1
        except HTTPException as e:
            failed.append(BulkFailure(id=r["id"], title=r["title"], reason=str(e.detail)))
    return BulkResult(sent=sent, failed=failed)


async def list_sets(user_id: UUID) -> List[DocSetSummary]:
    """The creator's documents for the hub, with how their scripts stand."""
    rows = await fetch_all(
        """
        SELECT s.id, s.title, s.updated_at,
               (SELECT d2.id FROM script_documents d2 WHERE d2.set_id = s.id AND d2.deleted_at IS NULL ORDER BY d2.position LIMIT 1) AS first_id,
               count(d.id) AS total,
               count(*) FILTER (WHERE d.status = 'DRAFT') AS draft,
               count(*) FILTER (WHERE d.status IN ('IN_REVIEW', 'LEVEL2_REVIEW')) AS in_review,
               count(*) FILTER (WHERE d.status = 'CHANGES_REQUESTED') AS changes_requested,
               count(*) FILTER (WHERE d.status = 'APPROVED') AS approved
        FROM script_sets s JOIN script_documents d ON d.set_id = s.id AND d.deleted_at IS NULL
        WHERE s.created_by = $1 AND s.deleted_at IS NULL
        GROUP BY s.id ORDER BY s.updated_at DESC LIMIT 200
        """, user_id)
    return [
        DocSetSummary(id=r["id"], title=r["title"], first_script_id=r["first_id"], total=r["total"], draft=r["draft"],
                      in_review=r["in_review"], changes_requested=r["changes_requested"], approved=r["approved"],
                      needs_you=r["changes_requested"] > 0 or r["approved"] > 0, updated_at=r["updated_at"])
        for r in rows
    ]


async def delete_set(set_id: UUID, user_id: UUID) -> None:
    """Delete a whole document of the creator's, only while every script in it is still a draft."""
    row = await fetch_one("SELECT created_by FROM script_sets WHERE id = $1 AND deleted_at IS NULL", set_id)
    if not row:
        raise HTTPException(status_code=404, detail="Script not found")
    if row["created_by"] != user_id and not await subs._can_manage_team_content(user_id):
        raise HTTPException(status_code=403, detail="You can't delete this script")
    sent = await fetch_one(
        "SELECT count(*) AS n FROM script_documents WHERE set_id = $1 AND deleted_at IS NULL AND status <> 'DRAFT'", set_id)
    if sent["n"]:
        raise HTTPException(status_code=409, detail="Only a script that hasn't been sent for review can be deleted")
    await fetch_all("UPDATE script_documents SET deleted_at = NOW() WHERE set_id = $1 AND deleted_at IS NULL RETURNING id", set_id)
    await execute("UPDATE script_sets SET deleted_at = NOW() WHERE id = $1 RETURNING id", set_id)
    await log_audit_event("script_document", str(set_id), "DELETE_SET", user_id, {})
