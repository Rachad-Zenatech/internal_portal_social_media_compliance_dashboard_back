"""Reconcile every final finding with shared evidence before the score is calculated."""
from models.compliance.compliance_model import ContextEvaluation
from . import source_evidence_context as evidence


def clear_supported(findings, checks):
    """Resolve factual support, never use a citation as a blanket policy exemption."""
    from .source_check_service import PUBLISHED_OK_RULES
    records = evidence.catalog(checks)
    cleared = 0
    for finding in findings:
        unresolved_source = finding.title in {"Source couldn't be checked", "Newsroom article couldn't be checked",
            "Claim differs from the newsroom", "Claim doesn't match its source"}
        factual = finding.rule_number == "Rule 18" and not unresolved_source and finding.status in {"EVIDENCE_REQUIRED", "NEEDS_REVIEW"}
        public = finding.rule_number == "Rule 6" and "Rule 6" in PUBLISHED_OK_RULES and finding.status in {"VIOLATION", "NEEDS_REVIEW", "EVIDENCE_REQUIRED"}
        if not (factual or public) or (public and finding.kind == "MISSING_DISCLAIMER"):
            continue
        matching = [item for item in records if evidence.linked(finding, item)]
        if any(item["verdict"] == "CONTRADICTED" for item in matching):
            continue
        supported = next((item for item in matching if item["verdict"] == "SUPPORTED" and
                         (not public or item["authority"] in {"COMPANY", "OFFICIAL"})), None)
        if supported is None:
            continue
        before = finding.status
        reason = f"{supported['title'] or supported['reference']} supports this claim: {supported['evidence']}"
        finding.status, finding.traffic_light = "CLEARED", "GREEN"
        finding.penalty, finding.uncounted_penalty, finding.counted_with = 0, 0, None
        finding.explanation = reason
        finding.evaluation = ContextEvaluation(decision="CLEARED", matched_text=supported["claim"], reasoning=reason,
            confidence=supported["confidence"], rule_decision=before, source="AI", model=supported["model"])
        cleared += 1
    return cleared


async def reconcile(req, rules, findings, checks, reviewed):
    """Only new/changed evidence-linked concerns need a focused model decision; do not reread sources."""
    from . import context_evaluator
    clear_supported(findings, checks)
    records = evidence.catalog(checks)
    by_id = {rule.id: rule for rule in rules}
    by_number = {rule.rule_number: rule for rule in rules}
    candidates = []
    for finding in findings:
        if finding.status == "CLEARED" or finding.rule_number == "Rule 18":
            continue
        if reviewed.get(str(finding.rule_id)) == evidence.signature(finding):
            continue
        rule = by_id.get(finding.rule_id) or by_number.get(finding.rule_number)
        if rule is None:
            continue
        if (finding.kind == "MISSING_DISCLAIMER" and rule.evaluation_mode != "CONTEXT"
                and not rule.disclaimer_triggers):
            continue  # Evidence cannot waive an unconditional published disclosure.
        context = f"{req.text}\nObserved concern: {finding.context or finding.explanation}"
        if not evidence.relevant(finding, records, context):
            continue
        candidates.append(context_evaluator.Candidate(finding, rule, finding.passage or "", context,
                                                     trigger_only=finding.status != "VIOLATION"))
    if candidates:
        evidence.attach(candidates, checks)
        await context_evaluator.evaluate(candidates, req.text, brand=req.brand_code)
    clear_supported(findings, checks)


async def run(req, rules, findings, contextual, checks, *, match_req=None):
    """Give text and visuals one evidence set, then reconcile late findings before scoring."""
    from . import context_candidates, context_evaluator, visual_review, synthetic_media_disclosure
    clear_supported(findings, checks)
    candidates = [item for item in context_candidates.for_post(contextual, req, rules, findings)
                  if item.finding.status != "CLEARED"]
    evidence.attach(candidates, checks)
    await context_evaluator.evaluate(candidates, req.text, brand=req.brand_code)
    reviewed = {str(item.finding.rule_id): evidence.signature(item.finding) for item in candidates}
    visual_checks = await visual_review.review(req, rules, findings, source_checks=checks)
    synthetic_media_disclosure.ensure_review(match_req or req, rules, findings)
    await reconcile(req, rules, findings, checks, reviewed)
    return visual_checks
