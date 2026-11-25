"""Post creation inputs and the optional video script review setting."""

from typing import Literal, Optional

from pydantic import BaseModel, Field

# AD: paid social ads. Ad-specific fields and rules are still to be defined.
ContentType = Literal["VIDEO", "TEXT_POST", "SOCIAL_POST", "CAROUSEL", "IMAGE", "BLOG", "AD"]


class VideoScriptOptions(BaseModel):
    # Omission preserves the normal script-first default for new video posts.
    script_required: Optional[bool] = None


class SubmissionCreate(VideoScriptOptions):
    title: str = Field(..., min_length=1, max_length=300)
    content_type: ContentType
