"""Require an explicit Yes/No disclosure for attached post images."""
from datetime import datetime, timezone
import json
from uuid import UUID
from fastapi import HTTPException
from models.compliance.submission_model import SubmissionFile
from postgresql_db.database import fetch_one

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
    fields = {}
    answer = payload.get("ai_generated_images")
    if answer is not None:
        if not row.get("ai_generated_images_confirmed"):
            fields["ai_generated_images_confirmed"] = True
        if answer != bool(row.get("ai_generated_images")):
            fields.update(ai_generated_images=answer, image_provenance_edited_at=datetime.now(timezone.utc))
    people_answer = payload.get("people_in_images")
    if people_answer is not None:
        fields["people_in_images"] = people_answer
        fields["people_in_images_confirmed"] = True
    employee_answer = payload.get("employee_in_images")
    if employee_answer is not None:
        fields["employee_in_images"] = employee_answer
        fields["employee_in_images_confirmed"] = True
    face_flags = payload.get("face_employee_flags")
    if isinstance(face_flags, dict):
        clean_flags = {str(k): bool(v) for k, v in face_flags.items()}
        fields["face_employee_flags"] = json.dumps(clean_flags)
        if employee_answer is None and clean_flags:
            fields["employee_in_images"] = any(clean_flags.values())
            fields["employee_in_images_confirmed"] = True
    return fields


async def sync_faces(submission_id: UUID, payload: dict, user_id: UUID) -> None:
    if isinstance(payload.get("face_employee_flags"), dict):
        from services.compliance.media import face_recognition_service
        await face_recognition_service.sync_employee_faces(submission_id, payload["face_employee_flags"], user_id)


async def retry_image_read(submission_id: UUID, file_id: UUID, user_id: UUID) -> SubmissionFile:
    """Queue an image's OCR and anonymous face read again, including a stalled attempt."""
    from services.ai.observability import ai_usage_service
    from services.compliance.media import image_text_service
    from services.compliance.submissions import submission_service

    await submission_service._get_owned_row(submission_id, user_id)
    await ai_usage_service.reserve("IMAGE_TEXT", user_id, submission_id)
    f = await fetch_one(
        "UPDATE content_submission_files SET image_text_status = NULL, image_text_error = NULL, "
        "image_text_updated_at = NOW() WHERE id = $1 AND submission_id = $2 AND deleted_at IS NULL AND mime_type LIKE 'image/%' "
        "RETURNING id, kind, original_name, mime_type, size_bytes, created_at, image_text, image_text_status, image_text_error, "
        "person_detected, face_boxes, face_detection_version",
        file_id, submission_id,
    )
    if not f:
        raise HTTPException(status_code=404, detail="Image file not found")
    image_text_service.schedule([file_id])
    return submission_service._file_out(submission_id, f)


async def schedule_missing_images(files) -> None:
    """Queue face/text reads for attached images that haven't completed face detection yet."""
    from services.compliance.media import image_text_service

    due = [
        f["id"] for f in files
        if image_text_service.is_image_file(f.get("kind", ""), f.get("mime_type") or "")
        and (f.get("image_text_status") in (None, "PROCESSING")
             or (f.get("image_text_status") == "COMPLETED" and (f.get("face_detection_version") or 0) < 2))
    ]
    if due:
        await image_text_service.schedule_missing(due)
