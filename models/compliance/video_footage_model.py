"""An explicit Yes/No declaration; None means the creator has not answered."""
from pydantic import BaseModel, StrictBool


class VideoFootageFields(BaseModel):
    video_third_party_footage: StrictBool | None = None
