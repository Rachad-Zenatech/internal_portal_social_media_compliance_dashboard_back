"""A result for every configured method of every applicable rule; absence is never a pass."""
from models.compliance.rule_check_model import MethodCheckResult, RuleCheckResult

VERSION = "rule-results-v1"
PRIORITY = {"ISSUE": 0, "EVIDENCE_REQUIRED": 1, "HUMAN_REVIEW_REQUIRED": 2, "NO_ISSUE_FOUND": 3, "NOT_APPLICABLE": 4}
OUTCOMES = {"VIOLATION": "ISSUE", "EVIDENCE_REQUIRED": "EVIDENCE_REQUIRED", "NEEDS_REVIEW": "HUMAN_REVIEW_REQUIRED", "CLEARED": "NO_ISSUE_FOUND"}


def _from_findings(method, findings):
    if not findings:
        return None
    ranked = sorted(findings, key=lambda v: PRIORITY[OUTCOMES[v.status]])
    selected = ranked[0]
    missing = any(v.evaluation and v.evaluation.source == "UNAVAILABLE" for v in findings)
    reason = selected.evaluation.reasoning if selected.evaluation and selected.evaluation.reasoning else selected.explanation
    if missing:
        reason = "The automated evaluation did not finish. " + reason
    return MethodCheckResult(method=method, outcome=None if missing else OUTCOMES[selected.status], complete=not missing, reason=reason.strip() or "A finding was recorded for this rule.")


def _text_result(rule, req, findings):
    method = "TEXT_ANALYSIS"
    result = _from_findings(method, [v for v in findings if v.category != "Source Check"])
    if result:
        return result
    if not req.text.strip():
        return MethodCheckResult(method=method, reason="No readable text was available for this check.")
    if rule.evaluation_mode == "RULE" and (rule.prohibited_terms or rule.required_disclaimers):
        return MethodCheckResult(method=method, outcome="NO_ISSUE_FOUND", complete=True,
                                 reason="The configured phrases, exceptions and required disclaimers were checked; no issue was found.")
    return MethodCheckResult(method=method, reason="No full text judgment was recorded for this rule. No keyword match does not establish that the full policy passed.")


def _source_result(rule, checks, findings):
    method = "SOURCE_VERIFICATION"
    result = _from_findings(method, [v for v in findings if v.category == "Source Check"])
    if result:
        return result
    if rule.rule_number != "Rule 18":
        return MethodCheckResult(method=method, reason="No source-verification judgment was recorded specifically for this rule.")
    compared = [claim for check in checks if check.status == "READ" for claim in check.claims
                if check.where != "newsroom" or claim.verdict in ("SUPPORTED", "CONTRADICTED")]
    incomplete = any(check.status != "READ" for check in checks)
    if not compared:
        return MethodCheckResult(method=method, reason="No claim-to-source comparison was completed. Add evidence or have a reviewer establish whether the rule applies.")
    if any(c.verdict == "CONTRADICTED" for c in compared):
        outcome, reason = "ISSUE", "A recorded claim contradicts its source. Review the source comparisons."
    elif any(c.verdict == "NOT_FOUND" for c in compared):
        outcome, reason = "EVIDENCE_REQUIRED", "A cited source does not establish a recorded claim. Add evidence or revise the claim."
    elif any(c.verdict == "UNCLEAR" for c in compared):
        outcome, reason = "HUMAN_REVIEW_REQUIRED", "A source comparison is unclear and requires reviewer confirmation."
    else:
        outcome, reason = "NO_ISSUE_FOUND", f"{len(compared)} recorded claim comparison(s) found supporting evidence; this result covers those recorded claims."
    return MethodCheckResult(method=method, outcome=outcome, complete=not incomplete,
                             reason=reason + (" Some supplied sources could not be checked." if incomplete else ""))


def _method_result(method, rule, req, response, findings):
    if method == "TEXT_ANALYSIS":
        return _text_result(rule, req, findings)
    if method == "SOURCE_VERIFICATION":
        return _source_result(rule, response.source_checks, findings)
    if method == "HUMAN_CONFIRMATION":
        return MethodCheckResult(method=method, outcome="HUMAN_REVIEW_REQUIRED", complete=True,
                                 reason="This rule is explicitly configured for an authorized reviewer's confirmation.")
    if method == "VISUAL_INSPECTION":
        media = set(response.content_parts) & {"video", "imagery"}
        if not media and req.content_type in ("TEXT", "BLOG", "ARTICLE"):
            return MethodCheckResult(method=method, outcome="NOT_APPLICABLE", complete=True,
                                     reason="This text-only submission has no image or video to inspect.")
        return MethodCheckResult(method=method, reason="Image/video inspection has not been recorded. OCR and a transcript do not establish that the actual visuals were inspected.")
    return MethodCheckResult(method=method, reason="Clearance documents have not been verified by an authorized reviewer. A source link is not clearance.")


def rule_result(rule, req, response):
    findings = [v for v in response.violations if v.rule_id == rule.id or v.rule_number == rule.rule_number]
    methods = [_method_result(method, rule, req, response, findings) for method in rule.checking_methods]
    complete = bool(methods) and all(item.complete for item in methods)
    known = [item for item in methods if item.outcome is not None]
    selected = min(known, key=lambda item: PRIORITY[item.outcome]) if known else None
    outcome = selected.outcome if selected else None
    # An unfinished method never becomes a clean/no-applicability result for the whole rule.
    if not complete and outcome in ("NO_ISSUE_FOUND", "NOT_APPLICABLE"):
        outcome = None
    reason = selected.reason if selected else "No checking method is configured for this rule."
    if not complete:
        pending = [item.reason for item in methods if not item.complete]
        known_reason = [selected.reason] if selected and selected.complete and outcome is not None else []
        reason = " ".join([*known_reason, *pending]) if pending else reason
    return RuleCheckResult(rule_id=rule.id, rule_number=rule.rule_number, title=rule.title,
                           outcome=outcome, complete=complete, reason=reason, methods=methods)


def attach_results(req, rules, response):
    response.rule_results = [rule_result(rule, req, response) for rule in rules]
    response.rule_results_version = VERSION
    response.rule_results_complete = all(result.complete for result in response.rule_results)
    return response
