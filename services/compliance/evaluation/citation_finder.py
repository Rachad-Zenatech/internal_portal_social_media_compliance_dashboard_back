"""Citation finder: what a post says it got its claims from.

A post that states a fact, a figure or a date is expected to cite where it comes from, either in the caption or
printed on the image. This module finds those citations in the text the check reads (the caption, the script and the
text read from the images). It is plain pattern matching, no AI: the same answer every time and no quota.

Two kinds of citation come back:

- URL    a link the post points to as its source ("Read the full article: https://..."). It can be opened and read.
- NAMED  a source given by name only ("Source: Project Agincourt, Memorandum, Secretary of War, September 30, 2026").
         There is nothing to open, so the author has to attach a copy or give a link before it can be checked.

A link that is only a call to action ("Learn more: zenatech.com", "Book a demo: ...") is not a citation, and neither is
a link to a social media profile.
"""

import re
from dataclasses import dataclass
from typing import List, Literal, Optional, Tuple

from services.compliance.analytics import post_signals

# Bump when what counts as a citation changes.
VERSION = "citations-1"

# At most this many citations are followed per post: each costs a fetch and a model call.
MAX_CITATIONS = 6

# The check's text marks where the text read from the images starts (see submission_service._run_check).
_IMAGE_MARK = re.compile(r"^\s*Text in image \d+\s*:", re.IGNORECASE)

# "Source: ...", "Sources: ...", "Via: ...", "Data from: ...", at the start of a line or after "(", a bullet or a sentence.
_SOURCE_LINE = re.compile(
    r"(?:^|[(\[*•†]\s*|(?<=[.!?])\s+)"
    r"(?P<cue>sources?(?:\s+links?)?|source\s+cited|references?|citations?|via|data\s+(?:from|source)|image\s+source"
    r"|photo\s+(?:source|credit))"
    r"\s*[:–—-]\s*(?P<rest>.+)$",
    re.IGNORECASE,
)
# A "Sources:" heading on a line of its own: every link under it, until a blank line, is given as a source.
_SOURCES_HEADING = re.compile(r"^\s*(?:sources?(?:\s+links?)?|references?|citations?)\s*[:–—-]\s*$", re.IGNORECASE)
# "According to Gartner, ...", "per the FAA", "as reported by Reuters": a named source in the sentence itself.
_ATTRIBUTION = re.compile(
    r"\b(?P<cue>(?i:according\s+to|as\s+reported\s+(?:by|in)|reported\s+(?:by|in)|data\s+from|per))\s+(?:the\s+)?"
    r"(?P<name>[A-Z][\w&.'’-]*(?:\s+(?:of\s+|for\s+|and\s+|&\s+)?[A-Z][\w&.'’-]*){0,6})"
)
# Words right before a link that say it is where the claim comes from.
_SOURCE_CUE = re.compile(
    r"(sources?|read\s+the\s+full|full\s+(?:article|report|release|story|study|text)|read\s+more\s+about|according\s+to|"
    r"via|study|report|press\s+release|announcement|memo(?:randum)?|filing|details\s+here|read\s+the|see\s+the|reference)",
    re.IGNORECASE,
)
# Links to a social media profile or post aren't something a claim can be checked against.
_SOCIAL_HOSTS = ("facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "youtu.be",
                 "tiktok.com", "threads.net", "reddit.com", "t.co", "bit.ly", "lnkd.in")
_TRAILING = ".,;:!?)]}'\"”’"
# A link cut short ("zenatech.com/zenatech-unveils-stacklens-ai.../"): the dots stand in for the rest of it.
_CUT_SHORT = re.compile(r"\.{2,}|…")


@dataclass(frozen=True)
class Citation:
    kind: Literal["URL", "NAMED"]
    # The link (URL) or the cited text as written (NAMED).
    reference: str
    where: Literal["caption", "image"]
    # The words that marked it as a citation ("Source:", "Read the full article", "link in the post").
    cue: str
    # The link ends in "..." so it can't be opened as written.
    truncated: bool = False


def _clean_link(raw: str) -> str:
    link = raw.strip().rstrip(_TRAILING) if not _CUT_SHORT.search(raw) else raw.strip().rstrip(",;:!?)]}'\"")
    if not re.match(r"^https?://", link, re.IGNORECASE):
        link = "https://" + link
    return link


def _host_and_path(link: str) -> Tuple[str, str]:
    rest = re.sub(r"^https?://", "", link, flags=re.IGNORECASE)
    host, _, path = rest.partition("/")
    host = host.split("?", 1)[0].split("#", 1)[0].lower()
    host = host[4:] if host.startswith("www.") else host
    return host, "/" + path.split("?", 1)[0].split("#", 1)[0]


