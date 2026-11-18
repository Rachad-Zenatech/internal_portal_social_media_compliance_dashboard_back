"""Text normalization, story deduplication, and named-entity helpers for conversation evidence."""

import hashlib
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional, Set, Tuple

from services.compliance.analytics.listening_providers import PUBLISHER_SUFFIX, clean_headline
from services.compliance.evaluation.compliance_service import _matches, _normalize

# Headlines this alike (share of words) are the same story.
SAME_STORY_JACCARD = 0.8

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
    """(firm, initial): named companies, products and places as capitalised phrases (lowercased)."""
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
