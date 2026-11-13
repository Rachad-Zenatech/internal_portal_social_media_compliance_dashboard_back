"""Newsroom facts: the company's own published facts as one more source for a post's claims.

A post about a product or an announcement often states things the author didn't cite a source for, or cited a source that
doesn't mention. The ZenaTech newsroom is read into Messaging Context as one fact per article (see newsroom_sync_service),
so the facts closest to what the post says are put beside it and the claims compared with them like any other source:

- a claim the newsroom supports is accurate, even if the cited source never mentions it (it clears "needs a source" and the
  "not found in its source" finding for that claim);
- a claim that differs from the newsroom is for a person to confirm, at no cost: a fact is a short summary of an older
  article, so a difference may mean the post is newer;
- a claim the newsroom doesn't cover says nothing: it stays as the cited source left it.

Which facts are picked is plain word overlap with the post (no embedding, so it works when the embedding quota is used
up). Only active facts the newsroom sync keeps are used.
"""

import logging
import re
from typing import List, Optional, Sequence, Tuple

from models.compliance.compliance_model import SourceCheck, SourceClaim
from postgresql_db.database import fetch_all

logger = logging.getLogger(__name__)

REFERENCE = "ZenaTech newsroom"
NEWSROOM_URL = "https://www.zenatech.com/newsroom/"
# At most this many facts go to the model, and each must share at least this many distinctive words with the post.
MAX_FACTS = 8
MIN_SHARED_WORDS = 3
MAX_TEXT_CHARS = 7000

_IMAGE_MARK = re.compile(r"Text in image \d+\s*:", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9]{4,}")
# Words in nearly every post and article: they say nothing about which fact a post matches.
_COMMON = {
    "this", "that", "with", "from", "have", "will", "your", "their", "about", "more", "than", "into", "also", "were",
    "been", "they", "them", "what", "when", "where", "which", "learn", "read", "here", "https", "http", "www", "com",
    "zenatech", "zenadrone", "drone", "drones", "company", "announced", "today", "news", "support", "growing", "new",
}


def _words(text: str) -> set:
    return {w for w in _WORD.findall(_IMAGE_MARK.sub(" ", text or "").lower()) if w not in _COMMON}


def rank(post_text: str, facts: Sequence[dict]) -> List[dict]:
    """The facts that share enough distinctive words with the post, best match first."""
    mine = _words(post_text)
    scored = []
    for fact in facts:
        theirs = _words(f"{fact['title']} {fact['body']}")
        shared = len(mine & theirs)
        if shared >= MIN_SHARED_WORDS:
            scored.append((shared / (len(theirs) ** 0.5), fact))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [fact for _, fact in scored[:MAX_FACTS]]


async def material(post_text: str) -> Optional[Tuple[SourceCheck, Tuple[str, str]]]:
    """The newsroom as a source to read: its check and (label, text) for the model, or None when no fact is close to
    the post (or the facts can't be read: this is extra, never something that stops the check)."""
    try:
        rows = await fetch_all(
            """
            SELECT title, body, COALESCE(source_url, source) AS url FROM messaging_context
            WHERE kind = 'FACT' AND is_active AND deleted_at IS NULL AND auto_source = 'NEWSROOM'
            """
        )
        facts = rank(post_text, [dict(r) for r in rows])
    except Exception as e:
        logger.warning("Newsroom facts couldn't be read (the check goes on without them): %s", e)
        return None
    if not facts:
        return None
    text = "The ZenaTech newsroom: short summaries of articles the company published.\n\n" + "\n\n".join(
        f"{f['title']} ({f['url'] or NEWSROOM_URL}): {f['body']}" for f in facts
    )
    check = SourceCheck(
        reference=REFERENCE, kind="URL", where="caption", cue="newsroom facts", status="READ", title=REFERENCE,
        url=NEWSROOM_URL, authority="COMPANY",
    )
    return check, (REFERENCE + " facts", text[:MAX_TEXT_CHARS])


def is_newsroom(check: SourceCheck) -> bool:
    return check.reference == REFERENCE


def _same(a: str, b: str) -> bool:
    x, y = _words(a), _words(b)
    return bool(x and y) and len(x & y) / len(x | y) >= 0.5


def supports(checks: Sequence[SourceCheck], claim: str) -> bool:
    """Whether the newsroom supports this claim (as another source worded it)."""
    return any(
        is_newsroom(c) and k.verdict == "SUPPORTED" and _same(k.claim, claim) for c in checks for k in c.claims
    )


def unsettled(claim: SourceClaim) -> bool:
    return claim.verdict in ("NOT_FOUND", "UNCLEAR")
