"""Whether the AI is working, for the banner shown to every signed-in user."""

from fastapi import APIRouter

from services.ai.observability import ai_health

router = APIRouter()


@router.get("/ai/status", response_model=ai_health.AiStatus)
async def ai_status():
    """The AI's state: available, or why it isn't (key, quota, models, busy). Anyone signed in may ask."""
    return await ai_health.status()
