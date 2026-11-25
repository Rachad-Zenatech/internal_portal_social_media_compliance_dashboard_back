"""Whether the AI (and each AI feature) is working right now, for the overhead banner every signed-in user sees.

Read from the AI calls themselves: each one is traced with its feature, whether it worked and, when it didn't, what
kind of failure it was (ai_observability.classify_error). Also checks embedding pauses and Public Mood scraper state
so both whole-AI outages and single-feature outages/overflows show clearly overhead.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from postgresql_db.database import fetch_all

WINDOW = timedelta(minutes=30)
# The latest call failed and nothing has worked for this long: the AI (or feature) is down.
QUIET_AFTER_OK = timedelta(minutes=2)
CACHE_SECONDS = 15

_PRIORITY = ["KEY", "QUOTA", "MODEL", "BUSY", "OUTPUT_LIMIT", "RESPONSE_INVALID", "OTHER"]
_RESPONSE_FAILURES = {"OUTPUT_LIMIT", "RESPONSE_INVALID"}
_OVERFLOW_KINDS = {"QUOTA", "BUSY"}

_FEATURE_LABELS: Dict[str, str] = {
    "CHAT": "AI Assistant Chat",
    "COMPLIANCE_CHECK": "Compliance Rulebook Check",
    "VISUAL_COMPLIANCE": "Visual Compliance Review",
    "SOURCE_CHECK": "Source Link Fact-Check",
    "SOURCE_PDF_TEXT": "PDF Source Reading",
    "IMAGE_TEXT": "Image OCR & Face Detection",
    "IMAGE_SOURCE_OCR": "Image Source OCR",
    "IMAGE_FINDING_REGIONS": "Image Finding Locator",
    "TRANSCRIPTION": "Video Transcription",
    "SENTIMENT": "Sentiment Analysis",
    "PUBLIC_MOOD": "Public Mood Summary",
    "PUBLIC_MOOD_QUERY": "Public Mood Search Planner",
    "CAPTION": "Caption Suggestions",
    "SCRIPT_FIX": "Script Fix Suggestions",
    "BRAND_VOICE": "Brand Voice Extraction",
    "EMBEDDINGS": "Vector Search & Embeddings",
    "PUBLIC_MOOD_SCRAPE": "Public Mood Scraper",
}

_FEATURE_AFFECTS: Dict[str, List[str]] = {
    "EMBEDDINGS": [
        "Compliance Rulebook Check (RAG precedent)",
        "Sentiment Analysis (review memory)",
        "AI Assistant Chat (RAG & semantic cache)",
        "Messaging Context vector search",
    ],
    "PUBLIC_MOOD_SCRAPE": ["Public Mood (Financial ZenaTech)", "Public Mood (Today's Posts)", "Trend Discovery"],
    "PUBLIC_MOOD": ["Public Mood Summary", "Pre-Publish Mood Check"],
    "PUBLIC_MOOD_QUERY": ["Public Mood Search Planner"],
    "COMPLIANCE_CHECK": ["Post Compliance Rulebook Check", "Script Review Check", "Submission Approval Gate"],
    "VISUAL_COMPLIANCE": ["Image & Video Visual Compliance Review"],
    "SOURCE_CHECK": ["Source Link Fact-Check", "Image Source Verification"],
    "SOURCE_PDF_TEXT": ["PDF Source Reading", "Source Link Fact-Check"],
    "IMAGE_TEXT": ["Image OCR & Face Detection"],
    "IMAGE_SOURCE_OCR": ["Image Source Citation OCR"],
    "IMAGE_FINDING_REGIONS": ["Image Finding Locator"],
    "TRANSCRIPTION": ["Video Transcription", "Video On-Screen Text Scan"],
    "SENTIMENT": ["Reviewer Sentiment Analysis"],
    "CAPTION": ["Caption Suggestions"],
    "SCRIPT_FIX": ["Script Fix Suggestions"],
    "BRAND_VOICE": ["Brand Manual PDF Extraction"],
    "CHAT": ["AI Assistant Chat"],
}

_TEXT = {
    "OUTPUT_LIMIT": ("The AI assessment was cut short", "The response reached its output limit before finishing."),
    "RESPONSE_INVALID": ("The AI assessment could not be read", "The returned assessment did not pass validation."),
    "KEY": (
        "The AI key isn't working",
        "It may have expired or been revoked. Ask an admin to renew the AWS Bedrock key.",
    ),
    "QUOTA": (
        "The AI limit has been reached",
        "The Bedrock limit is used up for now. It works again when the limit resets, or when an admin raises it.",
    ),
    "MODEL": (
        "The AI models aren't available",
        "The configured Bedrock models have been retired or renamed. Ask an admin to update them.",
    ),
    "BUSY": (
        "The AI isn't responding",
        "The AI service is overloaded or timing out. It usually clears on its own.",
    ),
    "OTHER": (
        "The AI isn't working",
        "AI calls are failing. Ask an admin to check the logs.",
    ),
}
_EFFECT = (
    "Until it works: scores that need the AI are held back and posts can't be approved without one, and sentiment, "
    "captions and reading images, documents and video aren't updated."
)


class FeatureIssue(BaseModel):
    feature: str
    label: str
    status: Literal["OUT", "OVERFLOW"]
    reason: str
    message: str
    since: Optional[datetime] = None
    affected_features: List[str] = Field(default_factory=list)


class AiStatus(BaseModel):
    available: bool = True
    # KEY, QUOTA, MODEL, BUSY or OTHER while it isn't.
    reason: Optional[str] = None
    title: Optional[str] = None
    message: Optional[str] = None
    since: Optional[datetime] = None
    degraded_features: List[FeatureIssue] = Field(default_factory=list)


def _feature_label(feature: str) -> str:
    return _FEATURE_LABELS.get(feature, feature.replace("_", " ").title())


def summarize_features(rows: List[Dict[str, Any]], now: datetime) -> List[FeatureIssue]:
    """Identify individual features whose latest calls are failing or hitting quota/overflow."""
    by_feat: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        if r.get('status') == 'CANCELLED':
            continue
        feat = (r.get("feature") or "").strip().upper()
        if feat and feat != "GENERAL":
            by_feat.setdefault(feat, []).append(r)

    issues: List[FeatureIssue] = []
    for feat, frows in by_feat.items():
        if frows[0]["status"] in {"OK", "RETRYING"}:
            continue
        last_ok = next((r.get("finished_at", r["started_at"]) for r in frows if r["status"] == "OK"), None)
        failed = [r for r in frows if r["status"] not in {"OK", "RETRYING"} and (last_ok is None or r.get("finished_at", r["started_at"]) > last_ok)]
        if not failed:
            continue
        # Surface immediately on >= 2 consecutive failures or any overflow (QUOTA/BUSY), or after QUIET_AFTER_OK.
        kinds = {r.get("kind") or "OTHER" for r in failed}
        reason = next((k for k in _PRIORITY if k in kinds), "OTHER")
        is_overflow = reason in _OVERFLOW_KINDS
        if not is_overflow and len(failed) < 2 and last_ok is not None and (now - last_ok) < QUIET_AFTER_OK:
            continue
        title, _ = _TEXT[reason]
        issues.append(FeatureIssue(
            feature=feat,
            label=_feature_label(feat),
            status="OVERFLOW" if is_overflow else "OUT",
            reason=reason,
            message=title,
            since=min(r["started_at"] for r in failed),
            affected_features=_FEATURE_AFFECTS.get(feat, [_feature_label(feat)]),
        ))
    issues.sort(key=lambda i: (0 if i.status == "OUT" else 1, i.label))
    return issues


def summarize(rows: List[Dict[str, Any]], now: datetime, key_set: bool = True) -> AiStatus:
    """Summarize completed calls, ordering overlapping requests by completion rather than start."""
    rows = sorted(rows, key=lambda r: r.get("finished_at", r["started_at"]), reverse=True)
    if not key_set:
        return AiStatus(available=False, reason="KEY", title="The AI key isn't set",
                        message="No AWS Bedrock key is configured. " + _EFFECT)
    rows = [r for r in rows if r.get('status') != 'CANCELLED']
    features = summarize_features(rows, now)
    if not rows or rows[0]["status"] in {"OK", "RETRYING"}:
        return AiStatus(degraded_features=features)
    last_ok = next((r.get("finished_at", r["started_at"]) for r in rows if r["status"] == "OK"), None)
    if last_ok is not None and now - last_ok < QUIET_AFTER_OK:
        return AiStatus(degraded_features=features)
    failed = [r for r in rows if r["status"] not in {"OK", "RETRYING"} and (last_ok is None or r.get("finished_at", r["started_at"]) > last_ok)]
    provider_failures = [r for r in failed if r.get("kind") not in _RESPONSE_FAILURES]
    kinds = {r.get("kind") or "OTHER" for r in provider_failures}
    affected = {r.get("feature") for r in provider_failures if r.get("feature") not in (None, "", "GENERAL")}
    # A response-format failure is local to its assessment. Other feature/model failures
    # need corroboration across independent features before declaring a shared outage.
    if "KEY" not in kinds and len(affected) < 2:
        return AiStatus(degraded_features=features)
    reason = next((k for k in _PRIORITY if k in kinds), "OTHER")
    title, message = _TEXT[reason]
    return AiStatus(
        available=False, reason=reason, title=title, message=f"{message} {_EFFECT}",
        since=min(r["started_at"] for r in failed) if failed else None,
        degraded_features=features,
    )


def _runtime_issues(now: datetime) -> List[FeatureIssue]:
    issues: List[FeatureIssue] = []
    try:
        from services.ai.core import vector_store_service
        paused = vector_store_service.embedding_pause_status()
        if paused:
            reason_msg, left_sec = paused
            is_denied = "denied" in reason_msg.lower()
            issues.append(FeatureIssue(
                feature="EMBEDDINGS",
                label=_feature_label("EMBEDDINGS"),
                status="OUT" if is_denied else "OVERFLOW",
                reason="KEY" if is_denied else "QUOTA",
                message=f"{reason_msg} (~{max(1, left_sec // 60)}m paused; using keyword fallback)",
                since=now,
                affected_features=_FEATURE_AFFECTS["EMBEDDINGS"],
            ))
    except Exception:
        pass
    try:
        from tools.compliance.analytics import listening_router
        sc = listening_router._scrape
        if sc.get("state") == "failed":
            issues.append(FeatureIssue(
                feature="PUBLIC_MOOD_SCRAPE",
                label=_feature_label("PUBLIC_MOOD_SCRAPE"),
                status="OVERFLOW" if "timeout" in str(sc.get("error") or "").lower() else "OUT",
                reason="BUSY" if "timeout" in str(sc.get("error") or "").lower() else "OTHER",
                message=str(sc.get("error") or "Last scrape failed")[:140],
                affected_features=_FEATURE_AFFECTS["PUBLIC_MOOD_SCRAPE"],
            ))
        else:
            sources = (sc.get("collection") or {}).get("sources") or []
            bad = [s for s in sources if s.get("status") in {"RATE_LIMITED", "FAILED", "TIMEOUT"}]
            if bad:
                issues.append(FeatureIssue(
                    feature="PUBLIC_MOOD_SCRAPE",
                    label=_feature_label("PUBLIC_MOOD_SCRAPE"),
                    status="OVERFLOW",
                    reason="QUOTA",
                    message=f"{len(bad)} scrape source(s) hit rate limit/timeout",
                    affected_features=_FEATURE_AFFECTS["PUBLIC_MOOD_SCRAPE"],
                ))
    except Exception:
        pass
    return issues


_cache: Dict[str, Any] = {"at": 0.0, "status": AiStatus()}


async def status() -> AiStatus:
    if time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["status"]
    now = datetime.now(timezone.utc)
    rows = await fetch_all(
        """
        SELECT CASE WHEN status = 'ERROR' AND attributes->>'response_retry_pending' = 'true'
                              AND attributes->>'error_kind' IN ('OUTPUT_LIMIT', 'RESPONSE_INVALID')
                         THEN 'RETRYING' ELSE status END AS status,
               attributes->>'error_kind' AS kind, attributes->>'feature' AS feature, started_at,
               started_at + duration_ms * INTERVAL '1 millisecond' AS finished_at
        FROM ai_trace_spans
        WHERE status <> 'CANCELLED' AND started_at > $1
          AND ((kind = 'model' AND name IN ('bedrock.generate_content', 'gemini.generate_content'))
               OR (kind = 'tool' AND name = 'source_pdf.result'))
        ORDER BY finished_at DESC LIMIT 300
        """,
        now - WINDOW,
    )
    from services.ai.core.bedrock_service import has_ai_key
    result = summarize([dict(r) for r in rows], now, has_ai_key())
    extra = _runtime_issues(now)
    if extra:
        seen = {i.feature for i in result.degraded_features}
        result.degraded_features.extend(i for i in extra if i.feature not in seen)
    _cache.update(at=time.monotonic(), status=result)
    return result
