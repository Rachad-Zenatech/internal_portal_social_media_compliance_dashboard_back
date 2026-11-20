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

    def test_people_and_employee_answers_are_saved(self):
        fields = provenance.update_fields({}, dict(people_in_images=True, employee_in_images=False))
        self.assertTrue(fields["people_in_images"])
        self.assertTrue(fields["people_in_images_confirmed"])
        self.assertFalse(fields["employee_in_images"])
        self.assertTrue(fields["employee_in_images_confirmed"])

    def test_face_boxes_validation(self):
        from services.compliance.media.image_text_service import _face_boxes
        self.assertEqual(_face_boxes(None), [])
        self.assertEqual(_face_boxes("invalid"), [])
        self.assertEqual(_face_boxes([{"box": [10, 20, 100, 200]}]), [[10, 20, 100, 200]])
        # clamped to 0..1000
        self.assertEqual(_face_boxes([{"box": [-50, -20, 1500, 2000]}]), [[0, 0, 1000, 1000]])
        # inverted or empty boxes dropped
        self.assertEqual(_face_boxes([{"box": [200, 200, 100, 100]}]), [])


class SubmissionGate(unittest.IsolatedAsyncioTestCase):
    async def test_retry_image_read_schedules_ocr_and_face_detection(self):
        from unittest.mock import AsyncMock, patch
        from uuid import uuid4
        sub_id = uuid4()
        file_id = uuid4()
        user_id = uuid4()
        fake_file_row = dict(
            id=file_id, kind="MAIN", original_name="test.jpg", mime_type="image/jpeg",
            size_bytes=1000, created_at=datetime.now(timezone.utc), image_text=None,
            image_text_status=None, image_text_error=None, person_detected=None,
            face_boxes=[], face_detection_version=0
        )
        with patch("services.compliance.submissions.submission_service._get_owned_row", AsyncMock(return_value={"id": sub_id})), \
             patch("services.compliance.submissions.image_provenance_service.fetch_one", AsyncMock(return_value=fake_file_row)), \
             patch("services.compliance.media.image_text_service.schedule") as mock_schedule:
            res = await provenance.retry_image_read(sub_id, file_id, user_id)
            mock_schedule.assert_called_once_with([file_id])
            self.assertEqual(res.id, file_id)

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

    async def test_row_to_submission_preserves_employee_and_people_fields(self):
        from unittest.mock import AsyncMock, patch
        from uuid import uuid4
        from services.compliance.submissions import submission_service as subs
        row = dict(
            id=uuid4(), reference_code="REF-1", title="Image post", brand_code="ZENATECH",
            audience="GENERAL", region="Global", platforms=["LINKEDIN"], rule_sets=["ZenaTech General"],
            caption_text="A caption", caption_html="<p>A caption</p>", content_type="IMAGE",
            status="DRAFT", approval_needed_by=datetime.now(timezone.utc), reviewer_ids=[uuid4()],
            script_required=False, has_images=True, ai_generated_images=False, ai_generated_images_confirmed=True,
            people_in_images=True, people_in_images_confirmed=True, people_in_images_detected=True,
            employee_in_images=True, employee_in_images_confirmed=True,
            current_version=1, latest_check="{}", publish_at=None, account_id=None, reviewer_route=[],
            submitted_at=None, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )
        with patch.object(subs, "fetch_all", AsyncMock(return_value=[])):
            sub = await subs._to_submission(row)
            self.assertTrue(sub.people_in_images)
            self.assertTrue(sub.people_in_images_confirmed)
            self.assertTrue(sub.people_in_images_detected)
            self.assertTrue(sub.employee_in_images)
            self.assertTrue(sub.employee_in_images_confirmed)
