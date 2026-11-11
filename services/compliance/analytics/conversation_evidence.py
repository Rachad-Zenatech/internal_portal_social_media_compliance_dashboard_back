"""Conversation evidence for public mood: the news articles and X posts a post is compared with, and the saved
assessment of what they say about it. Advisory only, like the rest of public mood.

1. Collected items are kept (listening_items), whatever a scrape returns. An item that isn't in today's feed is not
   deleted; it stops being searchable only when it ages out (PUBLIC_MOOD_ITEM_WINDOW_DAYS). Every text an item has
   had is kept (listening_item_versions).
2. Searchable items are embedded into the Atlas vector store (kind X_CONVERSATION). Only new or changed text is
   embedded. After a batch that changed what is searchable, `evidence_version` changes.
3. For a post: Atlas finds up to PUBLIC_MOOD_RETRIEVAL_LIMIT candidates; duplicates and outdated ones are removed;
   each story is checked for whether it concerns the post's subject (topic terms, named companies/products/places,
   the team's excluded keywords); the mood is calculated from ALL verified stories. At most PUBLIC_MOOD_DISPLAY_LIMIT
   of them are returned as examples.
4. The assessment is saved with the exact item versions it used, and reused until the draft, the evidence, the
   matching policy, the embedding model or the clock (expiry) says otherwise.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from uuid import UUID

from models.compliance.public_mood_model import ConversationMatch, ConversationMood
from postgresql_db.database import fetch_all, fetch_one, get_pool
from services.ai.core import vector_store_service as store
from services.compliance.analytics import conversation_feedback as feedback
from services.compliance.analytics import mood_features, mood_model, mood_records
from services.compliance.analytics.listening_providers import (
    PUBLISHER_SUFFIX,
    classify_comment_sentiment,
    clean_headline,
    evaluate_publishing_decision,
)
from services.compliance.evaluation.compliance_service import _matches, _normalize

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


KIND = "X_CONVERSATION"
# Bump when the matching rules change, so saved assessments made under the old rules are redone.
POLICY_VERSION = "conversation-v2"
# Starting values to validate on real data, not proven thresholds.
RETRIEVAL_LIMIT = int(_env_float("PUBLIC_MOOD_RETRIEVAL_LIMIT", 50))
DISPLAY_LIMIT = int(_env_float("PUBLIC_MOOD_DISPLAY_LIMIT", 12))
ASSESSMENT_HOURS = _env_float("PUBLIC_MOOD_ASSESSMENT_HOURS", 24)
ITEM_WINDOW_DAYS = _env_float("PUBLIC_MOOD_ITEM_WINDOW_DAYS", 14)
# An item from the same topic as the post counts as relevant on meaning alone from this retrieval score up.
SAME_TOPIC_MIN_SCORE = _env_float("PUBLIC_MOOD_MEANING_MIN_SCORE", 0.84)
# Headlines this alike (share of words) are the same story.
SAME_STORY_JACCARD = 0.8


# ---------------------------------------------------------------------------
# Text: stable ids, hashes, stories, entities
# ---------------------------------------------------------------------------

_PUBLISHER = PUBLISHER_SUFFIX
_clean_headline = clean_headline


def story_key(text: str) -> str:
    return hashlib.sha1(_clean_headline(text).encode("utf-8")).hexdigest()


def item_key(topic_external_id: str, url: Optional[str], text: str) -> str:
    """Stable for an article or post across scrapes: its link when it has one (so an edited headline is the same item
    with a new version), otherwise its text."""
    basis = (url or "").strip() or _clean_headline(text)
    return hashlib.sha1(f"{topic_external_id}|{basis}".encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def parse_published(value: Any) -> Optional[datetime]:
    """An ISO timestamp or an RSS date as an aware datetime, else None. A bare date isn't trusted (the scraper has
    stamped 'today' on items whose date it didn't know)."""
    if not value or not isinstance(value, str) or len(value.strip()) <= 10:
        return None
    text = value.strip()
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _tokens(text: str) -> Set[str]:
    return {w for w in _clean_headline(text).split() if len(w) > 2}


