"""Script-only drafts can run analyses before a video, caption or script approval exists."""
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from fastapi import HTTPException
from services.compliance.submissions import submission_service as subs
from services.compliance.sentiment.input_builder import _transcript_pending


class ScriptAnalysisReadiness(unittest.IsolatedAsyncioTestCase):
    async def check(self, required=True, script=True, extra=None, approved=False):
        row = dict(id=uuid4(), content_type="VIDEO", status="DRAFT", script_required=required,
                   script_status="APPROVED" if approved else "DRAFT")
        missing = ["main_file", "caption"] + ([] if approved else ["script_approval"]) + (extra or [])
        with patch.object(subs, "_get_owned_row", AsyncMock(return_value=row)), \
             patch.object(subs, "_missing_for_submit", AsyncMock(return_value=missing)), \
             patch.object(subs, "fetch_one", AsyncMock(return_value={"ready": 1} if script else None)):
            await subs.require_details_complete_for(row["id"], uuid4(), "running the sentiment analysis")

    async def test_uploaded_script_runs_without_video_caption_or_approval(self):
        await self.check()

    async def test_attached_script_is_recognized_without_legacy_stage_flag(self):
        await self.check(required=False)

    async def test_unread_or_missing_script_is_refused(self):
        with self.assertRaises(HTTPException) as error:
            await self.check(script=False)
        self.assertIn("script", error.exception.detail)
        self.assertNotIn("image or video", error.exception.detail)

    async def test_script_draft_still_needs_brand_and_scope(self):
        with self.assertRaises(HTTPException):
            await self.check(extra=["brand_code"])

    async def test_approved_script_returns_to_video_requirements(self):
        with self.assertRaises(HTTPException) as error:
            await self.check(approved=True)
        self.assertIn("image or video", error.exception.detail)

    def test_no_video_does_not_wait_for_transcription(self):
        self.assertFalse(_transcript_pending(dict(video_id=None, transcript_status=None)))


