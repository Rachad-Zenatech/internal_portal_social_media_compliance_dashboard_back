"""Long visual assessments receive a larger retry instead of a silent incomplete result."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from services.compliance.evaluation.visual_response import read


def parse(response):
    return json.loads(response.text)['assessments']


def test_visual_cutoff_retries_with_a_larger_response_allowance():
    budgets = []
    async def generate(limit):
        budgets.append(limit)
        return SimpleNamespace(text='{"assessments":[',stop_reason='max_tokens') if len(budgets)==1 else SimpleNamespace(text='{"assessments":[]}',stop_reason='end_turn')
    _, result = asyncio.run(read(generate,parse))
    assert result == []
    assert budgets == [4096,8192]


def test_repeated_cutoff_never_counts_as_a_clear_visual_review():
    generate=AsyncMock(return_value=SimpleNamespace(text='{"assessments":[',stop_reason='max_tokens'))
    with pytest.raises(ValueError, match='could not finish'):
        asyncio.run(read(generate,parse))
    assert generate.await_count == 2


def test_visual_permission_error_is_not_retried_as_bad_json():
    generate=AsyncMock(side_effect=RuntimeError('AccessDenied'))
    with pytest.raises(RuntimeError, match='AccessDenied'):
        asyncio.run(read(generate,parse))
    assert generate.await_count == 1
