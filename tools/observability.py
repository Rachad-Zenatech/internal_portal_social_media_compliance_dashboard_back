import logging
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, Field

from services import ai_observability_service
from services.auth_service import get_current_user_id_dependency, require_permission


router = APIRouter()
logger = logging.getLogger(__name__)


class ClientErrorReport(BaseModel):
    error_type: Literal["react", "runtime", "unhandled_promise"]
    message: str = Field(min_length=1, max_length=2_000)
    stack: Optional[str] = Field(default=None, max_length=8_000)
    component_stack: Optional[str] = Field(default=None, max_length=8_000)
    path: str = Field(default="/", max_length=2_048)


class ClientPerformanceReport(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=2_048)
    duration_ms: float = Field(ge=0, le=600_000)
    status_code: int = Field(ge=0, le=599)


@router.post("/observability/client-error", status_code=status.HTTP_202_ACCEPTED)
async def report_client_error(
    report: ClientErrorReport,
    request: Request,
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    logger.error(
        "Frontend error: %s\n%s\n%s",
        report.message,
        report.stack or "",
        report.component_stack or "",
        extra={
            "event": "frontend_error",
            "error_type": report.error_type,
            "path": report.path.split("?", 1)[0],
            "user_id": str(user_id),
            "client_ip": request.client.host if request.client else None,
        },
    )
    return {"accepted": True}


@router.post("/observability/client-performance", status_code=status.HTTP_202_ACCEPTED)
async def report_client_performance(
    report: ClientPerformanceReport,
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    logger.warning(
        "Slow browser-observed API request",
        extra={
            "event": "slow_client_request",
            "method": report.method,
            "path": report.path.split("?", 1)[0],
            "duration_ms": round(report.duration_ms, 2),
            "status_code": report.status_code,
            "user_id": str(user_id),
        },
    )
    return {"accepted": True}

# --- AI observability: traces, prompt logs and usage (people who can view System Logs) ---
_ai_logs_viewer = [Depends(require_permission("SYSTEM_LOGS_VIEW"))]


@router.get('/observability/usage/tokens', status_code=status.HTTP_200_OK, dependencies=_ai_logs_viewer)
async def get_token_usage(hours: int = Query(24, ge=1, le=24 * 30)):
    """Tokens, calls, errors and latency per feature and model over the last `hours`."""
    return await ai_observability_service.usage_summary(hours)


@router.get('/observability/ai/summary', dependencies=_ai_logs_viewer)
async def get_ai_summary(hours: int = Query(24, ge=1, le=24 * 30)):
    """Per feature and model: calls, errors, p50/p95 latency, tokens; plus cache hits, guard verdicts, tool calls."""
    return await ai_observability_service.summary(hours)


@router.get('/observability/ai/traces', dependencies=_ai_logs_viewer)
async def list_ai_traces(
    limit: int = Query(50, ge=1, le=200),
    kind: Optional[str] = Query(None, max_length=30),
    status_filter: Optional[str] = Query(None, alias="status", max_length=20),
):
    """Recent traces, newest first, each with its span count, total time and any errors."""
    return await ai_observability_service.list_traces(limit, kind, status_filter)


@router.get('/observability/ai/traces/{trace_id}', dependencies=_ai_logs_viewer)
async def get_ai_trace(trace_id: str = Path(max_length=128)):
    """Every span of one trace (model calls, embeddings, retrieval, tool calls, guard, cache) and its prompt logs."""
    trace = await ai_observability_service.get_trace(trace_id)
    if not trace["spans"] and not trace["prompts"]:
        raise HTTPException(status_code=404, detail="Trace not found")
    return trace


@router.get('/observability/ai/prompt-logs', dependencies=_ai_logs_viewer)
async def list_ai_prompt_logs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    feature: Optional[str] = Query(None, max_length=40),
    status_filter: Optional[str] = Query(None, alias="status", max_length=20),
    guard_verdict: Optional[str] = Query(None, max_length=10),
):
    """Prompt and response of each model call (PII-redacted), newest first."""
    return await ai_observability_service.list_prompt_logs(limit, offset, feature, status_filter, guard_verdict)

