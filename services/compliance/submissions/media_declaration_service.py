"""Required creator declarations for uploaded media."""
from . import image_provenance_service, video_footage_service


def missing_fields(row, has_main):
    missing = []
    if image_provenance_service.missing(row):
        missing.append("ai_generated_images")
    if video_footage_service.missing(row, row["content_type"] == "VIDEO" and has_main):
        missing.append("video_third_party_footage")
    return missing
