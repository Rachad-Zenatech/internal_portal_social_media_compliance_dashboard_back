"""Model routing: picks the cheapest suitable model for easy work, and stronger models when needed.

Three tiers, each an ordered list of models (override with AI_ROUTE_TIER_<TIER>=model-a,model-b):

    LIGHT     easy/fast tasks (chat, OCR, captions, short evals)      cheapest Haiku models first
    STANDARD  ordinary analysis (compliance, source check, mood)     cheap Haiku first, stronger fallback
    HEAVY     long (>60k chars) or high-stakes requests               stronger Sonnet first, Haiku fallback

Each feature has a base tier (AI_ROUTE_<FEATURE>=LIGHT|STANDARD|HEAVY overrides it), and the request can move up:
attachments or a long prompt lift LIGHT to STANDARD; a very long prompt or high stakes lift it to HEAVY.
When AWS_PROFILE_ID is configured, cheap profiles stay first for LIGHT/STANDARD while HEAVY tries the stronger tier
first and falls back to AWS_PROFILE_ID so accounts limited to one inference profile never break.
"""

import os
from dataclasses import dataclass
from typing import Dict, List

from services.ai.core.gemini_service import without_retired

TIERS = ("LIGHT", "STANDARD", "HEAVY")
_TIER_DEFAULTS: Dict[str, str] = {
    "LIGHT": "us.anthropic.claude-haiku-5-5,us.anthropic.claude-3-5-haiku-20241022-v1:0",
    "STANDARD": "us.anthropic.claude-haiku-5-5,us.anthropic.claude-3-5-haiku-20241022-v1:0,us.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "HEAVY": "us.anthropic.claude-3-5-sonnet-20241022-v2:0,us.anthropic.claude-haiku-5-5",
}
_FEATURE_TIERS: Dict[str, str] = {
    "CHAT": "LIGHT",
    "CAPTION": "STANDARD",
    "IMAGE_TEXT": "LIGHT",
    "IMAGE_SOURCE_OCR": "LIGHT",
    "IMAGE_FINDING_REGIONS": "LIGHT",
    "PUBLIC_MOOD_QUERY": "LIGHT",
    "SCRIPT_FIX": "LIGHT",
    "TRANSCRIPTION": "LIGHT",
    "CONTEXT_EVAL": "LIGHT",
    "COMPLIANCE_CHECK": "STANDARD",
    "PUBLIC_MOOD": "STANDARD",
    "SOURCE_CHECK": "STANDARD",
    "VISUAL_COMPLIANCE": "STANDARD",
    "BRAND_VOICE": "STANDARD",
    "SENTIMENT": "STANDARD",
}
LONG_PROMPT_CHARS = 8_000
VERY_LONG_PROMPT_CHARS = 60_000


@dataclass
class Route:
    tier: str
    models: List[str]
    reason: str


def enabled() -> bool:
    return os.getenv("AI_ROUTING_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def tier_models(tier: str) -> List[str]:
    if tier == "HEAVY":
        heavy_override = (os.getenv("BEDROCK_HEAVY_PROFILE_ID") or "").strip()
        raw = os.getenv("AI_ROUTE_TIER_HEAVY", _TIER_DEFAULTS["HEAVY"])
        models = without_retired([m.strip().lower() for m in raw.split(",") if m.strip()])
        if heavy_override:
            return [heavy_override] + [m for m in models if m != heavy_override]
        return models
    raw = os.getenv(f"AI_ROUTE_TIER_{tier}", _TIER_DEFAULTS[tier])
    return without_retired([m.strip().lower() for m in raw.split(",") if m.strip()])


def base_tier(feature: str) -> str:
    tier = os.getenv(f"AI_ROUTE_{feature}", _FEATURE_TIERS.get(feature, "STANDARD")).strip().upper()
    return tier if tier in TIERS else "STANDARD"


def route(feature: str, *, prompt_chars: int = 0, attachments: int = 0, high_stakes: bool = False) -> Route:
    """The models to try, in order, for this request, and why that tier was chosen."""
    from services.ai.core.bedrock_service import get_bedrock_profile_id, is_expensive_model

    profile = get_bedrock_profile_id()
    tier = base_tier(feature)
    reasons = [f"{feature.lower()} defaults to {tier.lower()}"]
    if enabled():
        if high_stakes or prompt_chars > VERY_LONG_PROMPT_CHARS:
            if tier != "HEAVY":
                tier = "HEAVY"
                reasons.append("high stakes" if high_stakes else f"prompt over {VERY_LONG_PROMPT_CHARS:,} characters")
        elif tier == "LIGHT" and (attachments or prompt_chars > LONG_PROMPT_CHARS):
            tier = "STANDARD"
            reasons.append("has attachments" if attachments else f"prompt over {LONG_PROMPT_CHARS:,} characters")
    else:
        reasons = ["routing disabled (using standard order)"]

    preferred = tier_models(tier)
    if profile:
        if tier == "HEAVY":
            # Stronger tier model goes first when needed; configured profile remains the safe fallback.
            preferred = preferred + ([profile] if profile not in preferred else [])
        elif not is_expensive_model(profile):
            preferred = [profile] + [m for m in preferred if m != profile]
        else:
            preferred = preferred + ([profile] if profile not in preferred else [])
    return Route(tier, list(dict.fromkeys(preferred)), "; ".join(reasons))
