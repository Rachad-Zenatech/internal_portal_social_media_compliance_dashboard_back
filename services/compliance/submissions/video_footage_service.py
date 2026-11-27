"""Save the creator's footage answer and invalidate checks only when it changes."""
from datetime import datetime, timezone


def update_fields(row, payload):
    answer = payload.get("video_third_party_footage")
    if answer is None or answer == row.get("video_third_party_footage"):
        return {}
    return {"video_third_party_footage": answer, "video_footage_edited_at": datetime.now(timezone.utc)}


def missing(row, has_video):
    return bool(has_video and row.get("video_third_party_footage") is None)


def response_fields(row):
    return {"video_third_party_footage": row.get("video_third_party_footage")}
