"""Identify a collection title separately from its numbered or bulleted scripts."""
import re
from typing import List, Optional, Tuple

_PAGE = re.compile(r"^\s*---\s*Page\s+\d+\s*---\s*$", re.I)
_NUMBERED = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:[-\u2022\u25cf\u25aa]\s+)?"
    r"(?:(?:script|video|reel|post|episode|short|clip|story)\s*(?:#|no\.?|number)?\s*)?"
    r"(\d{1,3})(?:\s*[.):\-\u2013\u2014]\s*|\s+)(\S.*?)\s*$", re.I,
)
_BULLET = re.compile(r"^\s*(?:#{1,6}\s*)?[-\u2022\u25cf\u25aa]\s+(\S.*?)\s*$")
_HEADING = re.compile(r"^\s*(#{1,3})\s+(\S.*?)\s*$")
_TIMECODE = re.compile(r"^\s*(?:#{1,6}\s*)?\d{1,2}:\d{2}\b")


def _substantial(lines: List[str]) -> bool:
    return any(line.lstrip().startswith("|") for line in lines) or len(" ".join(lines).split()) >= 15


def _has_script_table(lines: List[str]) -> bool:
    """Word's automatic numbering can disappear, but the timed script grid remains."""
    for line in lines:
        if not line.lstrip().startswith("|"):
            continue
        cells = [re.sub(r"[^a-z]", "", cell.lower()) for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0] in {"time", "timecode", "timestamp"} and any("voiceover" in cell for cell in cells):
            return True
    return False


def _starts(lines: List[str]) -> List[Tuple[int, str]]:
    numbered = [(i, int(m[1]), m[2]) for i, line in enumerate(lines)
                if not _TIMECODE.match(line) and (m := _NUMBERED.match(line))]
    marked = [item for item in numbered if re.match(r"^\s*#|^\s*(?:script|video|reel|post)\b", lines[item[0]], re.I)]
    if len(marked) >= 2:
        numbered = marked
    if len(numbered) >= 2:
        consecutive = all(right[1] == left[1] + 1 for left, right in zip(numbered, numbered[1:]))
        substantial = all(_substantial(lines[item[0] + 1:numbered[n + 1][0] if n + 1 < len(numbered) else len(lines)])
                          for n, item in enumerate(numbered))
        if consecutive and substantial:
            return [(i, title) for i, _, title in numbered]
    bullets = [(i, m[1]) for i, line in enumerate(lines) if (m := _BULLET.match(line))]
    # Only treat bullets as script names when every item owns a script table.
    if len(bullets) >= 2 and all(any(line.lstrip().startswith("|") for line in
            lines[item[0] + 1:bullets[n + 1][0] if n + 1 < len(bullets) else len(lines)])
            for n, item in enumerate(bullets)):
        return bullets
    headings = [(i, len(m[1]), m[2]) for i, line in enumerate(lines) if (m := _HEADING.match(line))]
    table_headings = [(item[0], item[2]) for n, item in enumerate(headings)
                      if _has_script_table(lines[item[0] + 1:headings[n + 1][0] if n + 1 < len(headings) else len(lines)])]
    if len(table_headings) >= 2:
        return table_headings
    if headings and sum(level == headings[0][1] for _, level, _ in headings) == 1:
        nested = [(i, title) for i, level, title in headings[1:] if level == headings[0][1] + 1]
        if len(nested) >= 2:
            return nested
    return []


def script_import_sections(text: str) -> Tuple[Optional[str], str]:
    """Keep collection headings out of script bodies; retain the existing fallback for ambiguous text."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    starts = _starts(lines)
    if not starts:
        return None, text
    preamble = [line.strip() for line in lines[:starts[0][0]] if line.strip() and not _PAGE.match(line)]
    title = re.sub(r"^[#*\s]+|[*\s]+$", "", preamble[0])[:300] if preamble else None
    selected = {i for i, _ in starts}
    for i in range(starts[0][0], len(lines)):
        if i not in selected:
            lines[i] = re.sub(r"^\s*#{1,3}\s+", "", lines[i])
    for i, name in starts:
        lines[i] = f"# {name}"
    return title, "\n".join(lines[starts[0][0]:])
