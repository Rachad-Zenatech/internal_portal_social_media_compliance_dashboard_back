"""Always read Rule 25 matches, including built-in claims, in the full post's context."""
from services.compliance.evaluation.context_evaluator import Candidate


def include(rules, findings, candidates, content):
    """Override legacy keyword mode without changing the saved rulebook settings."""
    forward_rules = [rule for rule in rules if rule.rule_number == "Rule 25"]
    if not forward_rules:
        return
    for finding in findings:
        if finding.rule_number != "Rule 25":
            continue
        rule = next((rule for rule in forward_rules if rule.id == finding.rule_id), forward_rules[0])
        candidates[:] = [item for item in candidates if item.finding is not finding]
        candidates.append(Candidate(finding, rule, finding.passage or content, content))
