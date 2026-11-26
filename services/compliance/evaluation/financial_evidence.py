"""Link deterministic financial flags to the source-verified assertions, not display fragments."""
import re
from uuid import uuid5

from . import claim_checks
from .image_marks import IMAGE_MARK
from .source_assertions import _ABBREVIATION, identity
from .source_claim_grounding import _tokens

_FINANCIAL = next(check for check in claim_checks.CHECKS if check.key == "financial-figure")
_BOUNDARY = re.compile(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])")


def _sentences(body):
    start = 0
    for match in _BOUNDARY.finditer(body):
        if "\n\n" not in match.group() and _ABBREVIATION.search(body[:match.start()]):
            continue
        yield start, match.start()
        start = match.end()
    yield start, len(body)


def _excerpt_spans(body, quotes):
    tokens = _tokens(body)
    values = [value for value, _, _ in tokens]
    spans = []
    for quote in quotes:
        wanted = [value for value, _, _ in _tokens(quote)]
        if sum(bool(re.search(r"[a-z]", word)) for word in wanted) < 3:
            continue  # Bare amounts or labels cannot establish the assertion's meaning.
        for at in range(len(tokens) - len(wanted) + 1):
            if wanted and values[at:at + len(wanted)] == wanted:
                spans.append((tokens[at][1], tokens[at + len(wanted) - 1][2]))
    return spans


def _supported_spans(body, where, checks):
    return _excerpt_spans(body, [claim.claim for check in checks if check.status == "READ"
                                for claim in check.claims if claim.verdict == "SUPPORTED" and claim.where == where])


def expand(violations, checks, text):
    """Split the built-in money flag by grounded assertion and retain uncovered financial occurrences.

    Exact source-verified excerpts may join wrapped lines. Equal amounts elsewhere, changed qualifiers,
    unreadable sources and unsupported source excerpts never provide this link.
    """
    prototype = next((v for v in violations if v.rule_id == _FINANCIAL.rule_id), None)
    if prototype is None:
        return set()
    groups, seen = {}, False
    addressed_quotes = [quote for v in violations if v.category == "Source Check" and v.rule_number == "Rule 18"
                        and v.status in {"VIOLATION", "NEEDS_REVIEW", "EVIDENCE_REQUIRED"} for quote in v.flagged_terms]
    for image, body in enumerate(IMAGE_MARK.split(text)):
        where = "caption" if image == 0 else "image"
        supported = _supported_spans(body, where, checks)
        addressed = _excerpt_spans(body, addressed_quotes)
        sentences = list(_sentences(body))
        for hit in claim_checks._MONEY.finditer(body.lower()):
            seen = True
            if any(a <= hit.start() and z >= hit.end() for a, z in addressed):
                continue  # The source checker already owns this assertion and its deduction.
            covering = [(a, z) for a, z in supported if a <= hit.start() and z >= hit.end()]
            start, end = min(covering, key=lambda span: span[1] - span[0]) if covering else next(
                (span for span in sentences if span[0] <= hit.start() < span[1]), (0, len(body)))
            passage = " ".join(body[start:end].split())
            key = (image, identity(passage, where))
            if key not in groups:
                groups[key] = (passage, [], body[start:end])
            terms = groups[key][1]
            term = hit.group().strip()
            if term not in terms:
                terms.append(term)
    replacements = []
    for (image, claim_key), (passage, terms, context) in groups.items():
        replacements.append(prototype.model_copy(update={
            "rule_id": uuid5(_FINANCIAL.rule_id, f"{image}:{claim_key}"),
            "passage": passage, "context": context, "flagged_terms": terms,
            "explanation": "These financial figures need supporting evidence: " + ", ".join(terms) + ".",
        }))
    # Retain the original warning if extraction could not ground any occurrence.
    if not replacements and not seen:
        return set()
    at = violations.index(prototype)
    violations[at:at + 1] = replacements
    return {finding.rule_id for finding in replacements}
