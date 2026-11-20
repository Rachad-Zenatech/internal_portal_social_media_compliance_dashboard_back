"""Route Army mentions to Rule 4's AI review without treating a mention as a breach."""
import re

from models.compliance.compliance_model import ComplianceViolation
from services.compliance.evaluation.context_evaluator import Candidate


_ARMY = re.compile(r"\b(?:us)?army\b", re.I)


def applies(rule, text):
    return rule.rule_number == "Rule 4" and bool(_ARMY.search(text or ""))


def include(req, rules, findings, candidates):
    """Use the complete submitted text, including citations and denials, for every Army mention."""
    for rule in rules:
        if not applies(rule, req.text):
            continue
        finding = next((item for item in findings if item.rule_id == rule.id), None)
        trigger_only = finding is None
        if finding is None:
            mention = _ARMY.search(req.text).group()
            finding = ComplianceViolation(
                rule_id=rule.id, rule_number=rule.rule_number, title=rule.title, category=rule.category,
                severity=rule.severity, traffic_light="YELLOW", status="NEEDS_REVIEW", penalty=0,
                flagged_terms=[mention], explanation="An Army mention needs to be read in the full post's context.",
            )
            findings.append(finding)
        candidates[:] = [item for item in candidates if item.finding.rule_id != rule.id]
        finding.passage = finding.passage or _ARMY.search(req.text).group()
        finding.context = req.text
        candidates.append(Candidate(
            finding, rule, finding.passage, req.text, trigger_only=trigger_only,
            review_metadata={"audiences": req.audiences, "content_type": req.content_type,
                             "published_text": req.disclaimer_text},
        ))
