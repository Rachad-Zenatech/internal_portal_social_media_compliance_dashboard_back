"""Score-linked visual rule review of server-selected post assets, with auditable coverage."""
import asyncio
import hashlib
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from typing import Literal

from services.ai.core import ai_types as types
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from models.compliance.compliance_model import ComplianceViolation, ContextEvaluation
from services.compliance.evaluation import context_evaluator, visual_response, image_detail_review
from services.compliance.evaluation.visual_review_prompt import PROMPT, PROMPT_VERSION, RULE_NUMBERS, SCHEMA
from services.compliance.media import image_text_service
from services.compliance.media.image_finding_service import region_from_box
from services.shared.core.cache_service import AsyncInMemoryCache

logger = logging.getLogger(__name__)
MAX_VIDEO_BYTES = 18 * 1024 * 1024
_VISUAL_CACHE = AsyncInMemoryCache(maxsize=1000)
_VISUAL_CACHE_TTL_SEC = int(os.getenv("VISUAL_REVIEW_CACHE_TTL_SECONDS", str(24 * 3600)))
# Visual rule judgments need the complete compliance chain, not the smaller OCR-only list.
MODELS = [m.strip() for m in os.getenv(
    "VISUAL_COMPLIANCE_MODELS", ",".join(context_evaluator.MODELS)
).split(",") if m.strip()]


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rule_number: str
    status: str
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_basis: Literal["OBSERVED_CONTENT", "NO_CONCERN", "COVERAGE_LIMITATION"]
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
            from services.compliance.evaluation.visual_video_frames import sample_parts
            yield await asyncio.to_thread(sample_parts, path)
            return
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
        allowed_keys = {"rule_number", "status", "confidence", "evidence", "reasoning", "box", "start_sec", "end_sec", "evidence_basis"}
        cleaned = {k: v for k, v in d.items() if k in allowed_keys}
        normalized.append(cleaned)
    assessments = [Assessment.model_validate(item) for item in normalized]
    expected = {r.rule_number for r in rules}
    if len(assessments) != len(expected) or {a.rule_number for a in assessments} != expected:
        raise ValueError("Visual review did not assess every applicable rule exactly once")
    if any(a.status not in {"VIOLATION", "NEEDS_REVIEW", "CLEARED", "UNAVAILABLE"} for a in assessments):
        raise ValueError("Invalid visual decision")
    for assessment in assessments:
        if (assessment.evidence_basis == "COVERAGE_LIMITATION"
                or (assessment.status != "CLEARED" and assessment.evidence_basis != "OBSERVED_CONTENT")
                or (assessment.status != "CLEARED" and not assessment.evidence.strip())
                or (assessment.status == "CLEARED" and assessment.confidence < .8)):
            assessment.status = "UNAVAILABLE"
    return assessments


def _finding(asset, rule, assessment, model):
    """Uncertain visual evidence cannot become a confirmed critical violation."""
    from services.compliance.evaluation.compliance_service import _penalty_and_light

    a = assessment
    if a.status == "UNAVAILABLE":
        return None
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
    if finding is None or finding.status == "CLEARED":
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


def _compact_rule(r) -> dict:
    return {k: v for k, v in {
        "rule_number": r.rule_number, "title": r.title, "severity": r.severity, "summary": r.summary,
        "guidance": r.evaluation_guidance, "exceptions": r.exception_terms,
        "allowed": r.allowed_examples, "prohibited": r.prohibited_examples,
    }.items() if v not in (None, "", [])}


def _compact_source(s) -> dict:
    return {k: v for k, v in {
        "reference": getattr(s, "reference", None), "status": getattr(s, "status", None),
        "title": getattr(s, "title", None),
    }.items() if v not in (None, "", [])}


async def review(req, rules, findings, *, source_checks=()):
    """Read every post asset, recording failures explicitly instead of silently treating them as compliant."""
    selected = [r for r in rules if r.rule_number in RULE_NUMBERS]
    coverage = []
    if not selected:
        return coverage
    from .source_evidence_context import catalog, POLICY
    payload = {
        "post": req.text, "published_text": req.disclaimer_text, "audiences": req.audiences,
        "brands": req.brand_codes, "region": req.region,
        "evidence": [_compact_source(s) for s in req.sources], "verified_sources": catalog(source_checks), "evidence_policy": POLICY,
        "rules": [_compact_rule(r) for r in selected],
    }
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    rule_stamp = ",".join(f"{r.id}:{getattr(r, 'updated_at', '')}" for r in selected)
    by_number = {r.rule_number: r for r in selected}
    for asset in req._visual_media:
        entry = {"file_id": str(asset["id"]), "name": asset["original_name"],
                 "rule_numbers": [r.rule_number for r in selected], "status": "UNAVAILABLE"}
        cache_key = hashlib.sha256(
            f"{PROMPT_VERSION}|{asset['id']}|{asset.get('storage_path')}|{rule_stamp}|{payload_json}".encode("utf-8")
        ).hexdigest()
        try:
            cached = await _VISUAL_CACHE.get(cache_key)
            if cached:
                assessments, model, input_mode = cached
                entry["input_mode"] = input_mode
            else:
                from services.ai.core import model_router
                routed_models = MODELS if os.getenv("VISUAL_COMPLIANCE_MODELS", "").strip() else (
                    model_router.route("VISUAL_COMPLIANCE", prompt_chars=len(payload_json), attachments=len(req._visual_media)).models or MODELS
                )
                async with image_text_service._semaphore, _asset_part(asset) as part:
                    response, assessments = await visual_response.read(
                        lambda limit: image_text_service._generate([part, PROMPT + payload_json],
                            feature="VISUAL_COMPLIANCE", response_schema=SCHEMA, models=routed_models, max_output_tokens=limit),
                        lambda response: _parse(response, selected))
                from services.ai.core.bedrock_service import is_bedrock_enabled
                input_mode = ("SAMPLED_FRAMES" if asset["mime_type"].startswith("video/")
                              and is_bedrock_enabled() else "FULL_ASSET")
                entry["input_mode"] = input_mode
                model = getattr(response, "model_version", None)
                if not any(a.status == "UNAVAILABLE" for a in assessments):
                    await _VISUAL_CACHE.set(cache_key, (assessments, model, input_mode), _VISUAL_CACHE_TTL_SEC)
            assessments = image_detail_review.apply(assessments, asset, by_number)
            for assessment in assessments:
                _merge(findings, _finding(asset, by_number[assessment.rule_number], assessment, model))
            unavailable = [a.rule_number for a in assessments if a.status == "UNAVAILABLE"]
            entry.update(status="UNAVAILABLE" if unavailable else "COMPLETED", model=model,
                         unavailable_rules=unavailable,
                         assessments=[a.model_dump() for a in assessments])
        except Exception:
            logger.warning("Visual compliance review unavailable for %s", asset["id"], exc_info=True)
            entry["message"] = "Visual review could not complete. Check again or request manual visual review."
        coverage.append(entry)
    return coverage
