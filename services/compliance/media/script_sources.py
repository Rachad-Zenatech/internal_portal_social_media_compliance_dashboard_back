"""Read optional sources from a script template without adding them to its spoken text."""
import re

from services.compliance.analytics.post_signals import URL_PATTERN

_LABEL = re.compile(r"^sources\s+used(?:\s*\(optional\))?(?:\s*[:\-]\s*|\s{2,}|$)", re.I)
_PLACEHOLDER = re.compile(r"\[?list the source names and links used to create this script\.?\]?", re.I)


def is_sources_label(value: str) -> bool:
    return bool(_LABEL.fullmatch(value.replace("**", "").strip()))


def split(text: str) -> tuple[str, str]:
    """Return the script body and citations from merged table rows or a plain-text footer."""
    body, entries = [], []
    footer = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.replace("**", "").strip() for cell in stripped[1:-1].split("|")]
            first = next((index for index, cell in enumerate(cells) if cell), None)
            if first is not None and (match := _LABEL.match(cells[first])):
                entries.append(" ".join([cells[first][match.end():], *cells[first + 1:]]))
                footer = False
                continue
        plain = stripped.lstrip("# ").replace("**", "")
        if match := _LABEL.match(plain):
            entries.append(plain[match.end():])
            footer = True
        elif footer and stripped and not stripped.startswith(("|", "#", "--- Page ")):
            entries.append(plain)
        else:
            footer = False
            body.append(line)
    urls, names = [], []
    for entry in entries:
        for part in _PLACEHOLDER.sub("", entry).split(";"):
            part = part.strip(" -[]")
            if not part or part.casefold().rstrip(".") in {"none", "n/a", "optional", "no sources"}:
                continue
            if links := [match.group() for match in URL_PATTERN.finditer(part)]:
                urls.extend(links)
            else:
                names.append(f"Source: {part}")
    listed = ["Sources:", *dict.fromkeys(urls)] if urls else []
    return "\n".join(body), "\n".join([*listed, *dict.fromkeys(names)])
