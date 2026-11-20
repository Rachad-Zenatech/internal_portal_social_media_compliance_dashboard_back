"""Searches built from what each draft actually says.

The scrape button used to search a fixed list of market topics. Now each draft that is still being worked on
(DRAFT, IN_REVIEW, CHANGES_REQUESTED) becomes its own searches, taken from its title, caption and script or
transcript: the companies and products it names, its hashtags, and the key words of its title. The news and posts
those searches find are collected under a topic for that draft, so its public mood is about its own subject.

Nothing here is estimated: a draft with nothing nameable in it produces no search, and says so.
"""

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from postgresql_db.database import fetch_all
from services.compliance.analytics.conversation_evidence import entities_split

MAX_QUERIES_PER_DRAFT = 3
MAX_DRAFTS = int(os.getenv("PUBLIC_MOOD_POST_SEARCH_MAX_DRAFTS", "15"))
# Personal drafts are private work-in-progress and must never be sent to the Public Mood listener.
ACTIVE_STATUSES = ("IN_REVIEW", "CHANGES_REQUESTED")
ELIGIBLE_POST_STATUSES = ("APPROVED", "IN_REVIEW", "CHANGES_REQUESTED")
TODAY_TRACK = "today_posts"
FILE_ENV = "SCRAPE_POST_QUERIES_FILE"
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "America/New_York")

_HASHTAG = re.compile(r"#([A-Za-z][A-Za-z0-9_]{2,})")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{3,}")
_STOP = {
    "this", "that", "with", "from", "have", "will", "your", "about", "more", "than", "into", "over", "they", "their",
    "what", "when", "where", "which", "while", "would", "could", "should", "there", "these", "those", "been", "also",
    "just", "like", "video", "post", "today", "watch", "here", "learn", "check", "join", "share", "follow", "link",
}


# Labels from script templates ("Hook:", "Voiceover:", "Headline:"), not subjects anyone would search for.
_TEMPLATE = {"hook", "voiceover", "voice", "headline", "scene", "cta", "caption", "intro", "outro", "visual", "narrator",
             "title", "script", "body", "closing", "opening", "meet"}


# Everyday words: searching for one of these alone returns the news of a whole subject, not about this post.
_COMMON = {
    "american", "america", "defense", "defence", "drone", "drones", "service", "services", "development", "innovation",
    "automation", "technology", "cybersecurity", "saas", "digital", "global", "system", "systems", "platform", "security",
    "military", "government", "company", "business", "news", "test", "tests", "script", "post", "posts", "new", "meet",
    "future", "industry", "solution", "solutions", "product", "products", "customers", "customer", "team", "world",
    "data", "cloud", "software", "hardware", "energy", "health", "market", "markets", "transformation", "ai", "tech",
}


_COMMON |= {"show", "storyline", "engineers", "engineer", "that", "watches", "saa", "explain", "video", "scene", "powered", "driven", "based", "first", "best", "latest", "open", "smart"}


_FUNCTION = {"as", "a", "an", "the", "of", "and", "for", "to", "in", "on", "is", "it", "at", "by"}


def _only_everyday_words(word: str) -> bool:
    """True when the word is everyday words run together ("digitaltransformation") or hyphenated ("ai-powered")."""
    word = word.replace("-", "")
    ok = [False] * (len(word) + 1)
    ok[0] = True
    for i in range(1, len(word) + 1):
        ok[i] = any(ok[j] and (word[j:i] in _COMMON or word[j:i] in _FUNCTION) for j in range(max(0, i - 14), i))
    return ok[len(word)]


def _trim(phrase: str) -> str:
    """The name without everyday words at its edges ("meet stacklens" -> "stacklens", "shaun test" -> "shaun")."""
    words = phrase.split()
    while words and (words[0] in _COMMON or words[0] in _TEMPLATE):
        words.pop(0)
    while words and (words[-1] in _COMMON or words[-1] in _TEMPLATE):
        words.pop()
    return " ".join(words)


def _is_subject(phrase: str) -> bool:
    """A name worth searching for: more than one word (and not only everyday ones), or one distinctive word."""
    words = phrase.split()
    if not words or all(w in _COMMON or w in _TEMPLATE or _only_everyday_words(w) for w in words):
        return False
    if len(words) == 1:
        return len(words[0]) >= 4
    return True


def _hashtag_phrase(tag: str) -> str:
    return _CAMEL.sub(" ", tag.replace("_", " ")).strip()


def _in_order(phrases, text: str) -> List[str]:
    """The phrases in the order they first appear in the text (a set has no order, and the first names matter most)."""
    low = (text or "").lower()
    return sorted(phrases, key=lambda p: (low.find(p) if p in low else len(low), p))


