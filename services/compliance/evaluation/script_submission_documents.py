"""Create Post uploads entering the existing native/shared script-document review."""

import hashlib
import json
from uuid import UUID, NAMESPACE_URL, uuid5

from fastapi import HTTPException
from postgresql_db.database import fetch_one, get_pool
from services.compliance.evaluation import shared_script_service as shared, shared_script_review as remote
from services.compliance.evaluation.script_doc_templates import shared_script_html
from services.compliance.submissions import submission_service as subs

DETAIL_FIELDS = ("account_ids", "brand_codes", "audiences", "region", "platforms", "rule_sets", "approval_needed_by")
SUBMITTED = ("IN_REVIEW", "CHANGES_REQUESTED", "EDITS_PROPOSED", "APPROVED")


async def linked(submission_id):
    return await fetch_one(
        "SELECT d.id, d.share_document_id FROM script_document_posts p JOIN script_documents d ON d.id = p.document_id "
        "WHERE p.submission_id = $1 AND d.deleted_at IS NULL ORDER BY p.position LIMIT 1", submission_id)


async def require_unlinked(submission_id):
    if doc := await linked(submission_id):
        raise HTTPException(409, f"Continue this script's review in Script documents: /script-docs/{doc['id']}")


async def _plan(row, body, version, status):
    from services.compliance.evaluation import script_doc_service as core
    if not shared.enabled():
        raise HTTPException(503, "Shared-document script review is not enabled")
    owner = row.get("created_by")
    if not owner:
        raise HTTPException(409, "This historical script has no creator. An administrator must restore its creator before shared review.")
    reviewers = list(dict.fromkeys(row.get("reviewer_ids") or []))
    reviewers = [u for u in reviewers if u != owner]
    if status == "IN_REVIEW" and not reviewers:
        raise HTTPException(422, "Pick at least one reviewer other than the creator for shared-document review")
    managers = await core._managers()
    emails = await remote._emails([owner, *reviewers, *managers])
    if owner not in emails or any(u not in emails for u in reviewers):
        raise HTTPException(422, "The script creator or an assigned reviewer no longer has a user account")
    invitees = [{"email": emails[u], "role": "REVIEWER" if await subs._can_review(u) else "SUGGESTER", "capacity": "EDIT"} for u in reviewers]
    invitees += [{"email": emails[u], "role": "MANAGER", "capacity": "EDIT", "optional": True}
                 for u in managers if u in emails and u not in reviewers and u != owner]
    doc_id = uuid5(NAMESPACE_URL, f"social-media:submission-script:{row['id']}")
    from services.compliance.evaluation.script_submission_content import submission_beats
    details, beats = submission_beats(row["title"], body)
    payload = {
        "document_id": f"doc-social-submission-{doc_id.hex}", "user_email": emails[owner], "title": row["title"],
        "details": details, "rows": beats, "invitees": invitees, "status": status, "round": max(version, 1),
        "source_hash": hashlib.sha256(json.dumps({"body": body, "version": version, "status": status, "reviewers": [str(u) for u in reviewers]}, sort_keys=True).encode()).hexdigest(),
        "external_ref": {"system": shared.SOURCE_SERVICE, "id": str(doc_id), "submissionId": str(row["id"])}}
    return doc_id, reviewers, payload


async def _prepare(row, body, version, status):
    doc_id, reviewers, payload = await _plan(row, body, version, status)
    got = await remote._call("POST", "/from-submission", ["script_documents:write"], json=payload)
    return doc_id, reviewers, got