def same_story(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if a["story_key"] == b["story_key"]:
        return True
    ta, tb = _tokens(a["text"]), _tokens(b["text"])
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= SAME_STORY_JACCARD


_ENTITY = re.compile(r"[A-Z][A-Za-z0-9&'.-]*(?:[ \t]+[A-Z][A-Za-z0-9&'.-]*)*")
_ENTITY_STOP = {
    "the", "a", "an", "and", "or", "in", "on", "of", "for", "to", "at", "by", "with", "from", "after", "as", "is",
    "are", "new", "news", "says", "say", "will", "this", "that", "over", "into", "its", "it", "how", "why", "what",
    "who", "video", "report", "reports", "breaking", "update", "live", "photos", "watch", "opinion",
    "next", "last", "first", "today", "yesterday", "tomorrow", "week", "weekly", "daily", "now", "here", "our", "your",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
    "december",
}


def _starts_sentence(text: str, pos: int) -> bool:
    """Whether the word at `pos` begins the text, a line, or a sentence (so its capital says nothing about it)."""
    before = text[:pos].rstrip(" \t")
    return not before or before[-1] in ".!?:;\n\r|\u2013\u2014-\"\u201c("


def entities_split(text: str) -> Tuple[Set[str], Set[str]]:
    """(firm, initial): named companies, products and places as capitalised phrases (lowercased).

    firm     names that can't just be the capital of a sentence: more than one word, a capital inside ("ZenaTech"),
             all capitals ("FAA"), or a capitalised word in the middle of a sentence.
    initial  a lone plain capitalised word that begins a headline, a line or a sentence ("Joby tests...", "Next week...").
             It may be a company or just the first word, so it counts only when the other side names it mid-sentence."""
    firm: Set[str] = set()
    initial: Set[str] = set()
    text = _PUBLISHER.sub("", text or "")
    for m in _ENTITY.finditer(text):
        words = [w.strip(".-'") for w in m.group(0).split()]
        while words and words[0].lower() in _ENTITY_STOP:
            words.pop(0)
        while words and words[-1].lower() in _ENTITY_STOP:
            words.pop()
        if not words:
            continue
        phrase = " ".join(words).lower()
        acronym = len(words) == 1 and words[0].isupper() and len(words[0]) >= 3
        if (len(phrase) < 4 and not acronym) or phrase in _ENTITY_STOP:
            continue
        if len(words) == 1 and _starts_sentence(text, m.start()) and not any(c.isupper() for c in words[0][1:]):
            initial.add(phrase)
        else:
            firm.add(phrase)
    return firm, initial


def entities(text: str) -> Set[str]:
    """The names that stand on their own (see entities_split)."""
    return entities_split(text)[0]


def _segment_with(text: str, needle: str) -> str:
    """The part of the text (sentence or headline half) that contains the term, for showing why it matched."""
    for seg in re.split(r"(?<=[.!?])\s+|\s[-|–—]\s", text or ""):
        if _matches(needle, f" {_normalize(seg)} "):
            return seg.strip()[:300]
    return (text or "")[:300]


# ---------------------------------------------------------------------------
# Relevance: is this item about the post's subject?
# ---------------------------------------------------------------------------

@dataclass
class PostContext:
    text: str
    padded: str
    entities: Set[str]
    brand: Set[str] = field(default_factory=set)    # the post's own company/brand names
    initial: Set[str] = field(default_factory=set)  # lone words that only begin a sentence in the post


def post_context_of(row: Dict[str, Any]) -> PostContext:
    text = "\n".join(str(row.get(k) or "") for k in ("title", "caption_text", "script_text", "transcript_text")
                     ).strip()
    # The post's own company is part of its subject even when the text only names its products.
    brand = {str(row.get(k)).strip().lower() for k in ("brand_name", "brand_code") if row.get(k)}
    brand = {b for b in brand if len(b) >= 4}
    firm, initial = entities_split(text)
    return PostContext(text=text, padded=f" {_normalize(text)} ", entities=firm | brand, brand=brand, initial=initial)


def _unambiguous(term: str) -> bool:
    """A phrase or a coined name rarely means anything else; one everyday word might."""
    return len(_normalize(term).split()) > 1 or term != term.lower()


def verify_relevance(item_text: str, topic: Optional[Dict[str, Any]], post: PostContext,
                     matched_topic_ids: Set[Any], similarity: float) -> Tuple[str, str, str]:
    """(status, reason, supporting text). RELEVANT only with a stated connection to the post:
    - the team excluded a word the item contains for its topic -> UNRELATED;
    - a topic phrase or name (or two topic words) in both item and post -> RELEVANT;
    - a company, product or place named in both -> RELEVANT;
    - the item's topic is one the post matched and it is close in meaning -> RELEVANT;
    - anything else (including a single everyday word in common) -> UNCERTAIN, left out of the mood."""
    padded = f" {_normalize(item_text)} "
    shared: List[str] = []
    if topic:
        for k in topic.get("excluded") or []:
            if _normalize(k) and _matches(k, padded):
                return ("UNRELATED", f"Contains '{k}', which the team excluded for {topic['name']}.",
                        _segment_with(item_text, k))
        shared = [k for k in topic.get("terms") or [] if _normalize(k) and _matches(k, padded)
                  and _matches(k, post.padded)]
        if any(_unambiguous(k) for k in shared) or len(shared) >= 2:
            return ("RELEVANT", f"Both mention {', '.join(repr(k) for k in shared[:3])} ({topic['name']}).",
                    _segment_with(item_text, shared[0]))
    item_firm, item_initial = entities_split(item_text)
    # A name stands when it is firm on one side and appears on the other; a sentence-initial word counts only against
    # the other side's firm name (never "Next" against "Next").
    common = sorted((item_firm & (post.entities | post.initial)) | (item_initial & post.entities))
    # A name in common is a connection when it is the post's own company, a multi-word name, or there are two of
    # them. One shared single word ("Toronto", "Orlando") is a coincidence as often as a link: left uncertain.
    strong = [e for e in common if e in post.brand or " " in e]
    if strong or len(common) >= 2:
        names = strong or common
        return ("RELEVANT", f"Both name {', '.join(names[:3])}.", _segment_with(item_text, names[0]))
    if common:
        return ("UNCERTAIN", f"Only one name in common ('{common[0]}'), which is not enough on its own.",
                _segment_with(item_text, common[0]))
    if topic and topic.get("id") in matched_topic_ids and similarity >= SAME_TOPIC_MIN_SCORE:
        return ("RELEVANT", f"Same topic as the post ({topic['name']}) and close in meaning.", item_text[:300])
    if shared:
        return ("UNCERTAIN", f"Only the everyday word '{shared[0]}' in common.", _segment_with(item_text, shared[0]))
    return ("UNCERTAIN", "Close in meaning, but no shared topic term, company, product or place.", item_text[:300])


# ---------------------------------------------------------------------------
# Assessment (pure): retrieved candidates -> mood
# ---------------------------------------------------------------------------

@dataclass
class EvidenceRow:
    item: Dict[str, Any]
    similarity: float
    margin: float
    status: str
    reason: str
    supporting: str
    story_key: str
    counted: bool = False
    displayed: bool = False
    duplicates: int = 0


MIN_STORIES = 10  # fewer distinct stories than this is a small sample (evaluate_publishing_decision won't say POST)


def assess(hits: List[Dict[str, Any]], items: Dict[str, Dict[str, Any]], topics_by_ext: Dict[str, Dict[str, Any]],
           post: PostContext, matched_topic_ids: Set[Any], rejected: Optional[Set[str]] = None
           ) -> Tuple[str, ConversationMood, List[EvidenceRow]]:
    """(status, mood, every candidate's evidence row). The status is READY, or NO_RELEVANT_MATCHES when nothing
    retrieved could be verified as about the post: that is a WAIT with its own reason, never a POST.
    Retrieval score only orders candidates; relevance is decided by verify_relevance, so several equally relevant
    items stay eligible however alike their scores are. The median margin is kept for diagnostics. Items a reviewer
    marked unrelated (`rejected`, item ids) are left out and recorded as UNRELATED."""
    rejected = rejected or set()
    scores = [float(h.get("score", 0)) for h in hits]
    median = statistics.median(scores) if scores else 0.0
    outdated = 0
    candidates: List[Tuple[Dict[str, Any], float]] = []
    rows: List[EvidenceRow] = []
    for h in sorted(hits, key=lambda h: -float(h.get("score", 0))):
        meta = h.get("meta") or {}
        item = items.get(str(meta.get("item_id")))
        if not item or not item["is_eligible"] or item["content_hash"] != meta.get("content_hash"):
            outdated += 1  # gone, aged out, or the vector is from an earlier text
            continue
        score = float(h.get("score", 0))
        if str(item["id"]) in rejected:
            rows.append(EvidenceRow(
                item=item, similarity=score, margin=round(score - median, 4), status="UNRELATED",
                reason="A reviewer marked this unrelated.", supporting="", story_key=item["story_key"],
            ))
            continue
        candidates.append((item, score))

    # One underlying story per group; the best-scoring item represents it.
    groups: List[List[Tuple[Dict[str, Any], float]]] = []
    for item, score in candidates:
        for g in groups:
            if same_story(g[0][0], item):
                g.append((item, score))
                break
        else:
            groups.append([(item, score)])

    for g in groups:
        rep, rep_score = g[0]
        topic = topics_by_ext.get(rep["topic_external_id"])
        status, reason, supporting = verify_relevance(rep["text"], topic, post, matched_topic_ids, rep_score)
        for i, (item, score) in enumerate(g):
            rows.append(EvidenceRow(
                item=item, similarity=score, margin=round(score - median, 4), status=status, reason=reason,
                supporting=supporting, story_key=item["story_key"], counted=(i == 0 and status == "RELEVANT"),
                duplicates=len(g) - 1 if i == 0 else 0,
            ))
    counted = [r for r in rows if r.counted]
    for r in counted[:DISPLAY_LIMIT]:  # groups are in best-score-first order
        r.displayed = True
    counts = {
        "retrieved_count": len(hits),
        "verified_relevant_count": sum(1 for r in rows if r.status == "RELEVANT"),
        "distinct_story_count": len(counted),
        "displayed_count": sum(1 for r in rows if r.displayed),
        "uncertain_count": sum(1 for r in rows if r.status == "UNCERTAIN"),
        "unrelated_count": sum(1 for r in rows if r.status == "UNRELATED"),
        "outdated_count": outdated,
        "retrieval_capped": len(hits) >= RETRIEVAL_LIMIT,
    }
    if not counted:
        return "NO_RELEVANT_MATCHES", ConversationMood(
            status="NO_RELEVANT_MATCHES", count=0, recommendation="WAIT",
            recommendation_reason="Conversations were found, but none could be verified as being about this post, so "
                                  "nothing here supports posting. Review it manually.",
            reason_codes=["NO_RELEVANT_EVIDENCE", "REVIEW_REQUIRED"], **counts,
        ), rows

    n = len(counted)
    by = {s: sum(1 for r in counted if r.item["sentiment"] == s) for s in ("POSITIVE", "NEUTRAL", "NEGATIVE")}
    pos, neg = round(by["POSITIVE"] / n * 100, 1), round(by["NEGATIVE"] / n * 100, 1)
    neu = round(max(0.0, 100.0 - pos - neg), 1)
    # Sufficiency is judged on every verified story, never on how many are displayed.
    rec, reason = evaluate_publishing_decision(
        positive_pct=pos, neutral_pct=neu, negative_pct=neg, volume=n, is_spike=False,
        top_posts=[{"text": r.item["text"]} for r in counted],
    )
    codes = []
    if rec != "POST":
        codes.append("REVIEW_REQUIRED")
    if rec == "HOLD":
        codes.append("HOLD_NEGATIVE")
    if n < MIN_STORIES:
        codes.append("SMALL_SAMPLE")
    if counts["retrieval_capped"]:
        codes.append("RETRIEVAL_CAPPED")
    mood = ConversationMood(
        status="READY", reason_codes=codes, count=n, positive_pct=pos, neutral_pct=neu, negative_pct=neg,
        recommendation=rec, recommendation_reason=reason, matches=[match_of(r) for r in rows if r.displayed], **counts,
    )
    return "READY", mood, rows


def match_of(r: EvidenceRow) -> ConversationMatch:
    it = r.item
    return ConversationMatch(
        topic=it["topic_name"], network=it.get("network") or "X", author=it.get("author") or "", text=it["text"],
        sentiment=it["sentiment"], similarity=round(r.similarity, 3), margin=round(r.margin, 3),
        item_id=it["id"], url=it.get("url"), published_at=it.get("published_at"),
        similarity_score=round(r.similarity, 3), relevance_status=r.status, relevance_reason=r.reason,
        supporting_text=r.supporting, also_reported_by=r.duplicates, category=it.get("category"),
        collected_by=it.get("collected_by"),
    )


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

def context_hash(row: Dict[str, Any]) -> str:
    """Every field the search text is built from, in full (the search text itself is shortened)."""
    payload = {k: row.get(k) or "" for k in (
        "brand_name", "brand_code", "audience", "title", "caption_text", "script_text", "transcript_text")}
    payload["platforms"] = sorted(row.get("platforms") or [])
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def policy_version() -> str:
    return f"{POLICY_VERSION}:r{RETRIEVAL_LIMIT}"


def topics_fingerprint(topics: Iterable[Dict[str, Any]]) -> str:
    """The team's keyword tuning decides relevance, so changing it must redo the assessment."""
    rows = sorted((t["external_id"], sorted(t.get("terms") or []), sorted(t.get("excluded") or []))
                  for t in topics)
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()[:16]


def cache_key(ctx_hash: str, evidence_version: str, embedding_version: str, matched_ids: Iterable[Any],
              topics_fp: str, rejected_ids: Iterable[Any] = ()) -> str:
    """What an assessment depends on. Items reviewers marked unrelated are part of it: marking one redoes the
    assessment without it."""
    return hashlib.sha256(json.dumps([
        ctx_hash, evidence_version, policy_version(), embedding_version, sorted(str(i) for i in matched_ids), topics_fp,
        sorted(str(i) for i in rejected_ids),
    ]).encode("utf-8")).hexdigest()


def post_search_text(row: Dict[str, Any], limit: int = 6000) -> str:
    """What the post is, as one context block to embed (the counterpart of _vector_text)."""
    body = (row.get("script_text") or row.get("transcript_text") or "")[:1500]
    return (
        "Post Context:\n"
        f"Brand: {row.get('brand_name') or row.get('brand_code') or ''}\n"
        f"Audience: {row.get('audience') or ''}\n"
        f"Platforms: {', '.join(row.get('platforms') or [])}\n"
        f"Title: {row.get('title') or ''}\n"
        f"Caption: {row.get('caption_text') or ''}\n"
        f"Script or transcript: {body}"
    ).strip()[:limit]


# ---------------------------------------------------------------------------
# Collected items (Postgres) and the vector index
# ---------------------------------------------------------------------------

async def get_state(key: str) -> Optional[str]:
    row = await fetch_one("SELECT value FROM listening_state WHERE key = $1", key)
    return row["value"] if row else None


async def _set_state(key: str, value: str) -> None:
    await get_pool().execute(
        """
        INSERT INTO listening_state (key, value) VALUES ($1, $2)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        key, value,
    )


def _num(x: Any) -> Decimal:
    """A float for a NUMERIC column."""
    return Decimal(str(round(float(x), 4)))


def _vector_text(topic: str, network: str, author: str, text: str) -> str:
    """What gets embedded for an item: topic, where it was said and what was said (the same kind of context the post
    side uses). Sentiment is left out on purpose: it is stored beside the vector, so it can't pull a post towards
    'angry' or 'happy' wording."""
    return f"Public Conversation Context:\nTopic: {topic}\nSource: {network} {author}\nText: {text}"


@dataclass
class FeedItem:
    topic_external_id: str
    topic_name: str
    network: str
    author: str
    url: Optional[str]
    text: str
    published_at: Optional[datetime]
    category: Optional[str] = None      # 'market' (a trend topic) or a company watch category
    collected_by: Optional[str] = None  # the query or account that found it
    key: str = field(init=False)

    def __post_init__(self) -> None:
        self.key = item_key(self.topic_external_id, self.url, self.text)


def feed_items(topics: List[Dict[str, Any]]) -> List[FeedItem]:
    """The feed's items, once each (by stable key)."""
    out: Dict[str, FeedItem] = {}
    for t in topics:
        for c in t.get("comments") or []:
            text = (c.get("text") or "").strip()
            if not text:
                continue
            it = FeedItem(
                topic_external_id=str(t.get("external_id")), topic_name=str(t.get("name") or ""),
                network=str(c.get("network") or "X"), author=str(c.get("author") or ""),
                url=(c.get("url") or None), text=text, published_at=parse_published(c.get("published_at")),
                category=c.get("category"), collected_by=c.get("collected_by"),
            )
            out.setdefault(it.key, it)
    return list(out.values())


async def record_feed(topics: List[Dict[str, Any]], provider: str, status: Optional[str] = None,
                      note: Optional[str] = None, index: bool = True) -> Dict[str, Any]:
    """Save the feed's items (new ones, changed texts, and a fresh 'last seen' on the rest), then update what is
    searchable (unless index=False: indexing takes a while under the embedding quota, so a caller can record first,
    refresh what depends on the recorded items, and index afterwards). Nothing is deleted because it is missing from
    this feed."""
    items = feed_items(topics)
    run_status = status or ("SUCCESS" if items else "EMPTY")
    pool = get_pool()
    run_id = await pool.fetchval(
        "INSERT INTO listening_collection_runs (provider, status, item_count, note) VALUES ($1, $2, $3, $4) RETURNING id",
        provider, run_status, len(items), note,
    )
    existing = {
        r["item_key"]: r for r in await fetch_all(
            "SELECT id, item_key, content_hash FROM listening_items WHERE item_key = ANY($1::text[])",
            [i.key for i in items],
        )
    }
    new = changed = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for it in items:
                h = content_hash(it.text)
                ex = existing.get(it.key)
                if ex is not None and ex["content_hash"] == h:
                    await conn.execute(
                        """
                        UPDATE listening_items
                        SET last_seen_at = NOW(), last_run_id = $2, url = COALESCE($3, url),
                            published_at = COALESCE(published_at, $4), category = COALESCE($5, category),
                            collected_by = COALESCE($6, collected_by)
                        WHERE id = $1
                        """,
                        ex["id"], run_id, it.url, it.published_at, it.category, it.collected_by,
                    )
                    continue
                # New text: score it once (the sentiment model is CPU work, so off the event loop).
                cls = await asyncio.to_thread(classify_comment_sentiment, it.text)
                if ex is None:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO listening_items (item_key, topic_external_id, topic_name, network, author, url, text,
                            content_hash, story_key, sentiment, compound, crisis_flags, published_at, last_run_id,
                            category, collected_by)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16) RETURNING id, version
                        """,
                        it.key, it.topic_external_id, it.topic_name, it.network, it.author, it.url, it.text, h,
                        story_key(it.text), cls["sentiment"], _num(cls["compound"]), cls["crisis_flags"],
                        it.published_at, run_id, it.category, it.collected_by,
                    )
                    new += 1
                else:
                    row = await conn.fetchrow(
                        """
                        UPDATE listening_items
                        SET text = $2, content_hash = $3, story_key = $4, sentiment = $5, compound = $6,
                            crisis_flags = $7, version = version + 1, indexed_hash = NULL, last_seen_at = NOW(),
                            last_run_id = $8, url = COALESCE($9, url), published_at = COALESCE($10, published_at),
                            category = COALESCE($11, category), collected_by = COALESCE($12, collected_by)
                        WHERE id = $1 RETURNING id, version
                        """,
                        ex["id"], it.text, h, story_key(it.text), cls["sentiment"], _num(cls["compound"]),
                        cls["crisis_flags"], run_id, it.url, it.published_at, it.category, it.collected_by,
                    )
                    changed += 1
                await conn.execute(
                    """
                    INSERT INTO listening_item_versions (item_id, version, content_hash, text, sentiment, compound)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    row["id"], row["version"], h, it.text, cls["sentiment"], _num(cls["compound"]),
                )
    await pool.execute("UPDATE listening_collection_runs SET finished_at = NOW() WHERE id = $1", run_id)
    await refresh_eligibility()
    indexed = await sync_index() if index else {"index_status": "NOT_RUN"}
    return {"run_id": str(run_id), "status": run_status, "items": len(items), "new": new, "changed": changed, **indexed}


