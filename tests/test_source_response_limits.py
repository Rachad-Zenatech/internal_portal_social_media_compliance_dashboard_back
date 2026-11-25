"""A long source answer must get a larger retry, rather than repeat the same truncated budget."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from services.compliance.evaluation import source_response as output
from services.compliance.evaluation.source_verdict_models import _Response, RESPONSE_SCHEMA


def response(text, stop=None, used=None):
    return SimpleNamespace(text=text, stop_reason=stop, usage_metadata=SimpleNamespace(candidates_token_count=used))


def run(generate):
    return asyncio.run(output.read(generate, _Response, system_prompt='Compare claims', schema=RESPONSE_SCHEMA, timeout=45))


def test_truncated_comparison_retries_with_more_room(monkeypatch):
    monkeypatch.delenv('SOURCE_CHECK_MAX_OUTPUT_TOKENS', raising=False)
    budgets = []
    async def generate(config, attempt):
        budgets.append(config.max_output_tokens)
        return response('{"claims":[{"claim":"Claim cut off', 'max_tokens', 8192) if attempt == 1 else response('{"claims":[]}')
    assert run(generate).claims == []
    assert budgets == [8192, 16384]


def test_output_limit_is_not_misreported_as_a_model_outage():
    generate = AsyncMock(return_value=response('{"claims":[', 'max_tokens'))
    with pytest.raises(output.IncompleteSourceResponse) as error:
        run(generate)
    assert error.value.code == 'OUTPUT_LIMIT'
    assert generate.await_count == 2


@pytest.mark.parametrize('text', ['{}', '', '{"claims":null}', 'not JSON'])
def test_malformed_output_cannot_be_cached_as_no_claims(text):
    generate = AsyncMock(side_effect=[response(text), response('{"claims":[]}')])
    assert run(generate).claims == []
    assert generate.await_count == 2


def test_token_usage_detects_cutoff_when_provider_omits_stop_reason():
    with pytest.raises(output.IncompleteSourceResponse) as error:
        output.parse(response('{"claims":[', used=8192), _Response, 8192)
    assert error.value.code == 'OUTPUT_LIMIT'


def test_permission_failures_are_not_retried_as_json_failures():
    generate = AsyncMock(side_effect=RuntimeError('AccessDenied'))
    with pytest.raises(RuntimeError, match='AccessDenied'):
        run(generate)
    assert generate.await_count == 1


def test_exhausted_larger_retry_does_not_repeat_across_all_model_aliases(monkeypatch):
    from services.compliance.evaluation import source_check_service as sc
    monkeypatch.setenv('SOURCE_CHECK_MODELS', 'model-a,model-b')
    monkeypatch.setattr(sc, 'MODELS', ['model-a', 'model-b'])
    monkeypatch.setattr(sc, 'model_is_available', lambda model: True)
    monkeypatch.setattr(sc.source_passages, 'excerpt', AsyncMock(return_value='A source'))
    monkeypatch.setattr(sc.source_comparison_cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(sc._cache, 'get', AsyncMock(return_value=None))
    ask = AsyncMock(side_effect=output.IncompleteSourceResponse('OUTPUT_LIMIT'))
    monkeypatch.setattr(sc, '_ask', ask)
    reasons = []
    answer = asyncio.run(sc._compare('Claim.', 'Source', None, None, None, 'A source', inventory=[{'claim':'Claim.','where':'caption'}], failure_reasons=reasons))
    assert answer == (None, None)
    assert reasons == ['OUTPUT_LIMIT']
    ask.assert_awaited_once()
