"""A plain-language log of how a post's public mood was made, shown on the page so a person can follow it:
what was read from the post, which searches it produced, what the last scrape found for them, which topics were
matched and why, and how the numbers and recommendation came out. It only reports what exists: nothing is estimated."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.compliance.analytics.listening_providers import load_feed
from services.compliance.analytics.post_queries import queries_for_text
from services.compliance.analytics.post_query_plan import contexts_of

POST_PREFIX = "post-"


def step(title: str, status: str, summary: str, lines: Optional[List[str]] = None) -> Dict[str, Any]:
    """status: ok (worked), warn (worked with a limit), none (nothing to show), info."""
    return {"title": title, "status": status, "summary": summary, "lines": lines or []}


def _age(iso: Optional[str]) -> str:
    try:
        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return "at an unknown time"
    mins = int((datetime.now(timezone.utc) - then).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 120:
        return f"{mins} min ago"
    return f"{mins // 60} h ago" if mins < 2880 else f"{mins // 1440} days ago"


def read_step(row: Dict[str, Any]) -> Dict[str, Any]:
    parts = {"title": row.get("title"), "caption": row.get("caption_text"), "script": row.get("script_text"),
             "video transcript": row.get("transcript_text"), "image text": row.get("image_text")}
    lines = [f"{name}: {len(text)} characters" for name, text in parts.items() if text]
    missing = [name for name, text in parts.items() if not text]
    if missing:
        lines.append("not present: " + ", ".join(missing))
    lines.append("Read: the script, the text of an attached script file, the video transcript, and text extracted from "
                 "attached images. Other attached documents are not read.")
    return step("1. What was read from your post", "ok" if row.get("text") else "none",
                f"{len(row.get('text') or '')} characters of your own text", lines)


def search_step(found: Dict[str, Any]) -> Dict[str, Any]:
    title = "2. Searches built from it"
    contexts = contexts_of(found)
    if not found["queries"] and not contexts:
        return step(title, "none", "No search was built",
                    ["The text names nothing distinctive enough to search for (everyday words are skipped), "
                     "so nothing was searched for this post.", found.get("note") or ""][:2 if found.get("note") else 1])
    lines = [f"planned by: {found.get('source', 'rules')}" + (f" - {found['note']}" if found.get("note") else "")]
    if found.get("subject"):
        lines.append(f"subject: {found['subject']}")
    lines += [f"direct search (about your announcement): {q}" for q in found["queries"]]
    if found.get("require_all"):
        lines.append("a headline counts as direct only if it names all of: " + ", ".join(found["require_all"]))
    elif found.get("anchors"):
        lines.append("names looked for in headlines: " + ", ".join(found["anchors"]))
    for i, c in enumerate(contexts, 1):
        lines.append(f"wider topic {i}: {c['topic']}")
        lines += [f"  search: {q}" for q in c["queries"]]
        lines.append("  a headline counts if it shares the main words of: " + ", ".join(c["terms"]))
    n = len(found["queries"]) + sum(len(c["queries"]) for c in contexts)
    return step(title, "ok", f"{n} search(es) from your post", lines)


def scrape_step(feed: Optional[Dict[str, Any]], queries: List[str]) -> Dict[str, Any]:
    title = "3. Last scrape (Google News)"
    if not feed:
        return step(title, "none", "Nothing has been collected yet", ["Click Scrape X to collect news."])
    when = f"collected {_age(feed.get('collected_at'))}"
    if not queries:
        return step(title, "info", when, ["No post-specific search to look up."])
    sources = (feed.get("collection") or {}).get("sources") or []
    mine = [s for s in sources if s.get("kind") in ("post:news", "post:context") and s.get("name") in queries]
    if not mine:
        return step(title, "warn", when + ", but not for these searches",
                    ["This post's searches were not part of that scrape (the draft is newer, or its text changed). "
                     "Click Scrape X."])
    lines, kept = [], 0
    for s in mine:
        found = s.get("items") or 0
        dropped = s.get("dropped_unrelated") or 0
        keep = max(found - dropped, 0)
        kept += keep
        kind = "context" if s.get("kind") == "post:context" else "direct"
        lines.append(f"[{kind}] {s['name']}: {s.get('status')}, {found} headlines found, {dropped} dropped as not "
                     f"matching, {keep} kept")
    return step(title, "ok" if kept else "warn", f"{when}; {kept} headline(s) kept for this post", lines)


def topics_step(mood) -> Dict[str, Any]:
    title = "4. Topics matched to this post"
    left_out = [f"left out by a reviewer: {d.name}" for d in mood.dismissed]
    if not mood.topics:
        return step(title, "none", "No topic matched", ["No collected topic mentions what your post says.", *left_out])
    lines = []
    for t in mood.topics:
        how = ", ".join(t.matched_terms) if t.matched_terms else "meaning only"
        lines.append(f"{t.name}: matched by {t.matched_by.lower()} ({how})")
    return step(title, "ok", f"{len(mood.topics)} topic(s) matched", [*lines, *left_out])


def scoring_step(mood) -> Dict[str, Any]:
    title = "5. How the numbers were made"
    scored = [t for t in mood.topics if t.snapshot]
    if not scored:
        return step(title, "none", "Nothing was scored", [])
    lines = []
    for t in scored:
        s = t.snapshot
        lines.append(f"{t.name}: {s.volume} headline(s) scored one by one by the sentiment model: "
                     f"{s.positive_pct}% positive, {s.neutral_pct}% neutral, {s.negative_pct}% negative. "
                     f"Recommendation {s.recommendation or 'WAIT'}. {s.recommendation_reason or ''}".strip())
    lines.append("Percentages are plain counts of the headlines found. They show the tone of the headlines, "
                 "not what the public thinks.")
    return step(title, "ok", f"{len(scored)} topic(s) scored", lines)


def conversation_step(mood) -> Dict[str, Any]:
    title = "6. Conversation check"
    c = mood.conversation_mood
    if c is None:
        return step(title, "none", "Not run",
                    ["The vector database is not connected, so headlines nearest your post in meaning were not searched."])
    lines = [f"status: {c.status}", *[f"reason: {r}" for r in c.reason_codes], *c.warnings]
    if c.recommendation_reason:
        lines.append(c.recommendation_reason)
    return step(title, "ok" if c.status == "READY" else "warn", f"{c.count} verified story(ies); status {c.status}", lines)


async def build(row: Dict[str, Any], mood) -> List[Dict[str, Any]]:
    from services.compliance.analytics.post_queries import combine_body
    from services.compliance.analytics.post_query_plan import plan_for_text

    found = await plan_for_text(row.get("title") or "", row.get("caption_text") or "",
                                combine_body(row.get("script_version_body"), row.get("script_file_text"),
                                             row.get("transcript_text"), row.get("image_text")),
                                allow_model=False)
    try:
        feed = await load_feed()
    except Exception:
        feed = None
    every_query = [*found["queries"], *(q for c in contexts_of(found) for q in c["queries"])]
    return [read_step(row), search_step(found), scrape_step(feed, every_query), topics_step(mood),
            scoring_step(mood), conversation_step(mood)]
