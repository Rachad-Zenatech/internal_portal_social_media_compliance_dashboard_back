"""Keep published script copy separate from production directions when scoring."""
import re

VERSION = "script-voiceover-supers-sources-2"
PUBLISHED = {"voiceover", "supers"}
_ALIASES = {
    "voiceover": "voiceover", "voiceovervo": "voiceover", "vo": "voiceover",
    "voice": "voiceover", "narration": "voiceover", "dialogue": "voiceover", "audio": "voiceover",
    "text": "supers", "onscreen": "supers", "supers": "supers", "super": "supers", "onscreentext": "supers",
    "textonscreen": "supers", "screentext": "supers", "textoverlay": "supers",
    "supersonscreentext": "supers", "onscreentextsupers": "supers",
    "visual": "visuals", "visuals": "visuals", "visualfootage": "visuals",
    "visualsfootage": "visuals", "footage": "visuals",
    "time": "timing", "timecode": "timing", "timestamp": "timing",
    "title": "details", "storyline": "details", "hook": "details", "hookscore": "details",
    "editinstructions": "details", "productionnotes": "details", "notes": "details",
    "sourcesused": "details", "sourcesandfactualbasis": "details",
}


def _name(text: str) -> str | None:
    return _ALIASES.get(re.sub(r"[^a-z]", "", text.casefold()))


def _clean(text: str) -> str:
    return re.sub(r"^\s*#{1,6}\s+", "", text.replace("**", "")).strip()


def sections(text: str, title: str = "") -> dict[str, str]:
    """Read timed tables and explicitly labelled sections; unidentified wording stays review-only."""
    parts = {key: [] for key in ("voiceover", "supers", "visuals", "timing", "details")}
    parts["details"].append(title)
    columns, active = [], "details"
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or re.fullmatch(r"---\s*Page\s+\d+\s*---", stripped, re.I):
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [_clean(cell.replace(r"\|", "|")) for cell in re.split(r"(?<!\\)\|", stripped[1:-1])]
            names = [_name(cell) for cell in cells]
            if any(name in PUBLISHED for name in names) and (len(cells) == 1 or sum(name is not None for name in names) >= 2):
                columns = names
                continue
            if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            for index, cell in enumerate(cells):
                section = columns[index] if index < len(columns) else None
                parts[section or "details"].append(cell)
            continue
        if beat := re.match(r"^\[([^\]]*)\]\s*Visual:\s*(.*?)\s*\|\s*Voiceover:\s*(.*?)\s*\|\s*On-screen:\s*(.*)$", _clean(stripped), re.I):
            for key, value in zip(("timing", "visuals", "voiceover", "supers"), beat.groups()):
                parts[key].append(value.strip())
            columns, active = [], "details"
            continue
        columns = []
        cleaned = _clean(stripped)
        if match := re.match(r"^([^:]{1,50}):\s*(.*)$", cleaned):
            if name := _name(match[1]):
                active = name
                parts[active].append(match[2])
                continue
        if name := _name(cleaned):
            active = name
            continue
        # Unlabelled headings end a preceding voiceover or supers section.
        if stripped.startswith("#"):
            active = "details"
        parts[active].append(cleaned)
    return {key: "\n".join(part for part in values if part) for key, values in parts.items()}


def published(parts: dict[str, str]) -> str:
    return "\n".join(parts.get(key, "") for key in ("voiceover", "supers")).strip()
