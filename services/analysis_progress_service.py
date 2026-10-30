"""How long a post's rulebook check has left.

Before the check can score a post, its video is transcribed, the text in its images is read and its script (or a
blog's article file) is read, each in the background (transcription_service, image_text_service,
document_text_service). While that runs, screens show "Under review"; this says what's still being read and when the
score should be ready, so they can show a countdown instead.

The estimate comes from how long recent reads of the same kind took (each job records when it started; its
*_updated_at moves when it finishes). Videos scale with file size, since uploading and processing take longer for
bigger files. Until there are a few finished jobs to go on, sensible defaults are used.

The steps run side by side, so the score is expected when the slowest one finishes, plus a moment for the re-check.
"""

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from models.submission_model import AnalysisProgress, AnalysisStep
from postgresql_db.database import fetch_all
from services import document_text_service, image_text_service

# Finished jobs needed before their timings replace the defaults.
MIN_SAMPLES = 3
# How many recent finished jobs, from how far back, the estimate learns from.
SAMPLE_SIZE = 50
SAMPLE_DAYS = 30
# Timings are re-read at most this often.
TIMINGS_TTL_SEC = 600
# After the last read finishes, the post is re-checked and saved.
RECHECK_SECONDS = 3

# Defaults (seconds) until there's history: a video per MB (with a floor), an image, a document.
DEFAULT_VIDEO_PER_MB = 6.0
DEFAULT_VIDEO_MIN = 30.0
DEFAULT_IMAGE = 12.0
DEFAULT_DOCUMENT = 15.0
# Bounds on any single video estimate.
VIDEO_MIN = 15.0
VIDEO_MAX = 15 * 60.0


@dataclass(frozen=True)
class Timings:
    video_per_mb: float
    video_min: float
    image: float
    document: float
    from_history: bool


_timings: Optional[Tuple[float, Timings]] = None


async def timings() -> Timings:
    """Median durations of recent finished reads, by kind (cached for a few minutes)."""
    global _timings
    now = time.monotonic()
    if _timings and now - _timings[0] < TIMINGS_TTL_SEC:
        return _timings[1]
    rows = await fetch_all(
        f"""
        WITH recent AS (
            (SELECT 'VIDEO' AS kind, extract(epoch FROM transcript_updated_at - transcript_started_at) AS secs,
                    size_bytes / 1048576.0 AS mb
             FROM content_submission_files
             WHERE transcript_status = 'COMPLETED' AND transcript_started_at IS NOT NULL
               AND transcript_updated_at > NOW() - INTERVAL '{SAMPLE_DAYS} days'
             ORDER BY transcript_updated_at DESC LIMIT {SAMPLE_SIZE})
            UNION ALL
            (SELECT 'IMAGE', extract(epoch FROM image_text_updated_at - image_text_started_at), NULL
             FROM content_submission_files
             WHERE image_text_status = 'COMPLETED' AND image_text_started_at IS NOT NULL
               AND image_text_updated_at > NOW() - INTERVAL '{SAMPLE_DAYS} days'
             ORDER BY image_text_updated_at DESC LIMIT {SAMPLE_SIZE})
            UNION ALL
            (SELECT 'DOCUMENT', extract(epoch FROM doc_text_updated_at - doc_text_started_at), NULL
             FROM content_submission_files
             WHERE doc_text_status = 'COMPLETED' AND doc_text_started_at IS NOT NULL
               AND doc_text_updated_at > NOW() - INTERVAL '{SAMPLE_DAYS} days'
             ORDER BY doc_text_updated_at DESC LIMIT {SAMPLE_SIZE})
        )
        SELECT kind, count(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY secs) AS median_secs,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY secs / greatest(mb, 0.5)) AS median_per_mb,
               min(secs) AS min_secs
        FROM recent WHERE secs > 0 GROUP BY kind
        """
    )
    by_kind = {r["kind"]: r for r in rows}

    def learned(kind: str, field: str) -> Optional[float]:
        r = by_kind.get(kind)
        return float(r[field]) if r and r["n"] >= MIN_SAMPLES and r[field] is not None else None

    video_per_mb = learned("VIDEO", "median_per_mb")
    t = Timings(
        video_per_mb=video_per_mb or DEFAULT_VIDEO_PER_MB,
        video_min=max(VIDEO_MIN, learned("VIDEO", "min_secs") or DEFAULT_VIDEO_MIN),
        image=learned("IMAGE", "median_secs") or DEFAULT_IMAGE,
        document=learned("DOCUMENT", "median_secs") or DEFAULT_DOCUMENT,
        from_history=any(learned(k, "median_secs") for k in ("VIDEO", "IMAGE", "DOCUMENT")),
    )
    _timings = (now, t)
    return t


