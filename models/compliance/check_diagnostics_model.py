"""Public reasons a compliance review remains incomplete; never include provider secrets."""
from typing import Literal, Optional
from pydantic import BaseModel


class IncompleteCheckStage(BaseModel):
    stage: Literal['RULEBOOK', 'CLAIM_EXTRACTION', 'SOURCE_VERIFICATION', 'SOURCE_RETRIEVAL', 'CONTEXT_REVIEW', 'VISUAL_REVIEW']
    reason: str
    code: Optional[str] = None
