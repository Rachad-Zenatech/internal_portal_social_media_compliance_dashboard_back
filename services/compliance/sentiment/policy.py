"""Content types eligible for optional sentiment analysis."""
from fastapi import HTTPException


def applies(row):
    return str(row.get("content_type") or "").upper() != "TEXT_POST"


def require_enabled(row):
    if not applies(row):
        raise HTTPException(422, "Text posts only need compliance review; sentiment analysis is not available.")
    return row


async def load_eligible(submission_id, load):
    row = await load(submission_id)
    if not row:
        raise ValueError("Submission not found")
    return require_enabled(row)
