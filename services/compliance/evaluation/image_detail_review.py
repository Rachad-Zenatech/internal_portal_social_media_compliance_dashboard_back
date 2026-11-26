"""Do not silently clear a scoped crime-text rule when grounded crop evidence remains uncertain."""
import json
import math
import re


def words(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def apply(assessments, asset, rules):
    rule = rules.get("Rule 28")
    if rule is None:
        return assessments
    regions = asset.get("image_text_details") or []
    regions = json.loads(regions) if isinstance(regions, str) else regions
    candidates = []
    for region in regions if isinstance(regions, list) else []:
        if not isinstance(region, dict):
            continue
        text, confidence, box = region.get("text"), region.get("confidence"), region.get("box")
        if not isinstance(text, str) or isinstance(confidence, bool) or not isinstance(confidence, (int,float)) or not math.isfinite(confidence) or not .5 <= confidence <= 1:
            continue
        if not isinstance(box,list) or len(box)!=4 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in box):
            continue
        if not (0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000):
            continue
        normalized = f" {words(text)} "
        if any(f" {words(term)} " in normalized for term in getattr(rule, "exception_terms", []) or [] if words(term)):
            continue
        if any(f" {words(term)} " in normalized for term in rule.prohibited_terms or [] if words(term)):
            candidates.append(region)
    if not candidates:
        return assessments
    candidate = max(candidates,key=lambda r:r["confidence"])
    return [a.model_copy(update={"status":"NEEDS_REVIEW", "confidence":min(candidate["confidence"], .8),
            "evidence_basis":"OBSERVED_CONTENT", "evidence":candidate["text"], "box":candidate["box"],
            "reasoning":"An enlarged image section suggests prohibited wording, but the letters are uncertain. A reviewer should inspect this specific area."})
            if a.rule_number == "Rule 28" and a.status == "CLEARED" else a for a in assessments]
