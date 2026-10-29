"""Messaging Context: brand voice, facts, platform norms and audience profiles for the content analysis."""

from typing import Annotated, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from models.context_model import ContextEntry, ContextEntryIn
from services.compliance import context_service as svc
from services.shared.auth_service import get_current_user_id_dependency, require_permission

router = APIRouter(prefix="/messaging-context")

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]
# Editing it sits with editing the compliance rulebook.
ManageUser = Annotated[UUID, Depends(require_permission("CONFIG_ROLES_UPDATE"))]


@router.get("", response_model=List[ContextEntry])
async def list_entries(user_id: CurrentUser, kind: Optional[str] = Query(None)):
    """Everyone can read it: it's the guidance their content is assessed against."""
    return await svc.list_entries(kind)


@router.post("", response_model=ContextEntry, status_code=status.HTTP_201_CREATED)
async def create_entry(data: ContextEntryIn, user_id: ManageUser):
    return await svc.create_entry(data, user_id)


@router.put("/{entry_id}", response_model=ContextEntry)
async def update_entry(entry_id: UUID, data: ContextEntryIn, user_id: ManageUser):
    return await svc.update_entry(entry_id, data, user_id)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(entry_id: UUID, user_id: ManageUser):
    await svc.delete_entry(entry_id, user_id)
