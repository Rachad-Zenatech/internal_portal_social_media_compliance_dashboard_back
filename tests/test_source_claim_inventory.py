"""Stable claim identities and grounded extraction, independent of source material."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from services.compliance.evaluation import source_claim_inventory as inventory
from services.compliance.evaluation import source_check_service as sc


class ClaimInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_empty_inventory_is_reused_without_extraction(self):
        extract = AsyncMock()
        with patch.object(inventory.db, "fetch_one", AsyncMock(return_value={"claims": "[]"})):
            self.assertEqual(await inventory.get("Heading", extract), [])
        extract.assert_not_awaited()

    async def test_first_committed_inventory_wins_concurrent_extraction(self):
        saved = [{"claim": "Range is 30 km", "where": "caption"}]
        extract = AsyncMock(return_value=[{"claim": "Range is 20 km", "where": "caption"}])
        with patch.object(inventory.db, "fetch_one", AsyncMock(return_value=None)), \
             patch.object(inventory.db, "execute", AsyncMock(return_value={"claims": json.dumps(saved)})):
            self.assertEqual(await inventory.get("Range is 20 km or 30 km", extract), saved)

    async def test_failed_extraction_is_not_saved_as_empty(self):
        write = AsyncMock()
        with patch.object(inventory.db, "fetch_one", AsyncMock(return_value=None)), \
             patch.object(inventory.db, "execute", write):
            self.assertIsNone(await inventory.get("Post", AsyncMock(return_value=None)))
        write.assert_not_awaited()

    async def test_invented_claim_is_rejected(self):
        ask = AsyncMock(return_value=sc._Response(claims=[
            sc._Result(claim="Range is 100 km", verdict="UNCLEAR")]))
        self.assertIsNone(await inventory.extract("Range is 30 km", ask, ["model"], 12))

    async def test_branding_is_excluded_even_when_the_model_selects_it(self):
        ask = AsyncMock(return_value=sc._Response(claims=[
            sc._Result(claim="NASDAQ: ZENA", verdict="UNCLEAR")]))
        self.assertEqual(await inventory.extract("NASDAQ: ZENA", ask, ["model"], 12), [])

    async def test_meaningful_punctuation_changes_use_distinct_inventories(self):
        read = AsyncMock(return_value={"claims": []})
        with patch.object(inventory.db, "fetch_one", read):
            for text in ("Range is 3.0 km", "Range is 30 km", "Range is 3.0 km"):
                await inventory.get(text, AsyncMock())
        keys = [call.args[1] for call in read.await_args_list]
        self.assertNotEqual(keys[0], keys[1])
        self.assertEqual(keys[0], keys[2])

    async def test_exact_assertion_is_saved_once_with_image_location(self):
        claim = sc._Result(claim="Range is 30 km", verdict="UNCLEAR")
        ask = AsyncMock(return_value=sc._Response(claims=[claim, claim]))
        result = await inventory.extract("Text in image 1: Range is 30 km", ask, ["model"], 12)
        self.assertEqual(result, [{"claim": "Range is 30 km", "where": "image"}])
        self.assertNotIn("<source>", ask.call_args.args[1])

    async def test_source_refresh_reuses_inventory_and_ignores_extra_claims(self):
        from tests.test_claim_inventory import manifest
        saved = manifest("Range is 30 km")
        ask = AsyncMock(return_value=sc._Response(claims=[
            sc._Result(claim="A different claim", verdict="SUPPORTED", confidence=1)]))
        with patch.object(sc.claim_manifest, "get", AsyncMock(return_value=saved)) as get, \
             patch.object(sc.claim_manifest.claim_discovery, "classify", AsyncMock()) as extract, \
             patch.object(sc, "_ask", ask), \
             patch.object(sc.source_passages, "excerpt", AsyncMock(return_value="Source evidence")):
            token = sc.source_recheck.refresh.set(True)
            try:
                first, _ = await sc._compare("Range is 30 km", "one", None, None, None, "Source one")
                second, _ = await sc._compare("Range is 30 km", "two", None, None, None, "Source two")
            finally:
                sc.source_recheck.refresh.reset(token)
        extract.assert_not_awaited()
        self.assertEqual(get.await_count, 2)
        # The merged adapter reports an incomplete comparison when the model omits the saved claim.
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertGreaterEqual(ask.await_count, 2)
        for call in ask.await_args_list:
            self.assertIn("Range is 30 km", call.args[1])
            self.assertNotIn("A different claim", call.args[1])

    def test_omissions_and_duplicates_are_unresolved_and_order_is_stable(self):
        saved = [{"claim": claim, "where": "caption"} for claim in ("First", "Second")]
        answer = sc._Result(claim="Second", verdict="SUPPORTED", confidence=1)
        checked = inventory.reconcile(saved, [answer, answer], sc._Result)
        self.assertEqual([r.claim for r in checked], ["First", "Second"])
        self.assertTrue(all(r.verdict == "UNCLEAR" for r in checked))
