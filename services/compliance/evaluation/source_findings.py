"""Stable source-finding identities and construction, independent of source retrieval."""
from uuid import NAMESPACE_URL, UUID, uuid5

from models.compliance.compliance_model import ComplianceViolation, ContextEvaluation, SourceCheck, SourceClaim


NOT_FOUND_TITLE = "Claim not found in its source"
PENALTY_CONTRADICTED = 20
CONTRADICTED_ID = uuid5(NAMESPACE_URL, "zenatech-source-check:contradicted")
NOT_FOUND_ID = uuid5(NAMESPACE_URL, "zenatech-source-check:not-found")
UNCHECKED_ID = uuid5(NAMESPACE_URL, "zenatech-source-check:unchecked")
CHECK_IDS = {CONTRADICTED_ID, NOT_FOUND_ID, UNCHECKED_ID}


def claim_id(kind_id: UUID, claim: str) -> UUID:
    """Keep dismissal identities stable for the same claim wording."""
    return uuid5(kind_id, " ".join((claim or "").lower().split()))


def make_finding(rule_id: UUID, status: str, severity: str, title: str, claim: SourceClaim, check: SourceCheck,
                 explanation: str, remediation: str) -> ComplianceViolation:
    # Missing source material produced no AI judgment; it is not a low-confidence answer or an AI outage.
    return ComplianceViolation(
        rule_id=claim_id(rule_id, claim.claim_id or claim.claim), rule_number="Rule 18", title=title, category="Source Check", severity=severity,
        traffic_light="YELLOW", flagged_terms=[claim.claim], explanation=explanation, remediation_guidance=remediation,
        penalty=PENALTY_CONTRADICTED if status == "VIOLATION" else 0, status=status,
        evaluation=ContextEvaluation(
            decision=status, matched_text=claim.claim, reasoning=claim.evidence or claim.note or "",
            confidence=claim.confidence, recommended_action=claim.note, rule_decision=status, source="AI",
            model=check.model,
        ) if not claim.clarification_needed and check.status not in {"UNREADABLE", "NOT_PROVIDED"} else None,
    )


def source_label(check: SourceCheck) -> str:
    return f'"{check.title}"' if check.title else check.reference
