"""Validation, persistence helpers, and quick AI checking for submission source links."""

import json
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse
from uuid import UUID

from fastapi import HTTPException

from models.compliance.compliance_model import SourceCheck
from postgresql_db.database import execute
from services.ai.observability.ai_guardrails import screen_input
from services.compliance.evaluation import rule_set_policy
from services.compliance.evaluation import citation_finder, source_check_service, source_fetch_service, source_passages, source_post_checks


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


def normalize_scanned_source_url(raw: str) -> str:
    """Use HTTPS and www for root domains discovered by an image scan."""
    parsed = urlparse(normalize_source_url(raw))
    labels = (parsed.hostname or "").split(".")
    root_domain = len(labels) == 2 or (
        len(labels) == 3 and len(labels[-1]) == 2
        and labels[-2] in {"ac", "co", "com", "edu", "gov", "net", "org"}
    )
    netloc = parsed.netloc
    if root_domain and not netloc.lower().startswith("www.") and not parsed.username:
        netloc = "www." + netloc
    return parsed._replace(scheme="https", netloc=netloc).geturl()


def normalize_source_links(raw: Sequence[str]) -> List[str]:
    """Validate and deduplicate source URLs saved on a submission."""
    return list(dict.fromkeys(normalize_source_url(item) for item in raw if (item or "").strip()))


def apply_update_fields(payload: Dict[str, Any], fields: Dict[str, Any], rule_sets: Sequence[str], *, row=None) -> None:
    """Validate and copy rule_sets and source_links from a draft patch into SQL fields."""
    if payload.get("rule_sets") is not None:
        bad = [r for r in payload["rule_sets"] if r not in rule_sets]
        if bad:
            raise HTTPException(status_code=422, detail=f"Unknown rule set(s): {', '.join(bad)}")
        fields["rule_sets"] = list(dict.fromkeys(payload["rule_sets"]))
    if payload.get("source_links") is not None:
        fields["source_links"] = normalize_source_links(payload["source_links"])
    if row is not None:
        rule_set_policy.update_fields(row, fields)


def with_source_links(row: Dict[str, Any], parts: Sequence[str]) -> List[str]:
    """Append saved source_links to post text parts so full compliance checks read them."""
    links = list(row.get("source_links") or [])
    return [*parts, "Sources:\n" + "\n".join(links)] if links else list(parts)


async def check_single_url(url: str, post_text: str) -> SourceCheck:
    """Fetch one explicit URL and compare the post's material against it, including where in the source each claim appears."""
    citation = citation_finder.Citation(reference=url, kind="URL", where="source_links", cue="source")
    check, material = await source_check_service._read_link(citation, post_text)
    if not material:
        return check
    label, source_text = material
    return await compare_with_source(check, post_text, label, source_text)


async def compare_with_source(check: SourceCheck, post_text: str, label: str, source_text: str) -> SourceCheck:
    """Fill in a read source's claims: the post's claims compared with the source's text, through the safeguards."""
    results, model, incomplete = await source_post_checks.compare_parts(
        post_text, label, check.title, check.url, check.published, source_text, source_check_service._compare)
    if results is None:
        check.message = "No AI model could read this source just now, so it hasn't been compared."
        return check
    check.model = model
    check.claims = await source_passages.checked_claims(results, source_text)
    if incomplete:
        check.message = "Some caption or image claims could not be compared with this source. Check again to finish."
    elif not check.claims:
        check.message = "No claim in the post turned out to depend on this source."
    return check


async def save_source_link_check(
    submission_id: UUID, url: str, check: SourceCheck, *, add_link: bool = False,
) -> None:
    """Persist an extracted/checked source result (and optionally its URL) on the submission row."""
    payload = json.dumps({url: check.model_dump(mode="json")})
    if add_link:
        await execute(
            """
            UPDATE content_submissions
            SET source_links = CASE
                    WHEN $3 = ANY(COALESCE(source_links, '{}')) THEN COALESCE(source_links, '{}')
                    ELSE array_append(COALESCE(source_links, '{}'), $3)
                END,
                source_link_checks = COALESCE(source_link_checks, '{}'::jsonb) || $2::jsonb,
                updated_at = NOW()
            WHERE id = $1 AND deleted_at IS NULL
            """,
            submission_id,
            payload,
            url,
        )
    else:
        await execute(
            """
            UPDATE content_submissions
            SET source_link_checks = COALESCE(source_link_checks, '{}'::jsonb) || $2::jsonb,
                updated_at = NOW()
            WHERE id = $1 AND deleted_at IS NULL
            """,
            submission_id,
            payload,
        )


async def check_submission_source_link(
    submission_id: UUID, url: str, user_id: UUID, image_text: Optional[str] = None,
) -> SourceCheck:
    """Quickly fetch one source URL and check whether it supports the post's current material. `image_text` is the
    accepted for scan-call compatibility; only saved post material is assessed."""
    clean_url = normalize_source_url(url)
    material = await submission_material(submission_id, user_id, image_text)
    check = await check_single_url(clean_url, material)
    await save_source_link_check(submission_id, clean_url, check)
    return check


async def check_submission_pasted_source(
    submission_id: UUID, user_id: UUID, text: str, title: Optional[str] = None, url: Optional[str] = None,
) -> SourceCheck:
    """Check the post's claims against an article pasted in whole, and say where in it each claim is found."""
    source_text = source_fetch_service._tidy(text or "")
    clean_url = normalize_source_url(url) if url else None
    label = (title or "").strip() or "Pasted article"
    base = dict(reference=clean_url or label, kind="DOCUMENT", where="attached", cue="Pasted article", title=label,
                url=clean_url, authority=source_check_service.authority_of(clean_url) if clean_url else None)
    if len(source_text) < source_fetch_service.MIN_TEXT_CHARS:
        return SourceCheck(status="UNREADABLE", message="Paste the whole article: there is too little text to check.", **base)
    if screen_input(source_text).verdict != "ALLOW":
        return SourceCheck(status="UNREADABLE", message="The pasted text contains wording that looks like an attempt to "
                                                        "steer the review, so it wasn't used. A person should read it.", **base)
    material = await submission_material(submission_id, user_id)
    check = SourceCheck(status="READ", **base)
    return await compare_with_source(check, material, f"pasted article {label}", source_text[:source_fetch_service.MAX_TEXT_CHARS])


async def submission_material(submission_id: UUID, user_id: UUID, image_text: Optional[str] = None) -> str:
    """Use full compliance's material; scan OCR discovers sources but does not replace saved post text."""
    from services.compliance.submissions import submission_service, submission_check_material

    row = await submission_service._get_owned_row(submission_id, user_id, manager_can_read=True)
    material = await submission_check_material.load(row)
    return material.text
