"""Saved AI-image disclosure and whether the creator explicitly answered the question."""
from pydantic import BaseModel


class ImageProvenanceFields(BaseModel):
    ai_generated_images: bool = False
    ai_generated_images_confirmed: bool = False
    people_in_images: bool = False
    people_in_images_confirmed: bool = False
    people_in_images_detected: bool | None = None