async def _persist(conn, row, doc_id, reviewers, got):
    details = {k: row.get(k) for k in DETAIL_FIELDS}
    details.update(reviewer_ids=[str(u) for u in reviewers], source_submission_id=str(row["id"]))
    encoded = json.dumps(details, default=str)
    text = got.get("text") or ""
    html = shared_script_html(1, row["title"], text)
    await conn.execute(
        "INSERT INTO script_sets (id, title, created_by, details) VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (id) DO NOTHING",
        doc_id, row["title"], row["created_by"], encoded)
    inserted = await conn.fetchval(
        "INSERT INTO script_documents (id, set_id, title, created_by, status, round, body_html, body_text, details, share_document_id) "
        "VALUES ($1, $1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9) ON CONFLICT (id) DO NOTHING RETURNING id",
        doc_id, row["title"], row["created_by"], got["status"], got["round"], html, text, encoded, got["document_id"])
    if not inserted:
        return
    await conn.executemany(
        "INSERT INTO script_document_reviewers (document_id, user_id, position) VALUES ($1, $2, $3)",
        [(doc_id, u, pos) for pos, u in enumerate(reviewers)])
    await conn.execute(
        "INSERT INTO script_document_posts (document_id, position, title, body, submission_id) VALUES ($1, 1, $2, $3, $4)",
        doc_id, row["title"], text, row["id"])
    await conn.execute(
        "INSERT INTO script_document_versions (document_id, round, label, body_html, body_text, created_by) "
        "VALUES ($1, $2, 'Imported submitted script', $3, $4, $5)", doc_id, got["round"], html, text, row["created_by"])


async def send_checked(row, body, source, user_id, check, file_id=None, note=None, notify=True):
    from services.compliance.evaluation import script_review_service as scripts, script_doc_service as core
    await require_unlinked(row["id"])
    latest = await fetch_one("SELECT coalesce(max(version_number), 0) AS n FROM content_script_versions WHERE submission_id = $1", row["id"])
    version = latest["n"] + 1
    doc_id, reviewers, got = await _prepare(row, body, version, "IN_REVIEW")
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await scripts._lock_status(conn, row["id"], ("DRAFT",) + scripts.CREATOR_TURN)
            await scripts._add_version(conn, row, body, source, user_id, check, file_id)
            await _persist(conn, row, doc_id, reviewers, got)
            await conn.execute(
                "UPDATE content_submissions SET script_status = $3, script_send = NULL, updated_by = $2, updated_at = now(), "
                "due_at = NULL, due_kind = NULL, due_reminded_at = NULL, due_overdue_notified_at = NULL WHERE id = $1",
                row["id"], user_id, "IN_REVIEW" if got["status"] == "LEVEL2_REVIEW" else got["status"])
            await subs._add_event(conn, row["id"], "SCRIPT_SUBMITTED", f"Script version {version} sent for shared-document review", user_id,
                                  {"script_document_id": str(doc_id), "script_version": version})
    if notify:
        await core._notify(reviewers, "Script to review", f"{row['title']} was sent to you for review.", doc_id)
        await core._notify([row["created_by"]], "Script sent for review", f"{row['title']} is ready for shared-document review.", doc_id)
    return await scripts.get_stage(row["id"], user_id)


async def adopt(submission_id: UUID, user_id: UUID):
    """Explicitly open a historical upload in the canonical editor; reads/listing never import records."""
    row = await subs._get_owned_row(submission_id, user_id, manager_can_read=True)
    if found := await linked(submission_id):
        return {"document_id": str(found["id"])}
    if not shared.enabled():
        return {"document_id": None}
    if not row.get("script_required") or row.get("script_status") not in SUBMITTED:
        raise HTTPException(409, "Submit this script for review before opening its shared document")
    version = await fetch_one("SELECT body, version_number FROM content_script_versions WHERE submission_id = $1 ORDER BY version_number DESC LIMIT 1", submission_id)
    if not version:
        raise HTTPException(409, "There is no submitted script to import")
    status = "CHANGES_REQUESTED" if row["script_status"] == "EDITS_PROPOSED" else row["script_status"]
    doc_id, reviewers, payload = await _plan(row, version["body"], version["version_number"], status)
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            locked = await conn.fetchrow("SELECT * FROM content_submissions WHERE id = $1 FOR UPDATE", submission_id)
            found = await conn.fetchrow(
                "SELECT d.id FROM script_document_posts p JOIN script_documents d ON d.id = p.document_id "
                "WHERE p.submission_id = $1 AND d.deleted_at IS NULL LIMIT 1", submission_id)
            if found:
                return {"document_id": str(found["id"])}
            if not locked or locked["script_status"] != row["script_status"] or locked["updated_at"] != row["updated_at"]:
                raise HTTPException(409, "The script changed. Reload and try again.")
            # All portal lookups happened before this lock; only the remote RPC and this connection are used now.
            got = await remote._call("POST", "/from-submission", ["script_documents:write"], json=payload)
            await _persist(conn, dict(locked), doc_id, reviewers, got)
    return {"document_id": str(doc_id)}
