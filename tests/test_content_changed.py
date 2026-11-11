"""A save only makes the checks out of date when it changes something they read."""

from datetime import datetime, timezone
from uuid import uuid4

from services.compliance.submissions.submission_service import content_changed

ROW = {
    "title": "Daas",
    "caption_text": "Meet Drone as a Service.",
    "platforms": ["FACEBOOK", "INSTAGRAM"],
    "rule_sets": ["ZenaTech General"],
    "publish_at": datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc),
    "account_id": uuid4(),
    "approval_needed_by": None,
}


def test_saving_the_same_form_again_changes_nothing():
    same = {"title": "Daas", "caption_text": "Meet Drone as a Service.", "platforms": ["FACEBOOK", "INSTAGRAM"],
            "publish_at": "2026-11-12T14:00:00Z", "account_id": str(ROW["account_id"])}
    assert content_changed(ROW, same) is False


def test_a_new_caption_counts():
    assert content_changed(ROW, {"caption_text": "Something else."}) is True


def test_a_new_platform_list_counts():
    assert content_changed(ROW, {"platforms": ["FACEBOOK"]}) is True


def test_a_new_publish_date_counts():
    assert content_changed(ROW, {"publish_at": "2026-11-13T14:00:00Z"}) is True


def test_who_reviews_and_by_when_never_counts():
    assert content_changed(ROW, {"approval_needed_by": "2026-11-12T09:00:00Z", "reviewer_ids": [str(uuid4())]}) is False


def test_a_field_the_post_did_not_have_counts():
    assert content_changed(ROW, {"region": "Global"}) is True
