"""Locate exact flagged wording in video frames without inventing a visual region."""
import asyncio
import logging
import os
import shutil
import subprocess
import sys

from services.compliance.evaluation.compliance_service import _hit, _normalize
from services.compliance.evaluation.finding_evidence import highlight_terms
from services.compliance.media import image_finding_service
from services.compliance.media.image_finding_service import _ocr_regions, _ocr_word_boxes
from services.compliance.media.transcription_service import STORAGE_ROOT

logger = logging.getLogger(__name__)
FRAME_TIMEOUT_SECONDS = 15
VISUAL_SCAN_SECONDS = (0.5, 1.5, 2.5, 3.5)


def _ffmpeg_binary():
    """Use the configured Windows executable from WSL when development mounts the drive."""
    configured = os.getenv("FFMPEG_BINARY")
    if configured and sys.platform != "win32" and len(configured) > 3 and configured[1:3] == ":/":
        configured = f"/mnt/{configured[0].lower()}/{configured[3:]}"
    return configured if configured and os.path.isfile(configured) else shutil.which("ffmpeg")


def _ffmpeg_input_path(path, ffmpeg):
    """Pass a Windows path to a Windows FFmpeg executable launched from WSL."""
    source = str(path)
    if sys.platform != "win32" and ffmpeg.lower().endswith(".exe") and source.startswith("/mnt/"):
        return f"{source[5].upper()}:{source[6:]}"
    return source


def _frame_bytes(path, at_seconds):
    """Read one PNG video frame to memory without creating temporary media files."""
    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        logger.warning("Video finding localization skipped: ffmpeg is unavailable")
        return b""
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{at_seconds:.3f}", "-i", _ffmpeg_input_path(path, ffmpeg),
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
            capture_output=True, timeout=FRAME_TIMEOUT_SECONDS, check=False,
        )
        return result.stdout if result.returncode == 0 else b""
    except (OSError, subprocess.SubprocessError):
        logger.warning("Video finding localization could not extract a frame", exc_info=True)
        return b""


def _seconds(value):
    try:
        seconds = float(value)
        return seconds if seconds >= 0 else None
    except (TypeError, ValueError):
        return None


async def locate_video_findings(video, segments, findings):
    """Attach tight OCR boxes to matching transcript moments in a video."""
    if not video or not segments or not findings:
        return
    path = (STORAGE_ROOT / video["storage_path"]).resolve()
    if STORAGE_ROOT not in path.parents or not path.is_file():
        logger.warning("Video finding localization skipped: video is unavailable in storage")
        return
    for segment in segments:
        start, end = _seconds(segment.get("start_sec")), _seconds(segment.get("end_sec"))
        if start is None or end is None or end < start:
            continue
        padded = f" {_normalize(str(segment.get('text', '')))} "
        candidates = [finding for finding in findings if finding.status != "CLEARED" and any(
            _hit(term, padded, finding.exception_terms) for term in highlight_terms(finding)
        )]
        if not candidates:
            continue
        frame = await asyncio.to_thread(_frame_bytes, path, (start + end) / 2)
        if not frame:
            continue
        words = segment.get("ocr_words") or await asyncio.to_thread(_ocr_word_boxes, frame)
        if not words:
            logger.info("Video finding localization: no OCR words at %.2fs", start)
            continue
        for finding in candidates:
            regions = _ocr_regions(video["id"], words, highlight_terms(finding), start, end)
            for region in regions:
                if region not in finding.image_regions:
                    finding.image_regions.append(region)


async def visual_text_segments(video):
    """Read on-screen words from short videos when their audio transcript is unavailable."""
    if not video:
        return []
    path = (STORAGE_ROOT / video["storage_path"]).resolve()
    if STORAGE_ROOT not in path.parents or not path.is_file():
        return []
    segments = []
    for start in VISUAL_SCAN_SECONDS:
        frame = await asyncio.to_thread(_frame_bytes, path, start)
        words = await asyncio.to_thread(_ocr_word_boxes, frame) if frame else []
        text = " ".join(word["text"] for word in words).strip()
        if text:
            segments.append({"start_sec": start, "end_sec": start + 0.5,
                             "text": text, "ocr_words": words, "source": "ON_SCREEN_TEXT"})
    return segments


async def locate_media_findings(images, video, segments, findings, non_image_text=""):
    """Keep image and video localization beside the media-specific implementations."""
    source_regions = await image_finding_service.locate_image_findings(images, findings, non_image_text=non_image_text)
    await locate_video_findings(video, segments, findings)
    return source_regions
