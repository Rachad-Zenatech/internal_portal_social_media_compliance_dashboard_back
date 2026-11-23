"""Score-linked visual rule review of server-selected post assets, with auditable coverage."""
import asyncio
import json
import logging
import math
import os
from contextlib import asynccontextmanager

from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from models.compliance.compliance_model import ComplianceViolation, ContextEvaluation
from services.compliance.evaluation import context_evaluator
from services.compliance.evaluation.visual_review_prompt import PROMPT, RULE_NUMBERS, SCHEMA
from services.compliance.media import image_text_service
from services.compliance.media.image_finding_service import region_from_box

logger = logging.getLogger(__name__)
MAX_VIDEO_BYTES = 18 * 1024 * 1024
# Visual rule judgments need the complete compliance chain, not the smaller OCR-only list.
MODELS = [m.strip() for m in os.getenv(
    "VISUAL_COMPLIANCE_MODELS", ",".join(context_evaluator.MODELS)
).split(",") if m.strip()]


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rule_number: str
    status: str
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence: str = Field(max_length=2000)
    reasoning: str = Field(min_length=1, max_length=4000)
    box: list[FiniteFloat]
    start_sec: float = Field(ge=0, allow_inf_nan=False)
    end_sec: float = Field(ge=0, allow_inf_nan=False)


@asynccontextmanager
async def _asset_part(asset):
    """Only internally selected storage paths can be attached; never accept paths from API input."""
    from services.compliance.media.transcription_service import STORAGE_ROOT
    from services.compliance.sentiment.engine import _image_bytes, _upload_video, _delete_upload

    path = (STORAGE_ROOT / asset["storage_path"]).resolve()
    if STORAGE_ROOT not in path.parents or not path.is_file():
        raise ValueError("Asset is unavailable in storage")
    mime = asset["mime_type"]
    uploaded = None
    client = None
    try:
        if mime.startswith("image/"):
            data, mime = await asyncio.to_thread(_image_bytes, path, mime)
            part = types.Part.from_bytes(data=data, mime_type=mime)
        elif mime.startswith("video/"):
            if path.stat().st_size <= MAX_VIDEO_BYTES:
                data = await asyncio.to_thread(path.read_bytes)
                part = types.Part.from_bytes(data=data, mime_type=mime)
            else:
                client = image_text_service._get_client()
                uploaded = await _upload_video(client, str(asset["id"]))
                if not uploaded or not uploaded.uri:
                    raise ValueError("Video could not be attached for visual review")
                part = types.Part.from_uri(file_uri=uploaded.uri, mime_type=mime)
        else:
            raise ValueError("Unsupported visual asset")
        yield part
    finally:
        if uploaded:
            await _delete_upload(client, uploaded)


def _parse(response, rules):
    """Missing/duplicate rules and malformed output must never look like a successful clear."""
    raw = json.loads(response.text or "{}")
    items = raw.get("assessments", [])
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        d = dict(item)
        if "reasoning" not in d and "rationale" in d:
            d["reasoning"] = str(d.pop("rationale"))
        if "evidence" not in d:
            d["evidence"] = d.get("reasoning") or d.get("observation") or "No visual finding"
        if "box" not in d:
            f_box = (d.get("findings") or [{}])[0].get("box") if isinstance(d.get("findings"), list) else None
            d["box"] = f_box if isinstance(f_box, list) else []
        if "start_sec" not in d:
            d["start_sec"] = 0.0
        if "end_sec" not in d:
            d["end_sec"] = 0.0
        if "confidence" not in d:
            d["confidence"] = 1.0
        allowed_keys = {"rule_number", "status", "confidence", "evidence", "reasoning", "box", "start_sec", "end_sec"}
        cleaned = {k: v for k, v in d.items() if k in allowed_keys}
        normalized.append(cleaned)
    assessments = [Assessment.model_validate(item) for item in normalized]
    expected = {r.rule_number for r in rules}
    if len(assessments) != len(expected) or {a.rule_number for a in assessments} != expected:
        raise ValueError("Visual review did not assess every applicable rule exactly once")
    if any(a.status not in {"VIOLATION", "NEEDS_REVIEW", "CLEARED"} for a in assessments):
        raise ValueError("Invalid visual decision")
    return assessments


