"""Messaging Context: brand voice, facts, platform norms and audience profiles for the content analysis."""

from typing import Annotated, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import FileResponse

from models.compliance.brand_voice_model import BrandVoice, BrandVoiceIn
from models.compliance.context_model import ContextEntry, ContextEntryIn, ContextSync
from services.compliance.evaluation import brand_voice_service as voices
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


@router.get("/brand-voices", response_model=List[BrandVoice])
async def list_brand_voices(user_id: CurrentUser):
    """Every brand's voice: what was read from its brand manual (or written by hand), and whether the viewer may edit it."""
    return await voices.list_voices(user_id)


@router.get("/brand-voices/{brand_code}", response_model=BrandVoice)
async def get_brand_voice(brand_code: str, user_id: CurrentUser):
    return await voices.get_voice(brand_code, user_id)


@router.post("/brand-voices/{brand_code}/manual", response_model=BrandVoice, status_code=status.HTTP_202_ACCEPTED)
async def upload_brand_manual(brand_code: str, user_id: CurrentUser, file: UploadFile = File(...)):
    """Upload the brand's manual (PDF). Its voice, colours, typefaces and logo are read in the background.
    Reviewers for the brand, rulebook editors and super admins."""
    return await voices.upload_manual(brand_code, file, user_id)


@router.post("/brand-voices/{brand_code}/read", response_model=BrandVoice, status_code=status.HTTP_202_ACCEPTED)
async def reread_brand_manual(brand_code: str, user_id: CurrentUser):
    """Read the uploaded manual again (e.g. after the AI was unavailable)."""
    return await voices.reread(brand_code, user_id)


@router.put("/brand-voices/{brand_code}", response_model=BrandVoice)
async def update_brand_voice(brand_code: str, data: BrandVoiceIn, user_id: CurrentUser):
    """Edit the brand voice by hand (what was read from the manual, or without one)."""
    return await voices.update_voice(brand_code, data, user_id)


@router.get("/brand-voices/{brand_code}/manual")
async def download_brand_manual(brand_code: str, user_id: CurrentUser):
    path, mime, name = await voices.file_path(brand_code, "manual")
    return FileResponse(path, media_type=mime, filename=name)


@router.get("/brand-voices/{brand_code}/logo")
async def brand_logo(brand_code: str, user_id: CurrentUser):
    path, mime, _ = await voices.file_path(brand_code, "logo")
    return FileResponse(path, media_type=mime)


@router.post("", response_model=ContextEntry, status_code=status.HTTP_201_CREATED)
async def create_entry(data: ContextEntryIn, user_id: ManageUser):
    return await svc.create_entry(data, user_id)


@router.put("/{entry_id}", response_model=ContextEntry)
async def update_entry(entry_id: UUID, data: ContextEntryIn, user_id: ManageUser):
    return await svc.update_entry(entry_id, data, user_id)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(entry_id: UUID, user_id: ManageUser):
    await svc.delete_entry(entry_id, user_id)
