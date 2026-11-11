"""Brand voice per brand: the brand manual (PDF) and what was read from it (voice, colours, typefaces, logo)."""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# NONE: no manual yet. PROCESSING: reading it. DONE: read in full. PARTIAL: the AI couldn't read it, so only what the
# PDF itself gives (colour codes, fonts, a logo guess) was filled in. FAILED: nothing could be read.
BrandVoiceStatus = Literal["NONE", "PROCESSING", "DONE", "PARTIAL", "FAILED"]


class BrandColor(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    hex: str = Field(..., pattern=r"^#[0-9A-Fa-f]{6}$")
    usage: Optional[str] = Field(None, max_length=200)


class BrandTypeface(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    role: Optional[str] = Field(None, max_length=100)  # e.g. Headings, Body
    usage: Optional[str] = Field(None, max_length=200)


class BrandVoiceIn(BaseModel):
    """What a person can change by hand after (or instead of) reading the manual."""
    voice: Optional[str] = Field(None, max_length=2000)
    tone: List[str] = Field(default_factory=list, max_length=20)
    do_examples: List[str] = Field(default_factory=list, max_length=20)
    dont_examples: List[str] = Field(default_factory=list, max_length=20)
    colors: List[BrandColor] = Field(default_factory=list, max_length=24)
    typefaces: List[BrandTypeface] = Field(default_factory=list, max_length=12)
    logo_notes: Optional[str] = Field(None, max_length=500)


class BrandVoice(BrandVoiceIn):
    brand_code: str
    brand_name: str
    status: BrandVoiceStatus = "NONE"
    error: Optional[str] = None
    manual_name: Optional[str] = None
    manual_size: Optional[int] = None
    manual_uploaded_at: Optional[datetime] = None
    has_logo: bool = False
    extracted_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    updated_by_name: Optional[str] = None
    # The viewer may upload a manual or edit this brand's voice.
    can_edit: bool = False
