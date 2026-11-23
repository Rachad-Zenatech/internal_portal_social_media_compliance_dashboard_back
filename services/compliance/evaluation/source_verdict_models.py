"""Structured claim comparison answers shared by source review and its durable cache."""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class _Result(BaseModel):
    claim: str
    where: str = "caption"
    verdict: str
    evidence: Optional[str] = None
    source_location: Optional[str] = None
    note: Optional[str] = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    contradiction_kind: Optional[str] = None


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
                    "claim": {"type": "STRING"},
                    "where": {"type": "STRING", "enum": ["caption", "image"]},
                    "verdict": {"type": "STRING", "enum": ["SUPPORTED", "CONTRADICTED", "NOT_FOUND", "UNCLEAR"]},
                    "evidence": {"type": "STRING", "nullable": True},
                    "source_location": {"type": "STRING", "nullable": True},
                    "note": {"type": "STRING", "nullable": True},
                    "confidence": {"type": "NUMBER"},
                    "contradiction_kind": {"type": "STRING", "enum": ["EXPLICIT_CONFLICT", "MATERIAL_OVERSTATEMENT", "NOT_STATED", "NONE"]},
                },
                "required": ["claim", "verdict", "confidence", "contradiction_kind"],
            },
        }
    },
    "required": ["claims"],
}