def queries_for_text(title: str, caption: str, body: str) -> Dict[str, Any]:
    """{"queries": [...], "keywords": [...]} for one draft, from its own words. Empty when it names nothing."""
    whole = "\n".join(p for p in (title, caption, body) if p)
    firm, _initial = entities_split(whole)
    names = list(dict.fromkeys(n for n in map(_trim, _in_order(firm, whole)) if _is_subject(n)))[:4]
    # A hashtag is a subject only as a phrase ("drone as a service"), or when it is a name itself.
    tags = [t for t in dict.fromkeys(_hashtag_phrase(t).lower() for t in _HASHTAG.findall(whole)) if _is_subject(t) or len(t.split()) >= 3][:3]  # a 3-word tag is a phrase of its own
    anchors = list(dict.fromkeys([*names, *tags]))
    title_words = [w.lower() for w in _WORD.findall(title or "") if w.lower() not in _STOP and w.lower() not in _COMMON]
    title_words = list(dict.fromkeys(title_words))[:6]

    queries: List[str] = []
    if names:
        queries.append(" ".join(f'"{n}"' for n in names[:2]))
    if tags:
        queries.append(" OR ".join(f'"{t}"' for t in tags))
    queries = list(dict.fromkeys(q for q in queries if q))[:MAX_QUERIES_PER_DRAFT]
    keywords = list(dict.fromkeys([*anchors, *title_words]))
    # `anchors` are what a headline must mention to count as being about this draft.
    return {"queries": queries, "keywords": keywords, "anchors": anchors}


def combine_body(
    script: Optional[str],
    script_file_text: Optional[str],
    spoken: Optional[str],
    image_text: Optional[str] = None,
) -> str:
    """Everything the draft says besides its title and caption. The scrape and the process log both use this, so the
    saved search plan (keyed on the text) is found again."""
    return "\n".join(
        dict.fromkeys(p.strip() for p in (script, script_file_text, spoken, image_text) if p and p.strip())
    )[:6000]


