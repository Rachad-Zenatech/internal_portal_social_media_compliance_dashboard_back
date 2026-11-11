"""The measurements a public-mood assessment is described by, built by one function used in two places so they can't
drift apart: when a draft is assessed live (saved inside the assessment at that moment), and when a training dataset is
made (read back from the saved assessment, never rebuilt later from data that has since changed).

    features = build_features(draft_context, verified_evidence, state)

Unknown is unknown: a measurement that can't be made is None (the model sees it as missing), never 0. "No activity
history" must not read as "no growth", nor "no collection information" as "collection was complete".

Reviewer decisions and what happened after publication are NOT features: they are the label and the outcome, and
putting them in would let a model learn the answer instead of the evidence (FORBIDDEN_FEATURES is checked by a test).
"""

import math
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from services.compliance.analytics.listening_providers import clean_headline, volume_change

# Bump when a feature is added, removed or its meaning changes: datasets and models are tied to one version.
FEATURE_SCHEMA_VERSION = "v1"

FEATURE_NAMES: List[str] = [
    "relevant_item_count",    # verified relevant items, copies of a story included
    "distinct_story_count",   # underlying stories (the same story from several outlets counts once)
    "negative_share",         # negative proportion among those stories (None when there are none)
    "severe_event_count",     # stories with serious negative intensity or several crisis words
    "unique_author_count",    # distinct authors/outlets among the relevant items: breadth of the discussion
    "snapshot_age_minutes",   # how old the evidence collection was when the draft was assessed
    "collection_partial",     # 1 if the last collection was incomplete, 0 if complete, None if unknown
    "activity_change",        # % change in relevant stories per day, last 3 complete days vs the 3 before; else None
]

# Never inputs to a model of the recommendation.
FORBIDDEN_FEATURES = {
    "reviewer_label", "reviewer_decision", "accepted_label", "review_status", "published", "was_published",
    "post_outcome", "engagement_after_publish", "performance", "approved", "system_recommendation", "shadow_recommendation",
}

SEVERE_COMPOUND = -0.60   # the same line the publishing rules use for "serious negative intensity"
SEVERE_CRISIS_WORDS = 2
TREND_DAYS = 7


def _f(x: Any) -> Optional[float]:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _when(item: Dict[str, Any]) -> Optional[datetime]:
    for key in ("published_at", "first_seen_at"):
        v = item.get(key)
        if isinstance(v, str):
            try:
                v = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                v = None
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return None


def _is_severe(item: Dict[str, Any]) -> bool:
    compound = _f(item.get("compound"))
    return (compound is not None and compound <= SEVERE_COMPOUND) or len(item.get("crisis_flags") or []) >= SEVERE_CRISIS_WORDS


def build_features(draft_context: Dict[str, Any], verified_evidence: List[Dict[str, Any]],
                   state: Optional[Dict[str, Any]] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The measurements for one assessment.

    draft_context       the draft (brand, audience, platforms, text). Version v1 doesn't use it: its measurements are
                        about the evidence. It is part of the signature so a later version can, without changing callers.
    verified_evidence   the items verified as relevant to the draft, best match first. Each: story_key, sentiment,
                        compound, crisis_flags, author, published_at / first_seen_at.
    state               how collection stood when the draft was assessed: collected_at (datetime) and
                        collection_status ('SUCCESS', 'PARTIAL', ...), either of which may be missing.
    """
    now = now or datetime.now(timezone.utc)
    state = state or {}
    stories: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for item in verified_evidence:
        key = item.get("story_key") or clean_headline(item.get("text", ""))
        stories.setdefault(key, item)  # the best-scoring copy represents the story
    distinct = len(stories)

    negative_share = (sum(1 for s in stories.values() if s.get("sentiment") == "NEGATIVE") / distinct) if distinct else None

    authors = {str(i.get("author")).strip().lower() for i in verified_evidence if str(i.get("author") or "").strip()}

    collected_at = state.get("collected_at")
    age = ((now - collected_at).total_seconds() / 60.0) if isinstance(collected_at, datetime) else None
    status = state.get("collection_status")

    # Stories per day over the last week, from when each was published (or first seen, when it has no date).
    today = now.astimezone(timezone.utc).date()
    daily = [0] * TREND_DAYS
    for item in stories.values():
        at = _when(item)
        if at is not None:
            offset = (today - at.astimezone(timezone.utc).date()).days
            if 0 <= offset < TREND_DAYS:
                daily[TREND_DAYS - 1 - offset] += 1

    return {
        "relevant_item_count": len(verified_evidence),
        "distinct_story_count": distinct,
        "negative_share": None if negative_share is None else round(negative_share, 4),
        "severe_event_count": sum(1 for s in stories.values() if _is_severe(s)),
        "unique_author_count": len(authors),
        "snapshot_age_minutes": None if age is None else round(max(age, 0.0), 1),
        "collection_partial": None if status is None else (0 if status == "SUCCESS" else 1),
        "activity_change": volume_change(daily) if any(daily) else None,
    }


def to_row(features: Dict[str, Any]) -> List[float]:
    """The features as a model input row in FEATURE_NAMES order; unknown (None) becomes NaN, which XGBoost treats as
    missing rather than as a value."""
    return [math.nan if features.get(name) is None else float(features[name]) for name in FEATURE_NAMES]


def schema() -> Dict[str, Any]:
    return {"version": FEATURE_SCHEMA_VERSION, "features": list(FEATURE_NAMES)}
