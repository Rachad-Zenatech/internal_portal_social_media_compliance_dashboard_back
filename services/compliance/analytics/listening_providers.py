"""Social listening providers: where "public mood" data comes from.

The platform talks to one interface. Today that is the collected X (Twitter) and news feed, read from the vector
database (or a feed file) and scored per item by the sentiment model (VADER as the fallback). When the real Sprout API
is available, implement SproutProvider and set SOCIAL_LISTENING_PROVIDER=sprout; for the collected feed, set
SOCIAL_LISTENING_PROVIDER=x_json (the default whenever the vector database holds one).

There is no sample data: with no source, the provider says there is none (UnconfiguredProvider) rather than inventing
a mood.
"""

import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_VADER = SentimentIntensityAnalyzer()

# <repo>/.data/x_listening_feed.json, next to the app's other data (this file is <repo>/services/compliance/analytics/).
DEFAULT_X_JSON_PATH = Path(__file__).resolve().parents[3] / ".data" / "x_listening_feed.json"

# VADER's built-in lexicon rates words from -4.0 (most negative) to +4.0 (most positive).
# Words with a lexicon valence <= -2.0 represent severe negative/crisis intensity.
SEVERE_VALENCE_THRESHOLD = -2.0
SEVERE_COMPOUND_THRESHOLD = -0.60


# Google News appends the outlet to a headline (" - The New York Times"); the same story from two outlets is one story.
PUBLISHER_SUFFIX = re.compile(r"\s+[-|\u2013\u2014]\s+[^-|\u2013\u2014]{2,60}$")