def _article_like(path: str) -> bool:
    """A path that names a page (an article, a report, a PDF), not a home page or a section front."""
    p = path.strip("/")
    if not p:
        return False
    if re.search(r"\.(pdf|html?|aspx?|docx?)$", p, re.IGNORECASE):
        return True
    return p.count("/") >= 1 or (len(p) >= 20 and bool(re.search(r"[-_]", p)))


def _is_social(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in _SOCIAL_HOSTS)


def _named_parts(text: str) -> List[str]:
    """The sources in a "Source: ..." line: links and hashtags are taken out; several are split on ";"."""
    text = post_signals.URL_PATTERN.sub(" ", text)
    text = re.sub(r"(?<![\w#&])[#$][A-Za-z][\w]*", " ", text)
    parts = [" ".join(p.split()).strip(" ,;:-–—|") for p in re.split(r";|\s\|\s", text)]
    return [p for p in parts if len(re.findall(r"[A-Za-z]{2,}", p)) >= 2]


def _continues(previous: str, line: str) -> bool:
    """Whether a line carries on the source line before it. Image text breaks a long source over lines, so a source
    that doesn't end in a full stop runs on until it does, a blank line, or the next hashtag, cashtag or other cue."""
    if not line.strip() or not previous.strip():
        return False
    if previous.rstrip()[-1] in ".!?)":
        return False
    if re.match(r"^\s*[#$@]", line) or _IMAGE_MARK.match(line) or _SOURCE_LINE.search(line):
        return False
    return not post_signals.URL_PATTERN.search(line)


def find(text: str) -> List[Citation]:
    """The citations in this text, each once, in the order they appear, at most MAX_CITATIONS."""
    lines = (text or "").splitlines()
    found: List[Citation] = []
    seen = set()
    in_image = False
    under_heading = False

    def add(c: Citation) -> None:
        key = (c.kind, re.sub(r"\W+", "", c.reference.lower()))
        if key not in seen:
            seen.add(key)
            found.append(c)

    for n, line in enumerate(lines):
        if _IMAGE_MARK.match(line):
            in_image = True
        where: Literal["caption", "image"] = "image" if in_image else "caption"
        if _SOURCES_HEADING.match(line):
            under_heading = True
        elif not line.strip():
            under_heading = False

        # 1. "Source: ..." lines (and the lines that carry one on).
        m = _SOURCE_LINE.search(line)
        if m:
            rest = m.group("rest")
            last, k = rest, n
            # A source given only as a link is complete; a named one may run on over the next lines.
            while _named_parts(rest) and k + 1 < len(lines) and k - n < 3 and _continues(last, lines[k + 1]):
                k += 1
                last = lines[k]
                rest += " " + last.strip()
            for part in _named_parts(rest):
                add(Citation("NAMED", part, where, m.group("cue").strip().capitalize() + ":"))

        # 2. A named source inside a sentence ("according to Gartner").
        for a in _ATTRIBUTION.finditer(line):
            name = a.group("name").strip(" .,'")
            if len(name) >= 3 and name.lower() not in {"the", "a", "an"}:
                add(Citation("NAMED", name, where, a.group("cue").strip().lower()))

        # 3. Links.
        for u in post_signals.URL_PATTERN.finditer(line):
            raw = u.group(0)
            link = _clean_link(raw)
            host, path = _host_and_path(link)
            before = line[max(0, u.start() - 70):u.start()]
            cue = _SOURCE_CUE.search(before)
            # Without a cue, only a link to a page of its own counts: "Learn more: zenatech.com" is an invitation, not
            # a source, while "Learn more: zenatech.com/some-article-title" is the article the post is about.
            if not cue and not under_heading and (_is_social(host) or not _article_like(path)):
                continue
            add(Citation(
                "URL", link, where, cue.group(1).strip().lower() if cue else "sources" if under_heading else "link in the post",
                truncated=bool(_CUT_SHORT.search(raw)),
            ))

    return found[:MAX_CITATIONS]


def describe(c: Citation) -> Optional[str]:
    """Why a citation can't be checked as it stands, or None when it can."""
    if c.kind == "URL" and c.truncated:
        return ("The link is cut short with \"...\", so it can't be opened. Paste the full link into the caption "
                "(or attach the document).")
    if c.kind == "NAMED":
        return ("Cited by name only, so there is nothing to open. Attach a copy under Supporting documents, or add a "
                "link to it in the caption, and check again.")
    return None
