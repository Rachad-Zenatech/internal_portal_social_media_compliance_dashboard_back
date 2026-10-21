"""Video Upload & Compliance Background Job Processing Service
Tracks asynchronous video pipeline stages:
1. UPLOADING (Checksum & Codec Validation)
2. AUDIO_EXTRACTION (Demuxing Audio Track)
3. SPEECH_TRANSCRIBING (Gemini Audio Speech-to-Text)
4. DEFENSE_VISUAL_OCR (Watermark, Insignia & Frame Screening)
5. COMPLIANCE_SCREENING (Rulebook Cross-Referencing)
6. COMPLETED / FLAGGED
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# In-memory registry of video background jobs
_VIDEO_JOBS: Dict[str, Dict[str, Any]] = {}

PIPELINE_STAGES = [
    {
        "id": "UPLOADING",
        "label": "File Codec & Checksum Verification",
        "description": "Validating container format, video bitrates, and chunk checksums.",
        "target_progress": 20,
    },
    {
        "id": "AUDIO_EXTRACTION",
        "label": "Audio Stream Extraction & Normalization",
        "description": "Isolating vocal frequency bands and normalising audio to 16kHz mono.",
        "target_progress": 40,
    },
    {
        "id": "SPEECH_TRANSCRIBING",
        "label": "Gemini Speech-to-Text Transcription",
        "description": "Transcribing voiceover narrative with word-level second timestamps.",
        "target_progress": 65,
    },
    {
        "id": "DEFENSE_VISUAL_OCR",
        "label": "Defense Visual & Insignia OCR Analysis",
        "description": "Inspecting video keyframes for DoD logos, classified markings, and flight test data.",
        "target_progress": 85,
    },
    {
        "id": "COMPLIANCE_SCREENING",
        "label": "Defense Thought Leadership Rulebook Check",
        "description": "Evaluating claims and terminology against 25+ active defense compliance rules.",
        "target_progress": 100,
    },
]


def create_video_job(
    filename: str,
    video_title: str,
    platform: str = "TIKTOK",
    aspect_ratio: str = "9:16",
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Initialize a new video processing background job."""
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    job_data = {
        "job_id": job_id,
        "filename": filename,
        "video_title": video_title,
        "platform": platform,
        "aspect_ratio": aspect_ratio,
        "status": "PROCESSING",
        "progress_percent": 5,
        "current_stage_index": 0,
        "current_stage": PIPELINE_STAGES[0]["id"],
        "current_stage_label": PIPELINE_STAGES[0]["label"],
        "stages": [
            {
                "id": s["id"],
                "label": s["label"],
                "status": "IN_PROGRESS" if idx == 0 else "PENDING",
            }
            for idx, s in enumerate(PIPELINE_STAGES)
        ],
        "logs": [
            f"[{time.strftime('%H:%M:%S')}] Job created: {filename} ({video_title})",
            f"[{time.strftime('%H:%M:%S')}] Initializing pipeline for {platform}...",
        ],
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "result": None,
        "user_id": str(user_id) if user_id else None,
    }

    _VIDEO_JOBS[job_id] = job_data
    return job_data


def get_video_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve current status and progress of a background video job."""
    return _VIDEO_JOBS.get(job_id)


def list_video_jobs(limit: int = 10) -> List[Dict[str, Any]]:
    """List recent video processing jobs."""
    jobs = list(_VIDEO_JOBS.values())
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return jobs[:limit]


async def run_video_processing_pipeline(job_id: str):
    """Background task simulating end-to-end video processing through all stages."""
    job = _VIDEO_JOBS.get(job_id)
    if not job:
        return

    logger.info(f"Starting background video pipeline for job {job_id}")

    # Stage 1: Uploading & Checksum
    await asyncio.sleep(1.2)
    job["progress_percent"] = 20
    job["current_stage_index"] = 0
    job["current_stage"] = "UPLOADING"
    job["current_stage_label"] = PIPELINE_STAGES[0]["label"]
    job["stages"][0]["status"] = "COMPLETED"
    job["stages"][1]["status"] = "IN_PROGRESS"
    job["logs"].append(f"[{time.strftime('%H:%M:%S')}] Video integrity check passed: H.264 video track validated.")
    job["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Stage 2: Audio Extraction
    await asyncio.sleep(1.4)
    job["progress_percent"] = 40
    job["current_stage_index"] = 1
    job["current_stage"] = "AUDIO_EXTRACTION"
    job["current_stage_label"] = PIPELINE_STAGES[1]["label"]
    job["stages"][1]["status"] = "COMPLETED"
    job["stages"][2]["status"] = "IN_PROGRESS"
    job["logs"].append(f"[{time.strftime('%H:%M:%S')}] Audio track extracted: 16,000 Hz vocal track prepared.")
    job["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Stage 3: Speech-to-Text with Gemini
    await asyncio.sleep(1.8)
    job["progress_percent"] = 65
    job["current_stage_index"] = 2
    job["current_stage"] = "SPEECH_TRANSCRIBING"
    job["current_stage_label"] = PIPELINE_STAGES[2]["label"]
    job["stages"][2]["status"] = "COMPLETED"
    job["stages"][3]["status"] = "IN_PROGRESS"
    job["logs"].append(f"[{time.strftime('%H:%M:%S')}] Gemini 2.5 Flash Speech transcribed 5 timestamped segments.")
    job["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Stage 4: Visual & OCR Frame Inspection
    await asyncio.sleep(1.5)
    job["progress_percent"] = 85
    job["current_stage_index"] = 3
    job["current_stage"] = "DEFENSE_VISUAL_OCR"
    job["current_stage_label"] = PIPELINE_STAGES[3]["label"]
    job["stages"][3]["status"] = "COMPLETED"
    job["stages"][4]["status"] = "IN_PROGRESS"
    job["logs"].append(f"[{time.strftime('%H:%M:%S')}] Keyframe OCR completed: No classified badges detected.")
    job["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Stage 5: Rulebook Compliance Screening
    await asyncio.sleep(1.3)
    job["progress_percent"] = 100
    job["current_stage_index"] = 4
    job["current_stage"] = "COMPLIANCE_SCREENING"
    job["current_stage_label"] = PIPELINE_STAGES[4]["label"]
    job["stages"][4]["status"] = "COMPLETED"

    # Compile final result
    job["status"] = "COMPLETED"
    job["completed_at"] = datetime.now(timezone.utc).isoformat()
    job["logs"].append(f"[{time.strftime('%H:%M:%S')}] Compliance scan complete: 2 Rule violations flagged. Risk Tier: RED.")
    job["logs"].append(f"[{time.strftime('%H:%M:%S')}] Final clearance report generated.")
    job["updated_at"] = datetime.now(timezone.utc).isoformat()

    logger.info(f"Completed background video pipeline for job {job_id}")
