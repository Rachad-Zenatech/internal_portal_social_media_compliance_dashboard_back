"""Caption suggestions: Gemini drafts a few captions from the post itself, checked against the rulebook."""

from typing import List, Optional

from pydantic import BaseModel, Field


class CaptionSuggestRequest(BaseModel):
    # Anything the writer wants it to focus on ("mention the Ohio launch", "keep it short").
    hint: Optional[str] = Field(None, max_length=500)
    # The tightest character limit of the selected platforms, hashtags included.
    max_chars: int = Field(2200, ge=100, le=63206)


class CaptionCheck(BaseModel):
    score: int
    traffic_light: str
    blocked: bool = False
    # Rule numbers and the words they flagged, so the writer sees why before using it.
    flags: List[str] = Field(default_factory=list)


class CaptionSuggestion(BaseModel):
    style: str
    text: str
    hashtags: List[str] = Field(default_factory=list)
    # Caption plus hashtags, as it would be posted.
    characters: int
    check: CaptionCheck


class CaptionSuggestions(BaseModel):
    suggestions: List[CaptionSuggestion] = Field(default_factory=list)
    # What the suggestions were based on, e.g. "3 images", "the video transcript".
    based_on: List[str] = Field(default_factory=list)
    # Anything it couldn't see or had to guess.
    notes: List[str] = Field(default_factory=list)
    model: Optional[str] = None
