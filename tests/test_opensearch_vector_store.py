"""Tests for the AWS OpenSearch vector store client and service."""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from services.ai.core import opensearch_client, vector_store_service as store


class OpenSearchVectorStoreTests(unittest.TestCase):
    def test_host_and_service_detection(self):
        with patch.dict(os.environ, {"OPENSEARCH_HOST": "nk9lmc71nqjfzluzr9gb.ca-central-1.aoss.amazonaws.com", "OPENSEARCH_INDEX": "compliance-references"}):
            self.assertTrue(store.is_enabled())
            self.assertEqual(opensearch_client.opensearch_host(), "https://nk9lmc71nqjfzluzr9gb.ca-central-1.aoss.amazonaws.com")
            self.assertEqual(store.index_name(), "compliance-references")
            region, svc = opensearch_client._region_and_service(opensearch_client.opensearch_host())
            self.assertEqual(region, "ca-central-1")
            self.assertEqual(svc, "aoss")

    def test_disabled_when_no_host(self):
        with patch.dict(os.environ, {"OPENSEARCH_HOST": ""}, clear=False):
            self.assertFalse(store.is_enabled())
            self.assertEqual(asyncio.run(store.search("hello")), [])
            self.assertEqual(asyncio.run(store.upsert([store.VectorDoc("FACT", "1", "abc")])), {"embedded": 0, "unchanged": 0})

    def test_upsert_and_search_against_opensearch(self):
        class FakeResp:
            def __init__(self, status_code=200, data=None):
                self.status_code = status_code
                self._data = data or {}
                self.text = ""
                self.content = b"{}"

            def json(self):
                return self._data

        with patch.dict(os.environ, {"OPENSEARCH_HOST": "example.ca-central-1.aoss.amazonaws.com", "OPENSEARCH_INDEX": "compliance-references"}):
            store._index_ready = "compliance-references"
            req_json = AsyncMock(side_effect=[
                {"hits": {"hits": []}},  # _find_existing_by_doc_ids
                {"hits": {"hits": [{"_id": "os1", "_score": 0.91, "_source": {"doc_id": "FACT:1", "kind": "FACT", "title": "T", "text": "Body", "source_id": "1", "meta": {}, "brands": ["*"]}}]}},  # search
            ])
            bulk = AsyncMock(return_value={"errors": False, "items": []})
            embed = AsyncMock(return_value=[[0.1, 0.2]])
            with patch.object(opensearch_client, "request_json", req_json), patch.object(opensearch_client, "bulk", bulk), patch.object(store, "embed", embed), patch.object(store, "_reference_changed", AsyncMock()):
                res = asyncio.run(store.upsert([store.VectorDoc("FACT", "1", "Body", title="T")]))
                self.assertEqual(res, {"embedded": 1, "unchanged": 0})
                self.assertEqual(bulk.call_args[0][0][0], {"index": {"_index": "compliance-references"}})
                hits = asyncio.run(store.search("Body", kinds=["FACT"], min_score=0.5))
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]["_id"], "FACT:1")
                self.assertAlmostEqual(hits[0]["score"], 0.91)
