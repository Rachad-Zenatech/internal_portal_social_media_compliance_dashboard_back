"""Source links and image text for scraped X posts and news items.

- source_links: where the item can be checked: the post's own link, plus any outside link inside the post.
- image_urls: the pictures the post shows.
- image_text: the words in those pictures, read with Gemini (the same reader the submissions' images use), bounded by
  SCRAPE_IMAGE_TEXT_MAX images per run. Empty when Gemini isn't configured or the picture has no readable text.
"""

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
