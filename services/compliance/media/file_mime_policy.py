"""Server-side MIME type and Content-Disposition policy for user-uploaded files.

Derives MIME types strictly from allowlisted file extensions rather than trusting
client-supplied upload headers, so active content types (e.g. text/html,
image/svg+xml, application/javascript) cannot be served inline on the API origin.
"""

from pathlib import Path
from typing import Literal, Optional

SAFE_MIME_BY_EXT: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".m4v": "video/mp4",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
}

_INLINE_SAFE_MIME = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/webm",
    "video/x-matroska",
    "application/pdf",
})


def safe_file_mime(filename: Optional[str]) -> str:
    """Return an allowlisted MIME type based on file extension, or application/octet-stream."""
    ext = Path(filename or "").suffix.lower()
    return SAFE_MIME_BY_EXT.get(ext, "application/octet-stream")


def safe_disposition_type(mime: Optional[str]) -> Literal["inline", "attachment"]:
    """Allow inline browser preview only for safe raster images, videos, and PDFs."""
    base_mime = (mime or "").split(";", 1)[0].strip().lower()
    return "inline" if base_mime in _INLINE_SAFE_MIME else "attachment"
