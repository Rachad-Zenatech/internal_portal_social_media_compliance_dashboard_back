"""Only the existing dummy accounts support local test sign-in."""

from typing import Literal

from pydantic import BaseModel, Field


class TestLoginRequest(BaseModel):
    as_: Literal["creator", "manager", "reviewer", "reviewer2", "reviewer3", "suggester"] = Field(alias="as")
