"""Explicit rule methods and conservative every-rule result coverage."""
import unittest
from unittest.mock import AsyncMock, patch
from pydantic import ValidationError
from models.compliance.compliance_model import ComplianceRuleCreate, ComplianceRuleUpdate, ComplianceCheckRequest, ComplianceCheckResponse, ComplianceViolation, ContextEvaluation, SourceCheck, SourceClaim
from models.compliance.rule_check_model import MethodCheckResult
from services.compliance.evaluation import compliance_service as cs, rule_coverage as coverage
from postgresql_db.schemas.setup_rule_checks_schema import BASELINE_METHODS
from tests.test_compliance_scoring import rule


def response(**values):
    return ComplianceCheckResponse(is_compliant=True, **values)


def result(methods, **values):
    r=rule(checking_methods=methods, **values)
    return r, coverage.rule_result(r, ComplianceCheckRequest(text="Meet the team.",content_type="TEXT"), response())


class MethodConfigurationTests(unittest.TestCase):
    def test_create_requires_a_deliberate_nonempty_method_list(self):
        values=dict(rule_number="Custom",category="Test",title="Test",summary="Test")
        for extra in ({},{"checking_methods":[]},{"checking_methods":["GUESS"]}):
            with self.assertRaises(ValidationError):ComplianceRuleCreate(**values,**extra)
        self.assertEqual(ComplianceRuleCreate(**values,checking_methods=["TEXT_ANALYSIS","TEXT_ANALYSIS"]).checking_methods,["TEXT_ANALYSIS"])

    def test_updates_cannot_clear_methods_and_deduplicate_valid_choices(self):
        with self.assertRaises(ValidationError):ComplianceRuleUpdate(checking_methods=[])
        self.assertEqual(ComplianceRuleUpdate(checking_methods=["HUMAN_CONFIRMATION","HUMAN_CONFIRMATION"]).checking_methods,["HUMAN_CONFIRMATION"])

    def test_all_baseline_rules_have_explicit_methods(self):
        self.assertEqual(set(BASELINE_METHODS),{f"Rule {n}" for n in range(1,32)})
        for methods in BASELINE_METHODS.values():
            self.assertTrue(methods)
            rule(checking_methods=methods)
        self.assertEqual({number for number,methods in BASELINE_METHODS.items() if "HUMAN_CONFIRMATION" in methods},{"Rule 8","Rule 18","Rule 20","Rule 21","Rule 22"})

    def test_result_reasons_cannot_be_blank(self):
        with self.assertRaises(ValidationError):MethodCheckResult(method="TEXT_ANALYSIS",reason="   ")


