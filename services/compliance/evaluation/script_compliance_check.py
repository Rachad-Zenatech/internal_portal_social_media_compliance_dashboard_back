"""Script compliance checks and source evidence."""

from typing import Any, Dict

from services.compliance.evaluation.compliance_service import check_compliance
from services.compliance.media import document_layout, script_sources
from services.compliance.submissions import submission_service as subs
from services.compliance.submissions import check_snapshot_service
from services.compliance.evaluation import check_rule_capture
from services.compliance.media.script_content import VERSION as SCORING_VERSION, sections, published


async def check_text(row: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Check the planned video wording against its scoped rules and source evidence."""
    wording, citations = script_sources.split(text)
    body = "\n\n".join(p for p in [(row["title"] or "").strip(), document_layout.plain_text(wording).strip()] if p)
    request = subs.check_request(row, body, has_video=True, has_images=False, source_script=citations)
    request._script_sections = sections(wording, row.get("title") or "")
    request.disclaimer_text = published(request._script_sections)
    post, evaluated = check_snapshot_service.inputs(row, request, script=text)
    with check_rule_capture.collecting() as captured:
        result = await check_compliance(request)
    check = result.model_dump(mode="json")
    check["ai_unavailable"] = result.ai_unavailable
    check["script_scoring_version"] = SCORING_VERSION
    if not request.disclaimer_text:
        check["recommendations"].insert(0, "No voiceover or supers sections were identified. Findings require review without point deductions; label the published sections to score them.")
    return await check_snapshot_service.save(post, evaluated, captured, check)
