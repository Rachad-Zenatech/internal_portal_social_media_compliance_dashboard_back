"""A declared mixed-footage video needs a readable disclosure once, anywhere on screen."""
import re
from uuid import UUID
from models.compliance.compliance_model import ComplianceViolation

VERSION = "video-footage-disclosure-1"
RULE_ID = UUID("e9d67f06-c9e5-4fd0-8589-dde543a8f914")
DISCLAIMER = "This video may include stock, third-party or AI-generated footage."
_ORIGIN = r"(?:stock|third[ -]?party|ai[ -]?(?:generated|created)|computer[ -]generated|synthetic|licensed)"
_MEDIA = r"(?:footage|clips?|video|visuals?|content|images?)"


def find_disclaimer(text):
    """Match a disclosure about this video's media, never an ordinary mention of an AI product."""
    normalized = re.sub(r"\s+", " ", (text or "").replace("\u2011", "-").replace("\u2013", "-")).strip()
    patterns = [rf"\b{_ORIGIN}(?:[ ,/-]+(?:or|and)?\s*{_ORIGIN})*\s+{_MEDIA}\b",
                rf"\b{_MEDIA}\s+(?:(?:is|are|was|were|may be|includes?)\s+)?{_ORIGIN}\b"]
    for pattern in patterns:
        for match in re.finditer(pattern, normalized, re.I):
            before = normalized[max(0, match.start() - 24):match.start()].lower()
            if not re.search(r"\b(?:no|not|without|never)(?:\s+(?:use|uses|contain|contains|include|includes|any|using))*\s*$", before):
                return match.group()
    return None


def finding(video, *, uncertain=False):
    explanation = ("You declared that this video includes stock, third-party or AI-generated footage. "
                   + ("We couldn't finish checking the video for its disclaimer." if uncertain else
                      "We couldn't find a readable footage disclaimer in the video."))
    return ComplianceViolation(
        rule_id=RULE_ID, rule_number="Video disclosure", title="Missing video footage disclaimer",
        category="Video disclosure", severity="MEDIUM", traffic_light="YELLOW", status="NEEDS_REVIEW",
        kind="MISSING_DISCLAIMER", penalty=0, flagged_terms=[],
        passage="Creator declared stock, third-party or AI-generated footage.",
        explanation=explanation, remediation_guidance=f'Add this readable text anywhere in the video, at least once: "{DISCLAIMER}" Then upload the updated video and check again.',
        missing_disclaimers=[DISCLAIMER], visual_evidence=True, image_file_ids=[video["id"]],
    )


async def check(row, material, result):
    video = material.video
    if row.get("video_third_party_footage") is not True or not video:
        return {"status": "NOT_REQUIRED"}
    if video["transcript_status"] not in ("COMPLETED", "FAILED"):
        return {"status": "PENDING"}
    # Only actual on-screen observations count. Caption text, a script or a source never substitutes.
    on_screen = [segment for segment in [*material.segments, *material.visual_segments]
                 if segment.get("source") == "on_screen"]
    for segment in on_screen:
        match = find_disclaimer(segment.get("text", ""))
        if match:
            return {"status": "FOUND", "evidence": match, "at_sec": segment.get("start_sec")}
    from services.compliance.media.video_disclaimer_scan import scan
    scanned = await scan(video)
    if scanned["status"] != "FOUND":
        result.violations.append(finding(video, uncertain=scanned["status"] == "UNAVAILABLE"))
    return scanned
