"""Bounded, timestamped video samples for image-capable providers without video input."""
import re
import subprocess

from services.ai.core import ai_types as types
from services.compliance.media.canva_mock_render import _ffmpeg

MAX_FRAMES = 12


def sample_parts(path):
    """Decode actual frames; never substitute a transcript for visual input."""
    binary = _ffmpeg()
    probe = subprocess.run([binary, "-hide_banner", "-i", str(path)],
                           capture_output=True, timeout=15, check=False)
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr.decode(errors="replace"))
    if not match:
        raise ValueError("Could not determine video duration for visual samples")
    hours, minutes, seconds = map(float, match.groups())
    duration = hours * 3600 + minutes * 60 + seconds
    interval = max(duration / MAX_FRAMES, 0.1)
    filters = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval})',showinfo,scale='min(1280,iw)':-2"
    result = subprocess.run([binary, "-hide_banner", "-i", str(path), "-an", "-vf", filters,
                             "-fps_mode", "vfr", "-frames:v", str(MAX_FRAMES), "-f", "image2pipe",
                             "-vcodec", "mjpeg", "-q:v", "3", "pipe:1"],
                            capture_output=True, timeout=90, check=False)
    frames = re.findall(b"\xff\xd8.*?\xff\xd9", result.stdout, re.DOTALL)
    times = re.findall(r"\bpts_time:([0-9.]+)", result.stderr.decode(errors="replace"))
    if result.returncode or not frames or len(times) < len(frames):
        raise ValueError("Video frame sampling could not complete")
    parts = [types.Part.from_text(text="Actual timestamped video samples follow. Only these frames were inspected; "
                                 "the transcript supplements them. Do not claim whole-video or pixel-by-pixel coverage.")]
    for timestamp, frame in zip(times, frames):
        parts.extend([types.Part.from_text(text=f"Video frame at {timestamp} seconds"),
                      types.Part.from_bytes(data=frame, mime_type="image/jpeg")])
    return parts
