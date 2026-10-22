"""Canva Integration Models
Connecting a Canva account, picking a design and importing (or reimporting) its export into a submission.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

CanvaMode = Literal["MOCK", "LIVE"]
DesignKind = Literal["IMAGE", "VIDEO"]


class CanvaConnection(BaseModel):
    connected: bool
    # MOCK until the Canva Connect API keys are configured.
    mode: CanvaMode
    display_name: Optional[str] = None
    connected_at: Optional[datetime] = None


class CanvaDesign(BaseModel):
    id: str
    title: str
    kind: DesignKind
    width: int
    height: int
    # Goes up each time the design is edited in Canva; an import remembers the revision it took.
    revision: int
    updated_at: datetime
    thumbnail_url: str


class CanvaImportRequest(BaseModel):
    design_id: str = Field(..., min_length=1, max_length=200)


class CanvaReimportRequest(BaseModel):
    # Content sent back for changes: resubmit it for review straight after reimporting.
    resubmit: bool = False
