"""Verification of quotes and rule references in sentiment findings."""

import hashlib
import re
from typing import Any, Dict, List, Optional

from models.compliance.sentiment_model import SentimentFinding, SentimentResult
from services.compliance.sentiment.config import STRICT_AUDIENCES

# ---------------------------------------------------------------------------
# Evidence checks: every quote must exist in the content
# ---------------------------------------------------------------------------

_QUOTE_CHARS = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-"})


def _norm(text: str) -> str:
    text = text.translate(_QUOTE_CHARS).lower()
    return " ".join(re.sub(r"[^\w$%']+", " ", text).split())


def _locate(quote: str, inp: Dict[str, Any]) -> Optional[Tuple[str, Optional[float], Optional[float]]]:
    """Where a quote appears: (source, start_sec, end_sec), or None if it isn't in the content."""
    parts = [p for p in (_norm(x) for x in re.split(r"\.\.\.|…", quote)) if len(p) >= 3]
    if not parts:
        return None
    for source in ("title", "caption", "script"):
        haystack = f" {_norm(inp.get(source) or '')} "
        if all(f" {p} " in haystack for p in parts):
            return source, None, None
    transcript = inp.get("transcript")
    if isinstance(transcript, list) and transcript:
        # Join the segments, remembering where each starts, so a quote spanning two segments still maps.
        offsets: List[Tuple[int, dict]] = []
        joined = " "
        for seg in transcript:
            offsets.append((len(joined), seg))
            joined += _norm(seg["text"]) + " "
        positions = [joined.find(f" {p} ") for p in parts]
        if all(pos >= 0 for pos in positions):
            # Each position is the space before a part, so the quote's own characters run from first to last
            # (exclusive). A segment covers it only if their characters overlap; the space between two segments
            # doesn't count, or a quote starting a segment would also take the previous one's start time.
            first = min(positions) + 1
            last = max(pos + 1 + len(p) for pos, p in zip(positions, parts))
            covering = [seg for start, seg in offsets if start < last and start + len(_norm(seg["text"])) > first]
            if covering:
                return "transcript", covering[0]["start_sec"], covering[-1]["end_sec"]
            return "transcript", None, None
    return None


def _match_rule(cited: Optional[str], rule_numbers: Set[str]) -> Optional[str]:
    """Map the model's citation ("Rule 1", "1", "Rule 1 (Challenge...)") to an applicable rule number."""
    if not cited:
        return None
    if cited in rule_numbers:
        return cited
    m = re.search(r"\d+", cited)
    if m:
        for number in rule_numbers:
            if re.fullmatch(rf"\D*{m.group(0)}", number):
                return number
    return None


def finding_id(f: SentimentFinding) -> str:
    """Stable id for a finding within its analysis, so reviewer feedback can point at it."""
    start = "" if f.start_sec is None else f"{f.start_sec:.1f}"
    # Appended only for image findings, so ids of findings saved before images were analysed stay the same.
    image = "" if f.image_index is None else f"|image{f.image_index}"
    return hashlib.sha1(f"{f.category}|{f.basis}|{_norm(f.quote)}|{start}{image}".encode("utf-8")).hexdigest()[:12]


def _verify(result: SentimentResult, inp: Dict[str, Any], video_attached: bool = False,
            images_attached: int = 0) -> SentimentResult:
    rule_numbers = {g["rule_number"] for g in inp["guidelines"]}
    kept = []
    seen: Set[str] = set()
    # Most severe first, so when the same wording is flagged under several categories the strongest one is kept.
    order = {"HIGH": 0, "MODERATE": 1, "LOW": 2}
    for finding in sorted(result.findings, key=lambda f: order[f.severity]):
        if finding.basis == "OBSERVED_VISUAL" and finding.image_index is not None:
            # About an attached image: it needs to point at one that was actually attached.
            if not 1 <= finding.image_index <= images_attached:
                result.unverified_findings_removed += 1
                continue
            key = f"image{finding.image_index}:{_norm(finding.quote)}"
            if key in seen:
                continue
            seen.add(key)
            finding.source, finding.start_sec, finding.end_sec = "image", None, None
            finding.guideline_rule_number = _match_rule(finding.guideline_rule_number, rule_numbers)
            kept.append(finding)
            continue
        # Only meaningful on findings about an image; kept aside for wording that turns out to be text in one.
        named_image, finding.image_index = finding.image_index, None
        if finding.basis in ("OBSERVED_VISUAL", "OBSERVED_AUDIO"):
            # About what the video shows or how it sounds, so there's no wording to check; it needs the video and
            # a time in it.
            start = finding.start_sec
            if not video_attached or start is None or start < 0:
                result.unverified_findings_removed += 1
                continue
            end = finding.end_sec if finding.end_sec is not None and finding.end_sec >= start else None
            key = f"{finding.basis}:{_norm(finding.quote)}"
            if key in seen:
                continue
            seen.add(key)
            finding.source, finding.start_sec, finding.end_sec = "video", start, end
            finding.guideline_rule_number = _match_rule(finding.guideline_rule_number, rule_numbers)
            kept.append(finding)
            continue
        where = _locate(finding.quote, inp)
        if not where and images_attached:
            # Wording that isn't in the title, caption or transcript may be text inside an image (a headline on a
            # graphic): keep it against the image it names, or the only image there is.
            index = named_image if named_image and 1 <= named_image <= images_attached else (
                1 if images_attached == 1 else None)
            if index:
                key = f"image{index}:{_norm(finding.quote)}"
                if key in seen:
                    continue
                seen.add(key)
                finding.source, finding.start_sec, finding.end_sec, finding.image_index = "image", None, None, index
                finding.guideline_rule_number = _match_rule(finding.guideline_rule_number, rule_numbers)
                kept.append(finding)
                continue
        if not where:
            result.unverified_findings_removed += 1
            continue
        key = _norm(finding.quote)
        if key in seen:  # one finding per piece of wording
            continue
        seen.add(key)
        finding.source, finding.start_sec, finding.end_sec = where
        finding.guideline_rule_number = _match_rule(finding.guideline_rule_number, rule_numbers)
        kept.append(finding)
    for finding in kept:
        finding.id = finding_id(finding)
    result.findings = kept
    if not video_attached:
        result.audio = None  # nothing was heard, so there's nothing to report
    result.tone.evidence = [q for q in result.tone.evidence if _locate(q, inp)]
    if not result.controversy.applies:
        result.controversy = result.controversy.model_copy(update={
            "level": None, "target": None, "professional_and_nonpartisan": None,
            "capability_claims_precise": None, "defense_buyer_credibility_risk": None,
        })
    result.guidelines_used = sorted(rule_numbers, key=lambda n: (len(n), n))
    return result

