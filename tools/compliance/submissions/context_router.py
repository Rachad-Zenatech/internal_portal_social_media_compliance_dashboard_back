"""Messaging Context: brand voice, facts, platform norms and audience profiles for the content analysis."""

from typing import Annotated, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from models.compliance.context_model import ContextEntry, ContextEntryIn, ContextSync
from services.compliance.evaluation import context_service as svc
from services.compliance.evaluation import newsroom_sync_service as newsroom
from services.shared.auth.auth_service import get_current_user_id_dependency, require_permission

router = APIRouter(prefix="/messaging-context")

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]
# Editing it sits with editing the compliance rulebook.
ManageUser = Annotated[UUID, Depends(require_permission("CONFIG_ROLES_UPDATE"))]


@router.get("", response_model=List[ContextEntry])
async def list_entries(user_id: CurrentUser, kind: Optional[str] = Query(None)):
    """Everyone can read it: it's the guidance their content is assessed against."""
    return await svc.list_entries(kind)


@router.get("/newsroom-sync", response_model=Optional[ContextSync])
async def newsroom_sync_status(user_id: CurrentUser):
    """The newsroom sync running now, or how the last one went (none yet: null)."""
    return await newsroom.latest()


@router.post("/newsroom-sync", response_model=ContextSync, status_code=status.HTTP_202_ACCEPTED)
async def start_newsroom_sync(user_id: ManageUser):
    """Read the ZenaTech newsroom now and add or update its facts (it also runs once a day)."""
    return await newsroom.start("MANUAL", user_id)


@router.post("", response_model=ContextEntry, status_code=status.HTTP_201_CREATED)
async def create_entry(data: ContextEntryIn, user_id: ManageUser):
    return await svc.create_entry(data, user_id)


@router.put("/{entry_id}", response_model=ContextEntry)
async def update_entry(entry_id: UUID, data: ContextEntryIn, user_id: ManageUser):
    return await svc.update_entry(entry_id, data, user_id)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(entry_id: UUID, user_id: ManageUser):
    await svc.delete_entry(entry_id, user_id)
