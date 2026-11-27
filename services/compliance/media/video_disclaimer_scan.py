"""Cached local OCR across the video when its sampled transcript did not find a footage disclosure."""
import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path

from postgresql_db.database import fetch_one, execute
from services.compliance.evaluation.video_footage_disclosure import VERSION, find_disclaimer

_semaphore = asyncio.Semaphore(1)
MAX_SECONDS = 180


def read_frames(path):
    from services.compliance.media.canva_mock_render import _ffmpeg
    from services.compliance.media.image_finding_service import _tesseract_binary

    ocr = _tesseract_binary()
    if not ocr:
        return {"status": "UNAVAILABLE", "reason": "Local video text reading is unavailable"}
    deadline = time.monotonic() + 90
    with tempfile.TemporaryDirectory(prefix="video-disclosure-") as folder:
        # One frame per second, over the entire short video, rather than twelve widely spaced samples.
        output = str(Path(folder) / "%04d.png")
        subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-t", str(MAX_SECONDS + 1),
                        "-vf", "fps=1,scale='min(1600,iw)':-2", "-frames:v", str(MAX_SECONDS + 1), output],
                       capture_output=True, timeout=40, check=True)
        frames = sorted(Path(folder).glob("*.png"))
        for index, frame in enumerate(frames):
            if time.monotonic() >= deadline:
                return {"status": "UNAVAILABLE", "reason": "Video disclaimer scan timed out"}
            read = subprocess.run([ocr, str(frame), "stdout", "--psm", "11"],
                                  capture_output=True, timeout=5, check=True)
            match = find_disclaimer(read.stdout.decode("utf-8", errors="replace"))
            if match:
                return {"status": "FOUND", "evidence": match, "at_sec": index}
        if not frames or len(frames) > MAX_SECONDS:
            return {"status": "UNAVAILABLE", "reason": "Could not verify the whole video within the scan limit"}
        return {"status": "NOT_FOUND", "frames_read": len(frames)}


async def scan(video):
    from services.compliance.media.transcription_service import STORAGE_ROOT

    async with _semaphore:
        row = await fetch_one("SELECT video_disclaimer_scan FROM content_submission_files WHERE id=$1", video["id"])
        cached = row["video_disclaimer_scan"] if row else None
        if isinstance(cached, str):
            cached = json.loads(cached)
        if cached and cached.get("version") == VERSION and cached.get("status") in {"FOUND", "NOT_FOUND"}:
            return cached
        try:
            path = (STORAGE_ROOT / video["storage_path"]).resolve()
            if STORAGE_ROOT not in path.parents or not path.is_file():
                raise ValueError("Video file is unavailable")
            result = await asyncio.to_thread(read_frames, path)
        except Exception:
            result = {"status": "UNAVAILABLE", "reason": "Could not finish reading the video disclaimer"}
        result["version"] = VERSION
        await execute("UPDATE content_submission_files SET video_disclaimer_scan=$2::jsonb WHERE id=$1", video["id"], json.dumps(result))
        return result
