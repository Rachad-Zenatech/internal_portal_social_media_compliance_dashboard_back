"""Caption saves are attributed, conflict-safe, and scoped to the selected caption."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from models.compliance.caption_review_model import CaptionEditRequest
from services.compliance.submissions import caption_review_service as svc


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def setup(monkeypatch):
    user, post = uuid4(), uuid4()
    row = {'id': post, 'created_by': uuid4(), 'status': 'IN_REVIEW', 'current_version': 2,
           'caption_text': 'Original caption', 'platforms': ['FACEBOOK', 'INSTAGRAM'],
           'platform_captions': {'INSTAGRAM': 'Instagram caption'}, 'content_edited_at': None}
    conn = SimpleNamespace(execute=AsyncMock(), fetchrow=AsyncMock(return_value=row))

    @asynccontextmanager
    async def context():
        yield conn

    conn.transaction = context
    import json
    subs = SimpleNamespace(
        _get_owned_row=AsyncMock(return_value=row), _can_suggest=AsyncMock(return_value=True),
        _add_event=AsyncMock(), _platform_captions=lambda item: dict(item.get('platform_captions') or {}),
        _json=lambda value: json.loads(value) if isinstance(value, str) else value,
    )
    monkeypatch.setattr(svc, '_submissions', lambda: subs)
    monkeypatch.setattr(svc, 'get_pool', lambda: SimpleNamespace(acquire=context))
    monkeypatch.setattr(svc, 'get_caption_review', AsyncMock(return_value='saved'))
    return user, post, row, conn


def request(base='Original caption', after='Edited caption', platform=None):
    return CaptionEditRequest(base_caption=base, caption_text=after, platform=platform)


@pytest.mark.anyio
async def test_main_caption_saved_with_matching_html_and_attributed_history(setup):
    user, post, row, conn = setup
    assert await svc.save_caption(post, user, request(after='New <text>\nSecond line')) == 'saved'
    calls = conn.execute.call_args_list
    assert 'FOR UPDATE' in conn.fetchrow.call_args.args[0]
    assert calls[1].args[1:] == (post, 'New <text>\nSecond line', '<p>New &lt;text&gt;<br>Second line</p>', user)
    assert 'content_edited_at = NOW()' in calls[1].args[0]
    assert 'review_caption_edited' in calls[2].args[0]
    event = svc._submissions()._add_event.call_args.args
    assert event[1:5] == (post, 'CAPTION_EDITED', 'Caption edited during review', user)
    assert event[5] == {'before': 'Original caption', 'after': 'New <text>\nSecond line', 'platform': None, 'version': 2}


@pytest.mark.anyio
async def test_platform_edit_preserves_other_captions(setup):
    import json
    user, post, row, conn = setup
    await svc.save_caption(post, user, request('Instagram caption', 'New Instagram caption', 'INSTAGRAM'))
    call = conn.execute.call_args_list[1]
    assert 'platform_captions = ' in call.args[0]
    assert 'caption_text = ' not in call.args[0]
    assert json.loads(call.args[2]) == {'INSTAGRAM': 'New Instagram caption'}
    assert row['caption_text'] == 'Original caption'


@pytest.mark.anyio
async def test_new_platform_override_uses_main_as_optimistic_base(setup):
    user, post, _, _ = setup
    await svc.save_caption(post, user, request(platform='FACEBOOK'))
    assert svc._submissions()._add_event.call_args.args[5]['platform'] == 'FACEBOOK'


@pytest.mark.anyio
async def test_noop_does_not_add_revision_or_invalidate_score(setup):
    user, post, _, conn = setup
    await svc.save_caption(post, user, request(after='Original caption'))
    svc._submissions()._add_event.assert_not_awaited()
    assert conn.execute.await_count == 1  # lock timeout only


@pytest.mark.anyio
@pytest.mark.parametrize('condition,status', [('conflict', 409), ('approved', 409), ('deleted', 404), ('platform', 422), ('permission', 403)])
async def test_invalid_save_cannot_mutate_caption(setup, monkeypatch, condition, status):
    user, post, row, conn = setup
    data = request()
    if condition == 'conflict':
        row['caption_text'] = 'Another reviewer saved this'
    elif condition == 'approved':
        row['status'] = 'APPROVED'
    elif condition == 'deleted':
        conn.fetchrow.return_value = None
    elif condition == 'platform':
        data = request(platform='UNKNOWN')
    else:
        monkeypatch.setattr(svc._submissions(), '_can_suggest', AsyncMock(return_value=False))
    with pytest.raises(HTTPException) as error:
        await svc.save_caption(post, user, data)
    assert error.value.status_code == status
    svc._submissions()._add_event.assert_not_awaited()
    assert not any('UPDATE content_submissions' in call.args[0] for call in conn.execute.call_args_list)


@pytest.mark.anyio
async def test_post_access_checked_before_save(setup, monkeypatch):
    user, post, _, conn = setup
    monkeypatch.setattr(svc._submissions(), '_get_owned_row', AsyncMock(side_effect=HTTPException(404)))
    with pytest.raises(HTTPException):
        await svc.save_caption(post, user, request())
    conn.fetchrow.assert_not_awaited()


@pytest.mark.anyio
async def test_history_returns_before_after_and_author(setup, monkeypatch):
    user, post, row, _ = setup
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(svc, 'fetch_all', AsyncMock(return_value=[{
        'id': 7, 'data': {'before': 'Old', 'after': 'New', 'version': 2, 'platform': None},
        'actor_user_id': user, 'actor_name': 'Arifa Mansoor', 'created_at': now,
    }]))
    result = await ORIGINAL_READER(post, user)
    assert result.revisions[0].actor_id == user
    assert result.revisions[0].actor_name == 'Arifa Mansoor'
    assert (result.revisions[0].before, result.revisions[0].after) == ('Old', 'New')
    assert result.can_edit


@pytest.mark.anyio
async def test_approval_race_rejects_newer_caption(setup):
    _, _, row, conn = setup
    conn.fetchrow.return_value = {**row, 'content_edited_at': datetime.now(timezone.utc)}
    with pytest.raises(HTTPException) as error:
        await svc.decision_status(conn, row)
    assert error.value.status_code == 409


@pytest.mark.anyio
async def test_unchanged_caption_allows_existing_decision_flow(setup):
    _, _, row, conn = setup
    assert await svc.decision_status(conn, row) == 'IN_REVIEW'


ORIGINAL_READER = svc.get_caption_review


@pytest.mark.anyio
@pytest.mark.parametrize('still_pending', [False, True])
async def test_caption_edit_forces_recheck_before_approval(setup, monkeypatch, still_pending):
    import sys
    from services.compliance.submissions.review_approval_service import require_approval
    user, post, row, _ = setup
    subs = svc._submissions()
    row['latest_check'] = {'rulebook_version': 'current', 'review_caption_edited': True}
    subs.compliance_rulebook_version = AsyncMock(return_value='current')
    subs._recheck_one = AsyncMock()
    updated = {**row, 'latest_check': {'rulebook_version': 'current'}}
    if still_pending:
        updated = row
    subs._get_owned_row = AsyncMock(return_value=updated)
    sentinel = RuntimeError('reached normal approval gates')
    def require_check(*args, **kwargs):
        raise sentinel
    subs.manual_check_service = SimpleNamespace(require_current_check=require_check)
    monkeypatch.setitem(sys.modules, 'services.compliance.submissions.submission_service', subs)
    import services.compliance.submissions as package
    monkeypatch.setattr(package, 'submission_service', subs, raising=False)
    if still_pending:
        with pytest.raises(HTTPException) as error:
            await require_approval(row, user, False)
        assert error.value.status_code == 409
    else:
        with pytest.raises(RuntimeError, match='reached normal approval gates'):
            await require_approval(row, user, False)
    subs._recheck_one.assert_awaited_once()


@pytest.mark.parametrize('caption', ['', '   ', '\n\t'])
def test_empty_caption_cannot_bypass_submission_requirements(caption):
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match='Caption cannot be empty'):
        CaptionEditRequest(base_caption='Original', caption_text=caption)


@pytest.mark.anyio
async def test_caption_review_exposes_pending_recheck_state(setup, monkeypatch):
    user, post, row, _ = setup
    row['latest_check'] = {'review_caption_edited': True}
    monkeypatch.setattr(svc, 'fetch_all', AsyncMock(return_value=[]))
    assert (await ORIGINAL_READER(post, user)).needs_recheck is True
    row['latest_check'] = {'score': 95}
    assert (await ORIGINAL_READER(post, user)).needs_recheck is False
