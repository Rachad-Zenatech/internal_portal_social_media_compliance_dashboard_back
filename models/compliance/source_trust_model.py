"""Models for people's calls on sources (does this claim match the page? is the page a good source?) and the standing
a site earns from reviewers' calls."""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class SourceConfirmationRequest(BaseModel):
    """One call. With a claim: does it match what the page says (`matches`, or null to take the call back). Without a
    claim: is the page a good source (`good_source`, or null to take the call back)."""
    url: str = Field(..., min_length=1, max_length=2000)
    claim: Optional[str] = Field(None, max_length=1000)
    source_quote: Optional[str] = Field(None, max_length=2000)
    matches: Optional[bool] = None
    good_source: Optional[bool] = None


class SourceConfirmation(BaseModel):
    url: str
    # None for a call on the page itself.
    claim: Optional[str] = None
    matches: Optional[bool] = None
    good_source: Optional[bool] = None
    by_reviewer: bool = False
    # The call is the signed-in person's own.
    mine: bool = False


class SourceTrust(BaseModel):
    """What reviewers have said about a site, across every post checked against it."""
    host: str
    # TRUSTED: reviewers rated it a good source (or confirmed several claims against it and nobody rated it poor).
    # FLAGGED: reviewers rated it a poor source more often than a good one. UNRATED: no reviewer has said either yet.
    level: Literal["TRUSTED", "UNRATED", "FLAGGED"] = "UNRATED"
    confirmations: int = 0
    matches: int = 0
    good_votes: int = 0
    poor_votes: int = 0
    message: str = ""


class SourceConfirmationsResponse(BaseModel):
    confirmations: List[SourceConfirmation] = Field(default_factory=list)
    # By site.
    trust: Dict[str, SourceTrust] = Field(default_factory=dict)
    # The signed-in person's calls count towards a site's standing: they decide on this post (a reviewer, someone it
    # was sent or delegated to, or a named final approver or their delegate) and didn't write it.
    counts_toward_trust: bool = False
