"""Source links and image text for scraped X posts and news items.

- source_links: where the item can be checked: the post's own link, plus any outside link inside the post.
- image_urls: the pictures the post shows.
- image_text: the words in those pictures, read with Gemini (the same reader the submissions' images use), bounded by
  SCRAPE_IMAGE_TEXT_MAX images per run. Empty when Gemini isn't configured or the picture has no readable text.
"""

from datetime import datetime, timezone
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from google.genai import types

logger = logging.getLogger(__name__)

IMAGE_TEXT_ON = os.getenv("SCRAPE_IMAGE_TEXT", "on").strip().lower() not in ("off", "0", "false", "no")
IMAGE_TEXT_MAX = int(os.getenv("SCRAPE_IMAGE_TEXT_MAX", "40"))
MAX_IMAGES_PER_POST = 2
MAX_IMAGE_BYTES = 5 * 1024 * 1024

_MEDIA = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\((https?://[^)\s]+)\)")
_X_HOSTS = ("https://x.com/", "https://twitter.com/", "https://t.co/")


def image_urls_from_block(block: str) -> List[str]:
    """Pictures in a rendered post (avatars, emoji and icons are not the post's images)."""
    urls = []
    for u in _MEDIA.findall(block):
        if "pbs.twimg.com/media/" in u or "pbs.twimg.com/ext_tw_video_thumb/" in u:
            urls.append(u)
    return list(dict.fromkeys(urls))[:MAX_IMAGES_PER_POST]


def source_links_from_block(block: str, post_url: str) -> List[str]:
    """The post's own link first, then any outside link it contains."""
    links = [post_url]
    for u in _LINK.findall(block):
        if not u.startswith(_X_HOSTS) and u not in links:
            links.append(u)
    return links[:5]


def media_fields(block: str, post_url: Optional[str]) -> Dict[str, Any]:
    """source_links, image_urls and (to be filled in later) image_text for one collected item."""
    return {"source_links": source_links_from_block(block, post_url) if post_url else [],
            "image_urls": image_urls_from_block(block), "image_text": ""}


def snowflake_time(status_id: str) -> Optional[datetime]:
    """When an X post was made: its id encodes the time (milliseconds since X's 2010 epoch in the top bits)."""
    try:
        ms = (int(status_id) >> 22) + 1288834974657
        return datetime.fromtimestamp(ms / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _extract_tweets_from_rendered_profile(body: str, handle: str) -> List[Dict[str, Any]]:
    """Parse X posts from a rendered public X profile: text, link and real post time (from the post id). Engagement
    isn't read: the page's numbers can't be told apart from dates and counts of other posts.

    The page is a list; every post starts at a bullet ("*   [...") and runs to the next one. Inside a post: the
    author header ending in a dated link to the post, then the text, then media and counts."""
    posts: List[Dict[str, Any]] = []
    seen_ids = set()
    status = re.compile(r"https://x\.com/([A-Za-z0-9_]+)/status/(\d+)")
    for block in re.split(r"\n\s*\*\s{2,}(?=\[)", body)[1:]:
        first = status.search(block)
        if not first:
            continue
        author_handle, status_id = first.group(1), first.group(2)
        if status_id in seen_ids:
            continue
        seen_ids.add(status_id)
        posted = snowflake_time(status_id)

        # The text starts after the dated link ("[Nov 10](.../status/ID)", "[3h](...)"), the first one with a label.
        dated = re.search(rf"\[[^\]\n]+\]\(https://x\.com/[A-Za-z0-9_]+/status/{status_id}\)", block)
        chunk = block[dated.end():] if dated else block
        # ...and ends at the first media or empty link (photo, compose box), or the post's own counts.
        chunk = re.split(r"!\[|\[\]\(https://x\.com/", chunk, maxsplit=1)[0]
        chunk = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", chunk)      # links keep their label
        chunk = re.sub(r"\s+Show more.*$", "", chunk, flags=re.DOTALL)
        chunk = re.sub(r"\s+\d+(?:\s+\d+){0,4}\s*$", "", chunk)      # trailing reply / like counts
        chunk = re.sub(r"\s+", " ", chunk).strip(" *-\n\t")
        if len(chunk) < 25:
            continue

        posts.append({
            "network": "X",
            "author": f"@{author_handle}",
            "status_id": status_id,
            "url": f"https://x.com/{author_handle}/status/{status_id}",
            "text": chunk[:500],
            **media_fields(block, f"https://x.com/{author_handle}/status/{status_id}"),
            "engagements": 0,
            "published_at": posted.isoformat() if posted else None,
            "posted_on": posted.date().isoformat() if posted else None,
        })
    return posts


async def _download(client: httpx.AsyncClient, url: str) -> Optional[types.Part]:
    try:
        r = await client.get(url, timeout=20)
        mime = (r.headers.get("content-type") or "").split(";")[0]
        if r.status_code != 200 or not mime.startswith("image/") or len(r.content) > MAX_IMAGE_BYTES:
            return None
        return types.Part.from_bytes(data=r.content, mime_type=mime)
    except Exception as e:
        logger.info("Image download failed (%s): %s", url[:80], str(e)[:100])
        return None


async def add_image_text(items: List[Dict[str, Any]]) -> int:
    """Set item["image_text"] on items that have image_urls. Returns how many items got text. Never raises."""
    if not IMAGE_TEXT_ON or not os.getenv("GEMINI_API_KEY"):
        return 0
    from services.compliance.media import image_text_service as reader
    read = 0
    budget = IMAGE_TEXT_MAX
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for item in items:
            urls = item.get("image_urls") or []
            if not urls or budget <= 0:
                continue
            texts: List[str] = []
            for url in urls:
                if budget <= 0:
                    break
                part = await _download(client, url)
                if part is None:
                    continue
                budget -= 1
                try:
                    resp = await reader._generate([part, reader.PROMPT])
                    text = reader._clean(json.loads(resp.text or "{}").get("text", ""))
                    if text:
                        texts.append(text)
                except Exception as e:
                    logger.info("Image text read failed (%s): %s", url[:80], str(e)[:100])
            if texts:
                item["image_text"] = "\n".join(texts)
                read += 1
    return read
