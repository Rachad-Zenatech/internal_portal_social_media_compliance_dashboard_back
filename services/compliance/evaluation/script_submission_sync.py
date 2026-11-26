"""Mirror canonical script-document review back to its original Create Post submission."""

import json
from postgresql_db.database import fetch_one, get_pool
from services.compliance.evaluation import shared_script_review as remote
from services.compliance.evaluation.script_doc_sources import export_text

STATUS = {"IN_REVIEW": "IN_REVIEW", "LEVEL2_REVIEW": "IN_REVIEW", "CHANGES_REQUESTED": "CHANGES_REQUESTED", "APPROVED": "APPROVED"}


async def sync_document(doc_id):
    doc = await fetch_one(
        "SELECT d.*, p.submission_id, p.body AS imported_body FROM script_documents d JOIN script_document_posts p ON p.document_id = d.id "
        "WHERE d.id = $1 AND p.position = 1", doc_id)
    if not doc or doc["status"] not in STATUS:
        return
    # Only adopted uploads are mirrored; native documents still create posts through the existing approval flow.
    details = json.loads(doc["details"]) if isinstance(doc["details"], str) else (doc["details"] or {})
    if str(doc["submission_id"]) != details.get("source_submission_id"):
        return
    body = doc["body_text"]
    html = doc["body_html"]
    if current := await remote.body(dict(doc)):
        html, body = current
    script_body = body
    body = export_text(doc, body)
    state = STATUS[doc["status"]]
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            source = await conn.fetchrow("SELECT script_status FROM content_submissions WHERE id = $1 FOR UPDATE", doc["submission_id"])
            if not source:
                return
            latest = await conn.fetchrow(
                "SELECT version_number, body FROM content_script_versions WHERE submission_id = $1 ORDER BY version_number DESC LIMIT 1", doc["submission_id"])
            changed = not latest or (body.strip() != doc["imported_body"].strip() and latest["body"].replace("\r\n", "\n").strip() != body.replace("\r\n", "\n").strip())
            if changed:
                number = (latest["version_number"] if latest else 0) + 1
                await conn.execute(
                    "INSERT INTO content_script_versions (submission_id, version_number, body, source, created_by) VALUES ($1, $2, $3, $4, $5)",
                    doc["submission_id"], number, body, "CREATOR_EDIT" if state == "IN_REVIEW" else "REVIEWER_EDIT", doc.get("decided_by") or doc["created_by"])
            else:
                number = latest["version_number"]
            if state in ("APPROVED", "CHANGES_REQUESTED"):
                await conn.execute(
                    "UPDATE content_script_versions SET review_decision = $3, review_note = $4, reviewed_by = $5, reviewed_at = now() "
                    "WHERE submission_id = $1 AND version_number = $2 AND review_decision IS DISTINCT FROM $3",
                    doc["submission_id"], number, "APPROVE" if state == "APPROVED" else "SEND_BACK", doc.get("decision_note"), doc.get("decided_by"))
            reviewers = await conn.fetch("SELECT user_id FROM script_document_reviewers WHERE document_id = $1 ORDER BY position", doc_id)
            await conn.execute(
                "UPDATE content_submissions SET script_status = $2, script_send = NULL, reviewer_ids = $3::uuid[], "
                "script_precheck = CASE WHEN $4 THEN NULL ELSE script_precheck END, "
                "latest_check = CASE WHEN $4 THEN NULL ELSE latest_check END, updated_at = now() WHERE id = $1",
                doc["submission_id"], state, [r["user_id"] for r in reviewers], changed)
            await conn.execute("UPDATE script_documents SET body_html = $2, body_text = $3 WHERE id = $1", doc_id, html, script_body)
            await conn.execute("UPDATE script_document_posts SET body = $2 WHERE document_id = $1 AND position = 1", doc_id, body)
