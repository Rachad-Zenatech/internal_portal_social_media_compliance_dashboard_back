"""Exclude incidental OCR metadata without exempting factual capability assertions."""
import re
from .source_image_claims import is_identifier

_HEADING = re.compile(r"^(?:(?:19|20)\d{2}\s+)?(?:sponsors?|partners?|exhibitors?|thought leaders?|"
                      r"influencers?|brand partners?|media partners?)(?:\s+(?:19|20)\d{2})?$", re.I)


def incidental(claim, where):
    if where != "image":
        return False
    lines = [line.strip() for line in claim.splitlines() if line.strip()]
    if not lines:
        return False
    metadata = [line for line in lines if is_identifier(line) or _HEADING.fullmatch(line)]
    leftovers = [line for line in lines if line not in metadata]
    # A website/logo beside a sponsor heading is metadata; a sentence asserting sponsorship is not.
    return bool(metadata) and (not leftovers or (any(_HEADING.fullmatch(line) for line in lines)
        and all(re.fullmatch(r"[A-Z& -]{1,25}", line) and len(line.split()) <= 2 for line in leftovers)))
