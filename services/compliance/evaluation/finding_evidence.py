"""Use a finding's judged evidence consistently across image, video and text locations."""
import re

VERSION = "highlight-evidence-3-clearance-quotes"


def _value(item, key, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _normalized(text):
    return " ".join(re.findall(r"\w+", str(text).casefold()))


def _clean(text):
    return re.sub(r"^\s*Text in image\s+\d+\s*:\s*", "", str(text or ""), flags=re.I).strip()


def highlight_terms(finding):
    """Prefer the judged passage; never fall back to its generic trigger in another location."""
    evaluation = _value(finding, "evaluation")
    if _value(evaluation, "source") == "ADJUSTED" and _value(evaluation, "model_decision") == "NO_VIOLATION":
        return []  # The quote supports clearance, not the finding retained by a safeguard.
    matched = _clean(_value(evaluation, "matched_text"))
    passage = _clean(_value(finding, "passage"))
    passage = passage if len(passage) <= 400 else ""
    terms = _value(finding, "flagged_terms", []) or []

    def contains(text, term):
        return f" {_normalized(term)} " in f" {_normalized(text)} "

    if matched:
        return [passage if passage and contains(passage, matched) else matched]
    if passage and any(_normalized(term) and contains(passage, term) for term in terms):
        return [passage]
    return [term for term in terms if _normalized(term)]
