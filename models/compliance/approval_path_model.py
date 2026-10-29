"""Approval paths: who gives final approval for which content."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ApprovalPathIn(BaseModel):
    # Empty brand or audience: any. ceo_content: only Shaun's posts (his account or his rule set).
    brand_code: Optional[str] = Field(None, max_length=50)
    audience: Optional[str] = Field(None, max_length=50)
    ceo_content: bool = False
    label: Optional[str] = Field(None, max_length=200)
    approver_ids: List[UUID] = Field(..., min_length=1, max_length=20)


class ApprovalPath(ApprovalPathIn):
    id: UUID
    approver_names: List[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ApproverOption(BaseModel):
    id: UUID
    name: str
    email: str
    # Brands they work on; empty means every brand.
    brands: List[str] = Field(default_factory=list)


class ApprovalPathOptions(BaseModel):
    approvers: List[ApproverOption]
    brands: List[dict]
    audiences: dict
