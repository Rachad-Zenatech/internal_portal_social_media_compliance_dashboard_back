"""Safeguards on the model's claim-by-claim answer for a source. They only ever move SUPPORTED towards UNCLEAR ("a person
should confirm"), never the other way:

- the claim's figures and years have to appear in the source's text;
- the evidence the model gives has to be found in the source's text (most of its distinctive words), so a quote the
  model made up doesn't pass as support;
- SUPPORTED needs evidence and enough confidence.
"""

import os
import re
from typing import Any, List, Sequence

from models.compliance.compliance_model import SourceClaim

SUPPORT_MIN_CONFIDENCE = float(os.getenv("SOURCE_CHECK_MIN_CONFIDENCE", "0.7"))
# Share of the evidence's distinctive words the source's text must contain.
EVIDENCE_MIN_OVERLAP = float(os.getenv("SOURCE_CHECK_EVIDENCE_OVERLAP", "0.6"))
# Evidence with fewer distinctive words than this is too short to check this way.
EVIDENCE_MIN_WORDS = int(os.getenv("SOURCE_CHECK_EVIDENCE_MIN_WORDS", "3"))


# --- Figures must be in the source -----------------------------------------------------------------------------------

_WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}
_WORD_RE = re.compile(r"\b(" + "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True)) + r")\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _digits(text: str) -> str:
    """Text with figures in one form: no thousands commas, and the common number words as digits."""
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text or "")
    return _WORD_RE.sub(lambda m: str(_WORD_NUMBERS[m.group(1).lower()]), text)


def missing_figures(claim: str, source_text: str) -> List[str]:
    """Figures and years in the claim that the source's text never mentions (in digits or as words)."""
    have = set(_NUMBER_RE.findall(_digits(source_text)))
    # The claim's own figures as written: a word like "one" in a claim isn't a figure to find.
    written = re.sub(r"(?<=\d),(?=\d{3}\b)", "", claim or "")
    out: List[str] = []
    for n in _NUMBER_RE.findall(written):
        if n not in have and n not in out:
            out.append(n)
    return out


# --- The evidence must be in the source ------------------------------------------------------------------------------

# Words a model uses to talk about a source rather than words taken from it.
_FRAMING = {
    "source", "sources", "says", "said", "states", "stated", "notes", "noted", "mentions", "mentioned", "according",
    "article", "memo", "memorandum", "document", "page", "report", "text", "that", "this", "with", "will", "from",
    "which", "there", "their", "they", "have", "also", "into", "than", "then", "when", "were", "been", "being",
}


def _stems(text: str) -> List[str]:
    """Distinctive words, cut to their first five letters so "establish" matches "establishment"."""
    words = re.findall(r"[a-z0-9]+", _digits((text or "").lower()))
    return [w[:5] for w in words if (len(w) >= 4 or w.isdigit()) and w not in _FRAMING]


def evidence_overlap(evidence: str, source_text: str) -> float:
    """Share of the evidence's distinctive words that the source's text contains (1.0 when too short to judge)."""
    wanted = list(dict.fromkeys(_stems(evidence)))
    if len(wanted) < EVIDENCE_MIN_WORDS:
        return 1.0
    have = set(_stems(source_text))
    return sum(1 for w in wanted if w in have) / len(wanted)


# --- Applying them ---------------------------------------------------------------------------------------------------

def _add(note: Any, extra: str) -> str:
    return (note + " " if note else "") + extra


def _to_claims(results: Sequence[Any], source_text: str) -> List[SourceClaim]:
    """The model's answers, through the safeguards."""
    out: List[SourceClaim] = []
    for r in results:
        verdict = r.verdict if r.verdict in ("SUPPORTED", "CONTRADICTED", "NOT_FOUND", "UNCLEAR") else "UNCLEAR"
        note = (r.note or "").strip() or None
        evidence = (r.evidence or "").strip() or None
        if verdict == "SUPPORTED":
            lacking = missing_figures(r.claim, source_text)
            if not evidence or r.confidence < SUPPORT_MIN_CONFIDENCE:
                verdict = "UNCLEAR"
                note = _add(note, "It wasn't clear enough that the source says this, so a person should confirm.")
            elif lacking:
                verdict = "UNCLEAR"
                note = _add(note, f"The source's text doesn't contain {', '.join(lacking)}, so a person should confirm it.")
            elif evidence_overlap(evidence, source_text) < EVIDENCE_MIN_OVERLAP:
                verdict = "UNCLEAR"
                note = _add(note, "The evidence given couldn't be found in the source's text, so a person should confirm it.")
        loc = (r.source_location or "").strip() or None
        out.append(SourceClaim(
            claim=(r.claim or "").strip()[:400], verdict=verdict, where="image" if r.where == "image" else "caption",
            evidence=evidence[:600] if evidence else None, source_location=loc[:240] if loc else None,
            note=note[:400] if note else None,
            confidence=round(r.confidence, 2),
        ))
    return [c for c in out if c.claim]
