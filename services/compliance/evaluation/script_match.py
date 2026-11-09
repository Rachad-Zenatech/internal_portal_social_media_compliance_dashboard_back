"""Script vs video.

Once the script is approved and the video's transcript is ready, compare what the video says with the approved script,
so wording nobody approved can't slip into the video. Word by word and deterministic (no model, so no quota): speech
transcripts aren't verbatim, so the verdict is lenient about a few stray words and strict about whole added or dropped
passages. The spoken words are rulebook-checked either way; this says whether they are the words that were approved.
"""
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional
from uuid import NAMESPACE_URL, UUID, uuid5

from models.compliance.compliance_model import ComplianceViolation

MATCH_AT = 0.90      # this much alike or more: the video follows the script
PARTIAL_AT = 0.70    # below MATCH_AT but here or more: some wording differs
MIN_PASSAGE = 4      # fewer differing words in a row than this are transcription noise, not listed
MAX_LISTED = 6       # passages listed per side
MAX_WORDS_SHOWN = 30

# A stable id, so a reviewer can dismiss this flag like any other.
SCRIPT_MISMATCH_ID: UUID = uuid5(NAMESPACE_URL, "zenatech-script-check:video-matches-script")

_DIRECTION = re.compile(r"\[[^\]]*\]|\([^)]*\)")  # [SCENE 2], (pause), (on screen: ...)


def _words(text: str) -> List[str]:
    text = _DIRECTION.sub(" ", text or "")
    return re.sub(r"[^\w\s']+|_", " ", text.lower().replace("’", "'")).split()


def _shown(words: List[str]) -> str:
    return " ".join(words[:MAX_WORDS_SHOWN]) + (" …" if len(words) > MAX_WORDS_SHOWN else "")


def compare(script: Optional[str], spoken: Optional[str]) -> Optional[Dict[str, Any]]:
    """How closely the spoken words follow the script, or None when either is empty."""
    a, b = _words(script or ""), _words(spoken or "")
    if not a or not b:
        return None
    ops = SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    same = sum(i2 - i1 for tag, i1, i2, _, _ in ops if tag == "equal")
    similarity = 2 * same / (len(a) + len(b))
    added: List[str] = []    # said in the video, not in the script
    missing: List[str] = []  # in the script, never said
    for tag, i1, i2, j1, j2 in ops:
        if tag in ("insert", "replace") and j2 - j1 >= MIN_PASSAGE:
            added.append(_shown(b[j1:j2]))
        if tag in ("delete", "replace") and i2 - i1 >= MIN_PASSAGE:
            missing.append(_shown(a[i1:i2]))
    status = "MATCH" if similarity >= MATCH_AT else "PARTIAL" if similarity >= PARTIAL_AT else "MISMATCH"
    return {
        "status": status,
        "similarity": round(similarity * 100),
        "script_words": len(a),
        "spoken_words": len(b),
        "added": added[:MAX_LISTED],
        "missing": missing[:MAX_LISTED],
        "added_more": max(0, len(added) - MAX_LISTED),
        "missing_more": max(0, len(missing) - MAX_LISTED),
    }


def flag_mismatch(result: Any, match: Optional[Dict[str, Any]]) -> None:
    """A video that doesn't match its approved script becomes a "needs review" flag on the check: no points lost (the
    spoken words are rule-checked either way), but the post can't read as ready, and a reviewer can dismiss it."""
    if not match or match["status"] != "MISMATCH":
        return
    said = [" ".join(p.split()[:12]) for p in match["added"][:3]]
    reasoning = (
        f"The video's speech is only {match['similarity']}% like the approved script, so it may be the wrong video, "
        "or wording that no reviewer approved has been added."
    )
    result.violations.append(ComplianceViolation(
        rule_id=SCRIPT_MISMATCH_ID,
        rule_number="Script",
        title="Video doesn't match the approved script",
        category="Script match",
        severity="MEDIUM",
        traffic_light="YELLOW",
        flagged_terms=said,
        explanation=reasoning,
        remediation_guidance="Check the right video was uploaded. If the wording changed on purpose, send the new "
                             "script back through script review.",
        status="NEEDS_REVIEW",
    ))
    result.review_count += 1
    result.is_compliant = False
    if result.traffic_light == "GREEN":
        result.traffic_light = "YELLOW"
    result.recommendations.append("CHECK: the video doesn't match the approved script. Confirm it's the right video.")
