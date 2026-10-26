"""Public mood: what people online currently think about the topics a post touches, and what that means for it.

1. Topics come from the listening source (Sprout Social; mocked until API access exists) and are re-synced daily.
2. A post is tied to topics two ways: a topic's keywords appearing in its title, caption or transcript, and the
   post being close in meaning to the topic's description (MongoDB Atlas vector search over Gemini embeddings, via
   vector_store_service). Keywords catch exact mentions; meaning catches posts that talk about a topic without
   naming it, and weeds out keyword hits that are about something else ("crop" a photo, not crops).
3. Each topic's latest snapshot is shared by every post: it's fetched again only once it goes stale (sooner while the
   topic is spiking), so the listening API is called per topic, not per post.
4. A Gemini summary reads the post against those snapshots. It's saved with a hash of what it read, reused while
   nothing has changed, and marked out of date when the post or the moods move on.

Advisory only: nothing here affects the compliance score or approval.
"""

import hashlib
import json
import logging
import os
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException
from google import genai
from google.genai import types
from pydantic import ValidationError

from models.public_mood_model import (
    DismissedTopic,
    ListeningTopic,
    ListeningTopicUpdate,
    MatchedTopic,
    MatchTestResult,
    MatchTestTopic,
    MoodFeedbackIn,
    PublicMood,
    PublicMoodResult,
    PublicMoodSummary,
    TopicSnapshot,
)
from postgresql_db.database import fetch_all, fetch_one, get_pool
from services import ai_usage_service
from services import vector_store_service as store
from services.compliance_service import _matches, _normalize
from services.gemini_service import generate_content_with_fallback
from services.listening_providers import ListeningUnavailable, get_provider

logger = logging.getLogger(__name__)

PROMPT_VERSION = "public-mood-v1"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


