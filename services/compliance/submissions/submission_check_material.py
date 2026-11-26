"""The post material used by full compliance and every source comparison."""
import json
from dataclasses import dataclass
from typing import Any, Dict, List

from models.compliance.compliance_model import ProvidedSource
from postgresql_db.database import fetch_all, fetch_one
from services.compliance.media import document_layout, document_text_service, image_text_service, script_sources, video_finding_service
from services.compliance.submissions import source_link_service


@dataclass
class CheckMaterial:
    video: Any
    images: list
    documents: list
    script: Any
    segments: list
    visual_segments: list
    video_scored: bool
    text: str
    published: str
    source_script: str
    sources: list


async def load(row) -> CheckMaterial:
    """Read the same ordered files, script state and captions for all checking entry points."""
    from services.compliance.submissions import submission_service

    video = await fetch_one(
        "SELECT id, original_name, storage_path, mime_type, transcript, transcript_status, transcript_error FROM content_submission_files "
        "WHERE submission_id = $1 AND kind = 'MAIN' AND deleted_at IS NULL AND mime_type LIKE 'video/%' ORDER BY created_at DESC LIMIT 1",
        row["id"],
    )
    raw = video["transcript"] if video and video["transcript_status"] == "COMPLETED" else []
    segments: List[Dict[str, Any]] = (json.loads(raw) if isinstance(raw, str) else raw) or []
    spoken = " ".join(str(s.get("text", "")) for s in segments).strip()
    visual_segments = await video_finding_service.visual_text_segments(video) if video and video["transcript_status"] == "FAILED" else []
    visual_text = " ".join(str(s["text"]) for s in visual_segments).strip()
    images = await fetch_all(
        "SELECT id, original_name, storage_path, mime_type, image_text, image_text_status, image_text_error, image_text_details FROM content_submission_files "
        "WHERE submission_id = $1 AND kind = ANY($2::text[]) AND deleted_at IS NULL AND mime_type LIKE 'image/%' "
        "ORDER BY (kind <> 'MAIN'), created_at",
        row["id"], list(image_text_service.IMAGE_KINDS),
    )
    await image_text_service.schedule_missing([i["id"] for i in images if i["image_text_status"] in (None, "PROCESSING")])
    image_texts = [
        f"Text in image {n}: {i['image_text']}"
        for n, i in enumerate(images, start=1)
        if i["image_text_status"] == "COMPLETED" and (i["image_text"] or "").strip()
    ]
    documents = await fetch_all(
        """
        SELECT id, kind, original_name, doc_text, doc_text_status, doc_text_error FROM content_submission_files
        WHERE submission_id = $1 AND kind IN ('SCRIPT', 'MAIN') AND deleted_at IS NULL
          AND lower(original_name) ~ '\\.(txt|md|docx|pdf|doc)$'
        ORDER BY (kind <> 'SCRIPT'), created_at
        """,
        row["id"],
    )
    await document_text_service.schedule_missing([d["id"] for d in documents if d["doc_text_status"] in (None, "PROCESSING")])
    script = await submission_service.approved_script(row)
    # Score the completed video transcript and use the script for citations and script comparison.
    video_scored = bool(video and video["transcript_status"] == "COMPLETED")
    source_script = script or "\n\n".join(
        d["doc_text"] for d in documents
        if d["kind"] == "SCRIPT" and d["doc_text_status"] == "COMPLETED" and (d["doc_text"] or "").strip()
    )
    document_texts = [
        f"Script:\n{script_sources.split(d['doc_text'])[0]}" if d["kind"] == "SCRIPT" else f"Article:\n{d['doc_text']}"
        for d in documents
        if d["doc_text_status"] == "COMPLETED" and (d["doc_text"] or "").strip()
        and not (d["kind"] == "SCRIPT" and (script or video_scored))
    ]
    if script and not video_scored:
        document_texts.insert(0, "Script:\n" + script_sources.split(script)[0])
    # Scripts and articles carry their page, heading and table marks; the check reads the wording only.
    text = document_layout.plain_text("\n\n".join(p for p in source_link_service.with_source_links(row, [submission_service._check_text(row), *document_texts, spoken, visual_text, *image_texts]) if p)) or row["title"]
    # Supporting documents are read in the background and included as evidence.
    evidence = await fetch_all(
        """
        SELECT id, original_name, doc_text, doc_text_status FROM content_submission_files
        WHERE submission_id = $1 AND kind = 'SUPPORTING' AND deleted_at IS NULL
          AND lower(original_name) ~ '\\.(txt|md|docx|pdf)$'
        ORDER BY created_at
        """,
        row["id"],
    )
    await document_text_service.schedule_missing([d["id"] for d in evidence if d["doc_text_status"] in (None, "PROCESSING")])
    sources = [
        ProvidedSource(name=d["original_name"], text=d["doc_text"])
        for d in evidence if d["doc_text_status"] == "COMPLETED" and (d["doc_text"] or "").strip()
    ]
    # Only published material can satisfy a required disclaimer.
    published = document_layout.plain_text("\n\n".join(p for p in [
        *([row["caption_text"]] if row["caption_text"] else []), *submission_service._platform_captions(row).values(),
        spoken, visual_text, *image_texts,
        *[f"{d['doc_text']}" for d in documents
          if d["kind"] != "SCRIPT" and d["doc_text_status"] == "COMPLETED" and (d["doc_text"] or "").strip()],
    ] if p and str(p).strip()))
    return CheckMaterial(video, images, documents, script, segments, visual_segments,
                         video_scored, text, published, source_script, sources)