def clean_headline(text: str) -> str:
    """The headline without its outlet, lowercased, letters and digits only: equal for the same story."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", PUBLISHER_SUFFIX.sub("", (text or "").strip()).lower()).split())


# A change in mentions is claimed only with enough to compare: at least this many stories in the three earlier days
# (3 stories becoming 7 is +133%, but it is noise, and a noisy spike would push a topic towards HOLD).
MIN_TREND_BASE = int(os.getenv("PUBLIC_MOOD_TREND_MIN_BASE", "10"))


def volume_change(daily: List[int]) -> Optional[float]:
    """Percent change in mentions: the last three complete days against the three before them, from `daily` (oldest
    first, ending today). Today is left out because it is still filling up, and the two periods are the same length, so
    a steady volume reads as steady. None when there is too little in the earlier days to compare with."""
    if len(daily) < 7:
        return None
    prior, recent = sum(daily[-7:-4]), sum(daily[-4:-1])
    if prior < MIN_TREND_BASE:
        return None
    return round((recent - prior) / prior * 100, 1)


LOCAL_MODEL_NAME = os.getenv("SENTIMENT_MODEL", "cardiffnlp/twitter-roberta-base-sentiment-latest").strip()
_local_model: Any = None  # (tokenizer, model), False once loading failed (VADER is used from then on)
# Loading takes a while (and may download the model); callers in worker threads wait for the one load.
_model_lock = threading.Lock()


def _load_local_model() -> Any:
    """The trained sentiment model, loaded once. False when it's switched off (SENTIMENT_MODEL=vader), the packages
    aren't installed, or it can't be downloaded: callers then use VADER."""
    if _local_model is not None:
        return _local_model
    with _model_lock:
        return _load_local_model_locked()


async def warm_up_sentiment_model() -> None:
    """Load the sentiment model in a worker thread at startup, so no request waits for it (or freezes the server)."""
    import asyncio

    await asyncio.to_thread(_load_local_model)


def _load_local_model_locked() -> Any:
    global _local_model
    if _local_model is None:
        if not LOCAL_MODEL_NAME or LOCAL_MODEL_NAME.lower() == "vader":
            _local_model = False
        else:
            try:
                import torch  # noqa: F401
                from transformers import AutoModelForSequenceClassification, AutoTokenizer

                tok = AutoTokenizer.from_pretrained(LOCAL_MODEL_NAME)
                mdl = AutoModelForSequenceClassification.from_pretrained(LOCAL_MODEL_NAME).eval()
                _local_model = (tok, mdl)
            except Exception as e:
                logging.getLogger(__name__).warning("Local sentiment model unavailable, using VADER: %s", e)
                _local_model = False
    return _local_model


def _model_probs(text: str) -> Optional[Dict[str, float]]:
    """{'POSITIVE': p, 'NEUTRAL': p, 'NEGATIVE': p} from the trained model, or None to fall back to VADER."""
    loaded = _load_local_model()
    if not loaded:
        return None
    out = _run_model(text, loaded)
    return dict(out) if out is not None else None


# The same headlines are scored on every public-mood view; one model run is slow enough that repeating them in a
# request stalled the whole server. Keyed by the loaded model too, so a different model never reuses old scores.
@lru_cache(maxsize=20000)
def _run_model(text: str, loaded: Tuple[Any, Any]) -> Optional[Dict[str, float]]:
    try:
        import torch

        tok, mdl = loaded
        with torch.no_grad():
            enc = tok(text, return_tensors="pt", truncation=True, max_length=256)
            probs = torch.softmax(mdl(**enc).logits[0], dim=-1).tolist()
        labels = [mdl.config.id2label[i].upper() for i in range(len(probs))]
        out = {l: p for l, p in zip(labels, probs)}
        return out if {"POSITIVE", "NEUTRAL", "NEGATIVE"} <= set(out) else None
    except Exception as e:
        logging.getLogger(__name__).warning("Local sentiment model failed on a text, using VADER: %s", e)
        return None


def classify_comment_sentiment(text: str) -> Dict[str, Any]:
    """Classify an individual X comment/post as POSITIVE, NEGATIVE, or NEUTRAL with confidence (0-100%).
    A trained local model (SENTIMENT_MODEL, default twitter-roberta) decides when it's available; otherwise VADER.
    `compound` is -1..1 either way (the model's P(positive) - P(negative)); crisis words come from VADER's valence
    lexicon (with the model, only when it reads the text as negative)."""
    cleaned = (text or "").strip()
    if not cleaned:
        return {"sentiment": "NEUTRAL", "confidence": 50.0, "compound": 0.0, "crisis_flags": []}

    tokens = re.findall(r"[a-z0-9'-]+", cleaned.lower())
    flags = list(dict.fromkeys(w for w in tokens if _VADER.lexicon.get(w, 0.0) <= SEVERE_VALENCE_THRESHOLD))
    probs = _model_probs(cleaned)
    if probs:
        label = max(("POSITIVE", "NEUTRAL", "NEGATIVE"), key=lambda k: probs[k])
        return {
            "sentiment": label,
            "confidence": round(min(99.0, probs[label] * 100.0), 1),
            "compound": round(probs["POSITIVE"] - probs["NEGATIVE"], 3),
            # Crisis words only count where the model also reads the text as negative: "kill enemy drone threats" in
            # a defence contract headline isn't a crisis.
            "crisis_flags": flags if label == "NEGATIVE" else [],
            "model": LOCAL_MODEL_NAME,
        }

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
        f"Not enough positive evidence to recommend posting: {positive_pct:.0f}% positive, {neutral_pct:.0f}% neutral, "
        f"{negative_pct:.0f}% negative (posting needs at least 55% positive and no more than 15% negative). Review timing and angle.",
    )


class ListeningUnavailable(Exception):
    """The listening source can't be reached or isn't configured."""


# ---------------------------------------------------------------------------
# Where the collected X / news feed lives
#
# In the vector database (the `x_feed_data` collection) whenever it is configured (MONGODB_URI): the JSON file is then
# neither written nor read, so an old file on disk can't be mistaken for the feed. Without a vector database, or when a
# file is asked for explicitly (a path, or X_LISTENING_JSON_PATH), the feed is a JSON file as it used to be.
# ---------------------------------------------------------------------------

def _x_feed_store():
    from services.ai.core import x_feed_store  # imported when needed: it pulls in the vector store and its clients
    return x_feed_store


def feed_in_database(file_path: Optional[Path] = None) -> bool:
    return file_path is None and _x_feed_store().is_enabled()


async def load_feed(file_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The latest collected feed ({"collected_at", "collection", "topics": [...]}), or None if there is none yet."""
    if feed_in_database(file_path):
        return await _x_feed_store().read_feed()
    path = file_path or DEFAULT_X_JSON_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        raise ListeningUnavailable(f"Couldn't read the X listening feed file {path}: {e}")
    return data if isinstance(data, dict) else {"topics": data}


async def save_feed(payload: Dict[str, Any], file_path: Optional[Path] = None) -> str:
    """Store a collected feed as the current one; says where it went."""
    if feed_in_database(file_path):
        store = _x_feed_store()
        await store.write_feed(payload)
        return f"the vector database ({store.collection_name()})"
    path = file_path or DEFAULT_X_JSON_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(path)


async def feed_stamp(file_path: Optional[Path] = None) -> Optional[datetime]:
    """When the current feed was stored: the cue that topics and snapshots built from an earlier one are out of date."""
    if feed_in_database(file_path):
        return await _x_feed_store().written_at()
    try:
        return datetime.fromtimestamp((file_path or DEFAULT_X_JSON_PATH).stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


class ListeningProvider:
    name = "base"
    is_mock = False

    async def feed_stamp(self) -> Optional[datetime]:
        """When this source's data was last collected, if it is collected rather than fetched live."""
        return None

    async def collection_info(self) -> Optional[Dict[str, Any]]:
        return None

    async def list_topics(self) -> List[Dict[str, Any]]:
        """Topics the account tracks: [{external_id, name, description, keywords}]. The description says what the
        topic covers in plain words; it's what posts are matched against by meaning."""
        raise NotImplementedError

    async def snapshot(self, external_id: str, days: int = 7) -> Dict[str, Any]:
        """Recent sentiment for a topic: positive/neutral/negative percentages, mention volume and change,
        daily volume, and a few top posts."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# No source
# ---------------------------------------------------------------------------

NO_SOURCE_MESSAGE = ("No listening source is configured: there is no vector database holding a collected feed and no "
                     "feed file. Connect MongoDB and click Scrape X to collect one.")


class UnconfiguredProvider(ListeningProvider):
    """Used when there is nowhere to read public mood from. It has no data and says so. There is deliberately no sample
    data to fall back on: a mood that was not measured is not shown, so nobody can mistake an invented number for one."""

    name = "unconfigured"
    is_mock = False

    async def list_topics(self) -> List[Dict[str, Any]]:
        raise ListeningUnavailable(NO_SOURCE_MESSAGE)

    async def snapshot(self, external_id: str, days: int = 7) -> Dict[str, Any]:
        raise ListeningUnavailable(NO_SOURCE_MESSAGE)


# ---------------------------------------------------------------------------
# X (Twitter) JSON Feed Provider (Playwright / JSON Ingestion + VADER)
# ---------------------------------------------------------------------------

def analyze_raw_x_topic(topic_data: Dict[str, Any], days: int = 7) -> Dict[str, Any]:
    """Score every comment in an X topic payload using VADER, compute aggregate
    positive/neutral/negative percentages, daily volume, and top posts with confidence scores."""
    raw_comments = topic_data.get("comments") or topic_data.get("top_posts") or []
    scored_posts: List[Dict[str, Any]] = []
    counts = {"POSITIVE": 0, "NEUTRAL": 0, "NEGATIVE": 0}

    seen_stories = set()
    for idx, c in enumerate(raw_comments):
        text = (c.get("text") or "").strip()
        if not text:
            continue
        story = clean_headline(text)
        if story in seen_stories:  # the same story from another outlet, or the same post twice, counts once
            continue
        seen_stories.add(story)
        if c.get("sentiment") in counts and c.get("confidence") is not None:
            sentiment, confidence = c["sentiment"], float(c["confidence"])  # already scored: don't run the model again
        else:
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
            # Only a date the source gave us: never today's date standing in for an unknown one.
            "posted_on": c.get("posted_on") or None,
        })

    total = sum(counts.values())
    if total > 0:
        pos = round(counts["POSITIVE"] / total * 100, 1)
        neg = round(counts["NEGATIVE"] / total * 100, 1)
        neu = round(max(0.0, 100.0 - pos - neg), 1)
    else:
        # Nothing was collected: say so (all zeros, volume 0) rather than inventing a mood.
        pos = neu = neg = 0.0

    # Volume is what was actually collected unless the source reports its own mention count. Daily counts and the
    # change since the previous window exist only when the source supplies them: they are never estimated.
    volume = int(topic_data.get("volume") if topic_data.get("volume") is not None else total)
    supplied = [int(x) for x in (topic_data.get("daily_volume") or [])]
    daily = supplied[-days:] if len(supplied) >= days else []

    change = topic_data.get("volume_change_pct")
    if change is not None:
        change = float(change)
    elif daily:
        change = volume_change(daily)

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
    """Reads the collected X (Twitter) posts and news trends, and classifies each with the sentiment model. The feed
    comes from the vector database (`x_feed_data`) when it is configured, else from a JSON file (see load_feed)."""

    name = "x_json"
    is_mock = False

    def __init__(self, json_path: Optional[Path] = None) -> None:
        env_path = os.getenv("X_LISTENING_JSON_PATH")
        # An explicit file (argument or environment) means "use this file"; None means the default source.
        self.json_path: Optional[Path] = Path(env_path) if env_path else json_path

    async def _feed(self) -> Dict[str, Any]:
        feed = await load_feed(self.json_path)
        if not feed:
            raise ListeningUnavailable(
                "No X feed has been collected yet. Click Scrape X to collect one (or POST /listening/x-ingest)."
            )
        return feed

    async def _load_feed(self) -> List[Dict[str, Any]]:
        topics = (await self._feed()).get("topics")
        if not isinstance(topics, list) or not topics:
            raise ListeningUnavailable("The collected X feed has no topics.")
        return topics

    async def feed_stamp(self) -> Optional[datetime]:
        return await feed_stamp(self.json_path)

    async def collection_info(self) -> Optional[Dict[str, Any]]:
        """How the last collection went (status per source and overall), when the scraper recorded it."""
        try:
            info = (await self._feed()).get("collection")
        except ListeningUnavailable:
            return None
        return info if isinstance(info, dict) else None

    async def list_topics(self) -> List[Dict[str, Any]]:
        topics = await self._load_feed()
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
        topics = await self._load_feed()
        topic = next((t for t in topics if str(t.get("external_id")) == external_id), None)
        if not topic:
            raise ListeningUnavailable(f"Topic {external_id} not found in the collected X feed")
        # Scoring runs the sentiment model on each new comment: in a worker thread, so other requests keep moving.
        import asyncio

        return await asyncio.to_thread(analyze_raw_x_topic, topic, days)


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
        elif not mode and (_x_feed_store().is_enabled() or DEFAULT_X_JSON_PATH.exists()):
            # With a vector database the feed is there (or not collected yet): never made-up data.
            _provider = XJsonListeningProvider()
        else:
            _provider = UnconfiguredProvider()
    return _provider
