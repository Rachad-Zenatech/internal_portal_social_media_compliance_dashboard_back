"""Review Queue endpoints, protected from duplicate browser requests."""

from typing import Annotated, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from models.compliance.submission_model import QueueTab, ReviewQueueCounts, ReviewQueueItem
from services.compliance.submissions import submission_service as svc
from services.compliance.submissions.request_coalescer import review_queue_request_cache
from services.shared.auth.auth_service import get_current_user_id_dependency

router = APIRouter()
CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]
ViewScope = Literal["all", "mine"]


@router.get("/review-queue", response_model=List[ReviewQueueItem])
async def review_queue(
    user_id: CurrentUser,
    tab: QueueTab = Query("review"),
    search: Optional[str] = Query(None, max_length=200),
    scope: ViewScope = Query("all"),
):
    """Submitted posts for one Content Screener tab."""
    key = f"queue:{user_id}:{tab}:{search or ''}:{scope}"
    return await review_queue_request_cache.get_or_load(
        key, lambda: svc.review_queue(user_id, tab, search, mine=scope == "mine")
    )


@router.get("/review-queue/counts", response_model=ReviewQueueCounts)
async def review_queue_counts(user_id: CurrentUser, scope: ViewScope = Query("all")):
    """How many posts are in each Content Screener tab."""
    key = f"queue-counts:{user_id}:{scope}"
    return await review_queue_request_cache.get_or_load(
        key, lambda: svc.review_queue_counts(user_id, mine=scope == "mine")
    )
