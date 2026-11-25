"""Source-attributed answers for one final unresolved-claim review."""
from copy import deepcopy
from typing import List, Optional
from pydantic import BaseModel, Field
from .source_verdict_models import _Result, RESPONSE_SCHEMA


class RecheckClaim(_Result):
    source_id: Optional[str] = None
    excerpt_id: Optional[str] = None
    source_quote: Optional[str] = None


class RecheckResponse(BaseModel):
    claims: List[RecheckClaim] = Field(default_factory=list)


SCHEMA = deepcopy(RESPONSE_SCHEMA)
_item = SCHEMA["properties"]["claims"]["items"]
_item["properties"].update({name: {"type": "STRING", "nullable": True}
                            for name in ("source_id", "excerpt_id", "source_quote")})
_item["properties"]["verdict"]["enum"] = ["SUPPORTED", "UNCLEAR"]
_item["required"] = list(dict.fromkeys([*_item["required"], "where", "source_id", "excerpt_id", "source_quote"]))
