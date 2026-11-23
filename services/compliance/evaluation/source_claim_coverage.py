"""Identify claims even without sources; keep missing evidence distinct from a false claim."""
import logging

from models.compliance.compliance_model import ComplianceViolation
from services.compliance.evaluation import branding, citation_finder, newsroom_facts, source_claim_inventory
from services.compliance.evaluation.source_findings import NOT_FOUND_ID, claim_id

logger = logging.getLogger(__name__)


def post_body(text):
    return branding.without_branding(citation_finder.without_citation_text(text)).strip()


async def identify(text, ask, models, limit):
    try:
        return await source_claim_inventory.get(
            text, lambda: source_claim_inventory.extract(text, ask, models, limit))
    except Exception:
        logger.exception("Could not load or save the post claim inventory")
        return None


def claim_key(claim, where):
    return (" ".join(claim.casefold().split()), where)


def missing_findings(inventory, checks):
    # Explicit source judgments already have findings. Uncited newsroom silence is not evidence.
    addressed = {claim_key(claim.claim, claim.where)
                 for check in checks for claim in check.claims
                 if not newsroom_facts.is_newsroom(check) or claim.verdict in {"SUPPORTED", "CONTRADICTED"}}
    findings, seen = [], set()
    for item in inventory:
        key = claim_key(item["claim"], item["where"])
        if key in addressed or key in seen:
            continue
        seen.add(key)
        note = "No readable supporting source has verified this claim. Attach evidence that supports its scope and conditions."
        findings.append(ComplianceViolation(
            rule_id=claim_id(NOT_FOUND_ID, item["claim"]), rule_number="Rule 18", category="Source Check",
            title="Claim needs supporting evidence", severity="MEDIUM", traffic_light="YELLOW",
            status="EVIDENCE_REQUIRED", penalty=0, flagged_terms=[item["claim"]], passage=item["claim"],
            explanation=note, remediation_guidance=note,
        ))
    return findings


async def complete(result, inventory, text, ask, models, limit):
    if inventory is None:
        if result.ai_failed:
            return  # Failed comparison already marks the review incomplete; do not repeat failed AI work.
        inventory = await identify(post_body(text), ask, models, limit)
    if inventory is None:
        result.ai_failed = True
        return
    result.ai_failed = result.ai_failed or not getattr(inventory, "complete", True)
    result.unbacked = missing_findings(inventory, result.checks)
