"""Saved script scores survive reloads and stay visible when inputs change."""
import copy
import asyncio
import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from services.compliance.evaluation import script_doc_compliance as checks
from services.compliance.evaluation import script_doc_reads as reads


def document():
    return {"id": uuid4(), "title": "Scale", "body_text": "0:27 PRODUCT: An approved aircraft.",
            "sources_text": "Aircraft https://example.com/aircraft", "details": {"rule_sets": ["GENERAL"]}}


def test_run_persists_score_and_flags_that_a_fresh_read_restores():
    doc = document()
    result = {"score": 80, "violations": [{"rule_id": "26", "flagged_terms": ["approved"]}]}
    with patch.object(reads.core, "get_access", AsyncMock(return_value=(doc, [], None))), \
            patch.object(reads.scripts, "check_text", AsyncMock(return_value=result)), \
            patch.object(checks, "execute", AsyncMock()) as execute:
        returned = asyncio.run(reads.check(doc["id"], uuid4(), None))
    query, stored_id, payload = execute.await_args.args
    assert "SET latest_check" in query
    assert "body_text =" not in query
    assert stored_id == doc["id"]
    restored = checks.state({**doc, "latest_check": payload})
    assert restored["latest_check"] == returned
    assert returned["score"] == 80
    assert returned["violations"] == result["violations"]
    assert returned["checked_at"]
    assert restored["compliance_stale"] is False


@pytest.mark.parametrize("field,value", [
    ("body_text", "0:27 PRODUCT: An aircraft."), ("sources_text", "New source"),
    ("sources_text", ""), ("title", "Another claim"), ("details", {"rule_sets": ["DEFENSE"]}),
])
def test_edits_keep_previous_score_and_require_rerun(field, value):
    doc = document()
    with patch.object(checks, "execute", AsyncMock()):
        result = asyncio.run(checks.save(doc, {"score": 80, "violations": []}, doc["body_text"]))
    changed = {**doc, "latest_check": json.dumps(result), field: value}
    restored = checks.state(changed)
    assert restored["latest_check"]["score"] == 80
    assert restored["compliance_stale"] is True
    with patch.object(checks, "execute", AsyncMock()):
        refreshed = asyncio.run(checks.save(changed, {"score": 95, "violations": []}, changed["body_text"]))
    assert checks.state({**changed, "latest_check": refreshed})["compliance_stale"] is False


def test_notes_status_and_updated_time_do_not_invalidate_the_score():
    doc = document()
    result = {"score": 80, "checked_script_inputs": checks.inputs(doc)}
    assert checks.state({**doc, "latest_check": result, "status": "IN_REVIEW",
                         "updated_at": "later", "notes": ["please review"]})["compliance_stale"] is False
    assert checks.inputs({**doc, "body_text": "0:27\n PRODUCT: An approved aircraft. "}) == checks.inputs(doc)


def test_edit_during_check_is_outdated_instead_of_being_overwritten():
    doc = document()
    changed = copy.deepcopy(doc)
    changed["sources_text"] = "Edited while checking"
    with patch.object(checks, "execute", AsyncMock()):
        result = asyncio.run(checks.save(doc, {"score": 80}, doc["body_text"]))
    assert checks.state({**changed, "latest_check": result})["compliance_stale"] is True


def test_never_checked_script_has_no_score_or_rerun_warning():
    assert checks.state(document()) == {"latest_check": None, "compliance_stale": False}
