"""Splitting a script document into the scripts it holds.

A document written in the editor holds one or more scripts; each starts with its own heading (the template's "1. Title").
Sending and approving work on the document as a whole; when it is approved each script becomes a post of its own, as
for scripts uploaded in one file (see script_batch_service)."""

import html as html_lib
import re
from typing import List, Tuple

_H2 = re.compile(r"(?=<h2\b)", re.IGNORECASE)
_BREAK = re.compile(r"</(?:p|tr|li|h[1-6]|td|th)>|<br\s*/?>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_NUMBER = re.compile(r"^\s*\d{1,3}\s*[.):\-–]\s*")
MAX_TITLE = 120


def html_to_text(html: str) -> str:
    text = html_lib.unescape(_TAGS.sub("", _BREAK.sub("\n", html or "")))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def split_document(html: str, fallback_title: str) -> List[Tuple[str, str, str]]:
    """(title, html, text) for each script, in order. A document without headings is one script titled `fallback_title`."""
    chunks = [c for c in _H2.split(html or "") if c.strip()]
    out: List[Tuple[str, str, str]] = []
    for chunk in chunks:
        m = re.match(r"\s*<h2\b[^>]*>(.*?)</h2>", chunk, re.IGNORECASE | re.DOTALL)
        text = html_to_text(chunk)
        if not text:
            continue
        title = _NUMBER.sub("", html_to_text(m.group(1)).replace("\n", " ")) if m else ""
        out.append(((title or fallback_title)[:MAX_TITLE].strip(), chunk, text))
    if not out and html_to_text(html):
        out = [(fallback_title[:MAX_TITLE], html, html_to_text(html))]
    return out
