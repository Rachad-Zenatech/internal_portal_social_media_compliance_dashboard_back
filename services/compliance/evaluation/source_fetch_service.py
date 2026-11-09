"""Source fetcher: opens a link a post cites and returns its readable text.

The link comes from whoever wrote the post, so it is treated as hostile:

- only http and https, on the ordinary ports;
- the host is resolved first and refused when it points anywhere private (this machine, the local network, link-local
  and cloud metadata addresses), and again for every redirect, of which there are few;
- a short time limit and a size limit, read as a stream;
- only text, HTML and PDF come back, and what is read is capped, so a page can't flood a model request.

Nothing here uses AI. A page that can't be read (blocked, not found, needs a login or JavaScript, too large) comes back
with a plain reason the author can act on. Pages are remembered for a few minutes, so checking again costs nothing.
"""

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

from services.compliance.media import document_layout

logger = logging.getLogger(__name__)

TIMEOUT_SEC = float(os.getenv("SOURCE_FETCH_TIMEOUT_SEC", "12"))
MAX_BYTES = int(os.getenv("SOURCE_FETCH_MAX_BYTES", str(3 * 1024 * 1024)))
MAX_REDIRECTS = 4
# How long a connection (the TCP and TLS handshake) gets, and how many times it is tried.
CONNECT_TIMEOUT_SEC = float(os.getenv("SOURCE_FETCH_CONNECT_TIMEOUT_SEC", "5"))
CONNECT_TRIES = int(os.getenv("SOURCE_FETCH_CONNECT_TRIES", "4"))
# What a model is given of one source, at most.
MAX_TEXT_CHARS = int(os.getenv("SOURCE_MAX_CHARS", "60000"))
# Under this, a page has nothing to check a claim against (a script-built page, a login wall, an empty shell).
MIN_TEXT_CHARS = 200
CACHE_TTL_SEC = int(os.getenv("SOURCE_FETCH_CACHE_SECONDS", "900"))
USER_AGENT = "ZenaTech-Compliance-SourceCheck/1.0 (internal compliance tool)"
ALLOWED_PORTS = {None, 80, 443}

# OK, or why not: BLOCKED (refused for safety), NOT_FOUND, FORBIDDEN (login or bot wall), TIMEOUT, TOO_LARGE,
# UNSUPPORTED (a type that isn't read), EMPTY (nothing readable), ERROR.
OK = "OK"


@dataclass
class FetchedSource:
    url: str
    ok: bool
    status: str
    # What to tell the author when it isn't ok.
    error: Optional[str] = None
    final_url: Optional[str] = None
    title: Optional[str] = None
    published: Optional[str] = None
    text: str = ""
    content_hash: Optional[str] = None


_cache: Dict[str, Tuple[float, FetchedSource]] = {}
_CACHE_MAX = 128


class _Refused(Exception):
    pass


# --- Safety ----------------------------------------------------------------------------------------------------------

def _forbidden(ip: ipaddress._BaseAddress) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
            or ip.is_unspecified or not ip.is_global)


