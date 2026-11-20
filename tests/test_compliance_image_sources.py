"""Compliance uses saved source links without rediscovering image source credits."""
import unittest
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import SourceCheck
from services.compliance.evaluation import source_check_service as sources


class ComplianceImageSourceTests(unittest.IsolatedAsyncioTestCase):
    async def check_sources(self, text, include_image_citations=False):
        url = "https://www.army.mil/article/295038"
        source = SourceCheck(reference=url, url=url, kind="URL", where="source_links", status="READ")
        read = AsyncMock(return_value=(source, ("Army article", "The article's contents.")))
        compare = AsyncMock(return_value=([], "test-model", False))
        with (
            patch.object(sources, "ENABLED", True),
            patch.object(sources.newsroom_facts, "material", AsyncMock(return_value=None)),
            patch.object(sources, "_read_link", read),
            patch.object(sources.source_post_checks, "compare_parts", compare),
        ):
            result = await sources.run(text, include_image_citations=include_image_citations)
        return result, read, compare

    async def test_image_credit_does_not_create_another_uncheckable_source(self):
        text = ("Post caption.\nText in image 1: Continuous power through a tether.\n"
                "Source: U.S. Army / September 2, 2026 / Two weeks describes the assessment.\n\n"
                "Sources:\nhttps://www.army.mil/article/295038")
        result, read, compare = await self.check_sources(text)
        self.assertEqual([(check.kind, check.status) for check in result.checks], [("URL", "READ")])
        read.assert_awaited_once()
        self.assertEqual(read.await_args.args[0].where, "source_links")
        self.assertEqual(compare.await_args.args[0], text)
        self.assertEqual(sources.findings(result.checks), [])

    async def test_image_only_credit_is_not_resolved_during_compliance(self):
        result, read, compare = await self.check_sources("Text in image 1: A claim.\nSource: U.S. Army report")
        self.assertEqual(result.checks, [])
        read.assert_not_awaited()
        compare.assert_not_awaited()

    async def test_caption_source_requirements_are_preserved(self):
        result, _, _ = await self.check_sources("A claim.\nSource: U.S. Army report")
        self.assertEqual([(check.kind, check.status) for check in result.checks], [("NAMED", "NOT_PROVIDED")])
