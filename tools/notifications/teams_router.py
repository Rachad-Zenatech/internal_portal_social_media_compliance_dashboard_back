"""Microsoft Teams: the endpoint the bot's messages arrive on, and a person's own on/off switch for them."""

import logging
from typing import Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from services.notifications import teams_service
from services.shared.auth.auth_service import get_current_user_id_dependency

logger = logging.getLogger(__name__)

# No sign-in on the bot endpoint: Teams isn't a portal user. What it sends is checked by the Bot Framework's signed
# token instead (see teams_service.verify_inbound).
teams_bot_router = APIRouter()
# A person's own settings: signed in like the rest of the portal.
teams_router = APIRouter()


@teams_bot_router.post("/teams/messages")
async def receive(request: Request) -> Dict[str, str]:
    if not teams_service.enabled():
        raise HTTPException(status_code=503, detail="Teams messages aren't set up")
    try:
        activity = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Not a Teams activity")
    if not isinstance(activity, dict):
        raise HTTPException(status_code=400, detail="Not a Teams activity")
    if not await teams_service.verify_inbound(request.headers.get("authorization"), activity.get("serviceUrl")):
        raise HTTPException(status_code=401, detail="Not from Teams")
    try:
        await teams_service.handle_activity(activity)
    except Exception:
        # Teams retries a failed delivery; a bug here shouldn't make it do that for ever.
        logger.exception("Teams activity couldn't be handled")
    return {}


class TeamsPreference(BaseModel):
    enabled: bool


@teams_router.get("/teams/status")
async def my_status(user_id: UUID = Depends(get_current_user_id_dependency)) -> Dict:
    return await teams_service.status_for(str(user_id))


@teams_router.put("/teams/status")
async def set_my_status(body: TeamsPreference, user_id: UUID = Depends(get_current_user_id_dependency)) -> Dict:
    await teams_service.set_enabled_for(str(user_id), body.enabled)
    return await teams_service.status_for(str(user_id))