async def topic_daily_counts(topic_external_id: str, days: int = 7) -> List[int]:
    """Distinct stories per day for a topic over the last `days` days (oldest first, ending today, UTC), from everything
    collected so far, not just the latest scrape: a story counts on the day it was published (or first seen, when it has
    no date), once however many outlets or scrapes carried it. These are mentions we collected, not all mentions."""
    rows = await fetch_all(
        """
        WITH stories AS (
            SELECT DISTINCT ON (story_key) COALESCE(published_at, first_seen_at) AS at
            FROM listening_items
            WHERE topic_external_id = $1
              AND COALESCE(published_at, first_seen_at) >= date_trunc('day', now() AT TIME ZONE 'utc') - ($2::int - 1) * interval '1 day'
            ORDER BY story_key, COALESCE(published_at, first_seen_at) DESC
        )
        SELECT (date_trunc('day', at AT TIME ZONE 'utc'))::date AS day, count(*) AS n FROM stories GROUP BY 1
        """,
        topic_external_id, days,
    )
    by_day = {r["day"]: int(r["n"]) for r in rows}
    today = datetime.now(timezone.utc).date()
    return [by_day.get(today - timedelta(days=days - 1 - i), 0) for i in range(days)]


async def refresh_eligibility() -> None:
    """An item is searchable while it is recent (published, or first seen, within the window), whether or not the
    latest feed still contains it."""
    await get_pool().execute(
        """
        UPDATE listening_items SET is_eligible = (COALESCE(published_at, first_seen_at) >= NOW() - ($1::float8 * INTERVAL '1 day'))
        WHERE is_eligible IS DISTINCT FROM (COALESCE(published_at, first_seen_at) >= NOW() - ($1::float8 * INTERVAL '1 day'))
        """,
        ITEM_WINDOW_DAYS,
    )


