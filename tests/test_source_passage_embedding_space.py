"""Passage ranking must not reuse vectors from a different model or task."""
import asyncio
from collections import OrderedDict
from unittest.mock import AsyncMock

from services.ai.core import vector_store_service as store
from services.compliance.evaluation import source_passages as passages


def test_cached_passages_are_isolated_by_embedding_space(monkeypatch):
    space = ['model-A-768']
    embed = AsyncMock(side_effect=[[[1., 0.]], [[0., 1.]]])
    monkeypatch.setattr(passages, 'EMBED_ENABLED', True)
    monkeypatch.setattr(passages, '_vectors', OrderedDict())
    monkeypatch.setattr(store, '_embedding_path', lambda: space[0])
    monkeypatch.setattr(store, 'embed', embed)
    async def scenario():
        assert await passages._embeddings(['Same source text']) == [[1., 0.]]
        assert await passages._embeddings(['Same source text']) == [[1., 0.]]
        space[0] = 'model-B-1024'
        assert await passages._embeddings(['Same source text']) == [[0., 1.]]
    asyncio.run(scenario())
    assert embed.await_count == 2
    assert all(call.kwargs['task_type'] == 'SEMANTIC_SIMILARITY' for call in embed.await_args_list)


def test_invalid_optional_embedding_configuration_preserves_keyword_fallback(monkeypatch):
    monkeypatch.setattr(passages, 'EMBED_ENABLED', True)
    def invalid_space():
        raise ValueError('Embedding dimensions are not configured correctly')
    monkeypatch.setattr(store, '_embedding_path', invalid_space)
    ranked = asyncio.run(passages.rank(['Payload is 50 kg'], [passages.Passage(1, 'Payload is 50 kg.', None)]))
    assert ranked[0][0] > 0
