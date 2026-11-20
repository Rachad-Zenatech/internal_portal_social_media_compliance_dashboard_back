"""Display policy for compliance findings, separate from response assembly."""
from models.compliance.compliance_model import ComplianceViolation


def is_visible(finding: ComplianceViolation) -> bool:
    evaluation = finding.evaluation
    return not (
        finding.status == "NEEDS_REVIEW"
        and evaluation is not None
        and evaluation.source in {"AI", "ADJUSTED"}
        and evaluation.confidence < 0.5
    )
