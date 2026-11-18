"""Script vs video.

Once the script is approved and the video's transcript is ready, compare what the video says with the approved script,
so wording nobody approved can't slip into the video. Word by word and deterministic (no model, so no quota): speech
transcripts aren't verbatim, so the verdict is lenient about a few stray words and strict about whole added or dropped
passages. The spoken words are rulebook-checked either way; this says whether they are the words that were approved.
"""
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from models.compliance.compliance_model import ComplianceCheckResponse, ComplianceViolation

MATCH_AT = 0.90      # this much alike or more: the video follows the script
PARTIAL_AT = 0.70    # below MATCH_AT but here or more: some wording differs
MIN_PASSAGE = 4      # fewer differing words in a row than this are transcription noise, not listed
MAX_LISTED = 6       # passages listed per side
MAX_WORDS_SHOWN = 30

# A stable id, so a reviewer can dismiss this flag like any other.
SCRIPT_MISMATCH_ID: UUID = uuid5(NAMESPACE_URL, "zenatech-script-check:video-matches-script")

SCRIPT_COMPARISON_ID: UUID = uuid5(NAMESPACE_URL, "zenatech-script-check:comparison-unavailable")

_DIRECTION = re.compile(r"\[[^\]]*\]|\([^)]*\)")  # [SCENE 2], (pause), (on screen: ...)


def _words(text: str) -> List[str]:
    text = _DIRECTION.sub(" ", text or "")
    return re.sub(r"[^\w\s']+|_", " ", text.lower().replace("’", "'")).split()


def _shown(words: List[str]) -> str:
    return " ".join(words[:MAX_WORDS_SHOWN]) + (" …" if len(words) > MAX_WORDS_SHOWN else "")


def _script_column(script: str, name: str) -> Optional[str]:
    """Extract a production-table column, or return None when the column is absent."""
    lines = []
    column = None
    found = False
    for line in script.splitlines():
        row = line.strip()
        if not (row.startswith("|") and row.endswith("|")):
            if row:
                column = None
            continue
        cells = [cell.strip() for cell in row[1:-1].split("|")]
        headings = [cell.strip("*_ ").casefold() for cell in cells]
        if "voiceover" in headings and "time" in headings:
            column = headings.index(name) if name in headings else None
            found = found or column is not None
        elif column is not None and column < len(cells):
            lines.append(cells[column])
    return "\n".join(lines).strip() if found else None


def compare(script: Optional[str], spoken: Optional[str], on_screen: str = "") -> Optional[Dict[str, Any]]:
    """Compare dialogue and overlays separately, weighting similarity by their word counts."""
    script = script or ""
    voiceover = _script_column(script, "voiceover")
    overlays = _script_column(script, "on-screen text")
    channels = [(_words(script if voiceover is None else voiceover), _words(spoken or ""))]
    if overlays is not None:
        channels.append((_words(overlays), _words(on_screen)))
    planned_words = sum(len(planned) for planned, _ in channels)
    actual_words = sum(len(actual) for _, actual in channels)
    if not planned_words or not actual_words:
        return None
    same = 0
    added: List[str] = []
    missing: List[str] = []
    for planned, actual in channels:
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, planned, actual, autojunk=False).get_opcodes():
            if tag == "equal":
                same += i2 - i1
            if tag in ("insert", "replace") and j2 - j1 >= MIN_PASSAGE:
                added.append(_shown(actual[j1:j2]))
            if tag in ("delete", "replace") and i2 - i1 >= MIN_PASSAGE:
                missing.append(_shown(planned[i1:i2]))
    similarity = 2 * same / (planned_words + actual_words)
    status = "MATCH" if similarity >= MATCH_AT else "PARTIAL" if similarity >= PARTIAL_AT else "MISMATCH"
    return {
        "status": status,
        "similarity": round(similarity * 100),
        "script_words": planned_words,
        "spoken_words": actual_words,
        "added": added[:MAX_LISTED],
        "missing": missing[:MAX_LISTED],
        "added_more": max(0, len(added) - MAX_LISTED),
        "missing_more": max(0, len(missing) - MAX_LISTED),
    }


def assess_video(script: Optional[str], spoken: Optional[str], video: Optional[Mapping], *, on_screen: str = "") -> Optional[Dict[str, Any]]:
    """Keep a missing transcript distinct from a video that follows its retained approved script."""
    if not script or not video:
        return None
    state = video.get("transcript_status")
    if state == "COMPLETED":
        comparison = compare(script, spoken, on_screen)
        if comparison is not None:
            return comparison
    pending = state not in ("FAILED", "COMPLETED")
    message = ("The approved script is saved. Waiting for the video transcript before comparing it."
               if pending else "The approved script is saved, but the video could not be compared with it. "
               "Retry video transcription and run the compliance check again."
               if state == "FAILED" else "The approved script is saved, but no readable video transcript was found. "
               "A reviewer needs to check this video against the script.")
    return {"status": "PENDING" if pending else "UNAVAILABLE", "similarity": None,
            "script_words": len(_words(script)), "spoken_words": 0, "added": [], "missing": [],
            "added_more": 0, "missing_more": 0, "message": message}


def flag_mismatch(result: ComplianceCheckResponse, match: Optional[Dict[str, Any]]) -> None:
    """Flag script comparisons that need review and deduct points for low similarity."""
    if not match:
        return
    unavailable = match["status"] == "UNAVAILABLE"
    similarity = match["similarity"]
    if not unavailable and (similarity is None or similarity >= 80):
        return
    penalty = 0 if similarity is None else 10 if similarity < 50 else 5
    said = [" ".join(p.split()[:12]) for p in match["added"][:3]]
    reasoning = match["message"] if unavailable else (
        f"The video's wording is only {match['similarity']}% like the approved script, so it may be the wrong video, "
        "or wording that no reviewer approved has been added."
    )
    result.violations.append(ComplianceViolation(
        rule_id=SCRIPT_COMPARISON_ID if unavailable else SCRIPT_MISMATCH_ID,
        rule_number="Script",
        title="Video could not be compared with the approved script" if unavailable else "Video doesn't match the approved script",
        category="Script match",
        severity="MEDIUM",
        traffic_light="YELLOW",
        flagged_terms=said,
        explanation=reasoning,
        remediation_guidance=match["message"] if unavailable else "Check the right video was uploaded. If the wording changed on purpose, send the new "
                             "script back through script review.",
        status="NEEDS_REVIEW",
        penalty=penalty,
    ))
    if penalty:
        result.raw_score = max(0, result.raw_score - penalty)
        result.score = result.raw_score if result.score_cap is None else min(result.raw_score, result.score_cap)
    result.review_count += 1
    result.is_compliant = False
    if result.traffic_light == "GREEN":
        result.traffic_light = "YELLOW"
    result.recommendations.append(match["message"] if unavailable else "CHECK: the video doesn't match the approved script. Confirm it's the right video.")
