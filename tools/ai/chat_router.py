"""Streaming AI chat endpoint."""
import json
import logging
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from services.ai.core import assistant_chat_service
from services.shared.auth.auth_service import current_user_id_ctx
from tools.shared.auth.rbac_router import get_current_user_id

logger = logging.getLogger(__name__)
router = APIRouter()
CHAT_MAX_TOTAL_CHARS = 60_000


class MessageItem(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=20_000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    history: List[MessageItem] = Field(default_factory=list, max_length=50)


def _unavailable_response() -> JSONResponse:
    return JSONResponse(
        content={"reply": "The AI service is temporarily unavailable. Please try again."},
        status_code=500,
    )


@router.post("/ai/chat")
async def ai_chat(request: ChatRequest, user_id: UUID = Depends(get_current_user_id)):
    if len(request.message) + sum(len(item.content) for item in request.history) > CHAT_MAX_TOTAL_CHARS:
        raise HTTPException(status_code=413, detail="The conversation is too long. Start a new one.")
    try:
        current_user_id_ctx.set(user_id)
        prepared = await assistant_chat_service.prepare(request.message, request.history, user_id)
    except Exception:
        logger.exception("AI chat request failed", extra={"event": "ai_chat_failed"})
        return _unavailable_response()
    if not prepared.blocked and prepared.cached_answer is None:
        from services.ai.observability import ai_usage_service
        await ai_usage_service.reserve("CHAT", user_id)

    async def stream_generator():
        try:
            async for chunk in assistant_chat_service.stream(prepared):
                yield chunk
        except Exception:
            logger.exception("AI stream request failed", extra={"event": "ai_chat_stream_failed"})
            yield f"data: {json.dumps({'type': 'error', 'message': 'The AI service is temporarily unavailable.'})}\n\n"

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
