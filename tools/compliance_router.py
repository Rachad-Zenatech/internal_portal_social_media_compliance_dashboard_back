"""Compliance Rules & Governance Router
Endpoints for managing ("build on it and remove") and checking compliance rules.
"""

from typing import Annotated, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from models.compliance_model import (
    ComplianceCheckRequest,
    ComplianceCheckResponse,
    ComplianceRule,
    ComplianceRuleCreate,
    ComplianceRuleUpdate,
    RuleSelfTest,
)
from services import compliance_service as comp_svc
from services import submission_service
from services import flag_review_service
from models.flag_review_model import RuleFlagStats
from services.audit_service import log_audit_event
from services.auth_service import get_current_user_id_dependency, require_permission

router = APIRouter(prefix="/compliance", tags=["Compliance Rules & Governance"])

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]
ViewUser = Annotated[UUID, Depends(require_permission("CONFIG_ROLES_VIEW"))]
ManageUser = Annotated[UUID, Depends(require_permission("CONFIG_ROLES_UPDATE"))]


@router.get("/rules", response_model=List[ComplianceRule])
async def list_rules(
    _user_id: CurrentUser,
    category: Optional[str] = Query(None, description="Filter by category"),
    severity: Optional[str] = Query(None, description="Filter by severity: CRITICAL, HIGH, MEDIUM, LOW"),
    traffic_light: Optional[str] = Query(None, description="Filter by traffic light: RED, YELLOW, GREEN"),
    is_active: Optional[bool] = Query(None, description="Filter active/inactive rules"),
    search: Optional[str] = Query(None, description="Search term in rule number, title, summary or trigger phrases"),
    rule_set: Optional[str] = Query(None, description="Only rules that apply to this rule set"),
):
    """List compliance rules with multi-criteria filtering."""
    return await comp_svc.list_compliance_rules(
        category=category,
        severity=severity,
        traffic_light=traffic_light,
        is_active=is_active,
        search=search,
        rule_set=rule_set,
    )


@router.get("/rules/flag-stats", response_model=List[RuleFlagStats])
async def rules_flag_stats(_user_id: CurrentUser):
    """Per rule: how many submitted versions it flagged and how many of those flags reviewers dismissed."""
    return await flag_review_service.rule_stats()


@router.get("/rules/self-test", response_model=List[RuleSelfTest])
async def rules_self_test(_user_id: CurrentUser):
    """Every rule run against its own examples: "not acceptable" ones should be flagged, "acceptable" ones not.
    Declared before /rules/{rule_id} so "self-test" isn't read as an id."""
    return await comp_svc.self_test_all()


@router.get("/rules/{rule_id}", response_model=ComplianceRule)
async def get_rule(rule_id: UUID, _user_id: CurrentUser):
    """Retrieve details for a single compliance rule."""
    rule = await comp_svc.get_compliance_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compliance rule not found")
    return rule


@router.post("/rules", response_model=ComplianceRule, status_code=status.HTTP_201_CREATED)
async def create_rule(data: ComplianceRuleCreate, user_id: ManageUser):
    """Build on compliance rules: Add a new custom or refined compliance rule."""
    rule = await comp_svc.create_compliance_rule(data, user_id=user_id)
    submission_service.schedule_rulebook_recheck(f"{rule.rule_number} added")
    return rule


@router.put("/rules/{rule_id}", response_model=ComplianceRule)
async def update_rule(rule_id: UUID, data: ComplianceRuleUpdate, user_id: ManageUser):
    """Update or refine an existing compliance rule."""
    rule = await comp_svc.update_compliance_rule(rule_id, data, user_id=user_id)
    if not rule:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compliance rule not found")
    submission_service.schedule_rulebook_recheck(f"{rule.rule_number} edited")
    return rule


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: UUID, user_id: ManageUser):
    """Remove a compliance rule ("and remove")."""
    success = await comp_svc.delete_compliance_rule(rule_id, user_id=user_id)
    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compliance rule not found")
    submission_service.schedule_rulebook_recheck("a rule was removed")
    return None


@router.post("/check", response_model=ComplianceCheckResponse)
async def check_compliance(req: ComplianceCheckRequest, _user_id: CurrentUser):
    """Screen draft transcripts, captions or claims against the active rules that apply to the given scope."""
    return await comp_svc.check_compliance(req)


@router.get("/stats")
async def get_compliance_stats(_user_id: CurrentUser):
    """Get aggregate metrics and category distribution."""
    return await comp_svc.get_compliance_stats()


@router.post("/rules/reset-defaults")
async def reset_defaults(user_id: ManageUser):
    """Restore baseline rules to the official Defense Rulebook wording, scope and active state.
    Custom rules are not touched."""
    from postgresql_db.setup_compliance_schema import setup as setup_compliance
    await setup_compliance(reset=True)
    submission_service.schedule_rulebook_recheck("baseline rules restored")
    await log_audit_event(
        entity_type="compliance_rulebook",
        entity_id="baseline",
        action="RESET_COMPLIANCE_RULES",
        user_id=user_id,
    )
    return {"status": "ok", "message": "Baseline rules successfully re-synced from Rulebook"}

from pydantic import BaseModel
from services import video_job_service as vjs
from fastapi import BackgroundTasks

class VideoJobCreateRequest(BaseModel):
    filename: str
    video_title: str
    platform: Optional[str] = "TIKTOK"
    aspect_ratio: Optional[str] = "9:16"


@router.post("/jobs/video-upload")
async def start_video_upload_job(
    req: VideoJobCreateRequest,
    background_tasks: BackgroundTasks,
    user_id: CurrentUser,
):
    """Start an asynchronous video upload and compliance screening background job."""
    job = vjs.create_video_job(
        filename=req.filename,
        video_title=req.video_title,
        platform=req.platform or "TIKTOK",
        aspect_ratio=req.aspect_ratio or "9:16",
        user_id=str(user_id),
    )
    background_tasks.add_task(vjs.run_video_processing_pipeline, job["job_id"])
    return job


@router.get("/jobs/{job_id}")
async def get_video_job_status(job_id: str, _user_id: CurrentUser):
    """Get the real-time status, progress, and logs of a video background job."""
    job = vjs.get_video_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video job not found")
    return job


@router.get("/jobs")
async def list_video_jobs(_user_id: CurrentUser, limit: int = 10):
    """List recent video upload and screening background jobs."""
    return vjs.list_video_jobs(limit=limit)
