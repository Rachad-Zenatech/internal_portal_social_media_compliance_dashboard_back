"""Bulk script details may be shared or independent without changing reviewed pages."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.compliance.evaluation import script_doc_details as details


def save_calls(shared, requested, payload):
    doc = {"id": uuid4(), "set_id": uuid4(), "details": {"region": "Canada", "source_submission_id": "origin"}}
    connection = MagicMock()
    connection.fetchrow = AsyncMock(return_value={"shared_details": shared})
    connection.execute = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock()
    transaction.__aexit__ = AsyncMock(return_value=False)
    connection.transaction.return_value = transaction
    acquired = MagicMock()
    acquired.__aenter__ = AsyncMock(return_value=connection)
    acquired.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquired
    model = SimpleNamespace(model_dump=lambda **_: payload) if payload is not None else None
    with patch.object(details, "get_pool", return_value=pool):
        asyncio.run(details.save(doc, model, requested))
    return doc, [call.args for call in connection.execute.await_args_list]


def test_shared_details_update_only_editable_pages_and_preserve_origin_links():
    doc, calls = save_calls(True, None, {"region": "Canada"})
    pages = next(call for call in calls if "UPDATE script_documents" in call[0])
    assert "WHERE set_id = $1" in pages[0]
    assert "status IN ('DRAFT', 'CHANGES_REQUESTED')" in pages[0]
    assert "details->'source_submission_id'" in pages[0]
    assert pages[1] == doc["set_id"]


@pytest.mark.parametrize("requested", [None, False])
def test_per_script_details_never_overwrite_sibling_scripts(requested):
    doc, calls = save_calls(False, requested, {"region": "United States"})
    pages = [call for call in calls if "UPDATE script_documents" in call[0]]
    assert len(pages) == 1
    assert "WHERE id = $1" in pages[0][0]
    assert pages[0][1] == doc["id"]
    assert json.loads(pages[0][2]) == {"region": "United States"}


def test_unchecking_shared_details_preserves_existing_values():
    _, calls = save_calls(True, False, None)
    assert len(calls) == 1
    assert "SET shared_details" in calls[0][0]
    assert calls[0][2] is False


def test_switching_back_to_shared_uses_current_script_details():
    _, calls = save_calls(False, True, None)
    pages = next(call for call in calls if "UPDATE script_documents" in call[0])
    assert "WHERE set_id" in pages[0]
    assert json.loads(pages[2]) == {"region": "Canada"}


def test_scope_is_restored_from_database_after_refresh():
    doc = {"set_id": uuid4()}
    with patch.object(details, "fetch_one", AsyncMock(return_value={"shared_details": False})):
        assert asyncio.run(details.shared(doc)) is False
    assert asyncio.run(details.shared({})) is True
