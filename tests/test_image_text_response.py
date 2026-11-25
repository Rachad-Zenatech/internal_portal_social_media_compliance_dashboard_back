"""Image OCR must not lose the whole image because of a short or malformed model response."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from services.compliance.media import image_text_response as output, image_text_service as images


def response(text, stop_reason=None):
    return SimpleNamespace(text=text, stop_reason=stop_reason)


def test_truncated_image_response_retries_with_larger_budget(monkeypatch):
    monkeypatch.delenv("IMAGE_TEXT_MAX_OUTPUT_TOKENS", raising=False)
    generate = AsyncMock(side_effect=[response('{"text":"Unfinished', "max_tokens"), response('{"text":"Complete image text"}')])
    assert asyncio.run(output.read(generate, ["public test image"]))["text"] == "Complete image text"
    assert [call.kwargs["max_output_tokens"] for call in generate.await_args_list] == [8192, 16384]


@pytest.mark.parametrize("text", ['{"text":"Unterminated', '{}', '[]', '{"text":null}'])
def test_invalid_json_is_retried_but_never_reported_as_empty_image(text):
    generate = AsyncMock(side_effect=[response(text), response('{"text":"Readable"}')])
    assert asyncio.run(output.read(generate, []))["text"] == "Readable"
    assert generate.await_count == 2


def test_genuinely_textless_image_needs_no_retry():
    generate = AsyncMock(return_value=response('{"text":"","faces":[]}'))
    assert asyncio.run(output.read(generate, []))["text"] == ""
    assert generate.await_count == 1


def test_repeated_truncation_stops_after_one_retry():
    generate = AsyncMock(return_value=response('{"text":"Incomplete', "max_tokens"))
    with pytest.raises(ValueError, match="incomplete response twice"):
        asyncio.run(output.read(generate, []))
    assert generate.await_count == 2


def test_permission_failures_are_not_repeated_as_json_retries():
    generate = AsyncMock(side_effect=RuntimeError("AccessDenied"))
    with pytest.raises(RuntimeError, match="AccessDenied"):
        asyncio.run(output.read(generate, []))
    assert generate.await_count == 1


def test_explicit_image_retry_only_resets_failed_images():
    submission_id = uuid4()
    with patch.object(images, "fetch_all", AsyncMock(return_value=[{"id": uuid4()}])) as query:
        assert asyncio.run(images.retry_failed_images(submission_id)) == 1
    sql, passed_id, kinds = query.await_args.args
    assert "image_text_status='FAILED'" in sql
    assert "deleted_at IS NULL" in sql
    assert passed_id == submission_id
    assert kinds == ["MAIN", "IMAGE"]


def test_malformed_response_reported_by_adapter_is_retried():
    generate = AsyncMock(side_effect=[ValueError('Malformed model JSON'), response('{"text":"Recovered"}')])
    assert asyncio.run(output.read(generate, []))['text'] == 'Recovered'
    assert generate.await_count == 2


@pytest.mark.parametrize('field,value', [('ai_generated', 'false'), ('person_detected', 'true'), ('faces', 'none')])
def test_malformed_metadata_cannot_turn_false_strings_into_true_flags(field, value):
    import json
    generate = AsyncMock(side_effect=[response(json.dumps({'text': 'Readable', field: value})), response('{"text":"Recovered"}')])
    assert asyncio.run(output.read(generate, []))['text'] == 'Recovered'
    assert generate.await_count == 2
