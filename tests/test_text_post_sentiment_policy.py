import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from services.compliance.sentiment import jobs, policy
from services.compliance.submissions import approved_revision, post_steps_service


def row(kind="TEXT_POST", status="DRAFT"):
    now = datetime.now(timezone.utc)
    return dict(id=uuid4(), current_version=1, content_type=kind, status=status,
                title="Post", caption_text="Post text", brand_code="ZENA", audience="GENERAL",
                region="Global", platforms=["LINKEDIN"], has_account=True, rule_sets=["General"],
                has_main=True, check_at=now, latest_score=100, content_edited_at=now-timedelta(hours=1),
                revision_at=now-timedelta(hours=2), unresolved_comments=False,
                sentiment_at=None, sentiment_running=False, publish_after_changes=False)


def test_text_posts_can_finish_preparation_without_sentiment_but_images_still_require_it():
    text = post_steps_service.compute_steps(row())
    image = post_steps_service.compute_steps(row("IMAGE"))
    assert text.next == "send" and text.ready_to_send
    assert image.next == "sentiment" and not image.ready_to_send


def test_text_post_revision_can_be_resubmitted_without_sentiment():
    result = post_steps_service.compute_steps(row(status="CHANGES_REQUESTED"))
    assert result.next == "send" and result.ready_to_send


def test_manual_text_post_job_is_rejected_before_usage_or_queueing():
    async def scenario():
        with patch.object(jobs, '_load_submission', AsyncMock(return_value=row())), \
             patch.object(jobs, '_queue_for', AsyncMock()) as queue, \
             patch.object(jobs.ai_usage_service, 'reserve', AsyncMock()) as reserve:
            with pytest.raises(HTTPException) as error:
                await jobs.request_analysis(uuid4(), uuid4())
        assert error.value.status_code == 422
        queue.assert_not_awaited()
        reserve.assert_not_awaited()
    asyncio.run(scenario())


def test_publish_after_changes_keeps_compliance_required_and_skips_only_text_post_sentiment():
    async def scenario():
        text = row(status="CHANGES_REQUESTED")
        text['publish_after_changes'] = True
        state = dict(returned_at=text['revision_at'], sentiment_at=None)
        with patch.object(approved_revision, 'fetch_one', AsyncMock(return_value=state)):
            await approved_revision.require_fresh_checks(text, {'checked_at': text['check_at']})
            with pytest.raises(HTTPException):
                await approved_revision.require_fresh_checks(text, {})
            with pytest.raises(HTTPException):
                await approved_revision.require_fresh_checks(dict(text, content_type='IMAGE'), {'checked_at': text['check_at']})
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['IMAGE', 'SOCIAL_POST', 'VIDEO', 'CAROUSEL'])
def test_other_content_types_remain_eligible(kind):
    assert policy.applies({'content_type': kind})


def test_background_queueing_rejects_text_posts_before_capturing_or_reserving_work():
    async def scenario():
        with patch.object(jobs.reuse_freshness, 'capture', AsyncMock()) as capture, \
             patch.object(jobs.ai_usage_service, 'reserve', AsyncMock()) as reserve:
            with pytest.raises(HTTPException):
                await jobs._queue_for(row(), 'MANUAL', uuid4())
        capture.assert_not_awaited()
        reserve.assert_not_awaited()
    asyncio.run(scenario())
