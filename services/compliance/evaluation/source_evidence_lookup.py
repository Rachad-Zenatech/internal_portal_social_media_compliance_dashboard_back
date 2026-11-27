"""Source evidence for rule concerns is independent of optional general Rule 18 substantiation."""
import re
from . import rule18_policy, source_check_service, source_post_checks
from . import source_claim_coverage, source_claim_grounding, source_assertions

_EVIDENCE_RULE = re.compile(r"public|source|substanti|evidence|announc|claim", re.I)


def seeds(req, findings, rules):
    """Ground only already-raised, source-resolvable concerns; never invent claims from the whole video."""
    from .compliance_service import _passage
    by_id = {rule.id: rule for rule in rules}
    by_number = {rule.rule_number: rule for rule in rules}
    body = source_claim_coverage.post_body(req.text)
    from .financial_evidence import passages
    from . import claim_checks
    money_id = next(check.rule_id for check in claim_checks.CHECKS if check.key == "financial-figure")
    selected = {item["claim_id"]: item for item in passages(body)} if any(
        finding.rule_id == money_id for finding in findings) else {}
    for finding in findings:
        if finding.rule_number in {"Rule 18", "Rule 27", "Rule 28", "Rule 29"} or finding.kind == "MISSING_DISCLAIMER":
            continue
        rule = by_id.get(finding.rule_id) or by_number.get(finding.rule_number)
        if rule is None or not _EVIDENCE_RULE.search(" ".join([rule.title, rule.summary, rule.evaluation_guidance or "", finding.explanation])):
            continue
        passage = finding.passage or _passage(body, finding.flagged_terms)[0]
        for quote, where in source_claim_grounding.excerpts(body, passage):
            for item in source_assertions.grounded(body, quote, where):
                selected[item["claim_id"]] = item
    return list(selected.values())


async def check(req, findings, rules):
    """Return shared evidence plus Rule 18 findings only when general substantiation is enabled."""
    if rule18_policy.applies(req):
        result = await rule18_policy.check_sources(req)
        issues = source_check_service.findings(result.checks, failed=result.failed_claims)
        issues.extend(result.unbacked)
        issues.extend(source_post_checks.unsourced_image_findings(req.text, result.checks))
        return result, issues
    inventory = seeds(req, findings, rules)
    if not inventory:
        return source_check_service.SourceCheckResult(), []
    result = await source_check_service.run(req.text, req.sources, source_script=req.source_script,
        include_image_citations=False, claims=inventory)
    return result, []  # Evidence informs the existing rule, without enabling general video Rule 18 deductions.
