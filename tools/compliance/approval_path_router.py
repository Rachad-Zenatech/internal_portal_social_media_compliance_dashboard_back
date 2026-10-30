"""Approval paths: who gives final approval for which brand and audience."""

from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, status

from models.compliance.approval_path_model import ApprovalPath, ApprovalPathIn, ApprovalPathOptions
from services.compliance import approval_path_service as svc
from services.shared.auth_service import require_permission

router = APIRouter(prefix="/approval-paths")

# Configuring who approves what sits with assigning roles and brands.
CanRead = Depends(require_permission("CONFIG_USER_ROLE_ASSIGNMENT_READ"))
CanEdit = Depends(require_permission("CONFIG_USER_ROLE_ASSIGNMENT_UPDATE"))


@router.get("", response_model=List[ApprovalPath])
async def list_paths(user_id: UUID = CanRead):
    return await svc.list_paths()


@router.get("/options", response_model=ApprovalPathOptions)
async def path_options(user_id: UUID = CanRead):
    """Who can be named an approver, and the brands and audiences a path can match."""
    return await svc.options()


@router.post("", response_model=ApprovalPath, status_code=status.HTTP_201_CREATED)
async def create_path(data: ApprovalPathIn, user_id: UUID = CanEdit):
    return await svc.create_path(data, user_id)


@router.put("/{path_id}", response_model=ApprovalPath)
async def update_path(path_id: UUID, data: ApprovalPathIn, user_id: UUID = CanEdit):
    return await svc.update_path(path_id, data, user_id)


@router.delete("/{path_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_path(path_id: UUID, user_id: UUID = CanEdit):
    await svc.delete_path(path_id, user_id)
