import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from models.compliance.caption_review_model import CaptionEditRequest
from models.compliance.post_text_review_model import PostTextSave
from services.compliance.submissions import caption_review_service as captions, post_text_review as posts
from services.compliance.submissions.review_caption_format import saved_html


HTML = '<p><strong>Heading</strong></p><p>Read <a href="https://example.com/report">the report</a>.</p><p><br></p><p>Keep  spaces.</p>'
TEXT = 'Heading\n\nRead the report.\n\n\nKeep  spaces.'


def test_safe_formatting_and_spacing_survive_without_preserving_unsafe_markup():
    clean = saved_html(TEXT, HTML + '<script>alert(1)</script><img src=x onerror=alert(1)>')
    for value in ('<strong>Heading</strong>', 'href="https://example.com/report"', '<p><br></p>', 'Keep  spaces.'):
        assert value in clean
    assert '<script' not in clean and 'onerror' not in clean and '<img' not in clean
    assert 'javascript:' not in saved_html('link', '<a href="javascript:alert(1)">link</a>')


def test_legacy_noop_preserves_existing_format_and_missing_html():
    assert '<strong>' in saved_html(TEXT, None, TEXT, HTML)
    assert saved_html(TEXT, None, TEXT, None) is None
    assert saved_html('one\n\ntwo & three', None) == '<p>one<br><br>two &amp; three</p>'


def test_caption_persistence_keeps_html_and_records_both_formatted_versions():
    async def scenario():
        conn = SimpleNamespace(execute=AsyncMock())
        subs = SimpleNamespace(_add_event=AsyncMock())
        row = {'id': uuid4(), 'current_version': 1, 'caption_html': HTML}
        edited = HTML.replace('Heading', 'Updated heading')
        data = CaptionEditRequest(base_caption=TEXT, caption_text=TEXT.replace('Heading', 'Updated heading'), caption_html=edited)
        with patch.object(captions, '_submissions', return_value=subs):
            await captions._persist_edit(conn, row, uuid4(), data, {}, TEXT)
        assert '<strong>Updated heading</strong>' in conn.execute.await_args_list[0].args[3]
        event = subs._add_event.await_args.args[-1]
        assert event['before_html'] == HTML
        assert '<strong>Updated heading</strong>' in event['after_html']
    asyncio.run(scenario())


def test_text_post_formatting_only_edit_saves_html_and_keeps_plain_text():
    async def scenario():
        conn = SimpleNamespace(execute=AsyncMock(), fetchval=AsyncMock(return_value='Reviewer'))
        row = {'id': uuid4(), 'caption_text': TEXT, 'caption_html': HTML, 'current_version': 1}
        edited = HTML.replace('<strong>Heading</strong>', '<strong><em>Heading</em></strong>')
        with patch('services.compliance.submissions.submission_service._add_event', new_callable=AsyncMock) as event:
            await posts.write_edit(conn, row, uuid4(), TEXT, html=edited)
        assert conn.execute.await_args.args[2] == TEXT
        assert '<em>Heading</em>' in conn.execute.await_args.args[3]
        assert event.await_args.args[-1]['before_html'] == HTML
    asyncio.run(scenario())


def test_formatting_conflicts_are_rejected_even_if_words_are_unchanged():
    row = {'caption_text': TEXT, 'caption_html': HTML}
    data = PostTextSave(text=TEXT, base_text=TEXT, html=HTML, base_html='<p>Different format</p>')
    with pytest.raises(HTTPException) as error:
        posts.require_base(row, [], data)
    assert error.value.status_code == 409
