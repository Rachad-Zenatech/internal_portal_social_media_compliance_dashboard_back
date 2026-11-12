"""The Messaging Context the creator picks for a post's analysis: kinds they chose use exactly their entries; the
rest are picked automatically."""

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from services.compliance.evaluation import context_service as ctx
from services.compliance.submissions import submission_service as subs

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


def entry(kind, title, brands=(), audiences=(), platforms=()):
    return {"id": uuid4(), "kind": kind, "title": title, "body": f"{title} body", "do_examples": None,
            "dont_examples": None, "brands": list(brands), "audiences": list(audiences), "platforms": list(platforms),
            "source": None, "as_of": None, "updated_at": NOW}


DAAS_VOICE = entry("BRAND_VOICE", "DaaS voice", brands=["DAAS"])
ZENA_VOICE = entry("BRAND_VOICE", "ZenaTech voice", brands=["ZENATECH"])
FACT_A = entry("FACT", "Fact A")
FACT_B = entry("FACT", "Fact B")
IG_NORM = entry("PLATFORM_NORM", "Instagram norm", platforms=["INSTAGRAM"])
X_NORM = entry("PLATFORM_NORM", "X norm", platforms=["X"])
ROWS = [DAAS_VOICE, ZENA_VOICE, FACT_A, FACT_B, IG_NORM, X_NORM]


class ForContentTests(unittest.IsolatedAsyncioTestCase):
    async def titles(self, picks=None):
        with patch.object(ctx, "fetch_all", new=AsyncMock(return_value=ROWS)):
            got = await ctx.for_content(["DAAS"], ["GENERAL"], ["INSTAGRAM"], picks=picks)
        return [e["title"] for e in got]

    async def test_automatic_by_the_posts_brand_and_platforms(self):
        self.assertEqual(await self.titles(), ["DaaS voice", "Fact A", "Fact B", "Instagram norm"])

    async def test_a_chosen_kind_uses_exactly_the_creators_entries(self):
        got = await self.titles({"BRAND_VOICE": [str(ZENA_VOICE["id"])], "FACT": [str(FACT_B["id"])]})
        self.assertEqual(got, ["ZenaTech voice", "Fact B", "Instagram norm"])

    async def test_an_empty_choice_leaves_that_kind_out(self):
        self.assertNotIn("Instagram norm", await self.titles({"PLATFORM_NORM": []}))

    async def test_entries_outside_the_posts_scope_can_be_chosen(self):
        self.assertIn("X norm", await self.titles({"PLATFORM_NORM": [str(X_NORM["id"])]}))


class CleanPicksTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_active_entries_of_the_right_kind(self):
        rows = [{"id": DAAS_VOICE["id"], "kind": "BRAND_VOICE"}, {"id": FACT_A["id"], "kind": "FACT"}]
        with patch.object(subs, "fetch_all", new=AsyncMock(return_value=rows)):
            clean = await subs._clean_context_picks({
                "BRAND_VOICE": [DAAS_VOICE["id"], FACT_A["id"], uuid4()],  # a fact and a deleted entry under brand voice
                "FACT": [FACT_A["id"], FACT_A["id"]],
            })
        self.assertEqual(clean, {"BRAND_VOICE": [str(DAAS_VOICE["id"])], "FACT": [str(FACT_A["id"])]})

    async def test_unknown_kinds_are_refused(self):
        with self.assertRaises(HTTPException):
            await subs._clean_context_picks({"RUMOUR": []})


if __name__ == "__main__":
    unittest.main()