def today_window(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """Start (inclusive) and end (exclusive) of today in APP_TIMEZONE, returned as UTC datetimes."""
    try:
        tz = ZoneInfo(APP_TIMEZONE)
    except Exception:
        tz = timezone.utc
    ref = (now or datetime.now(timezone.utc)).astimezone(tz)
    start_local = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _in_window(value: Any, start: datetime, end: datetime) -> bool:
    if not isinstance(value, datetime):
        return False
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return start <= aware < end


def today_reason_for_row(row: Dict[str, Any], start: datetime, end: datetime) -> Optional[str]:
    """Why a post belongs to 'what is getting posted today': scheduled_today > today_activity > None."""
    if _in_window(row.get("publish_at"), start, end):
        return "scheduled_today"
    caption = (row.get("caption_text") or "").upper()
    if "NOT FOR PUBLICATION" in caption:
        return None
    for col in ("submitted_at", "content_edited_at", "created_at", "updated_at"):
        if _in_window(row.get(col), start, end):
            return "today_activity"
    return None


def _submission_sql(today_only: bool) -> str:
    status_filter = (
        """(
            s.status = ANY($1::text[])
            AND (
                (s.publish_at >= $3 AND s.publish_at < $4)
                OR (
                    COALESCE(s.caption_text, '') NOT ILIKE '%NOT FOR PUBLICATION%'
                    AND (
                        (s.submitted_at >= $3 AND s.submitted_at < $4)
                        OR (s.created_at >= $3 AND s.created_at < $4)
                        OR (COALESCE(s.content_edited_at, s.updated_at) >= $3 AND COALESCE(s.content_edited_at, s.updated_at) < $4)
                    )
                )
            )
        )"""
        if today_only
        else """(
            s.status = ANY($1::text[])
            OR (s.status = 'APPROVED' AND s.publish_at IS NOT NULL AND s.publish_at >= $3)
            OR (
                s.status = 'APPROVED'
                AND COALESCE(s.caption_text, '') NOT ILIKE '%NOT FOR PUBLICATION%'
                AND (
                    (s.submitted_at >= $3 AND s.submitted_at < $4)
                    OR (COALESCE(s.content_edited_at, s.updated_at) >= $3 AND COALESCE(s.content_edited_at, s.updated_at) < $4)
                )
            )
        )"""
    )
    return f"""
        SELECT s.id, s.title, s.caption_text, s.status, s.publish_at,
               s.submitted_at, s.content_edited_at, s.created_at, s.updated_at,
               sv.body AS script_body, v.transcript, sf.doc_text AS script_file_text, img.image_text
        FROM content_submissions s
        LEFT JOIN LATERAL (
            SELECT doc_text FROM content_submission_files f2
            WHERE f2.submission_id = s.id AND f2.kind = 'SCRIPT' AND f2.deleted_at IS NULL AND f2.doc_text_status = 'COMPLETED'
            ORDER BY f2.created_at DESC LIMIT 1
        ) sf ON true
        LEFT JOIN LATERAL (
            SELECT body FROM content_script_versions csv WHERE csv.submission_id = s.id
            ORDER BY csv.version_number DESC LIMIT 1
        ) sv ON true
        LEFT JOIN LATERAL (
            SELECT transcript FROM content_submission_files f
            WHERE f.submission_id = s.id AND f.kind = 'MAIN' AND f.deleted_at IS NULL AND f.transcript_status = 'COMPLETED'
            ORDER BY f.created_at DESC LIMIT 1
        ) v ON true
        LEFT JOIN LATERAL (
            SELECT string_agg(TRIM(image_text), E'\\n') AS image_text
            FROM content_submission_files f3
            WHERE f3.submission_id = s.id AND f3.kind IN ('MAIN', 'IMAGE')
              AND f3.deleted_at IS NULL AND f3.image_text_status = 'COMPLETED'
              AND NULLIF(TRIM(f3.image_text), '') IS NOT NULL
        ) img ON true
        WHERE s.deleted_at IS NULL AND {status_filter}
        ORDER BY
            (s.publish_at >= $3 AND s.publish_at < $4) DESC NULLS LAST,
            (s.status = 'APPROVED' AND s.publish_at >= $3 AND s.publish_at < $4) DESC NULLS LAST,
            COALESCE(s.publish_at, s.updated_at) DESC NULLS LAST
        LIMIT $2
        """


async def _build_searches_from_rows(rows: List[Any], *, allow_model: bool = True,
                                    only_today: bool = False) -> List[Dict[str, Any]]:
    from services.compliance.analytics.post_query_plan import contexts_of, plan_for_text

    day_start, day_end = today_window()
    out: List[Dict[str, Any]] = []
    for raw_row in rows:
        r = dict(raw_row)
        reason = today_reason_for_row(r, day_start, day_end)
        if only_today and reason is None:
            continue
        transcript = r.get("transcript")
        segments = json.loads(transcript) if isinstance(transcript, str) else (transcript or [])
        spoken = " ".join(str(x.get("text", "")) for x in segments if isinstance(x, dict))
        body = combine_body(r.get("script_body"), r.get("script_file_text"), spoken, r.get("image_text"))
        found = await plan_for_text(r.get("title") or "", r.get("caption_text") or "", body, allow_model=allow_model)
        if found["queries"] or contexts_of(found):
            pub = r.get("publish_at")
            out.append({
                "id": str(r["id"]),
                "title": (r.get("title") or "Untitled draft")[:120],
                "is_today": reason is not None,
                "today_reason": reason,
                "submission_status": r.get("status"),
                "publish_at": pub.isoformat() if isinstance(pub, datetime) else pub,
                **found,
            })
    out.sort(key=lambda s: (0 if s.get("today_reason") == "scheduled_today" else 1 if s.get("is_today") else 2))
    return out


async def active_draft_searches() -> List[Dict[str, Any]]:
    """Posts already in workflow plus posts scheduled/active today (today's posts prioritized first)."""
    day_start, day_end = today_window()
    rows = await fetch_all(_submission_sql(today_only=False), list(ACTIVE_STATUSES), MAX_DRAFTS, day_start, day_end)
    return await _build_searches_from_rows(rows, allow_model=True, only_today=False)


async def today_post_searches() -> List[Dict[str, Any]]:
    """Posts scheduled or worked on today that have searchable topics."""
    day_start, day_end = today_window()
    rows = await fetch_all(_submission_sql(today_only=True), list(ELIGIBLE_POST_STATUSES), MAX_DRAFTS, day_start, day_end)
    return await _build_searches_from_rows(rows, allow_model=True, only_today=True)


async def list_today_post_candidates() -> List[Dict[str, Any]]:
    """Read-only summary of today's posts (scheduled for today or created/edited today) without calling Gemini."""
    day_start, day_end = today_window()
    rows = await fetch_all(_submission_sql(today_only=True), list(ELIGIBLE_POST_STATUSES), MAX_DRAFTS, day_start, day_end)
    from services.compliance.analytics.post_query_plan import contexts_of, plan_for_text

    out: List[Dict[str, Any]] = []
    for raw_row in rows:
        r = dict(raw_row)
        reason = today_reason_for_row(r, day_start, day_end)
        if reason is None:
            continue
        transcript = r.get("transcript")
        segments = json.loads(transcript) if isinstance(transcript, str) else (transcript or [])
        spoken = " ".join(str(x.get("text", "")) for x in segments if isinstance(x, dict))
        body = combine_body(r.get("script_body"), r.get("script_file_text"), spoken, r.get("image_text"))
        found = await plan_for_text(r.get("title") or "", r.get("caption_text") or "", body, allow_model=False)
        out.append({
            "id": str(r["id"]),
            "title": (r.get("title") or "Untitled post")[:120],
            "status": r.get("status") or "DRAFT",
            "publish_at": r.get("publish_at"),
            "today_reason": reason,
            "has_searches": bool(found["queries"] or contexts_of(found)),
        })
    return out


def write_file(searches: List[Dict[str, Any]]) -> Optional[str]:
    """Hand the searches to the scraper process (it has no database connection) through a temporary file."""
    if not searches:
        return None
    fd, path = tempfile.mkstemp(prefix="post_queries_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(searches, fh)
    return path


def read_file(path: Optional[str] = None) -> List[Dict[str, Any]]:
    path = path or os.getenv(FILE_ENV)
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict) and (d.get("queries") or d.get("contexts") or d.get("context_queries"))] if isinstance(data, list) else []