def video_seconds(size_bytes: Optional[int], t: Timings) -> float:
    mb = (size_bytes or 0) / 1048576.0
    return min(VIDEO_MAX, max(t.video_min, t.video_per_mb * mb))


def _batched(n: int, each: float, concurrent: int) -> float:
    """n reads of `each` seconds, `concurrent` at a time."""
    return math.ceil(n / max(1, concurrent)) * each


def _started(values: Iterable[Optional[datetime]]) -> Optional[datetime]:
    started = [v for v in values if v]
    return min(started) if started else None


def _step(kind: str, label: str, started_at: Optional[datetime], seconds: float, now: datetime) -> AnalysisStep:
    start = started_at or now
    return AnalysisStep(
        kind=kind, label=label, state="RUNNING" if started_at else "WAITING", started_at=started_at,
        expected_seconds=round(seconds), expected_done_at=start + timedelta(seconds=seconds),
    )


def build(files: List[Dict[str, Any]], t: Timings, now: datetime) -> AnalysisProgress:
    """The progress of one post from its files' reading states (the same parts _run_check waits on)."""
    steps: List[AnalysisStep] = []

    video = next(
        (f for f in sorted(files, key=lambda f: f["created_at"], reverse=True)
         if f["kind"] == "MAIN" and (f["mime_type"] or "").startswith("video/")),
        None,
    )
    if video and video["transcript_status"] not in ("COMPLETED", "FAILED"):
        running = video["transcript_status"] == "PROCESSING"
        steps.append(_step("VIDEO", "Reading the video", video["transcript_started_at"] if running else None,
                           video_seconds(video["size_bytes"], t), now))

    images = [f for f in files if image_text_service.is_image_file(f["kind"], f["mime_type"] or "")
              and f["image_text_status"] in (None, "PROCESSING")]
    if images:
        running = [f["image_text_started_at"] for f in images if f["image_text_status"] == "PROCESSING"]
        n = len(images)
        steps.append(_step(
            "IMAGES", f"Reading the text in {'the image' if n == 1 else f'{n} images'}", _started(running),
            _batched(n, t.image, image_text_service.MAX_CONCURRENT), now,
        ))

    docs = [f for f in files if f["kind"] in ("SCRIPT", "MAIN")
            and document_text_service.is_document_file(f["kind"], f["original_name"] or "")
            and f["doc_text_status"] in (None, "PROCESSING")]
    if docs:
        running = [f["doc_text_started_at"] for f in docs if f["doc_text_status"] == "PROCESSING"]
        label = "Reading the script" if any(f["kind"] == "SCRIPT" for f in docs) else "Reading the article"
        steps.append(_step("DOCUMENTS", label, _started(running),
                           _batched(len(docs), t.document, document_text_service.MAX_CONCURRENT), now))

    started_at = _started(s.started_at for s in steps) or now
    # Reading done but the re-check hasn't saved yet: just a moment more.
    done = max((s.expected_done_at for s in steps), default=now) + timedelta(seconds=RECHECK_SECONDS)
    return AnalysisProgress(steps=steps, started_at=started_at, expected_done_at=done, server_time=now,
                            from_history=t.from_history)


async def for_submissions(submission_ids: List[UUID]) -> Dict[UUID, AnalysisProgress]:
    """Progress for each of these posts (only call with posts whose check is still pending). One query for all."""
    if not submission_ids:
        return {}
    rows = await fetch_all(
        """
        SELECT submission_id, kind, mime_type, original_name, size_bytes, created_at,
               transcript_status, transcript_started_at, image_text_status, image_text_started_at,
               doc_text_status, doc_text_started_at
        FROM content_submission_files
        WHERE submission_id = ANY($1::uuid[]) AND deleted_at IS NULL
        """,
        list(submission_ids),
    )
    files: Dict[UUID, List[Dict[str, Any]]] = {sid: [] for sid in submission_ids}
    for r in rows:
        files.setdefault(r["submission_id"], []).append(dict(r))
    t = await timings()
    now = datetime.now(timezone.utc)
    return {sid: build(fs, t, now) for sid, fs in files.items()}
