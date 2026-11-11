"""Splitting one document into the scripts it holds.

A creator uploads a single document with several scripts in it. They are told apart by what the document itself uses, in
this order, and the first way that finds at least two scripts wins:

  1. numbered markers on their own line: "Script 1", "Video 2: Launch day", "Reel #3 - Teaser", "Post 4"
  2. divider lines: "---", "===", "***" (three or more)
  3. markdown headings: "# Launch day", "## Teaser"
  4. "Title:" lines

A document none of these split is one script. The creator can still rename, edit, add and delete scripts before sending,
so a wrong guess costs a click, not a rewrite."""

import re
from typing import List, Optional, Tuple

MAX_TITLE_CHARS = 120

_MARKER = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:script|video|reel|post|episode|short|clip|story)\s*(?:#|no\.?|number)?\s*(\d+)\b\s*[:.\-–—)]*\s*(.*)$",
    re.IGNORECASE,
)
_DIVIDER = re.compile(r"^\s*(?:-{3,}|={3,}|\*{3,}|_{3,})\s*$")
_HEADING = re.compile(r"^\s*(#{1,2})\s+(\S.*?)\s*#*\s*$")
_TITLE_LINE = re.compile(r"^\s*title\s*[:\-–—]\s*(\S.*)$", re.IGNORECASE)


def _clip(title: str) -> str:
    title = " ".join(title.split()).strip(" -:–—#*_")
    return title[:MAX_TITLE_CHARS].rstrip()


def _first_line(lines: List[str]) -> Tuple[Optional[str], List[str]]:
    """The first filled line as a title candidate, and the lines after it."""
    for i, line in enumerate(lines):
        if line.strip():
            return _clip(line), lines[i + 1:]
    return None, []


def _by_starts(lines: List[str], starts: List[Tuple[int, Optional[str]]]) -> List[Tuple[Optional[str], List[str]]]:
    """Chunks that begin at each start line (its own title, if the marker line carried one). Text before the first start
    is its own chunk only when it has real content; a title page or a note is not a script."""
    chunks: List[Tuple[Optional[str], List[str]]] = []
    first = starts[0][0]
    if any(line.strip() for line in lines[:first]) and len(" ".join(lines[:first]).split()) >= 15:
        chunks.append((None, lines[:first]))
    for n, (at, title) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        chunks.append((title, lines[at + 1:end]))
    return chunks


def _by_dividers(lines: List[str]) -> List[Tuple[Optional[str], List[str]]]:
    chunks: List[Tuple[Optional[str], List[str]]] = []
    current: List[str] = []
    for line in lines:
        if _DIVIDER.match(line):
            chunks.append((None, current))
            current = []
        else:
            current.append(line)
    chunks.append((None, current))
    return chunks


def split_scripts(text: str) -> List[Tuple[str, str]]:
    """(title, body) for each script in the document, in order. Never empty for a document with any text."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not any(line.strip() for line in lines):
        return []

    chunks: List[Tuple[Optional[str], List[str]]] = []
    # A marker with no title of its own ("Script 2") is titled by it; the body is everything after it.
    markers = [(i, _clip(m.group(2)) or f"Script {m.group(1)}") for i, line in enumerate(lines) if (m := _MARKER.match(line))]
    if len(markers) >= 2:
        chunks = _by_starts(lines, markers)
    if len(chunks) < 2:
        divided = _by_dividers(lines)
        if sum(1 for _, c in divided if any(x.strip() for x in c)) >= 2:
            chunks = divided
    if len(chunks) < 2:
        headings = [(i, _clip(m.group(2))) for i, line in enumerate(lines) if (m := _HEADING.match(line))]
        if len(headings) >= 2:
            chunks = _by_starts(lines, headings)
    if len(chunks) < 2:
        titles = [(i, _clip(m.group(1))) for i, line in enumerate(lines) if (m := _TITLE_LINE.match(line))]
        if len(titles) >= 2:
            chunks = _by_starts(lines, titles)
    if len(chunks) < 2:
        chunks = [(None, lines)]

    out: List[Tuple[str, str]] = []
    for title, body_lines in chunks:
        body_lines = list(body_lines)
        if not title:
            title, body_lines = _first_line(body_lines) if len(chunks) > 1 else (None, body_lines)
        body = "\n".join(body_lines).strip()
        if not body and not title:
            continue
        out.append((title or "", body))

    # Numbered fallback titles for scripts that have none, and a document with no usable title for its only script.
    result: List[Tuple[str, str]] = []
    for n, (title, body) in enumerate(out, start=1):
        if not body:
            # A heading with nothing under it is a title page, not a script.
            continue
        result.append((title or f"Script {len(result) + 1}", body))
    if not result and out:
        result = [(out[0][0] or "Script 1", out[0][1] or out[0][0])]
    return result