INDEX_CHUNK = int(_env_float("PUBLIC_MOOD_INDEX_CHUNK", 60))
# Gemini's free tier allows 100 embedding texts a minute: after a full chunk, wait for the window to clear.
INDEX_PAUSE_SECONDS = _env_float("PUBLIC_MOOD_INDEX_PAUSE_SECONDS", 45)
INDEX_MAX_SECONDS = _env_float("PUBLIC_MOOD_INDEX_MAX_SECONDS", 300)
INDEX_RETRIES = 3


def _quota_wait(error: Exception) -> Optional[float]:
    """Seconds to wait if this is a rate-limit error (Gemini says how long), else None."""
    text = str(error)
    if "429" not in text and "RESOURCE_EXHAUSTED" not in text:
        return None
    m = re.search(r"retry in ([0-9.]+)s", text) or re.search(r"'retryDelay': '([0-9.]+)s'", text)
    return (float(m.group(1)) + 2.0) if m else INDEX_PAUSE_SECONDS


async def sync_index() -> Dict[str, Any]:
    """Make the vector index match the eligible items: embed new or changed texts (in chunks, pausing for the
    embedding quota and retrying a rate-limited chunk), remove vectors of items that aged out (their rows stay).
    Progress is saved after every chunk, so an interrupted run continues where it stopped. indexing_status is
    COMPLETE only when every eligible item is indexed; evidence_version changes whenever what is searchable changed.
    Atlas takes a few seconds to make new vectors searchable after this returns."""
    if not store.is_enabled():
        return {"indexed": 0, "dropped": 0, "index_status": "OFF"}
    model = store.embedding_model()
    rows = [dict(r) for r in await fetch_all(
        "SELECT id, topic_name, network, author, text, content_hash, version, is_eligible, indexed_hash FROM listening_items "
        "WHERE is_eligible OR indexed_hash IS NOT NULL"
    )]
    to_index = [r for r in rows if r["is_eligible"] and r["indexed_hash"] != f"{model}:{r['content_hash']}"]
    to_drop = [r for r in rows if not r["is_eligible"] and r["indexed_hash"] is not None]
    pool = get_pool()
    started = time.monotonic()
    indexed = stray = 0
    dropped = 0
    status = "COMPLETE"
    try:
        for start in range(0, len(to_index), INDEX_CHUNK):
            chunk = to_index[start:start + INDEX_CHUNK]
            if start and time.monotonic() - started + INDEX_PAUSE_SECONDS > INDEX_MAX_SECONDS:
                raise TimeoutError("indexing time limit reached; the rest continues on the next run")
            docs = [
                store.VectorDoc(
                    kind=KIND, source_id=str(r["id"]), title=r["topic_name"],
                    text=_vector_text(r["topic_name"], r["network"] or "X", r["author"] or "", r["text"]),
                    meta={"item_id": str(r["id"]), "content_hash": r["content_hash"], "version": r["version"]},
                )
                for r in chunk
            ]
            for attempt in range(INDEX_RETRIES + 1):
                try:
                    await store.upsert(docs)
                    break
                except Exception as e:
                    wait = _quota_wait(e)
                    if wait is None or attempt == INDEX_RETRIES or time.monotonic() - started + wait > INDEX_MAX_SECONDS:
                        raise
                    logger.info("Embedding quota reached; waiting %.0fs before retrying %d texts", wait, len(docs))
                    await asyncio.sleep(wait)
            await pool.executemany(
                "UPDATE listening_items SET indexed_hash = $2 WHERE id = $1 AND content_hash = $3",
                [(r["id"], f"{model}:{r['content_hash']}", r["content_hash"]) for r in chunk],
            )
            indexed += len(chunk)
            if start + INDEX_CHUNK < len(to_index):
                await asyncio.sleep(INDEX_PAUSE_SECONDS)
        if to_drop:
            await store.delete([f"{KIND}:{r['id']}" for r in to_drop])
            await pool.executemany("UPDATE listening_items SET indexed_hash = NULL WHERE id = $1",
                                   [(r["id"],) for r in to_drop])
            dropped = len(to_drop)
        # The index mirrors the eligible items exactly: vectors of anything else (an older format, removed items)
        # would otherwise take up retrieval slots. Only once every eligible item is in, so nothing is removed
        # before its replacement exists.
        stray = await store.delete_kind_except(KIND, [f"{KIND}:{r['id']}" for r in rows if r["is_eligible"]])
    except Exception as e:
        logger.warning("Couldn't finish updating scraped conversations in the vector store (topic matching still works): %s", e)
        status = "INCOMPLETE"
    if indexed or dropped or stray:
        await _set_state("evidence_version", uuid.uuid4().hex)
    await _set_state("indexing_status", status)
    return {"indexed": indexed, "pending": len(to_index) - indexed, "dropped": dropped, "stray_removed": stray,
            "index_status": status}


