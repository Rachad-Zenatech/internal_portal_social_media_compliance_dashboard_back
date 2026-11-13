"""Prompt and structured response schema for the Gemini public-mood summary."""

import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Tuple

from models.compliance.public_mood_model import MatchedTopic, PublicMood


def _enum(*values: str) -> Dict[str, Any]:
    return {"type": "STRING", "enum": list(values)}


RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "overall": _enum("FAVOURABLE", "MIXED", "UNFAVOURABLE", "NO_DATA"),
        "recommendation": _enum("POST", "WAIT", "HOLD"),
        "recommendation_reason": {"type": "STRING"},
        "headline": {"type": "STRING"},
        "topics": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "topic": {"type": "STRING"},
                    "reading": {"type": "STRING"},
                    "risk": _enum("LOW", "MEDIUM", "HIGH"),
                },
                "required": ["topic", "reading", "risk"],
            },
        },
        "risks": {"type": "ARRAY", "items": {"type": "STRING"}},
        "opportunities": {"type": "ARRAY", "items": {"type": "STRING"}},
        "timing_note": {"type": "STRING", "nullable": True},
    },
    "required": ["overall", "recommendation", "recommendation_reason", "headline", "topics", "risks", "opportunities"],
}


def build_summary_prompt(
    row: Dict[str, Any],
    mood: PublicMood,
    overall_recommendation: Callable[[List[MatchedTopic]], Tuple[str, str]],
) -> str:
    """Build the Gemini prompt summarizing how a submission lands against its matched listening snapshots."""
    topics = []
    for t in mood.topics:
        if not t.snapshot:
            continue
        s = t.snapshot
        topics.append({
            "topic": t.name,
            "how_the_post_relates": (
                f"names it ({', '.join(t.matched_terms)})" if t.matched_terms else "talks about it without naming it"
            ),
            "data_age_hours": round((datetime.now(timezone.utc) - s.fetched_at).total_seconds() / 3600, 1),
            "last_days": s.window_days,
            "positive_pct": s.positive_pct,
            "neutral_pct": s.neutral_pct,
            "negative_pct": s.negative_pct,
            "mentions": s.volume,
            "mentions_change_pct": s.volume_change_pct,
            "spiking": s.is_spike,
            "baseline_recommendation": s.recommendation,
            "baseline_reason": s.recommendation_reason,
            "sample_posts": [
                {"sentiment": p.sentiment, "confidence": p.confidence, "text": p.text}
                for p in s.top_posts
            ],
        })
    baseline_rec, baseline_reason = overall_recommendation(mood.topics)
    return f"""You advise a social media reviewer at a publicly listed drone and technology company.
Below is a post (including its title, caption, any uploaded script, any video speech transcript, and any text extracted from attached images) that is in review,
along with social listening data on how people online currently feel about the topics it touches.
Say, briefly and concretely, how this post or script is likely to land given that mood: where it could be
misread or draw criticism (for example, posting about attack/military drones while a country is being raided by attack drones),
where it rides goodwill or a positive trend the team can jump on, and recommend whether to POST, WAIT, or HOLD.

Decision Rules for `recommendation`:
- POST: At least ~70% positive sentiment (or clearly favourable mood), limited negative feedback, and no serious reputation or crisis alerts.
- WAIT: Mixed sentiment, an unusual increase in negative comments, low confidence, or insufficient data.
- HOLD: A serious verified reputation issue, geopolitical/safety crisis (e.g., drone attacks/raids/casualties in the news), or a significant negative trend. Send to a human reviewer rather than publishing now.
- Do not rely on sentiment percentage alone: a small number of serious crisis or safety complaints matters more than hundreds of positive comments.

Rules:
- Base every statement on the post/script/transcript/image text and the listening data given. Don't invent events, numbers or news.
- The headline is one sentence. `recommendation_reason` is one concise sentence explaining why POST, WAIT, or HOLD was chosen.
- Each topic reading is one or two sentences. Up to 3 risks and 3 opportunities.
- Use NO_DATA only if no topic data is given.
- timing_note only when spiking or shifting mentions matter for when to post; otherwise null.
- Today is {datetime.now(timezone.utc).date().isoformat()}.

POST / SCRIPT / TRANSCRIPT / IMAGE TEXT IN REVIEW
Brand: {row["brand_name"] or row["brand_code"] or "unknown"}
Audience: {row["audience"] or "general"}
Platforms: {", ".join(row["platforms"] or []) or "unspecified"}
Baseline Rule Recommendation: {baseline_rec} ({baseline_reason})
Text (title, caption, uploaded script, video transcript, and/or image text):
\"\"\"{row["text"]}\"\"\"

LISTENING DATA ({mood.source}{" (sample data)" if mood.is_mock else ""})
{json.dumps(topics, indent=1)}
"""
