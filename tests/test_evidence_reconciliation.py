"""Shared source evidence resolves factual concerns without waiving unrelated compliance rules."""
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock, patch
from contextlib import asynccontextmanager

from models.compliance.compliance_model import ComplianceViolation, SourceCheck, SourceClaim
from services.compliance.evaluation import source_evidence_context as ec, evidence_reconciliation as er
from services.compliance.evaluation import context_evaluator as ce, visual_review as vr

CLAIM = "Company revenue totals approximately $503,000."
OTHER = "Another company's revenue totals $800,000."


def source(claim=CLAIM, verdict="SUPPORTED", status="READ", authority="COMPANY", **updates):
    item = SourceClaim(claim=claim, verdict=verdict, evidence=claim, confidence=.95, **updates)
    return SourceCheck(reference="Official release", kind="URL", status=status, authority=authority,
                       title="Published release", claims=[item], model="test-model")


def finding(rule="Rule 18", status="EVIDENCE_REQUIRED", text=CLAIM, **updates):
    return ComplianceViolation.model_validate({"rule_id": str(uuid4()), "rule_number": rule, "title": "Claim needs evidence",
        "category": "Source Check", "severity": "MEDIUM", "traffic_light": "YELLOW", "status": status,
        "flagged_terms": [text], "passage": text, "context": text, "explanation": "Concern to verify", "penalty": 5, **updates})


def rule_for(item, mode="CONTEXT"):
    return SimpleNamespace(id=item.rule_id, rule_number=item.rule_number, title=item.title, category=item.category, severity="MEDIUM",
        traffic_light="YELLOW", updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc), summary="Apply the specific policy.",
        evaluation_guidance="Check the context", exception_terms=[], allowed_examples=[], prohibited_examples=[],
        required_disclaimers=[], disclaimer_triggers=[], remediation_guidance="Review", evaluation_mode=mode)


def request(text=CLAIM):
    return SimpleNamespace(text=text, brand_code="ZenaTech", brand_codes=["ZenaTech"], audiences=[],
        region="US", content_type="POST", disclaimer_text=text, sources=[], _visual_media=[])


class EvidenceClearingTests(unittest.TestCase):
    def test_supported_rule18_clears_both_pending_statuses_and_restores_points(self):
        for status in ["EVIDENCE_REQUIRED", "NEEDS_REVIEW"]:
            item = finding(status=status)
            self.assertEqual(er.clear_supported([item], [source()]), 1)
            self.assertEqual((item.status, item.penalty, item.uncounted_penalty, item.counted_with), ("CLEARED", 0, 0, None))
            self.assertIn("Published release", item.explanation)
            self.assertEqual(item.evaluation.model, "test-model")

    def test_no_blanket_exemption_for_other_rules_or_forward_projections(self):
        for number in ["Rule 4", "Rule 9", "Rule 25", "Rule 28"]:
            item = finding(rule=number, status="VIOLATION")
            self.assertEqual(er.clear_supported([item], [source()]), 0)
            self.assertEqual(item.status, "VIOLATION")

    def test_unreadable_unclear_low_confidence_or_missing_evidence_never_clear(self):
        cases = [source(status="UNREADABLE"), source(verdict="UNCLEAR"), source(verdict="NOT_FOUND")]
        weak = source(); weak.claims[0].confidence = .1
        empty = source(); empty.claims[0].evidence = None
        for check in [*cases, weak, empty]:
            self.assertEqual(er.clear_supported([finding()], [check]), 0)

    def test_preserves_existing_support_confidence_threshold(self):
        check = source(); check.claims[0].confidence = ec.SUPPORT_MIN_CONFIDENCE
        self.assertEqual(er.clear_supported([finding()], [check]), 1)

    def test_real_conflicts_and_source_errors_are_not_cleared(self):
        self.assertEqual(er.clear_supported([finding()], [source(), source(verdict="CONTRADICTED")]), 0)
        for status, title in [("VIOLATION", "Claim doesn't match its source"),
                ("NEEDS_REVIEW", "Claim differs from the newsroom"), ("NEEDS_REVIEW", "Source couldn't be checked")]:
            self.assertEqual(er.clear_supported([finding(status=status, title=title)], [source()]), 0)

    def test_whole_assertion_identity_protects_numbers_subject_and_qualifiers(self):
        for claim in [CLAIM.replace("503", "530"), CLAIM.replace("approximately ", ""), OTHER]:
            self.assertEqual(er.clear_supported([finding()], [source(claim)]), 0)
        self.assertEqual(er.clear_supported([finding(text="Company revenue")], [source()]), 0)

    def test_wrapped_supported_text_clears_without_exact_source_word_matching(self):
        item = finding(text=CLAIM.replace("revenue totals", "revenue\ntotals"))
        check = source(); check.claims[0].evidence = "The release reports this revenue figure with the same scope."
        self.assertEqual(er.clear_supported([item], [check]), 1)

    def test_public_information_needs_authoritative_support(self):
        for authority, expected in [("COMPANY", 1), ("OFFICIAL", 1), ("OTHER", 0)]:
            self.assertEqual(er.clear_supported([finding(rule="Rule 6", status="VIOLATION")], [source(authority=authority)]), expected)
        self.assertEqual(er.clear_supported([finding(rule="Rule 6", kind="MISSING_DISCLAIMER")], [source()]), 0)

    def test_evidence_changes_invalidate_context_cache_but_source_order_does_not(self):
        item = finding(rule="Rule 4")
        def candidate(checks):
            return ec.attach([ce.Candidate(item, rule_for(item), CLAIM, CLAIM)], checks)[0]
        original = candidate([source()]); changed = source(); changed.claims[0].evidence = "A new supporting passage."
        self.assertNotEqual(ce._key(original), ce._key(candidate([changed])))
        other = source(OTHER)
        self.assertEqual(ce._key(candidate([source(), other])), ce._key(candidate([other, source()])))
        self.assertNotEqual(ce._key(original), ce._key(candidate([])))

    def test_context_payload_has_relevant_evidence_and_rule_boundaries(self):
        item = finding(rule="Rule 4")
        candidate = ec.attach([ce.Candidate(item, rule_for(item), CLAIM, CLAIM)], [source(), source(OTHER)])[0]
        metadata = ce._batch_payload([(0, candidate)])["items"][0]["submission_details"]
        self.assertEqual([row["claim"] for row in metadata["verified_sources"]], [CLAIM])
        self.assertIn("required published", metadata["evidence_policy"])
        self.assertIn("required published", ce.SYSTEM_PROMPT)


class EvidenceFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_reconciliation_resolves_late_factual_flags_without_ai(self):
        item = finding(status="NEEDS_REVIEW")
        with patch.object(ce, "evaluate", AsyncMock()) as evaluate:
            await er.reconcile(request(), [], [item], [source()], {})
            evaluate.assert_not_awaited()
        self.assertEqual(item.status, "CLEARED")

    async def test_required_unconditional_disclaimer_is_not_waived(self):
        item = finding(rule="Rule 9", status="VIOLATION", kind="MISSING_DISCLAIMER")
        with patch.object(ce, "evaluate", AsyncMock()) as evaluate:
            await er.reconcile(request(), [rule_for(item, "RULE")], [item], [source()], {})
            evaluate.assert_not_awaited()
        self.assertEqual(item.status, "VIOLATION")

    async def test_rule_specific_context_review_gets_sources_for_new_visual_finding(self):
        item = finding(rule="Rule 11", status="NEEDS_REVIEW", visual_evidence=True)
        async def evaluate(candidates, *args, **kwargs):
            self.assertEqual(len(candidates[0].review_metadata["verified_sources"]), 1)
            result = SimpleNamespace(status="NO_VIOLATION", reasoning="The source resolves this rule's specific concern.",
                confidence=.95, matched_text=CLAIM, subject="Company", recommended_action="")
            ce._apply(candidates[0], result, "test-model", False)
        with patch.object(ce, "evaluate", evaluate):
            await er.reconcile(request(), [rule_for(item)], [item], [source()], {})
        self.assertEqual(item.status, "CLEARED")

    async def test_uncertain_critical_visual_review_is_not_promoted_to_a_breach(self):
        item = finding(rule="Rule 11", status="NEEDS_REVIEW", visual_evidence=True)
        rule = rule_for(item); rule.severity = "CRITICAL"; rule.traffic_light = "RED"
        async def evaluate(candidates, *args, **kwargs):
            result = SimpleNamespace(status="NO_VIOLATION", reasoning="Uncertain", confidence=.7,
                matched_text=CLAIM, subject="Company", recommended_action="")
            ce._apply(candidates[0], result, "test-model", False)
        with patch.object(ce, "evaluate", evaluate):
            await er.reconcile(request(), [rule], [item], [source()], {})
        self.assertEqual(item.status, "NEEDS_REVIEW")
    async def test_newsroom_clears_a_rule6_announcement_supported_in_separate_parts(self):
        combined = "ZenaTech has secured three new transportation survey contracts, bringing its DOT work to eight projects across three states with approximately $503,000 in total contract revenue."
        parts = ["3 NEW TRANSPORTATION SURVEY CONTRACTS IN VIRGINIA & SOUTH CAROLINA",
                 "8 PROJECTS ACROSS 3 STATES REPRESENTING OVER US$500,000"]
        item = finding(rule="Rule 6", status="VIOLATION", text=combined, category="Financial Disclosure")
        check = source(parts[0]); check.where = "newsroom"
        check.claims.append(SourceClaim(claim=parts[1], verdict="SUPPORTED", confidence=.95,
            evidence="The opening reports eight projects across three states and approximately $503,000 in revenue."))
        self.assertEqual(er.clear_supported([item], [check]), 0)  # Do not equate fragments mechanically.
        async def evaluate(candidates, *args, **kwargs):
            entries = candidates[0].review_metadata["verified_sources"]
            self.assertEqual({row["claim"] for row in entries}, set(parts))
            self.assertEqual({row["origin"] for row in entries}, {"newsroom"})
            result = SimpleNamespace(status="NO_VIOLATION", reasoning="The company newsroom already publicly announced this event and supports these figures.",
                confidence=.95, matched_text=combined, subject="ZenaTech", recommended_action="")
            ce._apply(candidates[0], result, "test-model", False)
        req = request(combined + "\nText in image 1:\n" + "\n".join(parts))
        with patch.object(ce, "evaluate", evaluate):
            await er.reconcile(req, [rule_for(item)], [item], [check], {})
        self.assertEqual((item.status, item.penalty), ("CLEARED", 0))
        self.assertIn("newsroom", item.evaluation.reasoning)

    async def test_context_keeps_an_independent_rule_concern_despite_newsroom_support(self):
        item = finding(rule="Rule 11", status="VIOLATION")
        check = source(); check.where = "newsroom"
        async def evaluate(candidates, *args, **kwargs):
            self.assertEqual(candidates[0].review_metadata["verified_sources"][0]["origin"], "newsroom")
            result = SimpleNamespace(status="VIOLATION", reasoning="The factual source does not authorize the prohibited use.",
                confidence=.95, matched_text=CLAIM, subject="Company", recommended_action="Review usage")
            ce._apply(candidates[0], result, "test-model", False)
        with patch.object(ce, "evaluate", evaluate):
            await er.reconcile(request(), [rule_for(item)], [item], [check], {})
        self.assertEqual(item.status, "VIOLATION")
        self.assertGreater(item.penalty, 0)
    async def test_automatic_newsroom_source_clears_rule6_without_an_attached_link(self):
        from services.compliance.evaluation import newsroom_claim_sources as news, newsroom_facts
        from services.compliance.evaluation import source_claim_routing as routing, source_passages
        from services.compliance.evaluation.source_fetch_service import FetchedSource
        url = "https://www.zenatech.com/transportation-survey-contracts/"
        row = {"title": "ZenaTech revenue announcement", "body": CLAIM, "as_of": "2026-11-10", "url": url}
        inventory = [{"claim": CLAIM, "where": "caption"}]
        fetched = FetchedSource(url=url, final_url=url, ok=True, status="OK", text=CLAIM, title=row["title"])
        async def compare(check, material, assigned):
            self.assertEqual(material[1], CLAIM)
            verified = [SourceClaim(**claim, verdict="SUPPORTED", evidence=CLAIM, confidence=.95) for claim in assigned]
            check.claims.extend(verified)
            return verified
        with patch.object(news, "fetch_all", AsyncMock(return_value=[row])), \
             patch.object(source_passages, "EMBED_ENABLED", False), \
             patch.object(news.newsroom_article_sources.source_fetch_service, "fetch", AsyncMock(return_value=fetched)) as fetch:
            readings = await news.readings(inventory)
            self.assertEqual(await routing.verify(inventory, readings, compare), set())
        checks = newsroom_facts.visible_checks([check for check, material in readings])
        self.assertEqual(checks[0].where, "newsroom")
        item = finding(rule="Rule 6", status="VIOLATION")
        req = request(); self.assertEqual(req.sources, [])
        with patch.object(ce, "evaluate", AsyncMock()) as evaluate:
            await er.reconcile(req, [rule_for(item)], [item], checks, {})
            evaluate.assert_not_awaited()
        self.assertEqual((item.status, item.penalty), ("CLEARED", 0))
        fetch.assert_awaited_once_with(url)
        explicit = checks[0].model_copy(update={"where": "source_links"})
        other = finding(rule="Rule 6", status="VIOLATION")
        er.clear_supported([other], [explicit])
        self.assertEqual((other.status, other.penalty), (item.status, item.penalty))
    async def test_unchanged_review_is_not_sent_to_ai_twice(self):
        item = finding(rule="Rule 4", status="NEEDS_REVIEW")
        reviewed = {str(item.rule_id): ec.signature(item)}
        with patch.object(ce, "evaluate", AsyncMock()) as evaluate:
            await er.reconcile(request(), [rule_for(item)], [item], [source()], reviewed)
            evaluate.assert_not_awaited()
        self.assertEqual(item.status, "NEEDS_REVIEW")

    async def test_flow_reviews_new_visual_flags_after_initial_context_without_rereading_sources(self):
        from services.compliance.evaluation import synthetic_media_disclosure, source_check_service
        initial = finding(rule="Rule 4", status="NEEDS_REVIEW")
        late = finding(rule="Rule 11", status="NEEDS_REVIEW", visual_evidence=True)
        rules = [rule_for(initial), rule_for(late)]
        calls = []
        async def evaluate(candidates, *args, **kwargs):
            calls.append([item.finding.rule_number for item in candidates])
            self.assertTrue(all(item.review_metadata["verified_sources"] for item in candidates))
        async def visual(req, selected, findings, *, source_checks):
            self.assertEqual(source_checks[0].claims[0].claim, CLAIM)
            findings.append(late)
            return [{"status": "COMPLETED"}]
        with patch.object(ce, "evaluate", evaluate), patch.object(vr, "review", visual), \
             patch.object(synthetic_media_disclosure, "ensure_review"), \
             patch.object(source_check_service, "run", AsyncMock()) as fetch:
            result = await er.run(request(), rules, [initial], [(initial, rules[0])], [source()])
        self.assertEqual(calls, [["Rule 4"], ["Rule 11"]])
        self.assertEqual(result, [{"status": "COMPLETED"}])
        fetch.assert_not_awaited()

    async def test_visual_payload_and_cache_identity_include_verified_source_passages(self):
        item = finding(rule="Rule 11"); rule = rule_for(item)
        req = request(); req._visual_media = [{"id": str(uuid4()), "mime_type": "image/png", "storage_path": "x", "original_name": "x.png"}]
        @asynccontextmanager
        async def asset_part(asset):
            yield "image"
        payload = {"assessments": [{"rule_number": "Rule 11", "status": "CLEARED", "confidence": .95,
            "evidence": "", "reasoning": "No concern", "box": [], "start_sec": 0, "end_sec": 0, "evidence_basis": "NO_CONCERN"}]}
        generate = AsyncMock(return_value=SimpleNamespace(text=json.dumps(payload), model_version="test-model"))
        with patch.dict("os.environ", {"VISUAL_COMPLIANCE_MODELS": "test-model"}), \
             patch.object(vr, "_asset_part", asset_part), patch.object(vr._VISUAL_CACHE, "get", AsyncMock(return_value=None)) as cached, \
             patch.object(vr._VISUAL_CACHE, "set", AsyncMock()), patch.object(vr.image_text_service, "_generate", generate), \
             patch.object(vr.image_detail_review, "apply", side_effect=lambda assessments, *args: assessments):
            first = await vr.review(req, [rule], [], source_checks=[source()])
            changed = source(); changed.claims[0].evidence = "Different source evidence."
            second = await vr.review(req, [rule], [], source_checks=[changed])
        self.assertEqual([row["status"] for row in first + second], ["COMPLETED", "COMPLETED"] )
        self.assertIn('"verified_sources"', generate.call_args_list[0].args[0][1])
        self.assertIn(CLAIM, generate.call_args_list[0].args[0][1])
        self.assertNotEqual(cached.call_args_list[0].args[0], cached.call_args_list[1].args[0])
