"""Checking a post against more than one candidate source found for an image's citation.

The candidates are tried by reviewers' standing of their sites first (rated good, unrated, rated poor; see
source_trust_service), then most authoritative first (official, then the company's own, then news). Up to MAX_SOURCES
readable ones are compared with the post, and their answers are merged claim by claim into the first one's check:

- a claim the first source doesn't back (NOT_FOUND or UNCLEAR) but another source does becomes SUPPORTED, with that
  source's evidence and a note naming it;
- a CONTRADICTED claim is never overruled; when another source backs it, the note says so, for a person to settle;
- a claim only another source speaks to is added, with a note naming that source.
"""

import os
from typing import List, Optional, Sequence, Tuple
from uuid import UUID

from models.compliance.compliance_model import SourceCheck, SourceClaim
from services.compliance.evaluation.source_check_service import authority_of
from services.compliance.evaluation.source_claim_checks import _stems

MAX_SOURCES = int(os.getenv("IMAGE_SOURCE_MAX_SOURCES", "2"))
MAX_CANDIDATES = int(os.getenv("IMAGE_SOURCE_MAX_CANDIDATES", "5"))
# Share of the shorter claim's distinctive words two claims must share to count as the same claim.
SAME_CLAIM_OVERLAP = float(os.getenv("IMAGE_SOURCE_SAME_CLAIM_OVERLAP", "0.6"))
_RANK = {"OFFICIAL": 0, "COMPANY": 1, "NEWS": 2, "OTHER": 3}


def rank_by_authority(urls: Sequence[str]) -> List[str]:
    """The candidates without repeats, most authoritative first; the search's own order within each kind."""
    unique = list(dict.fromkeys(u for u in urls if u))
    return sorted(unique, key=lambda u: _RANK.get(authority_of(u), 3))


def _same_claim(a: str, b: str) -> bool:
    sa, sb = set(_stems(a)), set(_stems(b))
    if not sa or not sb:
        return False
    return len(sa & sb) / min(len(sa), len(sb)) >= SAME_CLAIM_OVERLAP


def _name(check: SourceCheck) -> str:
    return check.url or check.title or check.reference


def _backing(text: str, others: Sequence[SourceCheck]) -> Optional[Tuple[SourceCheck, SourceClaim]]:
    for other in others:
        for c in other.claims:
            if c.verdict == "SUPPORTED" and _same_claim(text, c.claim):
                return other, c
    return None


def _merged_claim(c: SourceClaim, others: Sequence[SourceCheck]) -> SourceClaim:
    if c.verdict == "SUPPORTED":
        return c
    found = _backing(c.claim, others)
    if not found:
        return c
    other, backed = found
    if c.verdict == "CONTRADICTED":
        extra = f"However, {_name(other)} supports it, so a person should check which source is right."
        return c.model_copy(update={"note": f"{c.note} {extra}" if c.note else extra})
    return c.model_copy(update={
        "verdict": "SUPPORTED", "evidence": backed.evidence, "source_location": backed.source_location,
        "source_quote": backed.source_quote, "source_url": other.url, "confidence": backed.confidence,
        "note": f"Backed by {_name(other)}.",
    })


def merge_checks(primary: SourceCheck, others: Sequence[SourceCheck]) -> SourceCheck:
    """The first source's check with what the other sources add, claim by claim (see the module notes)."""
    claims = [_merged_claim(c, others) for c in primary.claims]
    for other in others:
        for c in other.claims:
            if c.verdict in ("SUPPORTED", "CONTRADICTED") and not any(_same_claim(c.claim, k.claim) for k in claims):
                claims.append(c.model_copy(update={
                    "source_url": other.url, "note": f"From {_name(other)}." + (f" {c.note}" if c.note else ""),
                }))
    message = primary.message
    if claims and message and "No claim in the post" in message:
        message = None
    return primary.model_copy(update={"claims": claims, "message": message})


async def check_candidates(
    submission_id: UUID, urls: Sequence[str], user_id: UUID, image_text: Optional[str],
) -> Tuple[Optional[SourceCheck], List[SourceCheck]]:
    """(the merged check, the readable checks it was built from), trying the candidates most authoritative first.
    When none could be read, the merged check is the first attempt's (it says why) and the list is empty."""
    from services.compliance.submissions import source_link_service

    from services.compliance.submissions.source_trust_service import rank_by_trust

    first: Optional[SourceCheck] = None
    read: List[SourceCheck] = []
    # Sites reviewers rated good come first, poor ones last; within each, official before company before news.
    for url in (await rank_by_trust(rank_by_authority(urls)))[:MAX_CANDIDATES]:
        check = await source_link_service.check_submission_source_link(submission_id, url, user_id, image_text=image_text)
        first = first or check
        if check.status == "READ" and check.claims:
            read.append(check)
            if len(read) >= MAX_SOURCES:
                break
    if not read:
        return first, []
    return merge_checks(read[0], read[1:]), read