def _finding(asset, rule, assessment, model):
    """Uncertain visual evidence cannot become a confirmed critical violation."""
    from services.compliance.evaluation.compliance_service import _penalty_and_light

    a = assessment
    status = a.status
    if status == "CLEARED" and a.confidence < .8:
        status = "NEEDS_REVIEW"
    if status == "VIOLATION" and (a.confidence < .85 or not a.evidence.strip()):
        status = "NEEDS_REVIEW"
    video = asset["mime_type"].startswith("video/")
    valid_time = a.end_sec >= a.start_sec and math.isfinite(a.end_sec)
    if video and not valid_time:
        status = "NEEDS_REVIEW"
    region = region_from_box(asset["id"], a.box,
                             a.start_sec if video and valid_time else None,
                             a.end_sec if video and valid_time else None)
    if video and not valid_time:
        region = None
    if status == "VIOLATION" and region is None:
        status = "NEEDS_REVIEW"
    penalty, light = _penalty_and_light(rule)
    reason = f"Visual review of {asset['original_name']}: {a.reasoning}"
    return ComplianceViolation(
        rule_id=rule.id, rule_number=rule.rule_number, title=rule.title, category=rule.category,
        severity=rule.severity, traffic_light=light if status == "VIOLATION" else "YELLOW",
        flagged_terms=[], image_file_ids=[] if video else [asset["id"]],
        image_regions=[region] if region and status != "CLEARED" else [], visual_evidence=True,
        explanation=reason, remediation_guidance=rule.remediation_guidance,
        penalty=penalty if status == "VIOLATION" else 0, status=status,
        passage=a.evidence, context=reason,
        evaluation=ContextEvaluation(decision=status, reasoning=reason, confidence=a.confidence,
                                     subject="Visible asset content", model=model, rule_decision="NEEDS_REVIEW",
                                     source="AI" if status == a.status else "ADJUSTED"),
    )


def _merge(findings, finding):
    """One charge per rule; preserve stronger text findings and all visual locations."""
    if finding.status == "CLEARED":
        return  # Clear assessments remain in the saved visual coverage, not the issue list.
    existing = next((f for f in findings if f.rule_id == finding.rule_id and f.status != "CLEARED"), None)
    if not existing:
        findings.append(finding)
        return
    ranks = {"CLEARED": 0, "NEEDS_REVIEW": 1, "EVIDENCE_REQUIRED": 1, "VIOLATION": 2}
    strength = lambda f: (ranks[f.status], f.evaluation.confidence if f.evaluation else 1.)
    if strength(finding) > strength(existing):
        finding.image_file_ids.extend(file_id for file_id in existing.image_file_ids if file_id not in finding.image_file_ids)
        finding.image_regions.extend(r for r in existing.image_regions if r not in finding.image_regions)
        finding.explanation += "\n" + existing.explanation
        findings[findings.index(existing)] = finding
    elif finding.status != "CLEARED":
        existing.image_file_ids.extend(file_id for file_id in finding.image_file_ids if file_id not in existing.image_file_ids)
        existing.visual_evidence = True
        existing.image_regions.extend(r for r in finding.image_regions if r not in existing.image_regions)
        existing.explanation += "\n" + finding.explanation


async def review(req, rules, findings):
    """Read every post asset, recording failures explicitly instead of silently treating them as compliant."""
    selected = [r for r in rules if r.rule_number in RULE_NUMBERS]
    coverage = []
    if not selected:
        return coverage
    payload = {"post": req.text, "published_text": req.disclaimer_text, "audiences": req.audiences,
               "brands": req.brand_codes, "region": req.region,
               "evidence": [s.model_dump(mode="json") for s in req.sources],
               "rules": [r.model_dump(mode="json") for r in selected]}
    for asset in req._visual_media:
        entry = {"file_id": str(asset["id"]), "name": asset["original_name"],
                 "rule_numbers": [r.rule_number for r in selected], "status": "UNAVAILABLE"}
        try:
            async with image_text_service._semaphore, _asset_part(asset) as part:
                response = await image_text_service._generate(
                    [part, PROMPT + json.dumps(payload, ensure_ascii=False)],
                    feature="VISUAL_COMPLIANCE", response_schema=SCHEMA, models=MODELS)
            assessments = _parse(response, selected)
            model = getattr(response, "model_version", None)
            by_number = {r.rule_number: r for r in selected}
            for assessment in assessments:
                _merge(findings, _finding(asset, by_number[assessment.rule_number], assessment, model))
            entry.update(status="COMPLETED", model=model,
                         assessments=[a.model_dump() for a in assessments])
        except Exception:
            logger.warning("Visual compliance review unavailable for %s", asset["id"], exc_info=True)
            entry["message"] = "Visual review could not complete. Check again or request manual visual review."
        coverage.append(entry)
    return coverage
