"""Review routing can reuse unchanged inputs; approval still needs current checks."""
from copy import deepcopy

import pytest
from fastapi import HTTPException

from services.compliance.submissions.manual_check_service import require_current_check, require_send_score


@pytest.fixture
def row():
    return {"media_edited_at": "2026-11-23T10:00:00+00:00", "source_links": ["https://example.com"],
            "latest_check": {"rulebook_version": "older-system", "score": 95,
                             "checked_media_edited_at": "2026-11-23T10:00:00+00:00",
                             "checked_source_links": ["https://example.com"]}}


def test_review_accepts_older_rules_with_unchanged_inputs(row):
    _, check = require_current_check(row, "new-system", allow_older_rules=True, validate=False)
    assert check["score"] == 95
    assert check["rulebook_version"] == "older-system"


def test_approval_still_requires_current_rules(row):
    with pytest.raises(HTTPException):
        require_current_check(row, "new-system", validate=False)


@pytest.mark.parametrize("change", ["media", "sources", "pending", "unavailable", "missing"])
def test_review_still_rejects_changed_or_incomplete_checks(row, change):
    row = deepcopy(row)
    if change == "media":
        row["media_edited_at"] = "2026-11-24T10:00:00+00:00"
    elif change == "sources":
        row["source_links"] = ["https://different.example.com"]
    elif change == "missing":
        row["latest_check"] = None
    else:
        row["latest_check"]["analysis_pending" if change == "pending" else "ai_unavailable"] = True
    with pytest.raises(HTTPException):
        require_current_check(row, "new-system", allow_older_rules=True, validate=False)


def test_review_still_enforces_score_threshold():
    with pytest.raises(HTTPException):
        require_send_score(80)
