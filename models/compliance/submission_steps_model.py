"""Creator progress through submission and revision steps."""
from typing import List, Literal, Optional
from pydantic import BaseModel, Field

StepState = Literal["done", "stale", "running", "unavailable", "todo"]


class SubmissionSteps(BaseModel):
    """How far a post has got through the five steps a creator works through (see post_steps_service)."""
    details: StepState
    compliance: StepState
    sentiment: StepState
    mood: StepState
    send: StepState
    revision: bool = False
    suggestions: StepState = "todo"
    next: Optional[Literal["suggestions", "details", "compliance", "sentiment", "mood", "send"]] = None
    ready_to_send: bool = False
    # Sent (in review, approved or published): nothing can be edited or re-run.
    locked: bool = False
    missing: List[str] = Field(default_factory=list)
