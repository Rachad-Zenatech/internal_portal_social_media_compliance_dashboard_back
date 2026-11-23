"""The supplied ZenaTech quick guide is scoped and passed to sentiment without invented rules."""
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance.context_model import ContextEntryIn
from postgresql_db.schemas import setup_context_schema
from postgresql_db.schemas.zenatech_brand_guideline import ZENATECH_GUIDELINE_ENTRIES as GUIDE
from services.compliance.evaluation import context_service
from services.compliance.sentiment.input_builder import _reference_context


class GuidelineTests(unittest.TestCase):
    def test_all_five_references_are_seeded_for_zenatech_only(self):
        self.assertEqual(len(GUIDE), 5)
        for entry in GUIDE:
            self.assertIn(entry, setup_context_schema.SEED_ENTRIES)
            self.assertEqual(entry["kind"], "BRAND_VOICE")
            self.assertEqual(entry["brands"], ["ZENATECH"])
            self.assertIn("ZenaTech_Guideline.pdf", entry["source"])
            ContextEntryIn(**{key: value for key, value in entry.items() if key != "seed_key"})
        keys = [entry["seed_key"] for entry in setup_context_schema.SEED_ENTRIES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_current_palette_and_font_applications_preserve_the_source(self):
        colours = next(entry["body"] for entry in GUIDE if entry["seed_key"].endswith(":colours"))
        for value in ("#005981", "#1E71B5", "#103B5D", "#919EA7", "#D80004", "#3456F5",
                      "#E7EEF3", "#D0DDE7", "#A2BBCF", "#8BAAC4", "#7399B8"):
            self.assertIn(value, colours)
        self.assertIn("source inconsistency", colours)
        fonts = next(entry["body"] for entry in GUIDE if entry["seed_key"].endswith(":typography"))
        for value in ("Poppins Extra Light", "Poppins Light", "Poppins Semibold", "Poppins Regular", "Century Gothic"):
            self.assertIn(value, fonts)

    def test_missing_tone_and_logo_requirements_are_not_invented(self):
        positioning = GUIDE[0]["body"]
        self.assertIn("does not state a writing-tone standard", positioning)
        logo = next(entry["body"] for entry in GUIDE if entry["seed_key"].endswith(":logo"))
        self.assertIn("all-white", logo)
        self.assertIn("does not require a logo in every post", logo)

    def test_thought_leadership_manual_is_preserved(self):
        old = [entry for entry in setup_context_schema.SEED_ENTRIES
               if entry["seed_key"].startswith("brand:CEO_THOUGHT_LEADERSHIP:")]
        self.assertEqual(len(old), 4)
        self.assertIn("Ottoman Red #ED2224", old[0]["body"])
        self.assertTrue(all(entry["brands"] == ["CEO_THOUGHT_LEADERSHIP"] for entry in old))


class GuidelineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_selection_passes_guideline_to_sentiment_with_source(self):
        now = datetime.now(timezone.utc)
        rows = [{**entry, "id": uuid4(), "audiences": [], "platforms": [], "as_of": None, "updated_at": now}
                for entry in GUIDE]
        with patch.object(context_service, "fetch_all", AsyncMock(return_value=rows)):
            selected = await context_service.for_content(["ZENATECH"], [], ["LINKEDIN"])
            unrelated = await context_service.for_content(["ZENADRONE"], [], ["LINKEDIN"])
        self.assertEqual(len(selected), 5)
        self.assertEqual(unrelated, [])
        prompt = _reference_context(selected)
        self.assertIn("Brand voice:", prompt)
        self.assertIn("ZenaTech_Guideline.pdf, page 3", prompt)
        self.assertIn("#3456F5", prompt)
        self.assertIn("Century Gothic", prompt)

    async def test_scoped_seeding_preserves_existing_edits_and_other_brand_entries(self):
        conn = AsyncMock()
        await setup_context_schema._seed_starter_entries(conn, GUIDE)
        sql, payload = conn.execute.call_args.args
        self.assertIn("ON CONFLICT (seed_key) DO NOTHING", sql)
        inserted = json.loads(payload)
        self.assertEqual(len(inserted), 5)
        self.assertTrue(all(entry["brands"] == ["ZENATECH"] for entry in inserted))