class SentimentRequestReadiness(unittest.IsolatedAsyncioTestCase):
    async def test_script_only_request_is_not_available_until_video_upload(self):
        from services.compliance.submissions import manual_check_service as manual
        row = dict(id=uuid4(), content_type="VIDEO", script_required=False, script_status="DRAFT")
        with patch.object(subs, "_get_owned_row", AsyncMock(return_value=row)), \
             patch.object(subs, "fetch_one", AsyncMock(side_effect=[None, {"readable": 1}])), \
             patch.object(subs, "require_details_complete", AsyncMock()) as full_post:
            with self.assertRaises(HTTPException) as error:
                await manual.require_sentiment_ready(row["id"], uuid4())
        self.assertIn("actual video", error.exception.detail)
        full_post.assert_not_awaited()

    async def test_script_request_requires_readable_text(self):
        from services.compliance.submissions import manual_check_service as manual
        row = dict(id=uuid4(), content_type="VIDEO")
        with patch.object(subs, "_get_owned_row", AsyncMock(return_value=row)), \
             patch.object(subs, "fetch_one", AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as error:
                await manual.require_sentiment_ready(row["id"], uuid4())
        self.assertIn("actual video", error.exception.detail)
        self.assertNotIn("caption", error.exception.detail)

    async def test_actual_video_keeps_finished_post_validation(self):
        from services.compliance.submissions import manual_check_service as manual
        row = dict(id=uuid4(), content_type="VIDEO")
        with patch.object(subs, "_get_owned_row", AsyncMock(return_value=row)), \
             patch.object(subs, "fetch_one", AsyncMock(return_value={"video": 1})), \
             patch.object(subs, "require_details_complete", AsyncMock()) as full_post:
            await manual.require_sentiment_ready(row["id"], uuid4())
        full_post.assert_awaited_once()

    async def test_request_endpoint_uses_script_aware_readiness(self):
        from types import SimpleNamespace
        from tools.compliance.submissions import submission_router as router
        from services.compliance.submissions import manual_check_service as manual
        sub_id, user_id, analysis_id = uuid4(), uuid4(), uuid4()
        with patch.object(subs, "get_submission", AsyncMock(return_value=SimpleNamespace(title="Script", caption_text=None, files=[]))), \
             patch.object(manual, "require_sentiment_ready", AsyncMock()) as ready, \
             patch.object(subs, "require_details_complete_for", AsyncMock()) as full_post, \
             patch.object(router.sentiment_service, "request_analysis", AsyncMock(return_value=analysis_id)) as start, \
             patch.object(router.sentiment_service, "get_analysis", AsyncMock(return_value="queued")):
            result = await router.request_sentiment(sub_id, user_id)
        self.assertEqual(result, "queued")
        ready.assert_awaited_once_with(sub_id, user_id)
        full_post.assert_not_awaited()
        start.assert_awaited_once_with(sub_id, user_id)


class ScriptComplianceRequest(unittest.IsolatedAsyncioTestCase):
    async def test_manual_check_reaches_runner_for_script_only_draft(self):
        from types import SimpleNamespace
        for script_status in ("DRAFT", "APPROVED"):
            row = dict(id=uuid4(), title="Attached script", caption_text=None, content_type="VIDEO",
                       status="DRAFT", script_status=script_status, script_required=True)
            runner_reached = RuntimeError("compliance runner reached")
            with self.subTest(script_status=script_status), \
                 patch.object(subs, "_get_owned_row", AsyncMock(return_value=row)), \
                 patch.object(subs, "fetch_one", AsyncMock(side_effect=[None, {"readable": 1}])), \
                 patch.object(subs, "_missing_for_submit", AsyncMock(return_value=["main_file", "caption", "script_approval"])), \
                 patch.object(subs, "require_details_complete", AsyncMock()) as video_validation, \
                 patch.object(subs, "_require_not_duplicate", AsyncMock(return_value=SimpleNamespace(repost=None))), \
                 patch.object(subs, "_run_check", AsyncMock(side_effect=runner_reached)) as run:
                with self.assertRaisesRegex(RuntimeError, "compliance runner reached"):
                    await subs.run_initial_check(row["id"], uuid4())
                run.assert_awaited_once_with(row)
                video_validation.assert_not_awaited()

    async def test_missing_script_cannot_run_compliance(self):
        from services.compliance.submissions import manual_check_service as manual
        with patch.object(subs, "fetch_one", AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as error:
                await manual.require_compliance_ready(dict(id=uuid4(), content_type="VIDEO"))
        self.assertIn("Attach a script", error.exception.detail)

    async def test_actual_video_uses_finished_post_requirements(self):
        from services.compliance.submissions import manual_check_service as manual
        row = dict(id=uuid4(), content_type="VIDEO")
        with patch.object(subs, "fetch_one", AsyncMock(return_value={"video": 1})), \
             patch.object(subs, "require_details_complete", AsyncMock()) as ready:
            await manual.require_compliance_ready(row)
        ready.assert_awaited_once_with(row, "running the compliance check")


class ScriptPrecheckEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_script_endpoint_checks_and_saves_without_finished_post_requirements(self):
        import json
        from types import SimpleNamespace
        from services.compliance.evaluation import script_review_service as service
        from tools.compliance.submissions import script_review_router as router
        for status in ("DRAFT", "CHANGES_REQUESTED"):
            row = dict(id=uuid4(), title="Attached script", content_type="VIDEO", status="DRAFT",
                       script_required=True, script_status=status, caption_text=None)
            body = "The uploaded script, before any video exists."
            check = {"score": 95, "rulebook_version": "current", "violations": []}
            stage = SimpleNamespace(precheck=check)
            with self.subTest(status=status), \
                 patch.object(subs, "_get_owned_row", AsyncMock(return_value=row)), \
                 patch.object(subs, "require_details_complete", AsyncMock()) as post_gate, \
                 patch.object(subs, "_missing_for_submit", AsyncMock()) as post_fields, \
                 patch.object(service, "_read_uploaded", AsyncMock(return_value=({}, body))), \
                 patch.object(service, "_require_not_duplicate", AsyncMock(return_value=SimpleNamespace(repost=None))), \
                 patch.object(service, "check_text", AsyncMock(return_value=check)) as run, \
                 patch.object(service, "execute", AsyncMock()) as save, \
                 patch.object(service, "get_stage", AsyncMock(return_value=stage)):
                result = await router.precheck_script(row["id"], uuid4())
                self.assertIs(result, stage)
                run.assert_awaited_once_with(row, body)
                post_gate.assert_not_awaited()
                post_fields.assert_not_awaited()
                saved = json.loads(save.await_args.args[2])
                self.assertEqual(saved["check"], check)
                self.assertEqual(service.still_valid(saved, row, body, "current"), check)
