"""Refresh source comparisons on request and keep a record of resolved findings."""
from contextvars import ContextVar

refresh = ContextVar("refresh_source_comparisons", default=False)


def record_cleared(result, previous):
    """Carry forward a cleared finding only when the new check independently finds support."""
    from . import source_check_service as sources

    existing = {str(f.rule_id) for f in result.violations}
    eligible = {"CLEARED", "NEEDS_REVIEW", "VIOLATION"} if refresh.get() else {"CLEARED"}
    prior = {str(f.get("rule_id")): f for f in previous
             if f.get("category") == "Source Check" and f.get("status") in eligible}
    for check in result.source_checks:
        if check.status != "READ":
            continue
        for claim in check.claims:
            if claim.verdict != "SUPPORTED":
                continue
            ids = [str(sources.claim_id(kind, claim.claim)) for kind in (sources.NOT_FOUND_ID, sources.CONTRADICTED_ID)]
            if any(key in existing for key in ids):
                continue
            key = next((key for key in ids if key in prior), None)
            if not key:
                continue
            kind = sources.NOT_FOUND_ID if key == ids[0] else sources.CONTRADICTED_ID
            finding = sources._finding(
                kind, "CLEARED", prior[key].get("severity") or "MEDIUM", "Claim supported by source",
                claim, check, f"The source supports this wording. {claim.evidence or ''}".strip(), "",
            )
            finding.traffic_light = "GREEN"
            result.violations.append(finding)
            result.cleared_count += 1
            existing.add(key)
