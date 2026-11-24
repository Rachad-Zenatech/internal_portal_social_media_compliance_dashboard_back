"""A foreground compliance check must not queue for minutes behind index rebuilding."""
import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.ai.core import embedding_service as service, vector_store_service as store
from services.compliance.evaluation import source_passages


def test_full_background_budget_does_not_delay_foreground_query(monkeypatch):
    monkeypatch.setenv("GEMINI_EMBEDDING_REQUESTS_PER_MINUTE", "60")
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: 100.))
    monkeypatch.setattr(service, "_rate_lock", asyncio.Lock())
    monkeypatch.setattr(service, "_rate_windows", {False: deque(), True: deque([(100., 20)])})
    with patch.object(service.asyncio, "sleep", AsyncMock()) as sleep:
        asyncio.run(service._pace(1))
    sleep.assert_not_awaited()


def test_only_background_job_waits_and_cancellation_does_not_reserve_future_capacity(monkeypatch):
    monkeypatch.setenv("GEMINI_EMBEDDING_REQUESTS_PER_MINUTE", "60")
    monkeypatch.setattr(service, "_rate_lock", asyncio.Lock())
    monkeypatch.setattr(service, "_rate_windows", {False: deque(), True: deque()})
    async def scenario():
        await service._pace(20, background=True)
        task = asyncio.create_task(service._pace(20, background=True))
        await asyncio.sleep(0)
        assert not task.done()
        await service._pace(1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert sum(size for _, size in service._rate_windows[True]) == 20
        assert sum(size for _, size in service._rate_windows[False]) == 1
    asyncio.run(scenario())


def test_slow_optional_passage_embeddings_use_keyword_ranking_without_waiting_for_index():
    async def slow(texts, **kwargs):
        await asyncio.sleep(30)
    async def scenario():
        with patch.object(store, "embed", slow), patch.dict(source_passages.os.environ, {"SOURCE_PASSAGES_EMBED_TIMEOUT_SEC": "0.01"}), \
                patch.object(source_passages, "EMBED_ENABLED", True):
            ranked = await asyncio.wait_for(source_passages.rank(
                ["Drone payload is 50 kg"], [source_passages.Passage(1, "Drone payload is 50 kg.", None)]), timeout=.5)
        assert ranked[0][0] > 0
    asyncio.run(scenario())


def test_interactive_quota_response_is_not_turned_into_a_long_retry_sleep():
    import httpx
    client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(
        429, json={"error": {"message": "per minute limit exceeded. Please retry in 50s."}})))
    async def scenario():
        with patch.object(service, "_pace", AsyncMock()), patch.object(service.asyncio, "sleep", AsyncMock()) as sleep:
            try:
                await service._request(client, "models/gemini-embedding-2", [{}], "test-key")
            except service.GeminiEmbeddingError:
                pass
            else:
                raise AssertionError("Expected an explicit quota response")
        sleep.assert_not_awaited()
        client.post.assert_awaited_once()
    asyncio.run(scenario())
