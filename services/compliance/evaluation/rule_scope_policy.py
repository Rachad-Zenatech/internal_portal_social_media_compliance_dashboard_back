"""Match a rule to the submission's selected policy scope and content parts."""
from services.compliance.evaluation import rule18_policy


def _overlaps(rule_values, content_values):
    return not rule_values or not content_values or bool(set(rule_values) & set(content_values))


def applies(rule, req, scope, *, content_parts, global_region):
    if rule.rule_number == "Rule 18" and not rule18_policy.applies(req):
        return False
    region = [] if not req.region or req.region == global_region else [req.region]
    rule_parts = [part for part in (value.strip().lower() for value in rule.applies_to or []) if part in content_parts]
    return (
        _overlaps(rule.rule_sets, scope.rule_sets)
        and _overlaps(rule.brands, req.brand_codes)
        and _overlaps(rule.audiences, scope.audiences)
        and _overlaps(rule.regions, region)
        and _overlaps(rule.platforms, req.platforms)
        and _overlaps(rule_parts, scope.content_parts)
    )
