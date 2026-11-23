"""Script details apply the post policy for documents, batches and per-script overrides."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance.script_batch_model import BatchDetails, BatchUpdate, BatchItemUpdate
from models.compliance.script_doc_model import DocUpdate
from services.compliance.evaluation import rule_set_policy as policy, script_doc_service as docs


class ScriptRuleSetPolicyTests(unittest.TestCase):
    def test_document_batch_and_per_script_updates_resolve_the_same_sets_as_posts(self):
        for model in (DocUpdate, BatchUpdate, BatchItemUpdate):
            with self.subTest(model=model.__name__):
                request = model(details={"brand_codes": ["ZENATECH"], "audiences": ["GENERAL", "INVESTORS"]})
                self.assertEqual(request.details.rule_sets, [policy.PUBLIC, policy.GENERAL])

    def test_changing_brand_replaces_managed_sets_and_retains_optional_sets(self):
        details = BatchDetails(brand_codes=["ZENADRONE"], audiences=["GENERAL"],
                               rule_sets=[policy.PUBLIC, policy.GENERAL, "Paid Social Ads"])
        self.assertEqual(details.rule_sets, [policy.PUBLIC, policy.DEFENSE, "Paid Social Ads"])

    def test_defense_audience_adds_defense_rules_for_a_general_brand(self):
        details = BatchDetails(brand_codes=["SOFTWARE"], audiences=["DEFENSE"])
        self.assertEqual(details.rule_sets, [policy.PUBLIC, policy.GENERAL, policy.DEFENSE])

    def test_required_sets_cannot_be_removed_from_a_script_request(self):
        details = BatchDetails(brand_codes=["CEO_THOUGHT_LEADERSHIP"], audiences=["GENERAL"], rule_sets=[])
        self.assertEqual(details.rule_sets, [policy.PUBLIC, policy.CEO])


class ScriptDetailsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_document_update_saves_resolved_sets_to_document_and_shared_pages(self):
        doc_id, user_id, set_id = uuid4(), uuid4(), uuid4()
        old = {"details": {"source_submission_id": "linked-post"}, "set_id": set_id}
        request = DocUpdate(details={"brand_codes": ["ZENATECH"], "audiences": ["GENERAL", "INVESTORS"]})
        with (
            patch.object(docs, "get_access", AsyncMock(return_value=(old, [], SimpleNamespace(can_submit=True)))),
            patch.object(docs, "execute", AsyncMock()) as save,
            patch.object(docs, "fetch_all", AsyncMock(return_value=[])) as pages,
            patch.object(docs, "get", AsyncMock(return_value="updated")),
        ):
            self.assertEqual(await docs.update(doc_id, user_id, request), "updated")
        saved = json.loads(save.await_args_list[0].args[2])
        self.assertEqual(saved["rule_sets"], [policy.PUBLIC, policy.GENERAL])
        self.assertEqual(saved["source_submission_id"], "linked-post")
        self.assertEqual(json.loads(pages.await_args.args[2]), saved)
