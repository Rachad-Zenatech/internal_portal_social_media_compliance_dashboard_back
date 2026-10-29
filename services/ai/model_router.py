"""Model routing: picks which Gemini models a request tries first, by feature and by how demanding the request is.

Three tiers, each an ordered list of models (override with AI_ROUTE_TIER_<TIER>=model-a,model-b):

    LIGHT     short, cheap requests (assistant chat, short prompts)      flash-lite models first
    STANDARD  ordinary analysis (captions, public mood, transcription)   flash models first
    HEAVY     long or high-stakes requests                               a stronger model first, when configured

Each feature has a base tier (AI_ROUTE_<FEATURE>=LIGHT|STANDARD|HEAVY overrides it), and the request can move up:
attachments or a long prompt lift LIGHT to STANDARD; a very long prompt or high stakes lift it to HEAVY. The rest
of the configured models (gemini_service.get_gemini_fallback_models) follow the tier's models, so a model that is
out of quota or retired still falls back as before. AI_ROUTING_ENABLED=false turns routing off (plain fallback
order for everything).

Sentiment analysis and image text reading keep their own chains (SENTIMENT_MODELS / SENTIMENT_MODEL_HIGH_STAKES and
image_text_service.MODELS): their results are cached by model, so changing their model changes their cache keys.
"""

import os
from dataclasses import dataclass
from typing import Dict, List

from services.ai.gemini_service import get_gemini_fallback_models

TIERS = ("LIGHT", "STANDARD", "HEAVY")
# HEAVY defaults to flash because Pro models have no free-tier allowance; on a paid key, put a Pro model first.
_TIER_DEFAULTS: Dict[str, str] = {
    "LIGHT": "gemini-3.1-flash-lite,gemini-2.5-flash-lite",
    "STANDARD": "gemini-2.5-flash,gemini-3-flash-preview",
    "HEAVY": "gemini-2.5-flash,gemini-3-flash-preview",
}
_FEATURE_TIERS: Dict[str, str] = {
    "CHAT": "LIGHT",
    "CAPTION": "STANDARD",
    "PUBLIC_MOOD": "STANDARD",
    "TRANSCRIPTION": "STANDARD",
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
    raw = os.getenv(f"AI_ROUTE_TIER_{tier}", _TIER_DEFAULTS[tier])
    return [m.strip().lower() for m in raw.split(",") if m.strip()]


def base_tier(feature: str) -> str:
    tier = os.getenv(f"AI_ROUTE_{feature}", _FEATURE_TIERS.get(feature, "STANDARD")).strip().upper()
    return tier if tier in TIERS else "STANDARD"


def route(feature: str, *, prompt_chars: int = 0, attachments: int = 0, high_stakes: bool = False) -> Route:
    """The models to try, in order, for this request, and why that tier was chosen."""
    available = get_gemini_fallback_models()
    if not enabled():
        return Route("FALLBACK", available, "routing disabled")
    tier = base_tier(feature)
    reasons = [f"{feature.lower()} defaults to {tier.lower()}"]
    if high_stakes or prompt_chars > VERY_LONG_PROMPT_CHARS:
        if tier != "HEAVY":
            tier = "HEAVY"
            reasons.append("high stakes" if high_stakes else f"prompt over {VERY_LONG_PROMPT_CHARS:,} characters")
    elif tier == "LIGHT" and (attachments or prompt_chars > LONG_PROMPT_CHARS):
        tier = "STANDARD"
        reasons.append("has attachments" if attachments else f"prompt over {LONG_PROMPT_CHARS:,} characters")
    preferred = [m for m in tier_models(tier) if m in available]
    return Route(tier, preferred + [m for m in available if m not in preferred], "; ".join(reasons))
