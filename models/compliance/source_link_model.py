"""Models for submission source links and quick AI link checks."""

import os
from typing import List, Literal, Optional
from pydantic import BaseModel, Field

from models.compliance.compliance_model import SourceCheck

# The same limits the source reader keeps to (SOURCE_MIN_CHARS / SOURCE_MAX_CHARS in source_fetch_service).
PASTED_MIN_CHARS = int(os.getenv("SOURCE_MIN_CHARS", "200"))
PASTED_MAX_CHARS = int(os.getenv("SOURCE_MAX_CHARS", "250000"))


class SourceLinksMixin(BaseModel):
    source_links: List[str] = Field(default_factory=list)


class SourceLinksUpdateMixin(BaseModel):
    source_links: Optional[List[str]] = Field(None, max_length=20)


class SourceLinkCheckRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000)


class PastedSourceCheckRequest(BaseModel):
    """A whole article (or memo, release...) pasted in as the source, for when its page can't be opened."""
    text: str = Field(..., min_length=PASTED_MIN_CHARS, max_length=PASTED_MAX_CHARS)
    title: Optional[str] = Field(None, max_length=300)
    # Where it was copied from, if known: shown with the result and used to judge who published it.
    url: Optional[str] = Field(None, max_length=2000)


class ImageSourceCitation(BaseModel):
    kind: Literal["URL", "NAMED"]
    reference: str
    cue: Optional[str] = None


class ImageSourceScanResponse(BaseModel):
    extracted_text: str = ""
    citations: List[ImageSourceCitation] = Field(default_factory=list)
    resolved_url: Optional[str] = None
    # The claims checked against every readable source found (up to two), merged claim by claim.
    check: Optional[SourceCheck] = None
    # Each readable source's own check, the most authoritative first.
    source_checks: List[SourceCheck] = Field(default_factory=list)
    message: Optional[str] = None


class ImageSourceScanJobResponse(ImageSourceScanResponse):
    job_id: str
    status: Literal["PROCESSING", "COMPLETED", "FAILED"] = "PROCESSING"

