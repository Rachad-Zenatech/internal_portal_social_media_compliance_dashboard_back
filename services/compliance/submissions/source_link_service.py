"""Validation, persistence helpers, and quick AI checking for submission source links."""

import json
from typing import Any, Dict, List, Sequence
from urllib.parse import urlparse
from uuid import UUID

from fastapi import HTTPException

from models.compliance.compliance_model import ProvidedSource, SourceCheck
from postgresql_db.database import fetch_all
from services.compliance.evaluation import citation_finder, source_check_service
from services.compliance.media import document_layout


def normalize_source_url(raw: str) -> str:
    """Trim and validate a source URL, adding https:// when the creator omitted the scheme."""
    url = (raw or "").strip()
    if not url:
        raise HTTPException(status_code=422, detail="Enter a source URL.")
    if "://" not in url:
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or "." not in parsed.netloc:
        raise HTTPException(status_code=422, detail=f"Invalid source URL: {raw}")
    if len(url) > 2000:
        raise HTTPException(status_code=422, detail="Source URL is too long.")
    return url


def normalize_source_links(raw: Sequence[str]) -> List[str]:
    """Validate and deduplicate source URLs saved on a submission."""
    return list(dict.fromkeys(normalize_source_url(item) for item in raw if (item or "").strip()))


def apply_update_fields(payload: Dict[str, Any], fields: Dict[str, Any], rule_sets: Sequence[str]) -> None:
    """Validate and copy rule_sets and source_links from a draft patch into SQL fields."""
    if payload.get("rule_sets") is not None:
        bad = [r for r in payload["rule_sets"] if r not in rule_sets]
        if bad:
            raise HTTPException(status_code=422, detail=f"Unknown rule set(s): {', '.join(bad)}")
        fields["rule_sets"] = list(dict.fromkeys(payload["rule_sets"]))
    if payload.get("source_links") is not None:
        fields["source_links"] = normalize_source_links(payload["source_links"])


def with_source_links(row: Dict[str, Any], parts: Sequence[str]) -> List[str]:
    """Append saved source_links to post text parts so full compliance checks read them."""
    links = list(row.get("source_links") or [])
    return [*parts, "Sources:\n" + "\n".join(links)] if links else list(parts)


async def check_single_url(url: str, post_text: str) -> SourceCheck:
    """Fetch one explicit URL and compare the post's material against it, including where in the source each claim appears."""
    citation = citation_finder.Citation(reference=url, kind="URL", where="caption", cue="source")
    check, material = await source_check_service._read_link(citation, post_text)
    if not material:
        return check
    label, source_text = material
    results, model = await source_check_service._compare(post_text, label, check.title, check.url, check.published, source_text)
    if results is None:
        check.message = "No AI model could read this source just now, so it hasn't been compared."
        return check
    check.model = model
    check.claims = source_check_service._to_claims(results, source_text)
    if not check.claims:
        check.message = "No claim in the post turned out to depend on this source."
    return check


async def check_submission_source_link(submission_id: UUID, url: str, user_id: UUID) -> SourceCheck:
    """Quickly fetch one source URL and check whether it supports the post's current material."""
    from services.compliance.submissions import submission_service

    clean_url = normalize_source_url(url)
    row = await submission_service._get_owned_row(submission_id, user_id, manager_can_read=True)
    docs = await fetch_all(
        """
        SELECT kind, original_name, doc_text, doc_text_status, image_text, image_text_status, transcript, transcript_status
        FROM content_submission_files
        WHERE submission_id = $1 AND deleted_at IS NULL
        ORDER BY created_at
        """,
        row["id"],
    )
    parts: List[str] = [submission_service._check_text(row)]
    sources: List[ProvidedSource] = []
    script = await submission_service.approved_script(row)
    if script:
        parts.append(f"Script:\n{script}")
    for d in docs:
        if d["kind"] in ("SCRIPT", "MAIN") and d["doc_text_status"] == "COMPLETED" and (d["doc_text"] or "").strip():
            if not (script and d["kind"] == "SCRIPT"):
                parts.append(d["doc_text"])
        elif d["kind"] == "MAIN" and d["transcript_status"] == "COMPLETED" and d["transcript"]:
            raw_seg = d["transcript"]
            segs = (json.loads(raw_seg) if isinstance(raw_seg, str) else raw_seg) or []
            spoken = " ".join(str(s.get("text", "")) for s in segs).strip()
            if spoken:
                parts.append(spoken)
        elif d["image_text_status"] == "COMPLETED" and (d["image_text"] or "").strip():
            parts.append(d["image_text"])
        elif d["kind"] == "SUPPORTING" and d["doc_text_status"] == "COMPLETED" and (d["doc_text"] or "").strip():
            sources.append(ProvidedSource(name=d["original_name"], text=d["doc_text"]))
    material = document_layout.plain_text("\n\n".join(p for p in parts if p and p.strip())) or row["title"]
    return await check_single_url(clean_url, material)
