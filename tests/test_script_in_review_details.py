"""Opening a sent script preserves its check, shows assignment, and completes preparation."""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from models.compliance.script_review_model import ScriptVersion
from services.compliance.evaluation import script_review_service as service
from services.compliance.evaluation import script_review_details as details
from services.compliance.submissions import post_steps_service


class SentScriptDetails(unittest.IsolatedAsyncioTestCase):
    async def test_opening_sent_script_never_reruns_or_replaces_compliance(self):
        viewer, submission_id = uuid4(), uuid4()
        saved = {"score": 82, "rulebook_version": "old-rulebook", "violations": []}
        version = ScriptVersion(version_number=1, body="# Submitted script", source="UPLOAD",
                                check=saved, created_at=datetime.now(timezone.utc))
        row = dict(id=submission_id, created_by=viewer, content_type="VIDEO", script_required=True,
                   script_status="IN_REVIEW", status="DRAFT", reference_code="SUB-1", title="Script")
        with patch.object(service.subs, "_get_owned_row", AsyncMock(return_value=row)), \
             patch("services.compliance.evaluation.script_submission_documents.linked", AsyncMock(return_value=None)), \
             patch.object(service.subs, "_is_suggester", AsyncMock(return_value=False)), \
             patch.object(service, "_send_state", return_value={"status": None, "error": None}), \
             patch.object(service, "_script_file", AsyncMock(return_value=None)), \
             patch.object(service, "_versions", AsyncMock(return_value=[version])), \
             patch.object(service, "rulebook_version", AsyncMock(return_value="new-rulebook")), \
             patch.object(service, "batch_ref", AsyncMock(return_value=None)), \
             patch.object(service, "_reviewer_may_act", AsyncMock(return_value=False)), \
             patch.object(service, "assigned_reviewer_names", AsyncMock(return_value=["Assigned Reviewer"])), \
             patch.object(service, "check_text", AsyncMock()) as run, \
             patch.object(service, "execute", AsyncMock()) as write:
            stage = await service.get_stage(submission_id, viewer)
        self.assertEqual(stage.versions[0].body, "# Submitted script")
        self.assertEqual(stage.versions[0].check, saved)
        self.assertEqual(stage.reviewer_names, ["Assigned Reviewer"])
        run.assert_not_awaited()
        write.assert_not_awaited()

    async def test_assignment_uses_the_posts_named_reviewers(self):
        ids = [uuid4(), uuid4()]
        with patch.object(details, "fetch_all", AsyncMock(return_value=[{"name": "First"}, {"name": "Second"}])) as query:
            names = await details.assigned_reviewer_names({"reviewer_ids": ids})
        self.assertEqual(names, ["First", "Second"])
        self.assertEqual(query.await_args.args[1], ids)

    async def test_unassigned_script_does_not_invent_a_reviewer(self):
        with patch.object(service.subs, "_review_recipients", AsyncMock(return_value=[])), \
             patch.object(details, "fetch_all", AsyncMock()) as query:
            self.assertEqual(await details.assigned_reviewer_names({"reviewer_ids": []}), [])
        query.assert_not_awaited()

    async def test_brand_review_team_names_match_the_notification_recipients(self):
        creator, reviewer = uuid4(), uuid4()
        row = {"reviewer_ids": [], "created_by": creator, "brand_codes": ["ZENATECH"]}
        with patch.object(service.subs, "_review_recipients", AsyncMock(return_value=[reviewer])) as recipients, \
             patch.object(details, "fetch_all", AsyncMock(return_value=[{"name": "Brand Reviewer"}])) as query:
            self.assertEqual(await details.assigned_reviewer_names(row), ["Brand Reviewer"])
        recipients.assert_awaited_once_with(row, exclude=creator)
        self.assertEqual(query.await_args.args[1], [reviewer])

    async def test_named_reviewers_are_not_replaced_by_the_whole_brand_team(self):
        picked = uuid4()
        with patch.object(service.subs, "_review_recipients", AsyncMock()) as recipients, \
             patch.object(details, "fetch_all", AsyncMock(return_value=[{"name": "Picked Reviewer"}])):
            self.assertEqual(await details.assigned_reviewer_names({"reviewer_ids": [picked, picked]}), ["Picked Reviewer"])
        recipients.assert_not_awaited()

    def test_sent_script_marks_all_three_preparation_sections_complete(self):
        steps = post_steps_service.compute_steps(dict(status="DRAFT", script_stage=True,
                                                       script_status="IN_REVIEW", main_is_video=False))
        self.assertEqual((steps.details, steps.compliance, steps.send), ("done", "done", "done"))
        self.assertTrue(steps.locked)
        self.assertIsNone(steps.next)
        self.assertEqual(steps.missing, [])
