"""Keep sensitive imagery disclosure checks independent of optional semantic matching."""
import re

from models.compliance.compliance_model import ComplianceViolation


SYNTHETIC_RULE = re.compile(r"\b(ai|synthetic|renders?|rendering)\b", re.I)
SENSITIVE_TOPIC = re.compile(r"\b(military|combat|battlefield|warzone|war zone|troops?|armed forces)\b", re.I)


def ensure_review(req, rules, findings):
    """Missing visual provenance is a review question, never proof that an image is synthetic."""
    from services.compliance.evaluation import compliance_service as cs
    if "imagery" not in (req.content_parts or cs.parts_for(req.content_type)):
        return
    text = cs.without_image_marks(req.text)
    topic = SENSITIVE_TOPIC.search(text)
    if not topic:
        return
    published = req.disclaimer_text if req.disclaimer_text is not None else text
    padded = f" {cs._normalize(published)} "
    for rule in rules:
        if not rule.required_disclaimers or not SYNTHETIC_RULE.search(f"{rule.category} {rule.title}"):
            continue
        if any(cs._disclaimer_present(label, padded) for label in rule.required_disclaimers):
            continue
        existing = [finding for finding in findings if finding.rule_id == rule.id]
        if any(finding.status != "CLEARED" for finding in existing):
            continue
        # A semantic model's empty result (or clearance) cannot settle visual provenance from OCR alone.
        findings[:] = [finding for finding in findings if finding not in existing]
        findings.append(ComplianceViolation(
            rule_id=rule.id, rule_number=rule.rule_number, title=rule.title, category=rule.category,
            severity=rule.severity, traffic_light="YELLOW", status="NEEDS_REVIEW", penalty=0,
            kind="MISSING_DISCLAIMER", flagged_terms=[topic.group()],
            missing_disclaimers=list(rule.required_disclaimers),
            explanation="Sensitive military imagery has no approved illustrative/conceptual disclosure. "
                        "Text extraction cannot establish whether this is an actual photograph or synthetic media. "
                        "Confirm the image's origin and whether it could be mistaken for an actual operation.",
            remediation_guidance="If the image is AI-generated, rendered or conceptual, add the rule's approved label: "
                                 + rule.required_disclaimers[0] + " A generic illustrative-purpose note does not "
                                 "replace a more specific label required by the rulebook.",
        ))
