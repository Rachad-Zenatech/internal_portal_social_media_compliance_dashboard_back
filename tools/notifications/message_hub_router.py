"""Message Hub endpoints for the topbar servers dropdown (mounted at /api/message-hub, authenticated)."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from services.shared.core.cache_service import AsyncInMemoryCache

message_hub_router = APIRouter()
_status_cache = AsyncInMemoryCache(maxsize=1)
_status_lock = asyncio.Lock()


@message_hub_router.get("/status")
async def message_hub_status(test: bool = False):
    """Live RabbitMQ Message Hub snapshot for the topbar servers dropdown (same contract as the other portals)."""
    from services.notifications.rabbitmq_service import rabbitmq_manager

    if test:
        return await rabbitmq_manager.get_hub_live_status(active_test=True)
    cached = await _status_cache.get("message-hub-status")
    if cached is not None:
        return cached
    async with _status_lock:
        cached = await _status_cache.get("message-hub-status")
        if cached is None:
            cached = await rabbitmq_manager.get_hub_live_status(active_test=False)
            await _status_cache.set("message-hub-status", cached, ttl_seconds=10)
        return cached


@message_hub_router.post("/test")
async def message_hub_test():
    from services.notifications.rabbitmq_service import rabbitmq_manager

    return await rabbitmq_manager.get_hub_live_status(active_test=True)


@message_hub_router.get("/stream")
async def message_hub_stream(request: Request):
    """SSE: an initial snapshot, then a new snapshot whenever any server's status changes."""
    import json as _json
    from services.notifications.rabbitmq_service import rabbitmq_manager

    async def _event_generator():
        q = rabbitmq_manager.subscribe_sse()
        try:
            initial = await rabbitmq_manager.get_hub_live_status(active_test=False)
            yield f"data: {_json.dumps(initial)}\n\n"
            while not rabbitmq_manager._shutting_down:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=10.0)
                    if payload is None or rabbitmq_manager._shutting_down:
                        break
                    yield f"data: {_json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            rabbitmq_manager.unsubscribe_sse(q)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
