"""Effective review score fields shared by submission and review queue responses."""
from typing import Optional
from pydantic import BaseModel


class ReviewScoreFields(BaseModel):
    # None when there is no completed score; dismissed flags are excluded.
    approval_score: Optional[int] = None
    min_approval_score: int = 90
