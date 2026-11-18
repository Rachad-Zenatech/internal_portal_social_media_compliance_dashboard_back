"""Require an explicit Yes/No disclosure for attached post images."""
from datetime import datetime, timezone

HAS_IMAGES_SQL = """EXISTS (SELECT 1 FROM content_submission_files ip WHERE ip.submission_id = s.id
    AND ip.deleted_at IS NULL AND ip.kind IN ('MAIN', 'IMAGE') AND ip.mime_type LIKE 'image/%')"""
COLUMNS_SQL = "s.ai_generated_images, s.ai_generated_images_confirmed, s.image_provenance_edited_at, " + HAS_IMAGES_SQL + " AS has_images, "


def is_confirmed(row):
    # A checked legacy checkbox, or an earlier explicit toggle, already records an answer.
    return bool(row.get("ai_generated_images_confirmed") or row.get("ai_generated_images")
                or row.get("image_provenance_edited_at"))


def missing(row):
    return bool(row.get("has_images") and not is_confirmed(row))


def update_fields(row, payload):
    answer = payload.get("ai_generated_images")
    if answer is None:
        return {}
    fields = {} if row.get("ai_generated_images_confirmed") else {"ai_generated_images_confirmed": True}
    if answer != bool(row.get("ai_generated_images")):
        fields.update(ai_generated_images=answer, image_provenance_edited_at=datetime.now(timezone.utc))
    return fields
