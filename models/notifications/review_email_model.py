"""Small workflow records shared by email selection, scheduling and rendering."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass
class ReviewEmailItem:
    key: str
    entity_id: str
    kind: str
    title: str
    reference: str
    path: str
    creator_id: str
    stage: str
    due_at: Optional[datetime]
    sent_at: Optional[datetime]
    resubmitted: bool = False
    reviewer_ids: list[str] = field(default_factory=list)
    checked_ids: list[str] = field(default_factory=list)
    brands: list[str] = field(default_factory=list)
    audiences: list[str] = field(default_factory=list)
    rule_sets: list[str] = field(default_factory=list)
    approver_ids: list[str] = field(default_factory=list)
    document: bool = False
    ceo_account: bool = False


class EmailTimezonePreference(BaseModel):
    time_zone: str = Field(min_length=1, max_length=100)
    source: Literal["BROWSER", "MANUAL"] = "BROWSER"

    @field_validator("time_zone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Choose a valid IANA time zone, such as America/Toronto") from exc
        return value