# ---------------------------------------------------------------------------
# Saved assessments
# ---------------------------------------------------------------------------

def _mood_from_saved(status: str, result: Any) -> Optional[ConversationMood]:
    if status not in ("READY", "NO_RELEVANT_MATCHES"):
        return None
    data = json.loads(result) if isinstance(result, str) else dict(result)
    return ConversationMood.model_validate(data)


async def _save_assessment(submission_id: UUID, version_number: int, key: str, ctx_hash: str, evidence_version: str,
                           embedding_version: str, status: str, mood: ConversationMood, rows: List[EvidenceRow],
                           expires_at: datetime) -> ConversationMood:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            saved = await conn.fetchrow(
                """
                INSERT INTO public_mood_assessments (submission_id, version_number, cache_key, context_hash,
                    evidence_version, policy_version, embedding_version, status, result, expires_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'{}'::jsonb,$9) RETURNING id, assessed_at
                """,
                submission_id, version_number, key, ctx_hash, evidence_version, policy_version(), embedding_version,
                status, expires_at,
            )
            mood.assessment_id, mood.assessed_at, mood.expires_at = saved["id"], saved["assessed_at"], expires_at
            mood.evidence_version = evidence_version
            await conn.execute("UPDATE public_mood_assessments SET result = $2::jsonb WHERE id = $1",
                               saved["id"], json.dumps(mood.model_dump(mode="json")))
            if rows:
                await conn.executemany(
                    """
                    INSERT INTO public_mood_assessment_items (assessment_id, item_id, item_version, content_hash,
                        similarity_score, margin, relevance_status, relevance_reason, supporting_text, story_key,
                        counted, displayed)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    """,
                    [(saved["id"], r.item["id"], r.item["version"], r.item["content_hash"], _num(r.similarity),
                      _num(r.margin), r.status, r.reason, r.supporting, r.story_key, r.counted, r.displayed)
                     for r in rows],
                )
    return mood


