"""Free-tier indexing must pace individual inputs and retain completed batches."""
import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.ai.core import embedding_service as service, vector_store_service as store


def test_interactive_capacity_is_immediate_until_its_input_budget_is_full(monkeypatch):
    monkeypatch.setenv("GEMINI_EMBEDDING_REQUESTS_PER_MINUTE", "60")
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: 100.))
    monkeypatch.setattr(service, "_rate_windows", {False: deque(), True: deque()})
    monkeypatch.setattr(service, "_rate_lock", asyncio.Lock())
    sleep = AsyncMock()
    async def scenario():
        await service._pace(20)
        await service._pace(20)
        with pytest.raises(service.EmbeddingCapacityBusy):
            await service._pace(1)
    with patch.object(service.asyncio, "sleep", sleep):
        asyncio.run(scenario())
    sleep.assert_not_awaited()


def test_short_per_minute_quota_wait_is_retried_once(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_EMBEDDING_DIMENSIONS", "768")
    denied = httpx.Response(429, json={"error": {"message": "per minute limit exceeded. Please retry in 2s."}})
    ok = httpx.Response(200, json={"embeddings": [{"values": [1.] * 768}]})
    client = SimpleNamespace(post=AsyncMock(side_effect=[denied, ok]))
    with patch.object(service, "_pace", AsyncMock()), patch.object(service.asyncio, "sleep", AsyncMock()) as sleep:
        vectors = asyncio.run(service._request(client, "models/gemini-embedding-2", [{}], "test-key", background=True))
    assert len(vectors) == 1
    assert client.post.await_count == 2
    sleep.assert_awaited_once_with(3.)


def test_indexing_keeps_first_batch_when_next_batch_hits_quota(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_EMBEDDING_DIMENSIONS", "768")
    docs = [store.VectorDoc(kind="FACT", source_id=str(i), text="Public example") for i in range(21)]
    collection = MagicMock()
    async def existing(*args):
        for item in []:
            yield item
    collection.find.side_effect = existing
    collection.bulk_write = AsyncMock()
    embed = AsyncMock(side_effect=[[[1.] * 768] * 20, service.GeminiEmbeddingError(429, "daily quota reached")])
    with patch.object(store, "_collection", return_value=collection), patch.object(store, "embed", embed), \
            patch.object(store, "_reference_changed", AsyncMock()):
        with pytest.raises(service.GeminiEmbeddingError):
            asyncio.run(store.upsert(docs))
    collection.bulk_write.assert_awaited_once()
    assert len(collection.bulk_write.await_args.args[0]) == 20
