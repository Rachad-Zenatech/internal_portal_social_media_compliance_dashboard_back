"""Non-blocking format notices for imported scripts."""
import re
from typing import List

FORMAT_WARNING = (
    "The imported document does not match the export template format. "
    "Review the separated scripts and their formatting before checking compliance."
)
_HEADERS = ["time", "visual / footage", "voiceover", "on-screen text"]


def script_import_warnings(scripts: List[tuple]) -> List[str]:
    """Compare structure, not the file extension or the script's wording."""
    for _, body in scripts:
        plain = body.replace("**", "").lower()
        rows = [[re.sub(r"\s+", " ", cell).strip() for cell in line.strip().strip("|").split("|")]
                for line in plain.splitlines() if line.lstrip().startswith("|")]
        if (_HEADERS not in rows or not re.search(r"\bstoryline\s*:", plain)
                or "hook score" not in plain or "sources used" not in plain):
            return [FORMAT_WARNING]
    return []
