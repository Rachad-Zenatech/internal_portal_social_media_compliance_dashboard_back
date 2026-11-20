"""Source comparison consumes the same full-post inventory across all published parts."""
from models.compliance.compliance_model import ComplianceViolation
from . import claim_manifest, claim_verification


def unsourced_image_findings(text, checks, manifest=None):
    """All extracted facts, including soft claims, stay visible when there is no usable evidence."""
    if manifest is None:
        return []  # An inventory is required; do not guess from isolated label words.
    from .source_check_service import claim_id, NOT_FOUND_ID
    seen = {c.claim_id for check in checks for c in check.claims}
    unavailable = bool(checks) and not any(check.status == "READ" for check in checks)
    out = []
    for claim in manifest.claims:
        if claim.id in seen:
            continue
        out.append(ComplianceViolation(
            rule_id=claim_id(NOT_FOUND_ID, claim.id), rule_number="Rule 18", category="Source Check",
            title="Clarify the claim" if claim.ambiguous else "Claim verification incomplete" if unavailable else "Claim needs a source", severity="MEDIUM",
            traffic_light="YELLOW", status="NEEDS_REVIEW" if claim.ambiguous or unavailable else "EVIDENCE_REQUIRED",
            flagged_terms=[claim.wording],
            explanation="The subject or meaning needs clarification." if claim.ambiguous else
                        "No readable source has established this factual assertion.",
            remediation_guidance="Clarify what is asserted and about whom." if claim.ambiguous else
                                 "Provide evidence supporting this claim with its stated scope and qualifications.",
        ))
    return out


async def compare_parts(text, label, title, url, published, source_text, compare, *, manifest=None):
    inventory = manifest if manifest is not None else await claim_manifest.get(text)
    return await claim_verification.compare(inventory, (label, title, url, published, source_text), compare)