async def assessment_evidence(assessment_id: UUID) -> List[Dict[str, Any]]:
    """The items an assessment used, as they read when it ran (from their saved versions), however the feed has
    changed since."""
    return [dict(r) for r in await fetch_all(
        """
        SELECT e.item_id, e.item_version, e.similarity_score, e.relevance_status, e.relevance_reason,
               e.supporting_text, e.counted, e.displayed, v.text, v.sentiment, i.url, i.topic_name, i.author,
               i.published_at
        FROM public_mood_assessment_items e
        JOIN listening_item_versions v ON v.item_id = e.item_id AND v.version = e.item_version
        JOIN listening_items i ON i.id = e.item_id
        WHERE e.assessment_id = $1
        ORDER BY e.counted DESC, e.similarity_score DESC
        """,
        assessment_id,
    )]


def _apply_model_decision(mood: ConversationMood, decision: "mood_model.Decision") -> None:
    """A promoted model's recommendation replaces the rules' (it has already been through the guard rails), and the
    reasons shown follow it, so a reviewer never sees POST next to 'review required'."""
    mood.recommendation = decision.final
    codes = [c for c in mood.reason_codes if c not in ("REVIEW_REQUIRED", "HOLD_NEGATIVE")]
    if decision.final != "POST":
        codes.insert(0, "REVIEW_REQUIRED")
    if decision.final == "HOLD":
        codes.insert(1, "HOLD_NEGATIVE")
    mood.reason_codes = list(dict.fromkeys([*codes, "MODEL_ASSISTED", *decision.guards]))
    mood.recommendation_reason = ("Model-assisted: " + decision.final + (f". {decision.note}" if decision.note else "")
                                  + f" (rules said otherwise: {mood.recommendation_reason})")


