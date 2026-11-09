"""Social listening providers: where "public mood" data comes from.

The platform talks to one interface. Today that's a mock of Sprout Social Listening or an X (Twitter) JSON feed
collected via Playwright/API and scored per comment with VADER (`vaderSentiment`). When the real Sprout API
is available, implement SproutProvider and set SOCIAL_LISTENING_PROVIDER=sprout; for X JSON listening, set
SOCIAL_LISTENING_PROVIDER=x_json (or load `.data/x_listening_feed.json`).
"""

import hashlib
import json
import os
import random
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_VADER = SentimentIntensityAnalyzer()

DEFAULT_X_JSON_PATH = Path(__file__).resolve().parents[2] / ".data" / "x_listening_feed.json"

# VADER's built-in lexicon rates words from -4.0 (most negative) to +4.0 (most positive).
# Words with a lexicon valence <= -2.0 represent severe negative/crisis intensity.
SEVERE_VALENCE_THRESHOLD = -2.0
SEVERE_COMPOUND_THRESHOLD = -0.60


def classify_comment_sentiment(text: str) -> Dict[str, Any]:
    """Classify an individual X comment/post as POSITIVE, NEGATIVE, or NEUTRAL with confidence (0-100%)
    and extract severe negative terms directly from VADER's 7,500-word valence lexicon."""
    cleaned = (text or "").strip()
    if not cleaned:
        return {"sentiment": "NEUTRAL", "confidence": 50.0, "compound": 0.0, "crisis_flags": []}

    scores = _VADER.polarity_scores(cleaned)
    compound = float(scores["compound"])
    tokens = re.findall(r"[a-z0-9'-]+", cleaned.lower())
    crisis_flags = list(
        dict.fromkeys(w for w in tokens if _VADER.lexicon.get(w, 0.0) <= SEVERE_VALENCE_THRESHOLD)
    )

    if compound >= 0.05:
        label = "POSITIVE"
        conf = min(99.0, round(60.0 + abs(compound) * 39.0, 1))
    elif compound <= -0.05:
        label = "NEGATIVE"
        conf = min(99.0, round(60.0 + abs(compound) * 39.0, 1))
    else:
        label = "NEUTRAL"
        conf = min(98.0, round(70.0 + (1.0 - abs(compound)) * 25.0, 1))

    return {
        "sentiment": label,
        "confidence": conf,
        "compound": round(compound, 3),
        "crisis_flags": crisis_flags,
    }


def evaluate_publishing_decision(
    positive_pct: float,
    neutral_pct: float,
    negative_pct: float,
    volume: int,
    volume_change_pct: Optional[float] = None,
    is_spike: bool = False,
    top_posts: Optional[List[Any]] = None,
) -> Tuple[str, str]:
    """Decide whether the team should POST, WAIT (review), or HOLD (escalate to human reviewer)
    based on sentiment percentages, mention spikes, and VADER's severe negative intensity scores.

    Rules:
    - HOLD: Severe negative intensity in comments (VADER compound <= -0.60 or multiple severe lexicon terms),
      negative sentiment >= 45%, or a negative spike (is_spike and negative_pct >= 35%).
    - POST: Positive sentiment >= 70% (or >= 55% with <= 15% negative) and no severe negative alerts or spikes.
    - WAIT: Mixed sentiment, unusual increase in negative comments, low volume, or insufficient clarity.
    """
    posts = top_posts or []
    severe_terms: List[str] = []
    severe_post_count = 0
    for p in posts:
        txt = p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "")
        info = classify_comment_sentiment(txt)
        if info["compound"] <= SEVERE_COMPOUND_THRESHOLD or len(info["crisis_flags"]) >= 2:
            severe_post_count += 1
            for flag in info["crisis_flags"]:
                if flag not in severe_terms:
                    severe_terms.append(flag)

    if severe_post_count > 0 and (negative_pct >= 25.0 or is_spike or len(severe_terms) >= 2):
        terms_str = ", ".join(severe_terms[:4]) if severe_terms else "high negative intensity"
        return (
            "HOLD",
            f"Serious negative intensity or crisis backlash detected ({terms_str}). Hold and escalate to a human reviewer before posting.",
        )

    if negative_pct >= 45.0 or (is_spike and negative_pct >= 35.0):
        spike_msg = f" with a {volume_change_pct:+.0f}% mention spike" if is_spike and volume_change_pct is not None else ""
        return (
            "HOLD",
            f"High negative sentiment ({negative_pct:.0f}% negative{spike_msg}). Hold publishing and escalate to a reviewer.",
        )

    if volume < 10:
        return (
            "WAIT",
            f"Insufficient comment volume ({volume} mentions) to confirm public mood. Wait or review manually.",
        )

    if positive_pct >= 70.0 and negative_pct <= 15.0:
        return (
            "POST",
            f"Strong positive reception ({positive_pct:.0f}% positive, {negative_pct:.0f}% negative) with no crisis or reputation alerts.",
        )

    if positive_pct >= 55.0 and negative_pct <= 15.0 and not is_spike:
        return (
            "POST",
            f"Favourable sentiment ({positive_pct:.0f}% positive vs {negative_pct:.0f}% negative) and stable conversation.",
        )

    return (
        "WAIT",
        f"Mixed sentiment ({positive_pct:.0f}% positive, {neutral_pct:.0f}% neutral, {negative_pct:.0f}% negative). Review timing and angle before posting.",
    )


