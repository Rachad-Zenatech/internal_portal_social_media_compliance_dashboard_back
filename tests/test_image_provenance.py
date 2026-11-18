"""The AI-image question requires an explicit saved Yes or No for post images."""
import unittest
from datetime import datetime, timezone
from services.compliance.submissions import image_provenance_service as provenance, post_steps_service
from tests.test_post_steps import post


class RequiredAnswer(unittest.TestCase):
    def test_default_false_is_not_an_answer(self):
        row = dict(has_images=True, ai_generated_images=False, ai_generated_images_confirmed=False)
        self.assertTrue(provenance.missing(row))
        self.assertIn("ai_generated_images", post_steps_service.details_missing(post(**row)))

    def test_yes_and_no_are_both_valid_explicit_answers(self):
        for answer in (False, True):
            row = dict(has_images=True, ai_generated_images=answer, ai_generated_images_confirmed=True)
            self.assertFalse(provenance.missing(row))
            self.assertNotIn("ai_generated_images", post_steps_service.details_missing(post(**row)))

    def test_posts_without_images_do_not_require_the_question(self):
        self.assertFalse(provenance.missing(dict(has_images=False)))

    def test_legacy_explicit_answers_are_preserved(self):
        self.assertTrue(provenance.is_confirmed(dict(ai_generated_images=True)))
        self.assertTrue(provenance.is_confirmed(dict(ai_generated_images=False, image_provenance_edited_at=datetime.now(timezone.utc))))

    def test_first_no_is_saved_without_invalidating_unchanged_media(self):
        fields = provenance.update_fields(dict(ai_generated_images=False), dict(ai_generated_images=False))
        self.assertEqual(fields, {"ai_generated_images_confirmed": True})

    def test_unanswered_autosaves_do_not_select_no(self):
        self.assertEqual(provenance.update_fields({}, {}), {})
        self.assertEqual(provenance.update_fields({}, dict(ai_generated_images=None)), {})

    def test_a_changed_answer_updates_the_existing_check_freshness_marker(self):
        fields = provenance.update_fields(dict(ai_generated_images=False), dict(ai_generated_images=True))
        self.assertTrue(fields["ai_generated_images"])
        self.assertTrue(fields["ai_generated_images_confirmed"])
        self.assertIn("image_provenance_edited_at", fields)

    def test_repeated_answer_does_not_create_an_edit(self):
        self.assertEqual(provenance.update_fields(dict(ai_generated_images=False, ai_generated_images_confirmed=True), dict(ai_generated_images=False)), {})


class SubmissionGate(unittest.IsolatedAsyncioTestCase):
    async def test_the_submission_gate_requires_an_answer_for_images(self):
        from unittest.mock import AsyncMock, patch
        from uuid import uuid4
        from services.compliance.submissions import submission_service as subs
        row = dict(id=uuid4(), title="Image post", brand_code="ZENATECH", audience="GENERAL", region="Global",
                   platforms=["LINKEDIN"], rule_sets=["ZenaTech General"], caption_text="A caption", content_type="IMAGE",
                   status="DRAFT", approval_needed_by=datetime.now(timezone.utc), reviewer_ids=[uuid4()],
                   script_required=False, has_images=True, ai_generated_images=False, ai_generated_images_confirmed=False)
        with patch.object(subs, "fetch_one", AsyncMock(return_value={"ok": 1})), \
             patch.object(subs, "_account_ids", return_value=[uuid4()]):
            self.assertIn("ai_generated_images", await subs._missing_for_submit(row))
            row["ai_generated_images_confirmed"] = True
            self.assertNotIn("ai_generated_images", await subs._missing_for_submit(row))
