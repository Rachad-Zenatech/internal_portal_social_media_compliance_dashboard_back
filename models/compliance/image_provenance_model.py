"""Saved AI-image disclosure and whether the creator explicitly answered the question."""
from typing import Dict
from pydantic import BaseModel, Field


class ImageProvenanceFields(BaseModel):
    ai_generated_images: bool = False
    ai_generated_images_confirmed: bool = False
    people_in_images: bool = False
    people_in_images_confirmed: bool = False
    people_in_images_detected: bool | None = None
    employee_in_images: bool = False
    employee_in_images_confirmed: bool = False
    face_employee_flags: Dict[str, bool] = Field(default_factory=dict)
