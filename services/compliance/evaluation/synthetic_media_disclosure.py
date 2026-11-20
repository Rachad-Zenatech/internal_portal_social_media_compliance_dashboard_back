"""Require evidence of synthetic media before adding an AI-image disclosure finding."""
import re
from models.compliance.compliance_model import ComplianceViolation, ContextEvaluation

SYNTHETIC_RULE = re.compile(r"\b(ai|synthetic|renders?|rendering)\b", re.I)
_VISUAL = r"(?:images?|imagery|photos?|photographs?|pictures?|visuals?|videos?|footage|animations?|illustrations?|artwork|scenes?|visualizations?)"
_MODIFIERS = r"(?:(?:military|combat|battlefield|drone|rescue|3d|photorealistic|realistic|illustrative)\s+){0,4}"
_SYNTHETIC_VISUAL = re.compile(
    r"\b(?:(?:ai|computer)[ -]*(?:generated|created)|synthetic|cgi|rendered|conceptual)\s*" + _MODIFIERS + _VISUAL + r"\b"
    r"|\b" + _VISUAL + r"\s+(?:(?:is|are|was|were|has been|have been)\s+)?"
    r"(?:(?:generated|created|made|rendered)\s+(?:with|using|by)\s+)?(?:ai[ -]*(?:generated|created)|synthetic|cgi|computer[ -]*generated)\b"
    r"|\b(?:ai[ -]*images?(?!\s+(?:recognition|analysis|processing|classification|detection))|(?:3d |digital )?render(?:ing)? of|concept(?:ual)? visualization)\b", re.I,
)
_NEGATED = re.compile(r"\b(?:no|not|without|never)\s+(?:(?:an?|any|using)\s+)?$", re.I)
_ILLUSTRATIVE_LABEL = re.compile(
    r"\bimages?\s+(?:(?:is|are)\s+)?for\s+(?:illustration|illustrative)\s+purposes\s+only\b",
    re.I,
)


def synthetic_evidence(text):
    """A statement about generated visuals, rather than AI-powered products or a sensitive topic."""
    for match in _SYNTHETIC_VISUAL.finditer(text):
        if not _NEGATED.search(text[max(0, match.start() - 50):match.start()]):
            return match.group()
    return None


def _direct_breach(finding, rule, padded):
    from services.compliance.evaluation import compliance_service as cs
    return finding.kind == "PROHIBITED_TERM" and any(
        term in finding.flagged_terms and cs._hit(term, padded, rule.exception_terms or [], honor_negation=True)
        for term in rule.prohibited_terms
    )


def _approved_disclosure(labels, padded):
    """Allow grammatical variants of an illustrative note only when the rule explicitly approves that note."""
    from services.compliance.evaluation import compliance_service as cs
    for label in labels:
        if cs._disclaimer_present(label, padded):
            return label
        if _ILLUSTRATIVE_LABEL.fullmatch(cs._normalize(label)):
            match = _ILLUSTRATIVE_LABEL.search(padded)
            if match:
                return match.group()
    return None


def _clear_disclosure(finding, label):
    previous = finding.status
    finding.status, finding.traffic_light = "CLEARED", "GREEN"
    finding.penalty, finding.uncounted_penalty, finding.counted_with = 0, 0, None
    finding.missing_disclaimers = []
    finding.remediation_guidance = None
    finding.explanation = "The published caption or image already carries an approved disclosure: " + label
    finding.evaluation = ContextEvaluation(
        decision="CLEARED", matched_text=label, reasoning=finding.explanation,
        confidence=1, rule_decision=previous, source="ADJUSTED",
    )


def ensure_review(req, rules, findings):
    """Unknown provenance or the word military alone must not imply that an image was generated."""
    from services.compliance.evaluation import compliance_service as cs
    text = cs.without_image_marks(req.text)
    evidence = synthetic_evidence(text) or ("Creator declared an AI-generated image" if req.ai_generated_images else None)
    padded_text = f" {cs._normalize(text)} "
    synthetic_rules = [rule for rule in rules if rule.required_disclaimers and SYNTHETIC_RULE.search(f"{rule.category} {rule.title}")]
    if not evidence:
        for rule in synthetic_rules:
            findings[:] = [finding for finding in findings if finding.rule_id != rule.id
                           or finding.status == "CLEARED" or _direct_breach(finding, rule, padded_text)]
        return
    if "imagery" not in (req.content_parts or cs.parts_for(req.content_type)):
        return
    published = req.disclaimer_text if req.disclaimer_text is not None else text
    padded = f" {cs._normalize(published)} "
    for rule in synthetic_rules:
        label = _approved_disclosure(rule.required_disclaimers, padded)
        if label:
            for finding in findings:
                if finding.rule_id == rule.id and finding.kind == "MISSING_DISCLAIMER":
                    _clear_disclosure(finding, label)
            continue
        existing = [finding for finding in findings if finding.rule_id == rule.id]
        if any(finding.status != "CLEARED" for finding in existing):
            continue
        findings[:] = [finding for finding in findings if finding not in existing]
        findings.append(ComplianceViolation(
            rule_id=rule.id, rule_number=rule.rule_number, title=rule.title, category=rule.category,
            severity=rule.severity, traffic_light="YELLOW", status="NEEDS_REVIEW", penalty=0,
            kind="MISSING_DISCLAIMER", flagged_terms=[evidence],
            missing_disclaimers=list(rule.required_disclaimers),
            explanation="The post has evidence of generated or conceptual visuals, "
                        "but the published content has no approved disclosure. Confirm the media's origin and label.",
            remediation_guidance="If the image is AI-generated, rendered or conceptual, add the rule's approved label: "
                                 + rule.required_disclaimers[0] + " A generic illustrative-purpose note does not "
                                 "replace a more specific label required by the rulebook.",
        ))
