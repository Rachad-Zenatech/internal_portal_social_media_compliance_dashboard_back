"""A claim needs support from a relevant source, not from every supplied source."""
from services.compliance.evaluation.source_claim_grounding import same_assertion


def supported_elsewhere(checks, claim):
    return any(check.status == "READ" and candidate.verdict == "SUPPORTED"
               and candidate.where == claim.where and same_assertion(candidate.claim, claim.claim)
               for check in checks for candidate in check.claims)


def merge_findings(findings):
    """Show one concern per stable claim, retaining real conflicts and the most informative review."""
    merged = {}
    for finding in findings:
        key = (finding.rule_id, finding.status)
        previous = merged.get(key)
        if previous is None:
            merged[key] = finding
        elif previous.title == "Claim not found in its source" and finding.title != previous.title:
            merged[key] = finding
    return list(merged.values())
