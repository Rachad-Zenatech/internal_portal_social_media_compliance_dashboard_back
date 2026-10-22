"""Canva account connection and design picker (mock Canva until the Connect API keys are configured)."""

from typing import Annotated, List
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from models.canva_model import CanvaConnection, CanvaDesign
from services import canva_service
from services.auth_service import get_current_user_id_dependency

router = APIRouter(prefix="/canva", tags=["Canva"])

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]


@router.get("/connection", response_model=CanvaConnection)
async def get_connection(user_id: CurrentUser):
    """Whether the user has connected Canva, and whether it's the mock or the live Canva."""
    return await canva_service.get_connection(user_id)


@router.post("/connection", response_model=CanvaConnection)
async def connect(user_id: CurrentUser):
    """Connect the user's Canva account (the mock connects immediately)."""
    return await canva_service.connect(user_id)


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(user_id: CurrentUser):
    await canva_service.disconnect(user_id)


@router.get("/designs", response_model=List[CanvaDesign])
async def list_designs(user_id: CurrentUser):
    """The user's Canva designs, each with its current revision."""
    return await canva_service.list_designs(user_id)


@router.get("/designs/{design_id}/thumbnail")
async def design_thumbnail(design_id: str, user_id: CurrentUser):
    """A small PNG preview of the design for the picker."""
    png = await canva_service.thumbnail(user_id, design_id)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/designs/{design_id}/simulate-edit", response_model=CanvaDesign)
async def simulate_edit(design_id: str, user_id: CurrentUser):
    """Mock Canva only: pretend the design was edited, so its next export is a new revision."""
    return await canva_service.simulate_edit(user_id, design_id)