class ResultTests(unittest.TestCase):
    def test_empty_phrases_do_not_turn_a_rule_into_a_human_check(self):
        _,out=result(["TEXT_ANALYSIS"])
        self.assertFalse(out.complete);self.assertIsNone(out.outcome)
        self.assertIn('No full text judgment',out.reason)

    def test_human_confirmation_is_deliberate_even_with_phrases(self):
        _,out=result(["HUMAN_CONFIRMATION"],prohibited_terms=["team"])
        self.assertEqual(out.outcome,'HUMAN_REVIEW_REQUIRED');self.assertTrue(out.complete)

    def test_literal_matching_can_return_no_issue_with_a_reason(self):
        _,out=result(["TEXT_ANALYSIS"],prohibited_terms=["guaranteed"])
        self.assertEqual(out.outcome,'NO_ISSUE_FOUND');self.assertTrue(out.complete)
        self.assertIn('configured phrases',out.reason)

    def test_no_trigger_does_not_claim_a_contextual_policy_passed(self):
        _,out=result(["TEXT_ANALYSIS"],evaluation_mode="CONTEXT",prohibited_terms=["guaranteed"])
        self.assertFalse(out.complete);self.assertIsNone(out.outcome)

    def test_ocr_or_video_transcript_is_not_a_visual_inspection(self):
        r=rule(checking_methods=["VISUAL_INSPECTION"])
        for kind in ('IMAGE','VIDEO'):
            out=coverage.rule_result(r,ComplianceCheckRequest(text="Image text or transcript",content_type=kind),response(content_parts=['imagery' if kind=='IMAGE' else 'video']))
            self.assertFalse(out.complete);self.assertIsNone(out.outcome)
            self.assertIn('not been recorded',out.reason)

    def test_visual_method_can_be_explicitly_not_applicable_for_text_only(self):
        _,out=result(["VISUAL_INSPECTION"])
        self.assertEqual(out.outcome,'NOT_APPLICABLE');self.assertTrue(out.complete)

    def test_clearance_is_not_inferred_from_an_attached_source(self):
        _,out=result(["CLEARANCE_DOCUMENTS"])
        self.assertFalse(out.complete);self.assertIn('not clearance',out.reason)

    def test_unconfigured_rule_remains_incomplete(self):
        _,out=result([])
        self.assertFalse(out.complete);self.assertIsNone(out.outcome);self.assertIn('No checking method',out.reason)

    def test_a_partial_clean_result_does_not_mark_the_whole_rule_passed(self):
        r=rule(checking_methods=['TEXT_ANALYSIS','VISUAL_INSPECTION'],prohibited_terms=['guaranteed'])
        out=coverage.rule_result(r,ComplianceCheckRequest(text='Meet the team',content_type='IMAGE'),response(content_parts=['imagery']))
        self.assertFalse(out.complete);self.assertIsNone(out.outcome)
        self.assertEqual(out.methods[0].outcome,'NO_ISSUE_FOUND')

    def test_known_issue_stays_visible_beside_an_incomplete_method(self):
        r=rule(checking_methods=['TEXT_ANALYSIS','VISUAL_INSPECTION'])
        hit=ComplianceViolation(rule_id=r.id,rule_number=r.rule_number,title=r.title,category='Policy',severity='HIGH',traffic_light='YELLOW',flagged_terms=['team'],explanation='Prohibited team claim',penalty=20,status='VIOLATION')
        out=coverage.rule_result(r,ComplianceCheckRequest(text='team',content_type='IMAGE'),response(violations=[hit],content_parts=['imagery']))
        self.assertFalse(out.complete);self.assertEqual(out.outcome,'ISSUE');self.assertIn('Prohibited team claim',out.reason)

    def test_provider_failure_cannot_be_recorded_as_evaluated(self):
        r=rule(checking_methods=['TEXT_ANALYSIS'])
        hit=ComplianceViolation(rule_id=r.id,rule_number=r.rule_number,title=r.title,category='Policy',severity='HIGH',traffic_light='YELLOW',flagged_terms=['team'],explanation='Provider unavailable',status='NEEDS_REVIEW',evaluation=ContextEvaluation(decision='NEEDS_REVIEW',source='UNAVAILABLE',reasoning='No model answered'))
        out=coverage.rule_result(r,ComplianceCheckRequest(text='team'),response(violations=[hit]))
        self.assertFalse(out.complete);self.assertIsNone(out.outcome)

    def test_source_comparisons_return_distinct_results(self):
        r=rule(rule_number='Rule 18',checking_methods=['SOURCE_VERIFICATION'])
        expected={'SUPPORTED':'NO_ISSUE_FOUND','CONTRADICTED':'ISSUE','NOT_FOUND':'EVIDENCE_REQUIRED','UNCLEAR':'HUMAN_REVIEW_REQUIRED'}
        for verdict,outcome in expected.items():
            check=SourceCheck(reference='Source',kind='URL',where='source_links',status='READ',claims=[SourceClaim(claim='A claim',verdict=verdict)])
            out=coverage.rule_result(r,ComplianceCheckRequest(text='A claim'),response(source_checks=[check]))
            self.assertEqual(out.outcome,outcome);self.assertTrue(out.complete)

    def test_missing_newsroom_summary_claim_does_not_become_a_source_warning(self):
        r=rule(rule_number='Rule 18',checking_methods=['SOURCE_VERIFICATION'])
        check=SourceCheck(reference='ZenaTech newsroom',kind='URL',where='newsroom',status='READ',claims=[SourceClaim(claim='A claim',verdict='NOT_FOUND')])
        out=coverage.rule_result(r,ComplianceCheckRequest(text='A claim'),response(source_checks=[check]))
        self.assertFalse(out.complete);self.assertIsNone(out.outcome)

    def test_no_source_result_is_not_an_implicit_factual_pass(self):
        _,out=result(['SOURCE_VERIFICATION'],rule_number='Rule 18')
        self.assertFalse(out.complete);self.assertIsNone(out.outcome)

    def test_every_applicable_rule_gets_one_row_without_altering_score_or_blocks(self):
        rules=[rule(checking_methods=['TEXT_ANALYSIS']),rule(checking_methods=['HUMAN_CONFIRMATION']),rule(checking_methods=[])]
        check=response(score=97,blocked=False,violations=[])
        original=check.model_dump(exclude={'rule_results','rule_results_version','rule_results_complete'})
        coverage.attach_results(ComplianceCheckRequest(text='Meet team'),rules,check)
        self.assertEqual([row.rule_id for row in check.rule_results],[r.id for r in rules])
        self.assertFalse(check.rule_results_complete)
        self.assertEqual(check.model_dump(exclude={'rule_results','rule_results_version','rule_results_complete'}),original)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_human_only_rule_is_not_scanned_as_a_text_rule(self):
        r=rule(checking_methods=['HUMAN_CONFIRMATION'],prohibited_terms=['team'])
        with patch.object(cs,'applicable_rules',new=AsyncMock(return_value=[r])),patch.object(cs,'rulebook_version',new=AsyncMock(return_value='v1')),patch.object(cs,'_evaluate_rule') as evaluate,patch.object(cs,'_semantic_findings',new=AsyncMock(return_value=[])),patch.object(cs.source_check_service,'run',new=AsyncMock(return_value=cs.source_check_service.SourceCheckResult())):
            checked=await cs.check_compliance(ComplianceCheckRequest(text='Meet team'))
        evaluate.assert_not_called()
        self.assertEqual(checked.manual_review_rules[0].rule_id,r.id)
        self.assertEqual(checked.rule_results[0].outcome,'HUMAN_REVIEW_REQUIRED')
        self.assertFalse(checked.blocked)


if __name__=='__main__':unittest.main()
