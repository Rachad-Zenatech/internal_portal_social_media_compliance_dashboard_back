"""Military names and citations alone do not establish use of government visual material."""
import re

from models.compliance.compliance_model import ComplianceRule


_AGENCY = r"(?:dod|department of defense|dvids|army|navy|air force|marine corps|space force|military|soldiers)"
_VISUAL = r"(?:footage|videos?|clips?|photos?|photographs?|images?|visual information|seals?|logos?|emblems?|pictured|shown)"
_GAP = r"(?:\s+\w+){0,8}\s+"
_MILITARY_VISUAL = re.compile(rf"\b(?:{_AGENCY}{_GAP}{_VISUAL}|{_VISUAL}{_GAP}{_AGENCY})\b")


def requires_visual_review(rule: ComplianceRule, normalized_text: str) -> bool:
    """Gate only the baseline DoD visual disclaimer, preserving custom rules and explicit prohibited claims.

    Text extraction cannot prove that an Army reference is government imagery. Require a positive media cue;
    source accuracy and actual endorsement claims are checked separately. The caller supplies normalized text
    without extraction labels such as 'Text in image 1', which are not evidence of military imagery.
    """
    if rule.is_custom or rule.rule_number not in {"Rule 4", "Rule 13"}:
        return True
    standard_disclaimer = any(
        "endorsement" in d.lower() and ("dod" in d.lower() or "department of defense" in d.lower())
        for d in rule.required_disclaimers
    )
    return not standard_disclaimer or bool(_MILITARY_VISUAL.search(normalized_text))
