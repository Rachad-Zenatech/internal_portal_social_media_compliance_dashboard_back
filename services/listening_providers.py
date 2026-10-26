"""Social listening providers: where "public mood" data comes from.

The platform talks to one interface. Today that's a mock of Sprout Social Listening, so the feature can be built and
tried before API access exists. When the real Sprout API is available, implement SproutProvider and set
SOCIAL_LISTENING_PROVIDER=sprout; nothing else changes.
"""

import hashlib
import os
import random
from datetime import date, timedelta
from typing import Any, Dict, List, Optional


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
     "base": (52, 36), "volume": 900},
    {"external_id": "mock-drone-delivery", "description": "Delivering parcels, food and medical supplies by drone; last-mile logistics, noise, safety and neighbourhood acceptance.",
     "name": "Drone delivery", "keywords": ["drone delivery", "delivery drone", "delivery drones", "last mile"],
     "base": (41, 38), "volume": 5200},
    {"external_id": "mock-counter-uas", "description": "Detecting, tracking and stopping hostile or unauthorised drones; interceptor drones, airspace security and military use.",
     "name": "Counter-drone (C-UAS)", "keywords": ["counter uas", "counter drone", "counter drones", "c uas", "interceptor", "interceptors"],
     "base": (30, 45), "volume": 3100},
    {"external_id": "mock-ag-drones", "description": "Drones used on farms: crop monitoring, spraying, mapping fields, yield and precision agriculture.",
     "name": "Agricultural drones", "keywords": ["agricultural drone", "agricultural drones", "crop", "crops", "farm", "farmers", "spraying", "precision agriculture"],
     "base": (58, 34), "volume": 2400},
    {"external_id": "mock-zoooffice", "description": "ZenaTech's ZooOffice: workplace and office software and services.",
     "name": "ZooOffice", "keywords": ["zoooffice", "zoo office"],
     "base": (47, 44), "volume": 310},
    {"external_id": "mock-agentic-ai", "description": "AI agents that act on their own: automation, jobs, trust, safety and hype around autonomous AI.",
     "name": "Agentic AI", "keywords": ["agentic ai", "ai agents", "ai agent", "agentic"],
     "base": (39, 37), "volume": 48000},
    {"external_id": "mock-daas", "description": "Drone as a Service: renting drone work (inspections, surveys, mapping) instead of buying drones; franchises and acquisitions.",
     "name": "Drone as a Service", "keywords": ["daas", "drone as a service"],
     "base": (50, 40), "volume": 640},
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
        "Really impressed by what {t} is doing lately. This is the direction the industry needs.",
        "Saw a demo of {t} today. Faster and quieter than I expected.",
        "{t} keeps coming up in our team chats, mostly good things.",
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
        pos = max(5, min(85, topic["base"][0] + rnd.randint(-5, 5)))
        neu = max(5, min(90 - pos, topic["base"][1] + rnd.randint(-4, 4)))
        neg = 100 - pos - neu
        base = topic["volume"]
        daily = [max(0, int(base / 7 * rnd.uniform(0.7, 1.3))) for _ in range(days * 2)]
        if topic.get("spike"):
            daily[-2:] = [int(v * 2.6) for v in daily[-2:]]
        recent, previous = sum(daily[-days:]), sum(daily[:days]) or 1
        change = round((recent - previous) / previous * 100, 1)
        posts = []
        # Three different sample posts (never the same text twice), by three different made-up authors.
        pool = [(mood, text) for mood, texts in _MOCK_POSTS.items() for text in texts]
        authors = rnd.sample(range(100, 1000), 3)
        for (mood, text), author in zip(rnd.sample(pool, 3), authors):
            posts.append({
                "network": rnd.choice(["X", "LINKEDIN", "TIKTOK", "INSTAGRAM"]),
                "author": f"@mock_user_{author}",
                "text": text.format(t=topic["name"]),
                "sentiment": mood.upper(),
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


def get_provider() -> ListeningProvider:
    global _provider
    if _provider is None:
        _provider = SproutProvider() if os.getenv("SOCIAL_LISTENING_PROVIDER", "mock").lower() == "sprout" else MockSproutProvider()
    return _provider
