"""Structured claim comparison answers shared by source review and its durable cache."""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class _Result(BaseModel):
    claim_id: Optional[str] = None
    post_start: Optional[int] = None
    post_end: Optional[int] = None
    image_index: Optional[int] = None
    context: Optional[str] = None
    claim: str
    where: str = "caption"
    verdict: str
    evidence: Optional[str] = None
    evidence_quotes: List[str] = Field(default_factory=list, max_length=3)
    source_location: Optional[str] = None
    note: Optional[str] = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    contradiction_kind: Optional[str] = None
    assertion_kind: Optional[str] = None


class _Response(BaseModel):
    claims: List[_Result] = Field(default_factory=list)


RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "claims": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "assertion_kind": {"type": "STRING", "enum": ["FACTUAL_ASSERTION", "INCIDENTAL_TEXT", "NON_ASSERTION"]},
                    "claim_id": {"type": "STRING", "nullable": True},
                    "claim": {"type": "STRING"},
                    "where": {"type": "STRING", "enum": ["caption", "image"]},
                    "verdict": {"type": "STRING", "enum": ["SUPPORTED", "CONTRADICTED", "NOT_FOUND", "UNCLEAR"]},
                    "evidence": {"type": "STRING", "nullable": True},
                    "evidence_quotes": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "source_location": {"type": "STRING", "nullable": True},
                    "note": {"type": "STRING", "nullable": True},
                    "confidence": {"type": "NUMBER"},
                    "contradiction_kind": {"type": "STRING", "enum": ["EXPLICIT_CONFLICT", "MATERIAL_OVERSTATEMENT", "NOT_STATED", "NONE"]},
                },
                "required": ["claim", "verdict", "confidence", "contradiction_kind", "evidence_quotes"],
            },
        }
    },
    "required": ["claims"],
}
