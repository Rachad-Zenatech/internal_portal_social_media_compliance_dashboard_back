"""An Atlas index-slot limit must not require deleting existing model fields."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.ai.core import vector_store_service as store


@pytest.fixture
def collection(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setenv("GEMINI_EMBEDDING_DIMENSIONS", "768")
    monkeypatch.setenv("VECTOR_INDEX_OVERRIDE", "vector_index")
    return MagicMock(update_search_index=AsyncMock())


def use_index(collection, definition):
    async def indexes(name):
        yield {"name": name, "status": "READY", "latestDefinition": definition}
    collection.list_search_indexes.side_effect = indexes
    return patch.object(store, "_collection", return_value=collection)


@pytest.mark.parametrize("provider, dimensions", [("gemini", 768), ("bedrock", 1024)])
def test_existing_vector_field_and_index_options_are_preserved(collection, monkeypatch, provider, dimensions):
    monkeypatch.setenv("EMBEDDING_PROVIDER", provider)
    monkeypatch.setenv("BEDROCK_EMBEDDING_DIMENSIONS", "1024")
    definition = {"fields": [{"type": "vector", "path": "embedding", "numDimensions": 3072, "similarity": "cosine"}], "numPartitions": 1}
    original = deepcopy(definition)
    with use_index(collection, definition):
        result = asyncio.run(store.ensure_index())
    assert result["updated"] is True
    name, updated = collection.update_search_index.await_args.args
    assert name == "vector_index"
    assert updated["fields"][0] == original["fields"][0]
    assert updated["numPartitions"] == 1
    assert any(f.get("path") == store._embedding_path() and f.get("numDimensions") == dimensions for f in updated["fields"])
    assert all(any(f.get("path") == path for f in updated["fields"]) for path in store.FILTER_PATHS)
    assert definition == original
    collection.drop_search_index.assert_not_called()


def test_matching_existing_index_needs_no_update(collection):
    definition = {"fields": [{"type": "vector", "path": store._embedding_path(), "numDimensions": 768, "similarity": "cosine"},
        *({"type": "filter", "path": path} for path in store.FILTER_PATHS)]}
    with use_index(collection, definition):
        assert asyncio.run(store.ensure_index())["reason"] == "already exists"
    collection.update_search_index.assert_not_called()


def test_incompatible_existing_field_is_not_overwritten(collection):
    definition = {"fields": [{"type": "vector", "path": store._embedding_path(), "numDimensions": 3072}]}
    with use_index(collection, definition), pytest.raises(ValueError, match="incompatible"):
        asyncio.run(store.ensure_index())
    collection.update_search_index.assert_not_called()


def test_existing_vector_field_with_missing_scope_filters_is_repaired(collection):
    definition = {'fields': [{'type': 'vector', 'path': store._embedding_path(), 'numDimensions': 768, 'similarity': 'cosine'}]}
    with use_index(collection, definition):
        assert asyncio.run(store.ensure_index())['updated'] is True
    _, updated = collection.update_search_index.await_args.args
    assert all(any(field.get('type') == 'filter' and field.get('path') == path for field in updated['fields']) for path in store.FILTER_PATHS)
    collection.drop_search_index.assert_not_called()
