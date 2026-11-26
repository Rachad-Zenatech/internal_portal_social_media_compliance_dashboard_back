"""Evidence belonging to one script, independent of document-wide publishing details."""
from urllib.parse import quote
from uuid import UUID

from fastapi import HTTPException, Response

from models.compliance.script_doc_model import DocSourcesUpdate
from postgresql_db.database import fetch_one
from services.compliance.media.script_sources import extract


def sources_for(doc: dict) -> str:
    """Old imports remain readable until their sources are explicitly saved or cleared."""
    saved = doc.get("sources_text")
    return saved if saved is not None else extract(doc.get("body_text") or "")[1]


def export_text(doc: dict, text: str | None = None) -> str:
    """Put evidence back into the template's source row for checks, approved posts and Word exports."""
    if text is None:
        from services.compliance.media.html_layout import html_to_text
        text = html_to_text(doc.get("body_html")) or doc.get("body_text", "")
    body, _ = extract(text)
    sources = sources_for(doc).strip()
    if not sources:
        return body
    entries = "; ".join(line.strip().replace("|", "/") for line in sources.splitlines() if line.strip())
    row = f"| Sources used | {entries} |"
    lines = body.splitlines()
    table_end = max((i + 1 for i, line in enumerate(lines) if line.strip().startswith("|") and line.strip().endswith("|")), default=len(lines))
    lines.insert(table_end, row)
    return "\n".join(lines)


async def save(doc_id: UUID, user_id: UUID, req: DocSourcesUpdate):
    from services.compliance.evaluation import script_doc_service as core

    doc, _, access = await core.get_access(doc_id, user_id)
    editable = {"DRAFT", "CHANGES_REQUESTED", "IN_REVIEW", "LEVEL2_REVIEW"}
    if not access.can_edit or doc["status"] not in editable:
        raise HTTPException(status_code=403, detail="You can't edit this script's sources")
    if sources_for(doc) != req.expected_sources_text:
        raise HTTPException(status_code=409, detail="Sources changed in another session. Refresh the script before saving again.")
    updated = await fetch_one(
        "UPDATE script_documents SET sources_text = $2, updated_at = NOW() WHERE id = $1 "
        "AND sources_text IS NOT DISTINCT FROM $3 AND status = ANY($4::text[]) RETURNING id",
        doc_id, req.sources_text.strip(), doc.get("sources_text"), list(editable))
    if not updated:
        raise HTTPException(status_code=409, detail="This script changed. Refresh it before saving sources.")
    await core._event(doc_id, doc["round"], user_id, "SOURCES")
    await core.note_change(doc_id, user_id, "text")
    await core._broadcast_state(doc_id)
    return await core.get(doc_id, user_id)


async def download(doc_id: UUID, user_id: UUID):
    from services.compliance.evaluation import script_doc_service as core
    from services.compliance.media.script_export import MIME, script_docx

    doc, _, _ = await core.get_access(doc_id, user_id)
    filename = quote(f"{doc['title']}.docx", safe="")
    return Response(script_docx(export_text(doc)), media_type=MIME,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})
