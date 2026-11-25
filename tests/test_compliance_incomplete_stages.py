"""Keep the failed stage distinct from a compliance breach or a clean result."""
from types import SimpleNamespace
from models.compliance.check_diagnostics_model import IncompleteCheckStage
from models.compliance.compliance_model import ComplianceCheckResponse
from services.compliance.evaluation.compliance_diagnostics import stages


def source(failed=False, reasons=()):
    return SimpleNamespace(ai_failed=failed, incomplete_stages=list(reasons))


def test_source_cutoff_is_saved_on_the_result():
    cause = IncompleteCheckStage(stage='SOURCE_VERIFICATION', code='OUTPUT_LIMIT', reason='Source comparisons were cut off.')
    details = stages(False, source(True, [cause]), [], [{'status':'COMPLETED'}])
    result = ComplianceCheckResponse(is_compliant=False, ai_unavailable=True, score=100, incomplete_stages=details)
    assert result.model_dump()['incomplete_stages'][0]['code'] == 'OUTPUT_LIMIT'
    assert result.score == 100
    assert not result.is_compliant


def test_distinct_failed_stages_are_all_retained():
    details = stages(True, source(), [SimpleNamespace(evaluation=SimpleNamespace(source='UNAVAILABLE'))], [{'status':'UNAVAILABLE'}])
    assert [d.stage for d in details] == ['RULEBOOK','CONTEXT_REVIEW','VISUAL_REVIEW']


def test_completed_check_has_no_failure_diagnostics():
    assert stages(False, source(), [], [{'status':'COMPLETED'}]) == []


def test_legacy_source_failure_gets_an_explicit_stage():
    assert stages(False, source(True), [], [])[0].stage == 'SOURCE_VERIFICATION'


def test_full_source_run_records_output_limit_stage(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from models.compliance.compliance_model import SourceCheck
    from services.compliance.evaluation import source_check_service as sc
    monkeypatch.setattr(sc, 'ENABLED', True)
    monkeypatch.setattr(sc.source_claim_coverage, 'identify', AsyncMock(return_value=[{'claim':'Payload is 50 kg','where':'caption'}]))
    monkeypatch.setattr(sc.citation_finder, 'find', lambda *args, **kwargs: [SimpleNamespace(kind='URL',reference='https://example.com/spec',where='caption',cue='source',truncated=False)] if args[0] else [])
    monkeypatch.setattr(sc, '_read_link', AsyncMock(return_value=(SourceCheck(reference='https://example.com/spec',kind='URL',where='caption',status='READ'), ('Source','Payload is 50 kg.'))))
    monkeypatch.setattr(sc.newsroom_claim_sources, 'readings', AsyncMock(return_value=[]))
    monkeypatch.setattr(sc.source_passages, 'rank', AsyncMock(return_value=[[1.0]]))
    async def compare(*args, failure_reasons, **kwargs):
        failure_reasons.append('OUTPUT_LIMIT')
        return None, None
    monkeypatch.setattr(sc, '_compare', compare)
    result = asyncio.run(sc.run('Payload is 50 kg. Source: https://example.com/spec'))
    assert result.ai_failed
    assert any(item.stage == 'SOURCE_VERIFICATION' and item.code == 'OUTPUT_LIMIT' for item in result.incomplete_stages)
    assert not result.unbacked  # Failed verification must not invent an unsupported-claim deduction.
