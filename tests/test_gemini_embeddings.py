"""Gemini embeddings stay separate from Claude reviews and existing vector spaces."""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services.ai.core import embedding_service as service, vector_store_service as store


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setenv("GEMINI_EMBEDDING_DIMENSIONS", "768")
    monkeypatch.setattr(service, "_pace", AsyncMock())


def transport(handler):
    real = httpx.AsyncClient
    def client(*args, **kwargs):
        return real(*args, **{**kwargs, "transport": httpx.MockTransport(handler)})
    return patch.object(service.httpx, "AsyncClient", client)


def test_real_vectors_are_normalized_and_requests_preserve_order():
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append((request, body))
        vectors = [{"values": [0.] * index + [2.] + [0.] * (767 - index)} for index in range(len(body["requests"]))]
        return httpx.Response(200, json={"embeddings": vectors})
    with transport(handler), patch.object(service.bedrock_embeddings, "embed", AsyncMock()) as bedrock:
        vectors = asyncio.run(service.embed(["First public text", "Second public text"], task_type="RETRIEVAL_QUERY"))
    assert vectors[0][0] == 1 and vectors[1][1] == 1
    request, body = calls[0]
    assert request.headers["x-goog-api-key"] == "test-key"
    assert "test-key" not in str(request.url)
    assert request.url.path.endswith("gemini-embedding-2:batchEmbedContents")
    assert [r["content"]["parts"][0]["text"] for r in body["requests"]] == ["First public text", "Second public text"]
    assert all(r["taskType"] == "RETRIEVAL_QUERY" for r in body["requests"])
    bedrock.assert_not_called()


@pytest.mark.parametrize("values", [[0.] * 768, [1.], [float("nan")] * 768])
def test_invalid_embeddings_are_never_stored_as_placeholders(values):
    def handler(request):
        return httpx.Response(200, content=json.dumps({"embeddings": [{"values": values}]}).encode())
    with transport(handler), pytest.raises(ValueError, match="invalid embedding"):
        asyncio.run(service.embed(["Public text"]))


def test_omitted_batch_results_fail_explicitly():
    with transport(lambda request: httpx.Response(200, json={"embeddings": []})), pytest.raises(ValueError, match="incomplete"):
        asyncio.run(service.embed(["Public text"]))


def test_quota_errors_do_not_fall_back_to_a_paid_provider():
    with transport(lambda request: httpx.Response(429, json={"error": {"message": "RESOURCE_EXHAUSTED"}})), \
            patch.object(service.bedrock_embeddings, "embed", AsyncMock()) as bedrock:
        with pytest.raises(service.GeminiEmbeddingError) as error:
            asyncio.run(service.embed(["Public text"]))
    assert error.value.code == 429
    bedrock.assert_not_called()


def test_provider_error_messages_do_not_expose_the_key():
    with transport(lambda request: httpx.Response(403, json={"error": {"message": "Denied test-key"}})):
        with pytest.raises(service.GeminiEmbeddingError) as error:
            asyncio.run(service.embed(["Public text"]))
    assert "test-key" not in str(error.value)


def test_new_provider_and_dimensions_cannot_reuse_titan_vectors(monkeypatch):
    google_path, google_hash = store._embedding_path(), store._hash("Public text", "Title")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "bedrock")
    assert store._embedding_path() != google_path
    assert store._hash("Public text", "Title") != google_hash
    monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_EMBEDDING_DIMENSIONS", "1536")
    assert store._embedding_path() != google_path


def test_titan_permission_pause_does_not_disable_gemini(monkeypatch):
    monkeypatch.setattr(store, "_embed_paused_until", 0)
    monkeypatch.setattr(store, "_embed_paused_model", None)
    monkeypatch.setattr(store, "_embed_pause_reason", "none")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "bedrock")
    store.note_embedding_error(RuntimeError("AccessDeniedException"))
    monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
    assert store.embedding_pause_status() is None
    store.check_embedding_quota()


def test_bedrock_is_preserved_until_gemini_is_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("EMBEDDING_PROVIDER")
    assert service.provider() == "bedrock"
