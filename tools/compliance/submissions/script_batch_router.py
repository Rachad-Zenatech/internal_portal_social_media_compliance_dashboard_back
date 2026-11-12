"""Script Batch Router
Several scripts uploaded in one document, reviewed together, each becoming a post once approved.
"""

from typing import Annotated, List, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from models.compliance.script_batch_model import (
    Batch,
    BatchItemCreate,
    BatchItemUpdate,
    BatchReviewRequest,
    BatchReviewResult,
    BatchSummary,
    BatchUpdate,
)
from services.compliance.evaluation import script_batch_service as svc
from services.shared.auth.auth_service import get_current_user_id_dependency

router = APIRouter(prefix="/script-batches", tags=["Script Batches"])

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.get("", response_model=List[BatchSummary])
async def list_batches(user_id: CurrentUser, scope: Literal["mine", "review"] = Query("mine")):
    """The batches you uploaded, or (reviewers) the batches sent for review."""
    return await svc.list_batches(user_id, scope)


@router.post("", response_model=Batch, status_code=status.HTTP_201_CREATED)
async def upload_batch(user_id: CurrentUser, file: UploadFile = File(...)):
    """Upload one document holding several scripts; it is split into scripts the creator can still change."""
    return await svc.create_batch(user_id, file.filename or "scripts", await file.read())


@router.get("/{batch_id}", response_model=Batch)
async def get_batch(batch_id: UUID, user_id: CurrentUser):
    return await svc.get_batch(batch_id, user_id)


@router.patch("/{batch_id}", response_model=Batch)
async def update_batch(batch_id: UUID, req: BatchUpdate, user_id: CurrentUser):
    """Rename the batch, or set the details every post made from it shares."""
    return await svc.update_batch(batch_id, user_id, req)


@router.delete("/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_batch(batch_id: UUID, user_id: CurrentUser):
    await svc.delete_batch(batch_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{batch_id}/items", response_model=Batch, status_code=status.HTTP_201_CREATED)
async def add_item(batch_id: UUID, req: BatchItemCreate, user_id: CurrentUser):
    return await svc.add_item(batch_id, user_id, req)


@router.patch("/{batch_id}/items/{item_id}", response_model=Batch)
async def update_item(batch_id: UUID, item_id: UUID, req: BatchItemUpdate, user_id: CurrentUser):
    return await svc.update_item(batch_id, item_id, user_id, req)


@router.delete("/{batch_id}/items/{item_id}", response_model=Batch)
async def delete_item(batch_id: UUID, item_id: UUID, user_id: CurrentUser):
    return await svc.delete_item(batch_id, item_id, user_id)


@router.post("/{batch_id}/items/{item_id}/check", response_model=Batch)
async def check_item(batch_id: UUID, item_id: UUID, user_id: CurrentUser):
    """Check one script against the rulebook before sending it (the result is kept and reused on send)."""
    return await svc.check_item(batch_id, item_id, user_id)


@router.post("/{batch_id}/send", response_model=Batch)
async def send_batch(batch_id: UUID, user_id: CurrentUser):
    """Send the waiting scripts to the reviewers (checked against the rulebook in the background)."""
    return await svc.send(batch_id, user_id)


@router.post("/{batch_id}/review", response_model=BatchReviewResult)
async def review_batch(batch_id: UUID, req: BatchReviewRequest, user_id: CurrentUser):
    """Approve or send back any number of the batch's scripts at once; each approved script becomes a post."""
    return await svc.review(batch_id, user_id, req)