class ListeningUnavailable(Exception):
    """The listening source can't be reached or isn't configured."""


class ListeningProvider:
    name = "base"
    is_mock = False

    async def list_topics(self) -> List[Dict[str, Any]]:
        """Topics the account tracks: [{external_id, name, description, keywords}]. The description says what the
        topic covers in plain words; it's what posts are matched against by meaning."""
        raise NotImplementedError

    async def snapshot(self, external_id: str, days: int = 7) -> Dict[str, Any]:
        """Recent sentiment for a topic: positive/neutral/negative percentages, mention volume and change,
        daily volume, and a few top posts."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Mock Sprout Social Listening
# ---------------------------------------------------------------------------

_MOCK_TOPICS: List[Dict[str, Any]] = [
    {"external_id": "mock-zenadrone", "description": "ZenaTech's ZenaDrone products: autonomous drones for inspection, agriculture, logistics and defense, including the ZenaDrone 1000 and IQ series.",
     "name": "ZenaDrone", "keywords": ["zenadrone", "zena drone", "zenadrone 1000"],
     "base": (74, 18), "volume": 900},
    {"external_id": "mock-drone-delivery", "description": "Delivering parcels, food and medical supplies by drone; last-mile logistics, noise, safety and neighbourhood acceptance.",
     "name": "Drone delivery", "keywords": ["drone delivery", "delivery drone", "delivery drones", "last mile"],
     "base": (41, 38), "volume": 5200},
    {"external_id": "mock-counter-uas", "description": "Detecting, tracking and stopping hostile or unauthorised drones; interceptor drones, airspace security, attack drones and military use.",
     "name": "Counter-drone & Attack Drones (C-UAS)", "keywords": ["counter uas", "counter drone", "counter drones", "c uas", "interceptor", "interceptors", "attack drone", "attack drones", "military drone", "drone raid", "drone strike"],
     "base": (22, 30), "volume": 6800, "spike": True},
    {"external_id": "mock-ag-drones", "description": "Drones used on farms: crop monitoring, spraying, mapping fields, yield and precision agriculture.",
     "name": "Agricultural drones", "keywords": ["agricultural drone", "agricultural drones", "crop", "crops", "farm", "farmers", "spraying", "precision agriculture"],
     "base": (76, 16), "volume": 2400},
    {"external_id": "mock-zoooffice", "description": "ZenaTech's ZooOffice: workplace and office software and services.",
     "name": "ZooOffice", "keywords": ["zoooffice", "zoo office"],
     "base": (47, 44), "volume": 310},
    {"external_id": "mock-agentic-ai", "description": "AI agents that act on their own: automation, jobs, trust, safety and hype around autonomous AI.",
     "name": "Agentic AI", "keywords": ["agentic ai", "ai agents", "ai agent", "agentic"],
     "base": (72, 18), "volume": 48000, "spike": True},
    {"external_id": "mock-daas", "description": "Drone as a Service: renting drone work (inspections, surveys, mapping) instead of buying drones; franchises and acquisitions.",
     "name": "Drone as a Service", "keywords": ["daas", "drone as a service"],
     "base": (73, 18), "volume": 640},
    {"external_id": "mock-defense-procurement", "description": "Government and military buying of drones and defense technology: contracts, budgets, Pentagon and allied programmes.",
     "name": "Defense procurement", "keywords": ["procurement", "pentagon", "dod", "defense contract", "defense budget"],
     "base": (22, 48), "volume": 7300},
    {"external_id": "mock-faa-rules", "description": "Drone regulation in the US: FAA Part 107, beyond-visual-line-of-sight (BVLOS) rules, waivers and certification.",
     "name": "FAA drone rules", "keywords": ["faa", "part 107", "bvlos", "waiver", "drone regulation", "drone rules"],
     "base": (25, 50), "volume": 1800},
    {"external_id": "mock-zena-stock", "description": "ZenaTech as a public company (Nasdaq: ZENA): share price, investors, earnings and financing news.",
     "name": "ZENA stock", "keywords": ["zena stock", "nasdaq zena", "nasdaq: zena", "zenatech stock", "investors", "shareholders"],
     "base": (35, 40), "volume": 1200, "spike": True},
]

_MOCK_POSTS = {
    "positive": [
        "Really impressed by what {t} is doing lately. Amazing breakthrough and great service!",
        "Saw a demo of {t} today. Faster and quieter than I expected. Game-changer for the team.",
        "{t} keeps coming up in our team chats, mostly great things and solid results.",
    ],
    "neutral": [
        "Anyone have numbers on how {t} actually performs outside of pilots?",
        "Reading up on {t} this week. Curious where the regulation lands.",
    ],
    "negative": [
        "Still not convinced by the hype around {t}. Show me results, not renders.",
        "Concerned about privacy and safety with {t}. Who's accountable when it goes wrong?",
    ],
}


class MockSproutProvider(ListeningProvider):
    """Plausible, made-up Sprout Listening data. Stable within a day, drifting a little day to day, so freshness and
    reuse can be tested. Never shown without a "mock data" label."""

    name = "sprout"
    is_mock = True

    async def list_topics(self) -> List[Dict[str, Any]]:
        return [
            {"external_id": t["external_id"], "name": t["name"], "description": t["description"], "keywords": t["keywords"]}
            for t in _MOCK_TOPICS
        ]

    async def snapshot(self, external_id: str, days: int = 7) -> Dict[str, Any]:
        topic = next((t for t in _MOCK_TOPICS if t["external_id"] == external_id), None)
        if not topic:
            raise ListeningUnavailable(f"Unknown topic {external_id}")
        seed = int(hashlib.sha256(f"{external_id}:{date.today().isoformat()}".encode()).hexdigest()[:8], 16)
        rnd = random.Random(seed)
        pos = max(5, min(85, topic["base"][0] + rnd.randint(-3, 3)))
        neu = max(5, min(90 - pos, topic["base"][1] + rnd.randint(-3, 3)))
        neg = 100 - pos - neu
        base = topic["volume"]
        daily = [max(0, int(base / 7 * rnd.uniform(0.7, 1.3))) for _ in range(days * 2)]
        if topic.get("spike"):
            daily[-2:] = [int(v * 2.6) for v in daily[-2:]]
        recent, previous = sum(daily[-days:]), sum(daily[:days]) or 1
        change = round((recent - previous) / previous * 100, 1)
        posts = []
        pool = [(mood, text) for mood, texts in _MOCK_POSTS.items() for text in texts]
        authors = rnd.sample(range(100, 1000), 3)
        for (mood, text), author in zip(rnd.sample(pool, 3), authors):
            formatted = text.format(t=topic["name"])
            scored = classify_comment_sentiment(formatted)
            posts.append({
                "network": rnd.choice(["X", "LINKEDIN", "TIKTOK", "INSTAGRAM"]),
                "author": f"@mock_user_{author}",
                "text": formatted,
                "sentiment": mood.upper(),
                "confidence": scored["confidence"],
                "engagements": rnd.randint(20, 4000),
                "posted_on": (date.today() - timedelta(days=rnd.randint(0, days - 1))).isoformat(),
            })
        return {
            "positive": pos,
            "neutral": neu,
            "negative": neg,
            "volume": recent,
            "volume_change_pct": change,
            "daily_volume": daily[-days:],
            "top_posts": posts,
        }


# ---------------------------------------------------------------------------
# X (Twitter) JSON Feed Provider (Playwright / JSON Ingestion + VADER)
# ---------------------------------------------------------------------------

def analyze_raw_x_topic(topic_data: Dict[str, Any], days: int = 7) -> Dict[str, Any]:
    """Score every comment in an X topic payload using VADER, compute aggregate
    positive/neutral/negative percentages, daily volume, and top posts with confidence scores."""
    raw_comments = topic_data.get("comments") or topic_data.get("top_posts") or []
    scored_posts: List[Dict[str, Any]] = []
    counts = {"POSITIVE": 0, "NEUTRAL": 0, "NEGATIVE": 0}

    for idx, c in enumerate(raw_comments):
        text = (c.get("text") or "").strip()
        if not text:
            continue
        cls = classify_comment_sentiment(text)
        sentiment = c.get("sentiment") if c.get("sentiment") in counts else cls["sentiment"]
        confidence = float(c.get("confidence") if c.get("confidence") is not None else cls["confidence"])
        counts[sentiment] += 1
        scored_posts.append({
            "network": c.get("network") or "X",
            "author": c.get("author") or f"@x_user_{idx + 1}",
            "text": text,
            "sentiment": sentiment,
            "confidence": confidence,
            "engagements": int(c.get("engagements") or 0),
            "posted_on": c.get("posted_on") or date.today().isoformat(),
        })

    total = sum(counts.values())
    if total > 0:
        pos = round(counts["POSITIVE"] / total * 100, 1)
        neg = round(counts["NEGATIVE"] / total * 100, 1)
        neu = round(max(0.0, 100.0 - pos - neg), 1)
    else:
        pos = float(topic_data.get("positive", 34.0))
        neu = float(topic_data.get("neutral", 33.0))
        neg = round(max(0.0, 100.0 - pos - neu), 1)

    volume = int(topic_data.get("volume") if topic_data.get("volume") is not None else max(total * 45, 10))
    daily = list(topic_data.get("daily_volume") or [])
    if len(daily) < days:
        avg = max(1, volume // max(days, 1))
        daily = [avg] * days
    else:
        daily = [int(x) for x in daily[-days:]]

    change = topic_data.get("volume_change_pct")
    if change is None:
        first_half = sum(daily[: max(1, days // 2)]) or 1
        second_half = sum(daily[max(1, days // 2):])
        change = round((second_half - first_half) / first_half * 100, 1)
    else:
        change = float(change)

    scored_posts.sort(key=lambda p: p["engagements"], reverse=True)
    return {
        "positive": pos,
        "neutral": neu,
        "negative": neg,
        "volume": volume,
        "volume_change_pct": change,
        "daily_volume": daily,
        "top_posts": scored_posts[:6],
    }


class XJsonListeningProvider(ListeningProvider):
    """Reads scraped or API-collected X (Twitter) comments & trends from a JSON file
    (`.data/x_listening_feed.json` by default) and classifies each comment with VADER."""

    name = "x_json"
    is_mock = False

    def __init__(self, json_path: Optional[Path] = None) -> None:
        env_path = os.getenv("X_LISTENING_JSON_PATH")
        self.json_path = Path(env_path) if env_path else (json_path or DEFAULT_X_JSON_PATH)

    def _load_feed(self) -> List[Dict[str, Any]]:
        if not self.json_path.exists():
            raise ListeningUnavailable(
                f"X listening JSON file not found at {self.json_path}. Run scripts/scrape_x_playwright.py or POST /listening/x-ingest first."
            )
        try:
            data = json.loads(self.json_path.read_text(encoding="utf-8"))
        except Exception as e:
            raise ListeningUnavailable(f"Couldn't read X listening JSON file: {e}")
        topics = data.get("topics") if isinstance(data, dict) else data
        if not isinstance(topics, list) or not topics:
            raise ListeningUnavailable("X listening JSON file has no topics.")
        return topics

    async def list_topics(self) -> List[Dict[str, Any]]:
        topics = self._load_feed()
        return [
            {
                "external_id": str(t["external_id"]),
                "name": str(t["name"]),
                "description": t.get("description") or f"X (Twitter) conversation and comments around {t['name']}.",
                "keywords": list(t.get("keywords") or [str(t["name"]).lower()]),
            }
            for t in topics
            if t.get("external_id") and t.get("name")
        ]

    async def snapshot(self, external_id: str, days: int = 7) -> Dict[str, Any]:
        topics = self._load_feed()
        topic = next((t for t in topics if str(t.get("external_id")) == external_id), None)
        if not topic:
            raise ListeningUnavailable(f"Topic {external_id} not found in X JSON feed")
        return analyze_raw_x_topic(topic, days=days)


# ---------------------------------------------------------------------------
# Real Sprout Social (to build once API access exists)
# ---------------------------------------------------------------------------

class SproutProvider(ListeningProvider):
    """Sprout Social Listening over its API. Needs SPROUT_API_TOKEN and SPROUT_CUSTOMER_ID, and a plan with Listening
    and API access. Until it's built, it says so instead of returning anything."""

    name = "sprout"

    def __init__(self) -> None:
        self.token = os.getenv("SPROUT_API_TOKEN")
        self.customer_id = os.getenv("SPROUT_CUSTOMER_ID")

    async def list_topics(self) -> List[Dict[str, Any]]:
        raise ListeningUnavailable("The Sprout Social API isn't connected yet.")

    async def snapshot(self, external_id: str, days: int = 7) -> Dict[str, Any]:
        raise ListeningUnavailable("The Sprout Social API isn't connected yet.")


_provider: Optional[ListeningProvider] = None


def reset_provider_cache() -> None:
    global _provider
    _provider = None


def get_provider() -> ListeningProvider:
    global _provider
    if _provider is None:
        mode = os.getenv("SOCIAL_LISTENING_PROVIDER", "").strip().lower()
        if mode == "sprout":
            _provider = SproutProvider()
        elif mode in ("x_json", "x", "twitter"):
            _provider = XJsonListeningProvider()
        elif not mode and DEFAULT_X_JSON_PATH.exists():
            _provider = XJsonListeningProvider()
        else:
            _provider = MockSproutProvider()
    return _provider