async def _check_destination(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise _Refused("Only web links (http or https) can be opened.")
    if parts.username or parts.password:
        raise _Refused("Links with a login in them aren't opened.")
    try:
        port = parts.port
    except ValueError:
        raise _Refused("The link's port isn't valid.")
    if port not in ALLOWED_PORTS:
        raise _Refused("Only the standard web ports are opened.")
    host = parts.hostname
    if not host:
        raise _Refused("The link has no address.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port or (443 if parts.scheme == "https" else 80),
                                                              type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise _Refused("The address of this link can't be found.")
    if not infos:
        raise _Refused("The address of this link can't be found.")
    for info in infos:
        if _forbidden(ipaddress.ip_address(info[4][0].split("%")[0])):
            raise _Refused("This link points to a private address, so it isn't opened.")


# --- Reading the page ------------------------------------------------------------------------------------------------

_SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "iframe", "template",
         "button", "select", "dialog"}
_BREAK = {"p", "div", "br", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "main",
          "table", "blockquote", "figcaption", "dt", "dd", "pre"}


class _Text(HTMLParser):
    """Visible text of a page, with the article's own text kept apart (it beats menus and related-story lists)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og_title: Optional[str] = None
        self.published: Optional[str] = None
        self.all: List[str] = []
        self.article: List[str] = []
        self._skip = 0
        self._article_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            content = a.get("content", "").strip()
            if key == "og:title" and content:
                self.og_title = content
            if key in ("article:published_time", "date", "pubdate", "datepublished", "og:published_time") and content:
                self.published = self.published or content
        if tag == "time" and a.get("datetime") and not self.published:
            self.published = a["datetime"].strip()
        if tag in _SKIP:
            self._skip += 1
        if tag in ("article", "main"):
            self._article_depth += 1
        if tag in _BREAK:
            self._put("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _SKIP and self._skip:
            self._skip -= 1
        if tag in ("article", "main") and self._article_depth:
            self._article_depth -= 1
        if tag in _BREAK:
            self._put("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if not self._skip:
            self._put(data)

    def _put(self, s: str) -> None:
        self.all.append(s)
        if self._article_depth and not self._skip:
            self.article.append(s)


def _tidy(raw: str) -> str:
    lines, seen = [], set()
    for line in raw.replace("\xa0", " ").splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        # Menus and share buttons repeat; the body doesn't.
        if len(line) < 40 and line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines)


def html_to_text(html: str) -> Tuple[str, Optional[str], Optional[str]]:
    """(text, title, published date) of an HTML page."""
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:  # broken markup: keep what was read
        pass
    body = _tidy("".join(p.article))
    if len(body) < 600:  # no article element, or a stub: the whole visible page
        body = _tidy("".join(p.all))
    title = " ".join((p.og_title or p.title or "").split()) or None
    return body, title, p.published


def _decode(data: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "cp1252"]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _fail(url: str, status: str, error: str) -> FetchedSource:
    return FetchedSource(url=url, ok=False, status=status, error=error)


async def _get_once(client: httpx.AsyncClient, url: str) -> Tuple[int, httpx.Headers, bytes, httpx.Request]:
    """One request, its body read as a stream and cut off at the size limit."""
    async with client.stream("GET", url) as response:
        if response.status_code in (301, 302, 303, 307, 308) or response.status_code >= 400:
            return response.status_code, response.headers, b"", response.request
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BYTES:
            raise OverflowError
        chunks: List[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise OverflowError
            chunks.append(chunk)
        return response.status_code, response.headers, b"".join(chunks), response.request


async def _download(url: str) -> Tuple[bytes, str, str]:
    """(bytes, content type, final url), following a few redirects, checking every destination. A connection that
    doesn't get through is tried again: some networks drop an occasional first packet, and a retry costs a moment."""
    current = url
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=httpx.Timeout(TIMEOUT_SEC, connect=CONNECT_TIMEOUT_SEC), trust_env=False,
        verify=True,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.8"},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await _check_destination(current)
            for attempt in range(CONNECT_TRIES):
                try:
                    status, headers, body, request = await _get_once(client, current)
                    break
                except (httpx.ConnectTimeout, httpx.ConnectError):
                    if attempt == CONNECT_TRIES - 1:
                        raise
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                current = urljoin(current, headers["location"])
                continue
            if status >= 400:
                raise httpx.HTTPStatusError("error status", request=request, response=httpx.Response(status, request=request))
            return body, headers.get("content-type", ""), current
    raise _Refused("The link redirects too many times.")


async def fetch(url: str) -> FetchedSource:
    """Open a link and return its readable text, or the reason it couldn't be read."""
    hit = _cache.get(url)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SEC:
        return hit[1]
    result = await _fetch(url)
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[url] = (time.monotonic(), result)
    return result


async def _fetch(url: str) -> FetchedSource:
    try:
        data, content_type, final = await _download(url)
    except _Refused as e:
        return _fail(url, "BLOCKED", str(e))
    except OverflowError:
        return _fail(url, "TOO_LARGE", "The page is too large to read. Attach the part that matters as a document.")
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code in (401, 402, 403, 429):
            return _fail(url, "FORBIDDEN", "The site wouldn't let the checker read this page (login, paywall or bot "
                                           "protection). Attach a copy as a supporting document.")
        if code in (404, 410):
            return _fail(url, "NOT_FOUND", "The page wasn't found (the link may be wrong, cut short or removed).")
        return _fail(url, "ERROR", f"The site answered with an error ({code}).")
    except (httpx.TimeoutException, asyncio.TimeoutError):
        return _fail(url, "TIMEOUT", "The site took too long to answer.")
    except httpx.HTTPError as e:
        logger.info("Source fetch failed for %s: %s", url, str(e)[:160])
        return _fail(url, "ERROR", "The page couldn't be opened.")

    kind = content_type.split(";")[0].strip().lower()
    title = published = None
    if kind == "application/pdf" or final.lower().split("?")[0].endswith(".pdf"):
        text = await asyncio.to_thread(document_layout.pdf_to_text, data) or ""
        if not text.strip():
            return _fail(url, "UNSUPPORTED", "This PDF has no text the checker can read (a scan, perhaps). Attach it "
                                             "as a supporting document instead.")
        text = _tidy(text)
    elif kind in ("text/html", "application/xhtml+xml", "") or "html" in kind:
        text, title, published = html_to_text(_decode(data, content_type))
    elif kind.startswith("text/"):
        text = _tidy(_decode(data, content_type))
    else:
        return _fail(url, "UNSUPPORTED", f"This kind of file ({kind or 'unknown'}) isn't read.")

    if len(text) < MIN_TEXT_CHARS:
        return _fail(url, "EMPTY", "The page has almost no readable text (it may need JavaScript or a login). Attach "
                                   "a copy as a supporting document.")
    text = text[:MAX_TEXT_CHARS]
    return FetchedSource(
        url=url, ok=True, status=OK, final_url=final, title=title, published=published, text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
