"""Versioned Rule 18 regression cases: no model, network, private posts, or database required."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from models.compliance.compliance_model import SourceClaim, SourceCheck, ProvidedSource
from services.compliance.evaluation import claim_manifest as inventory, claim_verification as verify
from services.compliance.evaluation import source_check_service as sc, source_post_checks as post


def factual(unit, **changes):
    claim = dict(wording=unit["wording"], subject="The depicted tether", assertion="Carries power and data",
                 scope="conceptual technology", qualification="Not assessed hardware", attribution="inferred",
                 evidence_needed="Technical explanation of tether power and data", ambiguous=False)
    claim.update(changes)
    return dict(index=unit["index"], role="factual", claims=[claim])


def manifest(text, roles=None):
    units = inventory.passages(text)
    answers = [factual(u) if not roles or roles[i] == "factual" else
               dict(index=i, role=roles[i], claims=[]) for i, u in enumerate(units)]
    return inventory.assemble(text, units, answers)


class ClaimInventoryTests(unittest.TestCase):
    def test_versioned_mixed_roles_keep_soft_labels_without_slogans(self):
        text = ("NASDAQ: ZENA\nPOWER THE MISSION\nRETHINK THE CONNECTION\n"
                "Text in image 1: Persistent surveillance\nPOWER + DATA\n"
                "Conceptual illustration. Not assessed hardware.\n"
                "I believe the mission should determine how a drone is powered.\n"
                "Source: U.S. Army / army.mil/article/295038")
        saved = manifest(text, ["identifier", "promotional", "promotional", "factual", "factual",
                                "disclosure", "opinion", "citation"])
        self.assertTrue(saved.complete)
        self.assertEqual([c.wording for c in saved.claims], ["Persistent surveillance", "POWER + DATA"])
        self.assertEqual(saved.claims[0].locations[0].image, 1)
        for claim in saved.claims:
            loc = claim.locations[0]
            self.assertEqual(text[loc.start:loc.end], claim.wording)

    def test_abbreviations_negation_and_decimals_are_not_split(self):
        text = "No U.S. Army endorsement is claimed. The tether supplies 1.5 kW, only in laboratory tests."
        units = inventory.passages(text)
        self.assertEqual(len(units), 1)
        answer = factual(units[0], wording="Army endorsement is claimed.")
        claim = inventory.assemble(text, units, [answer]).claims[0]
        self.assertEqual(claim.wording, text)
        self.assertTrue(claim.ambiguous)

    def test_wrapped_original_locations_and_negative_context_are_preserved(self):
        text = "Text in image 2: No U.S. Army evaluation or endorsement\nof ZenaTech is claimed."
        units = inventory.passages(text)
        self.assertEqual(units[0]["wording"], "No U.S. Army evaluation or endorsement")
        answer = factual(units[0], wording=text[units[0]["start"]:], ambiguous=True)
        saved = inventory.assemble(text, units, [answer, dict(index=1, role="disclosure", claims=[])])
        self.assertEqual(saved.claims[0].wording, text[units[0]["start"]:])
        self.assertTrue(saved.claims[0].ambiguous)

    def test_opinion_with_a_fact_is_not_exempt(self):
        text = "I believe our drone flew for 48 hours."
        self.assertEqual(len(manifest(text).claims), 1)

    def test_duplicate_occurrences_have_one_identity_and_all_locations(self):
        saved = manifest("Tether carries power and data.\nText in image 1: Tether carries power and data.")
        self.assertEqual(len(saved.claims), 1)
        self.assertEqual([loc.where for loc in saved.claims[0].locations], ["caption", "image"])

    def test_qualification_updates_existing_claim_not_a_new_disclosure_claim(self):
        before = manifest("Tether carries power and data.")
        after = manifest("Tether carries power and data.\nNot assessed hardware.", ["factual", "disclosure"])
        self.assertEqual(before.claims[0].id, after.claims[0].id)
        self.assertEqual(len(after.claims), 1)

    def test_malformed_or_missing_discovery_is_visible_without_deduction(self):
        text = "Could reduce inspection effort."
        saved = inventory.assemble(text, inventory.passages(text), [])
        self.assertFalse(saved.complete)
        self.assertTrue(saved.claims[0].ambiguous)
        finding = post.unsourced_image_findings(text, [], saved)[0]
        self.assertEqual((finding.status, finding.penalty), ("NEEDS_REVIEW", 0))

    def test_invented_quote_cannot_become_a_verified_claim(self):
        units = inventory.passages("May reduce inspection effort.")
        saved = inventory.assemble(units[0]["wording"], units, [factual(units[0], wording="Certified safe")])
        self.assertFalse(saved.complete)
        self.assertTrue(saved.claims[0].ambiguous)
        self.assertEqual(saved.claims[0].wording, units[0]["wording"])

    def test_soft_claim_without_source_is_not_lost(self):
        text = "May reduce inspection effort."
        saved = manifest(text)
        findings = post.unsourced_image_findings(text, [], saved)
        self.assertEqual(findings[0].status, "EVIDENCE_REQUIRED")
        self.assertEqual(findings[0].flagged_terms, [text])

    def test_real_date_statement_can_be_extracted_from_a_citation(self):
        text = "Source: U.S. Army / army.mil/article/295038 Reported assessment: July 27-August 7, 2026."
        units = inventory.passages(text)
        claim = "Reported assessment: July 27-August 7, 2026."
        saved = inventory.assemble(text, units, [factual(units[0], wording=claim)])
        self.assertEqual(saved.claims[0].wording, claim)
        self.assertNotIn("295038", saved.claims[0].wording)

    def test_factual_opinion_and_soft_benefit_cannot_disappear_as_promotional_copy(self):
        for text, role in [("I believe our drone flew for 48 hours.", "opinion"),
                           ("Could reduce inspection effort.", "promotional"),
                           ("Helps teams locate defects.", "promotional")]:
            with self.subTest(text=text):
                saved = manifest(text, [role])
                self.assertEqual(len(saved.claims), 1)
                self.assertTrue(saved.claims[0].ambiguous)

    def test_wrapped_claim_does_not_add_a_duplicate_fragment(self):
        text = "The tether carries power\nand data under test conditions."
        units = inventory.passages(text)
        saved = inventory.assemble(text, units, [factual(units[0], wording=text)])
        self.assertTrue(saved.complete)
        self.assertEqual(len(saved.claims), 1)

    def test_identical_labels_with_different_subjects_remain_distinct(self):
        text = "Persistent surveillance\nText in image 2: Persistent surveillance"
        units = inventory.passages(text)
        saved = inventory.assemble(text, units, [factual(units[0], subject="Tethered drones generally"),
                                               factual(units[1], subject="A specific prototype")])
        self.assertEqual(len(saved.claims), 2)


class ClaimPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, CLAIM_MANIFEST_DIR=self.directory.name)
        self.environment.start()

    async def asyncTearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    async def test_repeat_parallel_and_source_refresh_keep_same_saved_inventory(self):
        text = "May reduce inspection effort."
        answers = [factual(inventory.passages(text)[0])]
        with patch.object(inventory.claim_discovery, "classify", AsyncMock(return_value=answers)) as discover:
            first, second = await asyncio.gather(inventory.get(text), inventory.get(text))
            third = await inventory.get(text)
        discover.assert_awaited_once()
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(len(list(__import__("pathlib").Path(self.directory.name).glob("*.json"))), 1)

    async def test_failed_discovery_is_not_frozen(self):
        text = "May reduce inspection effort."
        answer = [factual(inventory.passages(text)[0])]
        with patch.object(inventory.claim_discovery, "classify", AsyncMock(side_effect=[None, answer])) as discover:
            self.assertFalse((await inventory.get(text)).complete)
            self.assertTrue((await inventory.get(text)).complete)
        self.assertEqual(discover.await_count, 2)

    async def test_no_top_n_claim_limit(self):
        text = "\n".join(f"Capability {i} may reduce inspection effort." for i in range(36))
        async def classify(_text, units):
            return [factual(u) for u in units]
        with patch.object(inventory.claim_discovery, "classify", classify):
            saved = await inventory.get(text)
        self.assertEqual(len(saved.claims), 36)
        self.assertTrue(saved.complete)


class ClaimVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_verifier_cannot_change_subject_or_original_wording(self):
        saved = manifest("POWER + DATA")
        claim = saved.claims[0]
        reader = AsyncMock(return_value=([sc._Result(claim_id=claim.id,
            claim="ZenaTech has proven deployed drones", verdict="NOT_FOUND", confidence=.9)], "model"))
        results, _, failed = await verify.compare(saved, ("report", None, None, None, "source"), reader)
        self.assertEqual(results[0].claim, "POWER + DATA")
        self.assertEqual(results[0].claim_id, claim.id)
        self.assertTrue(failed)
        self.assertEqual(results[0].verdict, "UNCLEAR")
        supplied = reader.await_args.args[0]
        self.assertIn("Not assessed hardware", supplied)
        self.assertIn("The depicted tether", supplied)

    async def test_omission_and_ambiguous_extraction_are_unclear_not_not_found(self):
        saved = manifest("Potential capability")
        reader = AsyncMock(return_value=([], "model"))
        results, _, failed = await verify.compare(saved, ("report", None, None, None, "source"), reader)
        self.assertTrue(failed)
        self.assertEqual(results[0].verdict, "UNCLEAR")
        saved.claims[0].ambiguous = True
        reader.return_value = ([sc._Result(claim_id=saved.claims[0].id, verdict="NOT_FOUND", confidence=1)], "model")
        results, _, failed = await verify.compare(saved, ("report", None, None, None, "source"), reader)
        self.assertEqual((results[0].verdict, results[0].confidence), ("UNCLEAR", 0))

    async def test_extra_claims_are_rejected_and_all_batches_verified(self):
        saved = manifest("\n".join(f"Capability {i}" for i in range(19)))
        async def reader(payload, *_args):
            items = json.loads(payload[payload.index("{"): ])["claims"]
            results = [sc._Result(claim_id=c["id"], verdict="NOT_FOUND", confidence=.9) for c in items]
            return [*results, sc._Result(claim_id="invented", claim="Invented fact", verdict="CONTRADICTED")], "model"
        results, _, failed = await verify.compare(saved, ("report", None, None, None, "source"), reader)
        self.assertEqual(len(results), 19)
        self.assertFalse(failed)
        self.assertEqual([r.claim for r in results], [c.wording for c in saved.claims])

    def test_same_assertion_supported_elsewhere_is_not_charged_for_another_source_omission(self):
        supported = SourceClaim(claim="A fact", claim_id="stable", verdict="SUPPORTED", confidence=.9)
        missing = supported.model_copy(update={"verdict": "NOT_FOUND"})
        checks = [SourceCheck(reference="a", kind="DOCUMENT", status="READ", claims=[supported]),
                  SourceCheck(reference="b", kind="DOCUMENT", status="READ", claims=[missing])]
        self.assertEqual(sc.findings(checks), [])
        checks[0].claims = [missing]
        self.assertEqual(len(sc.findings(checks)), 1)

    async def test_full_source_run_uses_one_inventory_for_every_source(self):
        text = "Tether carries power and data."
        saved = manifest(text)
        async def compare(payload, *_args):
            return [sc._Result(claim_id=saved.claims[0].id, verdict="NOT_FOUND", confidence=.9)], "model"
        with patch.object(sc.claim_manifest, "get", AsyncMock(return_value=saved)) as get, \
             patch.object(sc.newsroom_facts, "material", AsyncMock(return_value=None)), \
             patch.object(sc, "_compare_inventory", compare), \
             patch.object(sc.source_passages, "EMBED_ENABLED", False):
            result = await sc.run(text, [ProvidedSource(name="a", text="Source A"), ProvidedSource(name="b", text="Source B")])
        get.assert_awaited_once()
        self.assertFalse(result.ai_failed)
        self.assertEqual([c.claims[0].claim_id for c in result.checks], [saved.claims[0].id] * 2)
        self.assertEqual(result.manifest, saved)


class ClaimScoringTests(unittest.IsolatedAsyncioTestCase):
    async def score(self, text, saved, checks=()):
        from services.compliance.evaluation import compliance_service as cs
        from models.compliance.compliance_model import ComplianceCheckRequest
        result = sc.SourceCheckResult(manifest=saved, checks=list(checks))
        with patch.object(cs, "applicable_rules", AsyncMock(return_value=[])), \
             patch.object(cs, "rulebook_version", AsyncMock(return_value="test")), \
             patch.object(cs, "_semantic_findings", AsyncMock(return_value=[])), \
             patch.object(sc, "run", AsyncMock(return_value=result)):
            return await cs.check_compliance(ComplianceCheckRequest(text=text))

    async def test_soft_unsourced_facts_are_charged_once_and_inventory_is_saved(self):
        text = "Could reduce inspection effort.\nText in image 1: Could reduce inspection effort."
        saved = manifest(text)
        result = await self.score(text, saved)
        self.assertEqual(result.score, 95)
        self.assertEqual(result.evidence_count, 1)
        self.assertEqual(result.model_dump()["claim_manifest"]["revision"], saved.revision)

    async def test_disclosures_identifiers_and_promotional_copy_cost_nothing(self):
        text = "NASDAQ: ZENA\nPOWER THE MISSION\nConceptual illustration."
        result = await self.score(text, manifest(text, ["identifier", "promotional", "disclosure"]))
        self.assertEqual((result.score, result.review_count), (100, 0))

    async def test_unreadable_evidence_is_incomplete_not_an_unsupported_fact(self):
        text = "May reduce inspection effort."
        saved = manifest(text)
        checks = [SourceCheck(reference="report", kind="DOCUMENT", status="UNREADABLE")]
        result = await self.score(text, saved, checks)
        self.assertEqual(result.score, 100)
        self.assertGreaterEqual(result.review_count, 1)
        self.assertFalse(result.is_compliant)

    async def test_ambiguous_claim_stays_visible_after_source_comparison(self):
        text = "Persistent surveillance"
        saved = manifest(text)
        saved.claims[0].ambiguous = True
        results, _, _ = await verify.compare(saved, ("report", None, None, None, "source"),
            AsyncMock(return_value=([sc._Result(claim_id=saved.claims[0].id, verdict="NOT_FOUND", confidence=1)], "model")))
        checked = sc._to_claims(results, "source")
        result = await self.score(text, saved, [SourceCheck(reference="report", kind="DOCUMENT", status="READ", claims=checked)])
        self.assertEqual((result.score, result.review_count), (100, 1))
        self.assertIsNone(result.violations[0].evaluation)

    async def test_supported_soft_claim_costs_nothing(self):
        text = "May reduce inspection effort."
        saved = manifest(text)
        supported = SourceClaim(claim=text, claim_id=saved.claims[0].id, verdict="SUPPORTED", confidence=.9)
        result = await self.score(text, saved, [SourceCheck(reference="report", kind="DOCUMENT", status="READ", claims=[supported])])
        self.assertEqual((result.score, result.evidence_count), (100, 0))

    async def test_disabling_source_ai_preserves_legacy_rule18_safety_checks(self):
        with patch.object(sc, "ENABLED", False):
            result = await self.score("Revenue was $5 million.", None)
        self.assertEqual(result.score, 95)
        self.assertEqual(result.evidence_count, 1)


class PayloadLimits(unittest.TestCase):
    def test_context_and_many_claims_are_batched_within_existing_limits(self):
        from services.compliance.evaluation import claim_payload as bounded
        items = [{"wording": "A soft capability " * 60} for _ in range(30)]
        groups = list(bounded.batches(items))
        self.assertEqual(sum(map(len, groups)), 30)
        for group in groups:
            payload = bounded.payload("Full context " * 4000, "claims", group)
            self.assertLessEqual(len(payload), bounded.MAX_CHARS)
            self.assertEqual(len(json.loads(payload)["claims"]), len(group))

    def test_oversized_single_passage_is_incomplete_instead_of_silently_cut(self):
        from services.compliance.evaluation import claim_payload as bounded
        self.assertIsNone(bounded.payload("", "passages", [{"wording": "x" * (bounded.MAX_CHARS + 1)}]))
