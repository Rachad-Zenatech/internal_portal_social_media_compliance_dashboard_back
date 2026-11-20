"""Source-independent, versioned claims retained with each compliance result."""
from typing import List, Literal
from pydantic import BaseModel, Field


class ClaimLocation(BaseModel):
    start: int
    end: int
    where: Literal["caption", "image"] = "caption"
    image: int | None = None


class EvidenceClaim(BaseModel):
    id: str
    wording: str
    subject: str = ""
    assertion: str = ""
    scope: str = ""
    qualification: str = ""
    attribution: Literal["explicit", "inferred", "ambiguous"] = "ambiguous"
    evidence_needed: str = ""
    ambiguous: bool = False
    locations: List[ClaimLocation] = Field(default_factory=list)


class ClaimManifest(BaseModel):
    version: str
    revision: str
    published_text: str = ""
    complete: bool = True
    claims: List[EvidenceClaim] = Field(default_factory=list)
    # Classification coverage is retained so excluded text can be audited as well.
    passages: List[dict] = Field(default_factory=list)