# A snapshot is reused for this long, or for the shorter time while its topic is spiking.
FRESH_HOURS = _env_float("PUBLIC_MOOD_FRESH_HOURS", 24)
SPIKE_FRESH_HOURS = _env_float("PUBLIC_MOOD_SPIKE_FRESH_HOURS", 3)
# Mentions up this much on the previous window count as a spike.
SPIKE_CHANGE_PCT = _env_float("PUBLIC_MOOD_SPIKE_CHANGE_PCT", 35)
TOPIC_SYNC_HOURS = _env_float("PUBLIC_MOOD_TOPIC_SYNC_HOURS", 24)
WINDOW_DAYS = int(_env_float("PUBLIC_MOOD_WINDOW_DAYS", 7))
MAX_TOPICS = int(_env_float("PUBLIC_MOOD_MAX_TOPICS", 4))
MAX_TEXT_CHARS = 6000
# Matching by meaning (Atlas cosine scores, 0-1). Calibrated on gemini-embedding-2 with the sample topics: the
# topic a post is about leads the post's median topic score by 0.045-0.10; off-topic ones by 0.015-0.036.
# A post is about a topic it doesn't name when it leads by MEANING_MIN_MARGIN and scores at least MEANING_MIN_SCORE.
MEANING_MIN_MARGIN = _env_float("PUBLIC_MOOD_MEANING_MIN_MARGIN", 0.04)
MEANING_MIN_SCORE = _env_float("PUBLIC_MOOD_MEANING_MIN_SCORE", 0.84)
# A single everyday keyword ("crop") counts only if the meaning leads by this much too.
KEYWORD_MIN_MARGIN = _env_float("PUBLIC_MOOD_KEYWORD_MIN_MARGIN", 0.025)
TOPIC_KIND = "LISTENING_TOPIC"

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise HTTPException(status_code=503, detail="GEMINI_API_KEY isn't configured, so public mood can't be summarised.")
        _client = genai.Client(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Topics and snapshots
# ---------------------------------------------------------------------------

async def _sync_topics(force: bool = False) -> None:
    """Refresh the topic list from the listening source when it's older than a day (or empty, or asked to). The
    team's own keywords and watch settings are kept."""
    provider = get_provider()
    row = await fetch_one(
        "SELECT max(synced_at) AS at FROM listening_topics WHERE provider = $1 AND is_mock = $2 AND deleted_at IS NULL",
        provider.name, provider.is_mock,
    )
    if not force and row and row["at"] and row["at"] > datetime.now(timezone.utc) - timedelta(hours=TOPIC_SYNC_HOURS):
        return
    topics = await provider.list_topics()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO listening_topics (provider, external_id, name, description, keywords, is_mock, synced_at)
                SELECT $2, t.external_id, t.name, t.description, ARRAY(SELECT jsonb_array_elements_text(t.keywords)), $3, NOW()
                FROM jsonb_to_recordset($1::jsonb) AS t(external_id text, name text, description text, keywords jsonb)
                ON CONFLICT (provider, external_id) DO UPDATE
                SET name = EXCLUDED.name, description = EXCLUDED.description, keywords = EXCLUDED.keywords,
                    is_mock = EXCLUDED.is_mock, synced_at = NOW(), deleted_at = NULL
                """,
                json.dumps(topics), provider.name, provider.is_mock,
            )
            # Topics the source no longer tracks.
            await conn.execute(
                """
                UPDATE listening_topics SET deleted_at = NOW()
                WHERE provider = $1 AND deleted_at IS NULL AND NOT (external_id = ANY($2::text[]))
                """,
                provider.name, [t["external_id"] for t in topics],
            )
    await _sync_topic_vectors()


async def _sync_topic_vectors() -> None:
    """Keep one vector document per live topic (only new or changed text is embedded), and none for removed ones."""
    if not store.is_enabled():
        return
    rows = await fetch_all(
        "SELECT id, name, description, keywords, extra_keywords, excluded_keywords, deleted_at FROM listening_topics"
    )
    live = [dict(r) for r in rows if r["deleted_at"] is None]
    try:
        await store.upsert([
            store.VectorDoc(
                kind=TOPIC_KIND,
                source_id=str(r["id"]),
                title=r["name"],
                text=f"{r['description'] or ''}\nAlso called: {', '.join(_effective_keywords(r))}".strip(),
            )
            for r in live
        ])
        await store.delete([f"{TOPIC_KIND}:{r['id']}" for r in rows if r["deleted_at"] is not None])
    except Exception as e:
        logger.warning("Couldn't update listening topics in the vector store (keyword matching still works): %s", e)


def _effective_keywords(t: Dict[str, Any]) -> List[str]:
    """The source's keywords minus the ones the team switched off, plus the team's own."""
    off = {_normalize(k) for k in t.get("excluded_keywords") or []}
    words = [k for k in t.get("keywords") or [] if _normalize(k) not in off]
    return list(dict.fromkeys(words + list(t.get("extra_keywords") or [])))


async def _load_topics() -> List[Dict[str, Any]]:
    """Live topics from the current source, with the keywords that count for matching."""
    provider = get_provider()
    rows = [dict(t) for t in await fetch_all(
        """
        SELECT id, external_id, name, description, keywords, extra_keywords, excluded_keywords, is_watched, is_mock,
               synced_at
        FROM listening_topics WHERE provider = $1 AND is_mock = $2 AND deleted_at IS NULL
        ORDER BY name
        """,
        provider.name, provider.is_mock,
    )]
    for r in rows:
        r["source_keywords"] = list(r["keywords"] or [])
        r["keywords"] = _effective_keywords(r)
    return rows


# Meaning scores per post text, so viewing a post doesn't embed it again. {text hash: {topic id: (score, margin)}}
_meaning_cache: Dict[str, Dict[str, Tuple[float, float]]] = {}
_MEANING_CACHE_SIZE = 500


async def _meaning_scores(row: Dict[str, Any], topic_count: int) -> Optional[Dict[str, Tuple[float, float]]]:
    """How close the post is in meaning to each topic: the raw score, and its margin over the post's median score
    across all topics. Embedding scores are compressed (unrelated text still scores ~0.78), so the margin is what
    says "this topic, not just drones in general". The caption and the transcript are searched separately (a long
    transcript would drown out the caption) and the better margin counts. None when the store is off, unreachable
    or hasn't indexed the topics yet."""
    if not store.is_enabled() or not topic_count:
        return None
    parts = [p for p in ["\n".join(x for x in [row["title"], row["caption_text"]] if x), row["transcript_text"]] if p]
    if not parts:
        return None
    key = hashlib.sha256(json.dumps([parts, store.embedding_model(), topic_count]).encode("utf-8")).hexdigest()
    if key in _meaning_cache:
        return _meaning_cache[key]
    best: Dict[str, Tuple[float, float]] = {}
    for part in parts:
        # Every topic is scored, so the median is the post's own baseline.
        hits = await store.search(part[:MAX_TEXT_CHARS], kinds=[TOPIC_KIND], limit=topic_count)
        if len(hits) < max(3, topic_count // 2):
            # search() returns [] on any failure, and Atlas takes a few seconds to index new topics: fall back to
            # keywords this time rather than trust (or cache) a partial picture.
            return None
        median = statistics.median(float(h.get("score", 0)) for h in hits)
        for h in hits:
            score = float(h.get("score", 0))
            margin = round(score - median, 4)
            if margin > best.get(h["source_id"], (0.0, -1.0))[1]:
                best[h["source_id"]] = (score, margin)
    if len(_meaning_cache) >= _MEANING_CACHE_SIZE:
        _meaning_cache.pop(next(iter(_meaning_cache)))
    _meaning_cache[key] = best
    return best


def _unambiguous(term: str) -> bool:
    """A phrase ("drone delivery") or a coined name ("ZenaDrone") rarely means anything else; a single everyday
    word ("crop", "farm", "waiver") might."""
    return len(_normalize(term).split()) > 1 or term != term.lower()


def _match(text: str, topics: List[Dict[str, Any]], meaning: Optional[Dict[str, Tuple[float, float]]]
           ) -> List[Tuple[Dict[str, Any], List[str], Optional[float], bool]]:
    """Topics the post is about, strongest first: (topic, keywords found, similarity, close in meaning).
    - Named by a phrase or name: always.
    - Named by several of its words together: always.
    - Named by a single everyday word: only if the meaning agrees a little (or there are no meaning scores).
    - Not named: only if clearly closer in meaning than the post's other topics."""
    padded = f" {_normalize(text)} "
    found = []
    for t in topics:
        # One entry per distinct wording ("ZenaDrone" and "zenadrone" are the same hit).
        terms = list({_normalize(k): k for k in reversed([t["name"], *(t["keywords"] or [])])
                      if _normalize(k) and _matches(k, padded)}.values())[::-1]
        score, margin = (meaning or {}).get(str(t["id"]), (None, None))
        close = margin is not None and margin >= MEANING_MIN_MARGIN and score >= MEANING_MIN_SCORE
        if meaning is None:
            keep = bool(terms)
        elif any(_unambiguous(k) for k in terms) or len(terms) >= 2:
            # A phrase or name, or several of the topic's words together ("farmers", "crop", "spraying").
            keep = True
        elif terms:
            keep = margin is not None and margin >= KEYWORD_MIN_MARGIN
        else:
            keep = close
        if keep:
            found.append((t, terms, score, close, margin if margin is not None else -1.0))
    found.sort(key=lambda m: (-(bool(m[1]) and m[3]), -bool(m[1]), -m[4], m[0]["name"]))
    return [(t, terms, score, close) for t, terms, score, close, _ in found[:MAX_TOPICS]]


def _is_fresh(snap: Dict[str, Any], now: datetime, watched: bool = False) -> bool:
    hours = SPIKE_FRESH_HOURS if snap["is_spike"] or watched else FRESH_HOURS
    return snap["fetched_at"] > now - timedelta(hours=hours) and snap["window_days"] == WINDOW_DAYS


async def _latest_snapshots(topic_ids: List[UUID]) -> Dict[UUID, Dict[str, Any]]:
    rows = await fetch_all(
        """
        SELECT DISTINCT ON (topic_id) * FROM topic_snapshots
        WHERE topic_id = ANY($1::uuid[])
        ORDER BY topic_id, fetched_at DESC
        """,
        topic_ids,
    )
    return {r["topic_id"]: dict(r) for r in rows}


async def _fresh_snapshots(topics: List[Dict[str, Any]]) -> Dict[UUID, Dict[str, Any]]:
    """The latest snapshot for each topic, fetching new ones only for topics whose snapshot has gone stale. If the
    source can't be reached, the last snapshot (however old) is used rather than nothing."""
    latest = await _latest_snapshots([t["id"] for t in topics])
    now = datetime.now(timezone.utc)
    stale = [t for t in topics if not (t["id"] in latest and _is_fresh(latest[t["id"]], now, bool(t.get("is_watched"))))]
    if not stale:
        return latest
    provider = get_provider()
    fetched = []
    for t in stale:
        try:
            s = await provider.snapshot(t["external_id"], WINDOW_DAYS)
        except ListeningUnavailable as e:
            logger.warning("Couldn't refresh listening topic %s: %s", t["name"], e)
            continue
        change = s.get("volume_change_pct")
        fetched.append({
            **s,
            "topic_id": str(t["id"]),
            "is_spike": change is not None and change >= SPIKE_CHANGE_PCT,
        })
    if fetched:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO topic_snapshots (topic_id, window_days, positive_pct, neutral_pct, negative_pct, volume,
                                             volume_change_pct, is_spike, daily_volume, top_posts, is_mock)
                SELECT s.topic_id, $2, s.positive, s.neutral, s.negative, s.volume, s.volume_change_pct, s.is_spike,
                       ARRAY(SELECT jsonb_array_elements_text(s.daily_volume)::int), s.top_posts, $3
                FROM jsonb_to_recordset($1::jsonb) AS s(
                    topic_id uuid, positive numeric, neutral numeric, negative numeric, volume int,
                    volume_change_pct numeric, is_spike boolean, daily_volume jsonb, top_posts jsonb
                )
                """,
                json.dumps(fetched), WINDOW_DAYS, provider.is_mock,
            )
        latest = await _latest_snapshots([t["id"] for t in topics])
    return latest


def _snapshot_model(s: Dict[str, Any]) -> TopicSnapshot:
    posts = s["top_posts"]
    return TopicSnapshot(
        id=s["id"],
        window_days=s["window_days"],
        positive_pct=float(s["positive_pct"]),
        neutral_pct=float(s["neutral_pct"]),
        negative_pct=float(s["negative_pct"]),
        net_sentiment=round(float(s["positive_pct"]) - float(s["negative_pct"]), 1),
        volume=s["volume"],
        volume_change_pct=float(s["volume_change_pct"]) if s["volume_change_pct"] is not None else None,
        is_spike=s["is_spike"],
        daily_volume=list(s["daily_volume"] or []),
        top_posts=json.loads(posts) if isinstance(posts, str) else (posts or []),
        fetched_at=s["fetched_at"],
    )


# ---------------------------------------------------------------------------
# The post
# ---------------------------------------------------------------------------

async def _load(submission_id: UUID) -> Dict[str, Any]:
    row = await fetch_one(
        """
        SELECT s.id, s.title, s.caption_text, s.brand_code, s.audience, s.platforms, s.current_version,
               b.name AS brand_name, v.transcript
        FROM content_submissions s
        LEFT JOIN brands b ON b.code = s.brand_code
        LEFT JOIN LATERAL (
            SELECT transcript FROM content_submission_files f
            WHERE f.submission_id = s.id AND f.kind = 'MAIN' AND f.deleted_at IS NULL AND f.transcript_status = 'COMPLETED'
            ORDER BY f.created_at DESC LIMIT 1
        ) v ON true
        WHERE s.id = $1 AND s.deleted_at IS NULL
        """,
        submission_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Submission not found")
    row = dict(row)
    transcript = row["transcript"]
    segments = json.loads(transcript) if isinstance(transcript, str) else (transcript or [])
    row["transcript_text"] = " ".join(str(s.get("text", "")) for s in segments)
    row["text"] = "\n".join(p for p in [row["title"], row["caption_text"], row["transcript_text"]] if p)[:MAX_TEXT_CHARS]
    return row


async def _gather(submission_id: UUID) -> Tuple[Dict[str, Any], PublicMood]:
    """The post, its matched topics and their current snapshots."""
    row = await _load(submission_id)
    provider = get_provider()
    mood = PublicMood(source="Sprout Social", is_mock=provider.is_mock, connected=True)
    try:
        await _sync_topics()
    except ListeningUnavailable as e:
        mood.connected = False
        mood.message = str(e)
    topics = await _load_topics()
    meaning = await _meaning_scores(row, len(topics))
    mood.matched_by_meaning = meaning is not None
    dismissed = await fetch_all(
        """
        SELECT d.topic_id, t.name, u.full_name AS by_name, d.created_at AS at
        FROM public_mood_topic_dismissals d
        JOIN listening_topics t ON t.id = d.topic_id
        LEFT JOIN users u ON u.id = d.user_id
        WHERE d.submission_id = $1
        ORDER BY d.created_at
        """,
        submission_id,
    )
    mood.dismissed = [DismissedTopic(**dict(d)) for d in dismissed]
    left_out = {d["topic_id"] for d in dismissed}
    matches = _match(row["text"], [t for t in topics if t["id"] not in left_out], meaning)
    if not matches:
        if mood.connected:
            mood.message = "None of the topics being listened to come up in this post."
        return row, mood
    snaps = await _fresh_snapshots([t for t, _, _, _ in matches])
    mood.topics = [
        MatchedTopic(
            topic_id=t["id"],
            name=t["name"],
            matched_terms=terms,
            similarity=round(score, 3) if score is not None else None,
            matched_by=("BOTH" if terms and close else "KEYWORD" if terms else "MEANING"),
            snapshot=_snapshot_model(snaps[t["id"]]) if t["id"] in snaps else None,
        )
        for t, terms, score, close in matches
    ]
    return row, mood


def _input_hash(row: Dict[str, Any], mood: PublicMood) -> str:
    payload = {
        "prompt": PROMPT_VERSION,
        "text": row["text"],
        "audience": row["audience"],
        "platforms": sorted(row["platforms"] or []),
        "snapshots": sorted(str(t.snapshot.id) for t in mood.topics if t.snapshot),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _summary_model(r: Dict[str, Any], current_hash: Optional[str]) -> PublicMoodSummary:
    result = r["result"]
    return PublicMoodSummary(
        id=r["id"],
        version_number=r["version_number"],
        result=PublicMoodResult.model_validate(json.loads(result) if isinstance(result, str) else result),
        model=r["model"],
        is_mock=r["is_mock"],
        created_at=r["created_at"],
        is_stale=current_hash is not None and r["input_hash"] != current_hash,
    )


async def _with_feedback(summary: PublicMoodSummary, viewer: Optional[UUID]) -> PublicMoodSummary:
    row = await fetch_one(
        """
        SELECT count(*) FILTER (WHERE verdict = 'USEFUL') AS useful,
               count(*) FILTER (WHERE verdict = 'NOT_USEFUL') AS not_useful,
               max(verdict) FILTER (WHERE user_id = $2) AS mine
        FROM public_mood_feedback WHERE summary_id = $1
        """,
        summary.id, viewer,
    )
    summary.useful_count, summary.not_useful_count, summary.my_verdict = row["useful"], row["not_useful"], row["mine"]
    return summary


async def get_public_mood(submission_id: UUID, viewer: Optional[UUID] = None) -> PublicMood:
    row, mood = await _gather(submission_id)
    saved = await fetch_one(
        """
        SELECT * FROM public_mood_summaries
        WHERE submission_id = $1 AND version_number = $2 AND is_mock = $3
        ORDER BY created_at DESC LIMIT 1
        """,
        submission_id, max(row["current_version"] or 0, 1), mood.is_mock,
    )
    if saved:
        mood.summary = await _with_feedback(_summary_model(dict(saved), _input_hash(row, mood)), viewer)
    return mood


async def dismiss_topic(submission_id: UUID, topic_id: UUID, user_id: UUID) -> PublicMood:
    """Leave a topic out for this post, remembering which keywords tied it there (to spot noisy ones)."""
    row, mood = await _gather(submission_id)
    terms = next((t.matched_terms for t in mood.topics if t.topic_id == topic_id), None)
    if terms is None:
        raise HTTPException(status_code=404, detail="That topic isn't matched to this post")
    await get_pool().execute(
        """
        INSERT INTO public_mood_topic_dismissals (submission_id, topic_id, matched_terms, user_id)
        VALUES ($1, $2, $3, $4) ON CONFLICT (submission_id, topic_id) DO NOTHING
        """,
        submission_id, topic_id, terms, user_id,
    )
    return await get_public_mood(submission_id, user_id)


async def restore_topic(submission_id: UUID, topic_id: UUID, user_id: UUID) -> PublicMood:
    await get_pool().execute(
        "DELETE FROM public_mood_topic_dismissals WHERE submission_id = $1 AND topic_id = $2", submission_id, topic_id
    )
    return await get_public_mood(submission_id, user_id)


async def rate_summary(submission_id: UUID, summary_id: UUID, user_id: UUID, data: Optional[MoodFeedbackIn]) -> PublicMood:
    """Say whether a summary helped (or take the vote back with no data)."""
    owner = await fetch_one("SELECT submission_id FROM public_mood_summaries WHERE id = $1", summary_id)
    if not owner or owner["submission_id"] != submission_id:
        raise HTTPException(status_code=404, detail="Summary not found")
    if data is None:
        await get_pool().execute("DELETE FROM public_mood_feedback WHERE summary_id = $1 AND user_id = $2", summary_id, user_id)
    else:
        await get_pool().execute(
            """
            INSERT INTO public_mood_feedback (summary_id, user_id, verdict, note) VALUES ($1, $2, $3, $4)
            ON CONFLICT (summary_id, user_id) DO UPDATE SET verdict = EXCLUDED.verdict, note = EXCLUDED.note, created_at = NOW()
            """,
            summary_id, user_id, data.verdict, (data.note or "").strip() or None,
        )
    return await get_public_mood(submission_id, user_id)


# ---------------------------------------------------------------------------
# Managing topics
# ---------------------------------------------------------------------------

async def list_topics(refresh: bool = False) -> List[ListeningTopic]:
    """Every live topic with its current mood (refreshing stale snapshots) and how often it was matched wrongly."""
    await _sync_topics(force=refresh)
    topics = await _load_topics()
    snaps = await _fresh_snapshots(topics) if topics else {}
    stats = {
        r["topic_id"]: r for r in await fetch_all(
            """
            WITH terms AS (SELECT topic_id, unnest(matched_terms) AS term FROM public_mood_topic_dismissals)
            SELECT d.topic_id, count(*) AS n,
                   ARRAY(SELECT term FROM terms t WHERE t.topic_id = d.topic_id
                         GROUP BY term ORDER BY count(*) DESC, term LIMIT 5) AS terms
            FROM public_mood_topic_dismissals d
            GROUP BY d.topic_id
            """
        )
    }
    out = []
    for t in topics:
        st = stats.get(t["id"])
        out.append(ListeningTopic(
            id=t["id"], name=t["name"], description=t["description"], keywords=t["source_keywords"],
            extra_keywords=list(t["extra_keywords"] or []), excluded_keywords=list(t["excluded_keywords"] or []),
            is_watched=t["is_watched"], is_mock=t["is_mock"], synced_at=t["synced_at"],
            snapshot=_snapshot_model(snaps[t["id"]]) if t["id"] in snaps else None,
            dismissed_count=st["n"] if st else 0,
            dismissed_terms=[x for x in (st["terms"] if st else []) if x],
        ))
    return out


def _clean_words(words: List[str]) -> List[str]:
    return list(dict.fromkeys(w.strip() for w in words if w and w.strip() and _normalize(w)))


async def update_topic(topic_id: UUID, data: ListeningTopicUpdate, user_id: UUID) -> ListeningTopic:
    row = await fetch_one(
        """
        UPDATE listening_topics
        SET extra_keywords = $2, excluded_keywords = $3, is_watched = $4, updated_by = $5, updated_at = NOW()
        WHERE id = $1 AND deleted_at IS NULL
        RETURNING id
        """,
        topic_id, _clean_words(data.extra_keywords), _clean_words(data.excluded_keywords), data.is_watched, user_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Topic not found")
    _meaning_cache.clear()
    await _sync_topic_vectors()
    return next(t for t in await list_topics() if t.id == topic_id)


async def match_test(text: str) -> MatchTestResult:
    """Which topics a piece of text would match and why, with the near misses, for tuning keywords."""
    topics = await _load_topics()
    meaning = await _meaning_scores({"title": text, "caption_text": None, "transcript_text": ""}, len(topics))
    matched = {t["id"]: (terms, close) for t, terms, _, close in _match(text, topics, meaning)}
    padded = f" {_normalize(text)} "
    results = []
    for t in topics:
        terms = list({_normalize(k): k for k in reversed([t["name"], *t["keywords"]])
                      if _normalize(k) and _matches(k, padded)}.values())[::-1]
        score, margin = (meaning or {}).get(str(t["id"]), (None, None))
        hit = t["id"] in matched
        if hit:
            close = matched[t["id"]][1]
            if not terms:
                reason = "About it in meaning, without naming it"
            elif any(_unambiguous(k) for k in terms) or len(terms) >= 2:
                reason = "Named by a phrase, a name or several of its words" + (" (and close in meaning)" if close else "")
            elif meaning is None:
                reason = "Named by a keyword (meaning search unavailable)"
            elif close:
                reason = "Close in meaning, and mentions one of its words"
            else:
                reason = "Named by one everyday word, and the meaning agrees"
        elif not terms and margin is None:
            reason = "Not mentioned"
        elif terms:
            reason = "Only one everyday word, and the meaning doesn't agree (likely a different sense)"
        elif margin is not None and margin >= MEANING_MIN_MARGIN:
            reason = "Close in meaning, but other topics matched more strongly"
        else:
            reason = "Not mentioned, and not close enough in meaning"
        results.append(MatchTestTopic(
            topic_id=t["id"], name=t["name"], matched=hit, matched_terms=terms,
            similarity=round(score, 3) if score is not None else None,
            margin=round(margin, 3) if margin is not None else None, reason=reason,
        ))
    results.sort(key=lambda r: (not r.matched, -(r.margin if r.margin is not None else -1), r.name))
    return MatchTestResult(matched_by_meaning=meaning is not None, topics=results)


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------

def _enum(*values: str) -> Dict[str, Any]:
    return {"type": "STRING", "enum": list(values)}


RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "overall": _enum("FAVOURABLE", "MIXED", "UNFAVOURABLE", "NO_DATA"),
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
    "required": ["overall", "headline", "topics", "risks", "opportunities"],
}


def _prompt(row: Dict[str, Any], mood: PublicMood) -> str:
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
            "sample_posts": [{"sentiment": p.sentiment, "text": p.text} for p in s.top_posts],
        })
    return f"""You advise a social media reviewer at a publicly listed drone and technology company.
Below is a post that's about to be published, and social listening data on how people online currently feel about the
topics it touches. Say, briefly and concretely, how this post is likely to land given that mood: where it could be
misread or draw criticism, where it rides goodwill, and whether the timing is good (for example, a topic that's
spiking with negative mentions).

Rules:
- Base every statement on the post and the data given. Don't invent events, numbers or news.
- Advise; don't decide. Don't comment on legal compliance (a separate check does that).
- The headline is one sentence. Each topic reading is one or two sentences. Up to 3 risks and 3 opportunities.
- Use NO_DATA only if no topic data is given.
- timing_note only when spiking or shifting mentions matter for when to post; otherwise null.
- Weigh topics the post names above ones it only touches on. A sample post is one voice, not the trend: lean on the
  percentages and mention changes, and use sample posts only to say what kind of criticism or praise is going round.
- Today is {datetime.now(timezone.utc).date().isoformat()}.

POST
Brand: {row["brand_name"] or row["brand_code"] or "unknown"}
Audience: {row["audience"] or "general"}
Platforms: {", ".join(row["platforms"] or []) or "unspecified"}
Text (title, caption, then any transcript):
\"\"\"{row["text"]}\"\"\"

LISTENING DATA ({"Sprout Social" if not mood.is_mock else "Sprout Social (sample data)"})
{json.dumps(topics, indent=1)}
"""


async def summarise(submission_id: UUID, user_id: UUID) -> PublicMood:
    """Write (or reuse) the summary for the post's current version against the current moods."""
    row, mood = await _gather(submission_id)
    if not any(t.snapshot for t in mood.topics):
        raise HTTPException(status_code=422, detail=mood.message or "There's no listening data for this post's topics yet.")
    version = max(row["current_version"] or 0, 1)
    current_hash = _input_hash(row, mood)
    same = await fetch_one(
        """
        SELECT * FROM public_mood_summaries
        WHERE submission_id = $1 AND version_number = $2 AND input_hash = $3
        ORDER BY created_at DESC LIMIT 1
        """,
        submission_id, version, current_hash,
    )
    if same:
        mood.summary = await _with_feedback(_summary_model(dict(same), current_hash), user_id)
        return mood

    await ai_usage_service.reserve("PUBLIC_MOOD", user_id, submission_id)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        temperature=0,
    )
    try:
        response = await generate_content_with_fallback(_get_client(), _prompt(row, mood), config)
        result = PublicMoodResult.model_validate(json.loads(response.text or "{}"))
    except (ValueError, ValidationError) as e:
        logger.warning("Public mood summary was malformed: %s", e)
        raise HTTPException(status_code=502, detail="The AI summary came back malformed. Try again.")
    except RuntimeError as e:
        logger.warning("Public mood summary failed: %s", e)
        raise HTTPException(status_code=503, detail="The AI service is busy or out of quota. Try again later.")

    saved = await fetch_one(
        """
        INSERT INTO public_mood_summaries (submission_id, version_number, snapshot_ids, input_hash, result, model,
                                           is_mock, created_by)
        VALUES ($1, $2, $3::uuid[], $4, $5::jsonb, $6, $7, $8)
        RETURNING *
        """,
        submission_id, version, [t.snapshot.id for t in mood.topics if t.snapshot], current_hash,
        result.model_dump_json(), getattr(response, "model_version", None), mood.is_mock, user_id,
    )
    mood.summary = await _with_feedback(_summary_model(dict(saved), current_hash), user_id)
    return mood
