"""Services / Compliance / Sentiment Package

This package breaks down the 1,600+ line sentiment service into focused, maintainable modules:
- config: Configuration constants, guidelines, prompts and schemas.
- input_builder: Input building, context loading, and prompt assembly.
- evidence: Verification of quoted evidence and rule number matching.
- engine: Gemini client execution, video/image attachment, and fallback model chains.
- jobs: Job queueing, claiming, state transitions, scheduling, and retrieval.
- feedback: Reviewer ratings, feedback summaries, and evaluation test cases.
- media: Extracted video frames and image serving for findings.
"""

from services.compliance.sentiment.config import (
    MODEL,
    HIGH_STAKES_MODEL,
    MODEL_CHAIN,
    PROMPT_VERSION,
    SCHEDULE_CHECK_SEC,
    STALE_MINUTES,
    MAX_ATTEMPTS,
    API_RETRIES,
    GENERATE_TIMEOUT_SEC,
    FRAME_WIDTH,
    MAX_IMAGES,
    QuotaExhausted,
    ModelUnavailable,
)
from services.compliance.sentiment.input_builder import (
    _build_input,
    _load_submission,
    _load_images,
    _model_for,
    _transcript_pending,
    _prompt,
    _hash,
)
from services.compliance.sentiment.evidence import (
    finding_id,
    _verify,
)
from services.compliance.sentiment.engine import (
    analyse,
    next_quota_reset,
)
from services.compliance.sentiment.jobs import (
    request_analysis,
    run_analysis,
    schedule,
    get_analysis,
    get_for_version,
    get_latest,
    schedule_after_reset,
    promote_due,
    resume_unfinished,
    on_transcript_finished,
    scheduled_runs_loop,
)
from services.compliance.sentiment.feedback import (
    set_feedback,
    clear_feedback,
    feedback_summary,
    create_eval_case,
    list_eval_cases,
    delete_eval_case,
)
from services.compliance.sentiment.media import (
    video_frame,
    analysed_image,
)

__all__ = [
    "MODEL",
    "HIGH_STAKES_MODEL",
    "MODEL_CHAIN",
    "PROMPT_VERSION",
    "SCHEDULE_CHECK_SEC",
    "STALE_MINUTES",
    "MAX_ATTEMPTS",
    "API_RETRIES",
    "GENERATE_TIMEOUT_SEC",
    "FRAME_WIDTH",
    "MAX_IMAGES",
    "QuotaExhausted",
    "ModelUnavailable",
    "_build_input",
    "_load_submission",
    "_load_images",
    "_model_for",
    "_transcript_pending",
    "_prompt",
    "_hash",
    "finding_id",
    "_verify",
    "analyse",
    "next_quota_reset",
    "request_analysis",
    "run_analysis",
    "schedule",
    "get_analysis",
    "get_for_version",
    "get_latest",
    "schedule_after_reset",
    "promote_due",
    "resume_unfinished",
    "on_transcript_finished",
    "scheduled_runs_loop",
    "set_feedback",
    "clear_feedback",
    "feedback_summary",
    "create_eval_case",
    "list_eval_cases",
    "delete_eval_case",
    "video_frame",
    "analysed_image",
]

