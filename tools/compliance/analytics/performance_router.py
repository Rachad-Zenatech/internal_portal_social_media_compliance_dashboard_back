"""Post performance: how published posts are doing (views, likes, comments, shares) and whether the reaction is
still positive."""

from datetime import date
from typing import Annotated, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from models.compliance.performance_model import PerformanceReport
from services.compliance.analytics import performance_service as svc
from services.shared.auth.auth_service import get_current_user_id_dependency

router = APIRouter(prefix="/performance")

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]

# A custom range covers at most this many days, like the longest "last N days".
MAX_RANGE_DAYS = 366


@router.get("", response_model=PerformanceReport)
async def performance(
    user_id: CurrentUser,
    days: int = Query(30, ge=1, le=365),
    start: Optional[date] = Query(None, description="First day of a custom range (with end); replaces days"),
    end: Optional[date] = Query(None, description="Last day of a custom range, included"),
    platform: Optional[str] = Query(None, max_length=30),
    brand: Optional[str] = Query(None, max_length=50),
    mine: Optional[bool] = Query(
        None, description="true: only the viewer's posts; false: the whole team's. Left out: team for reviewers and "
                          "managers, own posts for creators."
    ),
):
    """Live posts (one row per platform) with their numbers and health, problems first, plus totals. Read-only, so
    anyone may view the whole team's posts. The period is the last `days` days, or start to end when both are given."""
    if (start is None) != (end is None):
        raise HTTPException(status_code=422, detail="Give both a start and an end date for a custom range")
    if start and end:
        if start > end:
            raise HTTPException(status_code=422, detail="The start date has to be on or before the end date")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise HTTPException(status_code=422, detail=f"A custom range can cover at most {MAX_RANGE_DAYS} days")
    return await svc.report(user_id, days=days, start=start, end=end, platform=platform, brand=brand, mine=mine)
