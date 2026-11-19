"""Shadow benchmark coverage, grounding safeguards, usage metrics and non-promotion."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from models.compliance.compliance_model import ComplianceCheckRequest,ComplianceCheckResponse
from services.compliance.evaluation import full_rule_benchmark as benchmark
from tests.test_compliance_scoring import rule


def answer(r,**changes):
    return dict(rule_id=str(r.id),outcome='NO_ISSUE_FOUND',reason='No issue in this caption.',evidence_quote='',**changes)


class ValidationTests(unittest.TestCase):
    def setUp(self):self.rule=rule(checking_methods=['TEXT_ANALYSIS'])

    def test_missing_duplicate_unknown_and_malformed_answers_are_not_passes(self):
        r=self.rule
        for rows in ([],[answer(r),answer(r)],[{'rule_id':'unknown'}],[{'rule_id':str(r.id),'outcome':'PASS','reason':'ok'}],[answer(r)|{'reason':' '} ]):
            output=benchmark.validated_results({'results':rows},[r],'Post')
            self.assertEqual(len(output),1);self.assertFalse(output[0]['complete']);self.assertIsNone(output[0]['outcome'])

    def test_issue_requires_a_quote_really_present_in_the_post(self):
        r=self.rule
        raw=answer(r)|{'outcome':'ISSUE','evidence_quote':'guaranteed'}
        self.assertFalse(benchmark.validated_results({'results':[raw]},[r],'A normal caption')[0]['complete'])
        self.assertTrue(benchmark.validated_results({'results':[raw]},[r],'Guaranteed results')[0]['complete'])

    def test_text_triage_cannot_finish_visual_clearance_or_human_methods(self):
        for method in ('VISUAL_INSPECTION','CLEARANCE_DOCUMENTS','HUMAN_CONFIRMATION'):
            r=rule(checking_methods=[method])
            output=benchmark.validated_results({'results':[answer(r)]},[r],'Post')[0]
            self.assertEqual(output['outcome'],'HUMAN_REVIEW_REQUIRED')

    def test_unconfigured_rules_are_incomplete_in_the_benchmark_too(self):
        r=rule(checking_methods=[])
        self.assertFalse(benchmark.validated_results({'results':[answer(r)]},[r],'Post')[0]['complete'])


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_latency_and_results_are_measured_without_modifying_saved_score(self):
        r=rule(checking_methods=['TEXT_ANALYSIS'])
        fast=ComplianceCheckResponse(is_compliant=True,score=100)
        before=fast.model_dump()
        usage=SimpleNamespace(prompt_token_count=100,candidates_token_count=20,thoughts_token_count=5,total_token_count=125)
        response=SimpleNamespace(text=json.dumps({'results':[answer(r)]}),usage_metadata=usage)
        with patch.object(benchmark.context_evaluator,'_get_client',return_value=object()),patch.object(benchmark.ai_observability,'generate',new=AsyncMock(return_value=response)):
            report=await benchmark.run(ComplianceCheckRequest(text='Meet the team'),[r],fast,model='benchmark-model')
        self.assertEqual(report['completed_rules'],1);self.assertEqual(report['total_tokens'],125)
        self.assertFalse(report['applied_to_score']);self.assertEqual(fast.model_dump(),before)

    async def test_batched_full_pass_covers_every_rule_and_sums_usage(self):
        rules=[rule(checking_methods=['TEXT_ANALYSIS']) for _ in range(9)]
        async def generate(*args,**kwargs):
            payload=json.loads(kwargs['contents'])
            rows=[{'rule_id':r['rule_id'],'outcome':'NO_ISSUE_FOUND','reason':'No issue in this caption.','evidence_quote':''} for r in payload['rules']]
            return SimpleNamespace(text=json.dumps({'results':rows}),usage_metadata=SimpleNamespace(prompt_token_count=10,candidates_token_count=5,total_token_count=15))
        with patch.object(benchmark.context_evaluator,'_get_client',return_value=object()),patch.object(benchmark.ai_observability,'generate',new=AsyncMock(side_effect=generate)) as call:
            report=await benchmark.run(ComplianceCheckRequest(text='Meet the team'),rules,ComplianceCheckResponse(is_compliant=True),model='benchmark-model',batch_size=3)
        self.assertEqual(call.await_count,3);self.assertEqual(report['completed_rules'],9)
        self.assertEqual(report['total_tokens'],45);self.assertEqual(report['request_count'],3)
        self.assertTrue(all(len(json.loads(c.kwargs['contents'])['rules'])<=3 for c in call.await_args_list))

    async def test_timeout_leaves_all_rules_incomplete(self):
        r=rule(checking_methods=['TEXT_ANALYSIS'])
        with patch.object(benchmark.context_evaluator,'_get_client',return_value=object()),patch.object(benchmark.ai_observability,'generate',new=AsyncMock(side_effect=TimeoutError())):
            report=await benchmark.run(ComplianceCheckRequest(text='Post'),[r],ComplianceCheckResponse(is_compliant=True),model='benchmark-model')
        self.assertEqual(report['completed_rules'],0);self.assertIsNotNone(report['error'])

    async def test_input_guard_prevents_the_request(self):
        r=rule(checking_methods=['TEXT_ANALYSIS'])
        with patch.object(benchmark,'screen_input',return_value=SimpleNamespace(verdict='BLOCK')),patch.object(benchmark.ai_observability,'generate',new=AsyncMock()) as generate:
            report=await benchmark.run(ComplianceCheckRequest(text='Untrusted post'),[r],ComplianceCheckResponse(is_compliant=True),model='benchmark-model')
        generate.assert_not_awaited();self.assertEqual(report['completed_rules'],0)


if __name__=='__main__':unittest.main()
