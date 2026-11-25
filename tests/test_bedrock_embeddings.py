"""Synthetic provider tests: no credentials, network, or private post data."""
import io
import asyncio
from functools import wraps
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.ai.core import bedrock_embeddings as be, vector_store_service as vs


@pytest.fixture(autouse=True)
def bedrock_provider(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "bedrock")
    monkeypatch.delenv("VECTOR_INDEX_OVERRIDE", raising=False)


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


@async_test
async def test_real_bedrock_embeddings_preserve_order_and_shape(monkeypatch):
    monkeypatch.setenv("BEDROCK_EMBEDDING_DIMENSIONS", "256")
    client = MagicMock()
    client.invoke_model.side_effect = lambda **kw: {
        "body": io.BytesIO(json.dumps({"embedding": [float(len(json.loads(kw["body"])["inputText"]))] * 256}).encode())}
    with patch.object(be, "get_bedrock_client") as adapter:
        adapter.return_value.get_boto_client.return_value = client
        result = await be.embed(["a", "abcd"])
    assert [v[0] for v in result] == [1, 4]
    assert client.invoke_model.call_args.kwargs["modelId"] == "amazon.titan-embed-text-v2:0"
    assert json.loads(client.invoke_model.call_args.kwargs["body"])["normalize"] is True


@async_test
@pytest.mark.parametrize("vector", [[0.0] * 256, [1.0], [float("nan")] * 256])
async def test_invalid_vectors_are_never_substituted_or_stored(monkeypatch, vector):
    monkeypatch.setenv("BEDROCK_EMBEDDING_DIMENSIONS", "256")
    with patch.object(be, "get_bedrock_client") as adapter:
        adapter.return_value.get_boto_client.return_value.invoke_model.return_value = {
            "body": io.BytesIO(json.dumps({"embedding": vector}).encode())}
        with pytest.raises(ValueError, match="invalid embedding"):
            await be.embed(["public test text"])


def test_bedrock_vector_space_does_not_reuse_legacy_or_other_dimensions(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test")
    monkeypatch.setenv("AWS_PROFILE_ID", "test-profile")
    monkeypatch.setenv("VECTOR_EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setenv("BEDROCK_EMBEDDING_DIMENSIONS", "256")
    old_hash, old_index = vs._hash("text", "title"), vs.index_name()
    assert vs.embedding_model() == "amazon.titan-embed-text-v2:0"
    assert vs._embedding_path() != "embedding"
    monkeypatch.setenv("BEDROCK_EMBEDDING_DIMENSIONS", "1024")
    assert vs._hash("text", "title") != old_hash
    assert vs.index_name() != old_index


@async_test
async def test_bedrock_retrieval_needs_no_gemini_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    collection = MagicMock()
    collection.aggregate.return_value.to_list = AsyncMock(return_value=[{"score": 0.9, "title": "rule"}])
    with patch.object(vs, "_collection", return_value=collection), patch.object(vs, "embed", AsyncMock(return_value=[[1.0]])):
        assert len(await vs.search("a public claim")) == 1
    collection.aggregate.assert_called_once()
    vector_search = collection.aggregate.call_args.args[0][0]["$vectorSearch"]
    assert vector_search["path"] == be.vector_path()
    assert vector_search["index"] == vs.index_name()
    assert vector_search["path"] != "embedding_7f66c59cdc371570"


def test_access_denial_pauses_repeated_embedding_calls(monkeypatch):
    monkeypatch.setattr(vs, "_embed_paused_until", 0)
    vs.note_embedding_error(RuntimeError("AccessDeniedException: denied"))
    with pytest.raises(vs.EmbeddingQuotaExhausted):
        vs.check_embedding_quota()
