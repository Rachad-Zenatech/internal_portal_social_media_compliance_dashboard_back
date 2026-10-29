"""Post performance: how published posts are doing (views, likes, comments, shares) and whether the reaction is
still positive."""

from typing import Annotated, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from models.performance_model import PerformanceReport
from services.compliance import performance_service as svc
from services.shared.auth_service import get_current_user_id_dependency

router = APIRouter(prefix="/performance")

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.get("", response_model=PerformanceReport)
async def performance(
    user_id: CurrentUser,
    days: int = Query(30, ge=1, le=365),
    platform: Optional[str] = Query(None, max_length=30),
    brand: Optional[str] = Query(None, max_length=50),
    mine: Optional[bool] = Query(
        None, description="true: only the viewer's posts; false: the whole team's. Left out: team for reviewers and "
                          "managers, own posts for creators."
    ),
):
    """Live posts (one row per platform) with their numbers and health, problems first, plus totals. Read-only, so
    anyone may view the whole team's posts."""
    return await svc.report(user_id, days=days, platform=platform, brand=brand, mine=mine)
