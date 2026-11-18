"""Script compliance checks and source evidence."""

from typing import Any, Dict

from services.compliance.evaluation.compliance_service import check_compliance
from services.compliance.media import document_layout, script_sources
from services.compliance.submissions import submission_service as subs


async def check_text(row: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Check the planned video wording against its scoped rules and source evidence."""
    wording, citations = script_sources.split(text)
    body = "\n\n".join(p for p in [(row["title"] or "").strip(), document_layout.plain_text(wording).strip()] if p)
    result = await check_compliance(subs.check_request(row, body, has_video=True, has_images=False, source_script=citations))
    check = result.model_dump(mode="json")
    check["ai_unavailable"] = result.ai_unavailable
    return check