def topic_terms(topics: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Topic rows (from the topic table, keywords already tuned by the team) keyed by the source's external id."""
    return {
        t["external_id"]: {
            "id": t["id"], "external_id": t["external_id"], "name": t["name"],
            "terms": list(dict.fromkeys([t["name"], *(t.get("keywords") or [])])),
            "excluded": list(t.get("excluded_keywords") or []),
        }
        for t in topics
    }


# ---------------------------------------------------------------------------
# What the reader is told: status, reasons, warnings, reviewer input
# ---------------------------------------------------------------------------

EVIDENCE_WARN_HOURS = _env_float("PUBLIC_MOOD_EVIDENCE_WARN_HOURS", 48)


@dataclass
class EvidenceContext:
    """How collection and indexing stand right now (as opposed to when an assessment was made)."""
    collection_status: Optional[str] = None
    collected_at: Optional[datetime] = None
    indexing_status: Optional[str] = None
    eligible: int = 0
    indexed: int = 0

    @property
    def pending(self) -> int:
        return max(0, self.eligible - self.indexed)


async def evidence_context() -> EvidenceContext:
    run = await fetch_one(
        "SELECT status, COALESCE(finished_at, started_at) AS at FROM listening_collection_runs "
        "ORDER BY started_at DESC LIMIT 1"
    )
    counts = await fetch_one(
        "SELECT count(*) FILTER (WHERE is_eligible) AS eligible, "
        "count(*) FILTER (WHERE is_eligible AND indexed_hash = $1 || ':' || content_hash) AS indexed "
        "FROM listening_items",
        store.embedding_model(),
    )
    return EvidenceContext(
        collection_status=run["status"] if run else None, collected_at=run["at"] if run else None,
        indexing_status=await get_state("indexing_status"),
        eligible=int(counts["eligible"] or 0), indexed=int(counts["indexed"] or 0),
    )


def decorate(mood: ConversationMood, ctx: EvidenceContext, now: Optional[datetime] = None) -> ConversationMood:
    """Add what is true now to a saved or fresh assessment: how the last collection went, whether indexing is
    complete, how old the evidence is."""
    now = now or datetime.now(timezone.utc)
    mood.collection_status, mood.collected_at = ctx.collection_status, ctx.collected_at
    mood.indexing_status, mood.pending_index_count = ctx.indexing_status, ctx.pending
    codes, warnings = list(mood.reason_codes), list(mood.warnings)
    if ctx.collection_status and ctx.collection_status != "SUCCESS":
        codes.append(f"COLLECTION_{ctx.collection_status}")
        warnings.append(f"The last collection was {ctx.collection_status.lower().replace('_', ' ')}: some sources "
                        "could not be read, so the evidence may be incomplete.")
    if ctx.pending:
        codes.append("INDEXING_INCOMPLETE")
        warnings.append(f"{ctx.pending} collected items are not searchable yet.")
    if ctx.collected_at and (now - ctx.collected_at).total_seconds() > EVIDENCE_WARN_HOURS * 3600:
        codes.append("EVIDENCE_AGING")
        warnings.append(f"The newest evidence was collected more than {EVIDENCE_WARN_HOURS:.0f} hours ago.")
    mood.reason_codes = list(dict.fromkeys(codes))
    mood.warnings = list(dict.fromkeys(warnings))
    return mood


async def _finish(mood: ConversationMood, ctx: EvidenceContext, submission_id: UUID,
                  viewer: Optional[UUID]) -> ConversationMood:
    decorate(mood, ctx)
    try:
        mood.excluded_by_reviewers = await feedback.excluded_for(submission_id)
        if mood.assessment_id:
            mood.reviewer_corrections = await feedback.corrections_for(mood.assessment_id, viewer)
    except Exception as e:
        logger.warning("Couldn't load reviewer input for the conversation assessment: %s", e)
    return mood


async def _without_assessment(status: str, code: str, message: str, submission_id: UUID, ctx: EvidenceContext,
                              viewer: Optional[UUID]) -> ConversationMood:
    """No current assessment can be made. If this draft has an earlier one, show it as STALE (with why it couldn't be
    refreshed); otherwise say plainly that there is none, and no recommendation."""
    prev = None
    try:
        saved = await fetch_one(
            "SELECT status, result FROM public_mood_assessments WHERE submission_id = $1 "
            "ORDER BY assessed_at DESC LIMIT 1",
            submission_id,
        )
        prev = _mood_from_saved(saved["status"], saved["result"]) if saved else None
    except Exception as e:
        logger.warning("Couldn't load the earlier conversation assessment: %s", e)
    if prev is not None:
        prev.status = "STALE"
        prev.reason_codes = list(dict.fromkeys([code, "STALE_ASSESSMENT", *prev.reason_codes]))
        prev.warnings = [*prev.warnings, f"This is an earlier assessment: {message}"]
        return await _finish(prev, ctx, submission_id, viewer)
    mood = ConversationMood(status=status, reason_codes=[code], recommendation=None, recommendation_reason=message)
    return await _finish(mood, ctx, submission_id, viewer)


async def conversation_mood(row: Dict[str, Any], submission_id: UUID, topics: List[Dict[str, Any]],
                            matched_topic_ids: Set[Any], viewer: Optional[UUID] = None) -> Optional[ConversationMood]:
    """The mood among verified-relevant conversations for this draft, always with a status (see ConversationMood):
    from a saved assessment while it is current, otherwise a new one saved with the exact item versions it used.
    None only when the draft has no text to search with."""
    if not (row.get("title") or row.get("caption_text") or row.get("text")):
        return None
    try:
        ctx = await evidence_context()
    except Exception as e:
        logger.warning("Couldn't read the collection and indexing state: %s", e)
        ctx = EvidenceContext()
    if not store.is_enabled():
        return await _without_assessment(
            "UNAVAILABLE", "STORE_NOT_CONFIGURED",
            "The vector store isn't configured, so conversations can't be searched.", submission_id, ctx, viewer)
    try:
        evidence_version = await get_state("evidence_version") or "0"
        embedding_version = store.embedding_model()
        by_ext = topic_terms(topics)
        rejected = await feedback.rejected_item_ids(submission_id)
        ctx_hash = context_hash(row)
        key = cache_key(ctx_hash, evidence_version, embedding_version, matched_topic_ids,
                        topics_fingerprint(by_ext.values()), rejected)
        saved = await fetch_one(
            "SELECT id, status, result FROM public_mood_assessments WHERE submission_id = $1 AND cache_key = $2 "
            "AND expires_at > NOW() ORDER BY assessed_at DESC LIMIT 1",
            submission_id, key,
        )
        if saved:
            mood = _mood_from_saved(saved["status"], saved["result"])
            if mood is not None:
                mood.assessment_id = mood.assessment_id or saved["id"]
                return await _finish(mood, ctx, submission_id, viewer)
        if ctx.pending > 0:
            # Some collected items aren't searchable yet: an assessment now would be made on part of the evidence.
            return await _without_assessment(
                "INDEXING_PENDING", "INDEXING_INCOMPLETE",
                f"{ctx.pending} collected items are still being indexed; try again shortly.", submission_id, ctx, viewer)
        hits = await store.search(post_search_text(row), kinds=[KIND], limit=RETRIEVAL_LIMIT)
        if not hits:
            # search() returns [] on any failure as well as on an empty index, so tell them apart by what is indexed.
            code, message = (("INDEX_EMPTY", "Nothing has been collected and indexed yet.") if ctx.indexed == 0 else
                             ("SEARCH_UNAVAILABLE", "The conversation search returned nothing or failed; try again."))
            return await _without_assessment("UNAVAILABLE", code, message, submission_id, ctx, viewer)
        ids = [UUID(str((h.get("meta") or {}).get("item_id"))) for h in hits if (h.get("meta") or {}).get("item_id")]
        items = {
            str(r["id"]): dict(r) for r in await fetch_all("SELECT * FROM listening_items WHERE id = ANY($1::uuid[])", ids)
        }
        status, mood, rows = assess(hits, items, by_ext, post_context_of(row), matched_topic_ids, rejected)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=ASSESSMENT_HOURS)
        version = max(row.get("current_version") or 0, 1)

        # What was measured, by the one function training also uses, and what the decision is (the rules, with a
        # trained model beside them in shadow mode, or deciding inside guard rails once one is promoted).
        state = {"collected_at": ctx.collected_at, "collection_status": ctx.collection_status,
                 "indexing_status": ctx.indexing_status}
        features = mood_features.build_features(
            mood_records.draft_context_of(row), [mood_records.evidence_dict(r.item) for r in rows if r.status == "RELEVANT"], state)
        decision = await mood_model.decide(mood.recommendation, features)
        mood.decision_source, mood.decision_note = decision.source, decision.note
        if decision.source == "MODEL" and decision.final != mood.recommendation:
            _apply_model_decision(mood, decision)

        try:
            mood = await _save_assessment(
                submission_id, version, key, ctx_hash, evidence_version, embedding_version, status, mood, rows, expires_at)
        except Exception as e:  # the reading is still good; it just isn't saved (and will be redone next time)
            logger.warning("Couldn't save the conversation assessment: %s", e)
        await mood_records.record_assessment(mood_records.build_assessment_record(
            assessment_id=mood.assessment_id, submission_id=submission_id, draft_version=version, row=row,
            evidence_rows=rows, mood=mood, features=features, feature_schema_version=mood_features.FEATURE_SCHEMA_VERSION,
            policy_version=policy_version(), embedding_version=embedding_version, evidence_version=evidence_version,
            state=state, expires_at=expires_at, decision=decision.as_record()))
        return await _finish(mood, ctx, submission_id, viewer)
    except Exception as e:
        logger.warning("Conversation assessment failed: %s", e)
        try:
            return await _without_assessment(
                "UNAVAILABLE", "ASSESSMENT_ERROR", "The conversation assessment failed; the topic mood is still shown.",
                submission_id, ctx, viewer)
        except Exception:
            return ConversationMood(status="UNAVAILABLE", reason_codes=["ASSESSMENT_ERROR"])