def _post_meta(search: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not search.get("is_today"):
        return None
    return {
        "submission_id": search["id"],
        "title": search.get("title"),
        "status": search.get("submission_status"),
        "publish_at": search.get("publish_at"),
        "today_reason": search.get("today_reason"),
    }


def topic_for_draft(search: Dict[str, Any]) -> Dict[str, Any]:
    """The listening topic a draft's searches become, matched back to that draft by its own keywords."""
    out = {
        "external_id": f"post-{search['id']}",
        "name": search["title"],
        "description": "Searched from this draft's own title, caption and script.",
        "keywords": search["keywords"],
        "anchors": search.get("anchors") or [],
        "require_all": search.get("require_all") or [],
        "category": "post",
        "priority": "draft",
    }
    if search.get("is_today"):
        out["scrape_track"] = TODAY_TRACK
        out["post_meta"] = _post_meta(search)
    return out


def context_topics_for_draft(search: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The wider topics around a draft's subject, one topic each: the public vibe about them, not coverage of this
    announcement. Every one carries its own searches."""
    from services.compliance.analytics.post_query_plan import contexts_of

    out = []
    meta = _post_meta(search)
    for i, c in enumerate(contexts_of(search), 1):
        item: Dict[str, Any] = {
            "external_id": f"post-{search['id']}-context-{i}",
            "name": f"Around this topic: {c['topic']}"[:120],
            "description": "A wider subject this draft is part of (not coverage of the draft's own announcement).",
            "keywords": c["terms"],
            "anchors": c["terms"],
            "queries": c["queries"],
            "category": "post-context",
            "priority": "context",
        }
        if meta:
            item["scrape_track"] = TODAY_TRACK
            item["post_meta"] = meta
        out.append(item)
    return out


_POST_CONTEXT_SUFFIX = re.compile(r"-context-\d+$")


def post_id_for_topic(topic_or_ext: Any) -> Optional[str]:
    """Extract the owning submission ID from a topic dict or external_id (`post-<id>` / `post-<id>-context-N`)."""
    if isinstance(topic_or_ext, dict):
        meta = topic_or_ext.get("post_meta") or {}
        if meta.get("submission_id"):
            return str(meta["submission_id"])
        ext = str(topic_or_ext.get("external_id") or "")
    else:
        ext = str(topic_or_ext or "")
    if not ext.startswith("post-"):
        return None
    return _POST_CONTEXT_SUFFIX.sub("", ext[5:]) or None


def assign_unique_per_post(
    pool: List[Dict[str, Any]],
    post_topics: List[Dict[str, Any]],
    assign_fn,
) -> Dict[str, List[Dict[str, Any]]]:
    """Deduplicate stories across topics *within each post* without letting one post steal stories from another."""
    grouped_orders: Dict[str, List[str]] = {}
    group_by_ext: Dict[str, str] = {}
    for t in post_topics:
        ext = t["external_id"]
        group = post_id_for_topic(t) or "__general__"
        group_by_ext[ext] = group
        grouped_orders.setdefault(group, []).append(ext)

    grouped_pools: Dict[str, List[Dict[str, Any]]] = {g: [] for g in grouped_orders}
    for cand in pool:
        group = group_by_ext.get(cand["topic"]) or post_id_for_topic(cand["topic"]) or "__general__"
        if group in grouped_pools:
            grouped_pools[group].append(cand)

    homes: Dict[str, List[Dict[str, Any]]] = {}
    for group, order in grouped_orders.items():
        homes.update(assign_fn(grouped_pools[group], order))
    return homes
