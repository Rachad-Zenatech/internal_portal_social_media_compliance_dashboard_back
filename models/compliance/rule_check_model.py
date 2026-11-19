"""Explicit rule checking requirements and auditable coverage results."""
from typing import Annotated, List, Literal, Optional
from uuid import UUID
from pydantic import BaseModel, Field, StringConstraints, field_validator

CheckingMethod = Literal["TEXT_ANALYSIS", "VISUAL_INSPECTION", "SOURCE_VERIFICATION", "CLEARANCE_DOCUMENTS", "HUMAN_CONFIRMATION"]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RuleOutcome = Literal["NO_ISSUE_FOUND", "ISSUE", "EVIDENCE_REQUIRED", "HUMAN_REVIEW_REQUIRED", "NOT_APPLICABLE"]


class RuleCheckingMethods(BaseModel):
    # Empty is reserved for old/unconfigured records and is reported as incomplete, never inferred from phrases.
    checking_methods: List[CheckingMethod] = Field(default_factory=list)

    @field_validator("checking_methods")
    @classmethod
    def unique_methods(cls, value):
        return list(dict.fromkeys(value)) if value is not None else None


class MethodCheckResult(BaseModel):
    method: CheckingMethod
    outcome: Optional[RuleOutcome] = None
    complete: bool = False
    reason: Reason


class RuleCheckResult(BaseModel):
    rule_id: UUID
    rule_number: str
    title: str
    outcome: Optional[RuleOutcome] = None
    complete: bool = False
    reason: Reason
    methods: List[MethodCheckResult] = Field(default_factory=list)


class RuleCoverageFields(BaseModel):
    rule_results: List[RuleCheckResult] = Field(default_factory=list)
    rule_results_version: Optional[str] = None
    rule_results_complete: bool = False
