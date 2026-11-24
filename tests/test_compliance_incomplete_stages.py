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
