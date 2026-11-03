"""Video frames and attached images for sentiment findings."""

import os
from typing import Any, Optional, Tuple
from uuid import UUID
from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from postgresql_db.database import fetch_one
from services.compliance.sentiment.config import FRAME_WIDTH

# ---------------------------------------------------------------------------
# Video frames for findings
# ---------------------------------------------------------------------------

async def video_frame(submission_id: UUID, analysis_id: UUID, at_sec: float):
    """A JPEG of the analysed video at a moment (cached on disk), so a finding can show what was on screen."""
    from services.compliance.canva_mock_render import _ffmpeg
    from services.compliance.transcription_service import STORAGE_ROOT
    import subprocess

    row = await _analysis_row(submission_id, analysis_id)
    file_id = (_json(row["input"]) or {}).get("video_file_id")
    if not file_id:
        raise HTTPException(status_code=404, detail="This analysis didn't include a video")
    # Earlier versions' videos are kept (soft-deleted) for history, so don't filter on deleted_at.
    f = await fetch_one("SELECT storage_path FROM content_submission_files WHERE id = $1", UUID(file_id))
    src = (STORAGE_ROOT / f["storage_path"]).resolve() if f else None
    if not src or STORAGE_ROOT not in src.parents or not src.is_file():
        raise HTTPException(status_code=404, detail="The video is no longer available")
    at = max(0.0, round(at_sec, 1))
    out = STORAGE_ROOT / str(submission_id) / "frames" / f"{file_id}_{int(at * 10)}.jpg"
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.jpg")
        done = await asyncio.to_thread(
            subprocess.run,
            [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{at:.1f}", "-i", str(src), "-frames:v", "1",
             "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "4", "-y", str(tmp)],
            capture_output=True, timeout=60, check=False,
        )
        if done.returncode != 0 or not tmp.is_file():
            raise HTTPException(status_code=404, detail="No frame at that time")
        tmp.replace(out)
    return out


async def analysed_image(submission_id: UUID, analysis_id: UUID, index: int) -> Tuple[Any, str]:
    """One of the images an analysis looked at (1 = the first), as (path, mime type), so a finding can show it."""
    from services.compliance.transcription_service import STORAGE_ROOT

    row = await _analysis_row(submission_id, analysis_id)
    images = (_json(row["input"]) or {}).get("images") or []
    if not 1 <= index <= len(images):
        raise HTTPException(status_code=404, detail="This analysis didn't include that image")
    # Earlier versions' files are kept (soft-deleted) for history, so don't filter on deleted_at.
    f = await fetch_one(
        "SELECT storage_path, mime_type FROM content_submission_files WHERE id = $1", UUID(images[index - 1]["file_id"])
    )
    path = (STORAGE_ROOT / f["storage_path"]).resolve() if f else None
    if not path or STORAGE_ROOT not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="The image is no longer available")
    return path, f["mime_type"]
