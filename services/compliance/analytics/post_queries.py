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
from typing import Any, Dict, List, Optional

from postgresql_db.database import fetch_all
from services.compliance.analytics.conversation_evidence import entities_split

MAX_QUERIES_PER_DRAFT = 3
MAX_DRAFTS = int(os.getenv("PUBLIC_MOOD_POST_SEARCH_MAX_DRAFTS", "15"))
ACTIVE_STATUSES = ("DRAFT", "IN_REVIEW", "CHANGES_REQUESTED")
FILE_ENV = "SCRAPE_POST_QUERIES_FILE"

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


def combine_body(script: Optional[str], script_file_text: Optional[str], spoken: Optional[str]) -> str:
    """Everything the draft says besides its title and caption. The scrape and the process log both use this, so the
    saved search plan (keyed on the text) is found again."""
    return "\n".join(dict.fromkeys(p.strip() for p in (script, script_file_text, spoken) if p and p.strip()))[:6000]


async def active_draft_searches() -> List[Dict[str, Any]]:
    """One entry per active draft that gave at least one search: {id, title, queries, keywords}."""
    rows = await fetch_all(
        """
        SELECT s.id, s.title, s.caption_text,
               sv.body AS script_body, v.transcript, sf.doc_text AS script_file_text
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
        WHERE s.deleted_at IS NULL AND s.status = ANY($1::text[])
        ORDER BY s.updated_at DESC NULLS LAST
        LIMIT $2
        """,
        list(ACTIVE_STATUSES), MAX_DRAFTS,
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        transcript = r["transcript"]
        segments = json.loads(transcript) if isinstance(transcript, str) else (transcript or [])
        spoken = " ".join(str(x.get("text", "")) for x in segments if isinstance(x, dict))
        # Everything the draft says: its script, the text of the attached script file, and the video transcript.
        body = combine_body(r["script_body"], r["script_file_text"], spoken)
        from services.compliance.analytics.post_query_plan import contexts_of, plan_for_text  # here: it imports this module
        found = await plan_for_text(r["title"] or "", r["caption_text"] or "", body)
        if found["queries"] or contexts_of(found):
            out.append({"id": str(r["id"]), "title": (r["title"] or "Untitled draft")[:120], **found})
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


def topic_for_draft(search: Dict[str, Any]) -> Dict[str, Any]:
    """The listening topic a draft's searches become, matched back to that draft by its own keywords."""
    return {
        "external_id": f"post-{search['id']}",
        "name": search["title"],
        "description": "Searched from this draft's own title, caption and script.",
        "keywords": search["keywords"],
        "anchors": search.get("anchors") or [],
        "require_all": search.get("require_all") or [],
        "category": "post",
        "priority": "draft",
    }


def context_topics_for_draft(search: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The wider topics around a draft's subject, one topic each: the public vibe about them, not coverage of this
    announcement. Every one carries its own searches."""
    from services.compliance.analytics.post_query_plan import contexts_of

    out = []
    for i, c in enumerate(contexts_of(search), 1):
        out.append({
            "external_id": f"post-{search['id']}-context-{i}",
            "name": f"Around this topic: {c['topic']}"[:120],
            "description": "A wider subject this draft is part of (not coverage of the draft's own announcement).",
            "keywords": c["terms"],
            "anchors": c["terms"],
            "queries": c["queries"],
            "category": "post-context",
            "priority": "context",
        })
    return out
