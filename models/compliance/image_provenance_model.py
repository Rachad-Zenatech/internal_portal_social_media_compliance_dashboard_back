"""Saved AI-image disclosure and whether the creator explicitly answered the question."""
from pydantic import BaseModel


class ImageProvenanceFields(BaseModel):
    ai_generated_images: bool = False
    ai_generated_images_confirmed: bool = False
