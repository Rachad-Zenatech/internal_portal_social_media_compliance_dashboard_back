import re
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance.compliance_model import ComplianceCheckResponse
from services.compliance.evaluation import script_match, script_batch_service
from services.compliance.submissions import submission_service as subs

SCRIPT = "Our drones help teams inspect construction sites and monitor the progress of buildings."


def result():
    return ComplianceCheckResponse(is_compliant=True)


class ComparisonStateTests(unittest.TestCase):
    def test_failed_transcription_leaves_a_review_flag_instead_of_silently_skipping(self):
        match = script_match.assess_video(SCRIPT, "", {"transcript_status": "FAILED"})
        self.assertEqual(match["status"], "UNAVAILABLE")
        self.assertIsNone(match["similarity"])
        check = result()
        script_match.flag_mismatch(check, match)
        self.assertEqual(check.violations[0].status, "NEEDS_REVIEW")
        self.assertEqual(check.violations[0].rule_id, script_match.SCRIPT_COMPARISON_ID)
        self.assertIn("Retry", check.violations[0].remediation_guidance)
        self.assertEqual((check.violations[0].penalty, check.score), (0, 100))
        self.assertEqual((check.review_count, check.is_compliant, check.traffic_light), (1, False, "YELLOW"))

    def test_no_transcript_text_is_not_a_successful_match(self):
        match = script_match.assess_video(SCRIPT, "", {"transcript_status": "COMPLETED"})
        self.assertEqual(match["status"], "UNAVAILABLE")
        self.assertIn("no readable", match["message"])

    def test_processing_is_shown_as_pending_without_a_premature_mismatch(self):
        for state in (None, "PENDING", "PROCESSING"):
            match = script_match.assess_video(SCRIPT, "", {"transcript_status": state})
            self.assertEqual(match["status"], "PENDING")
            check = result()
            script_match.flag_mismatch(check, match)
            self.assertEqual(check.violations, [])

    def test_different_completed_video_raises_actual_mismatch(self):
        match = script_match.assess_video(SCRIPT, "This phone camera takes portraits while the refrigerator keeps our food cool.",
                                         {"transcript_status": "COMPLETED"})
        check = result()
        script_match.flag_mismatch(check, match)
        self.assertEqual(match["status"], "MISMATCH")
        self.assertEqual(check.violations[0].rule_id, script_match.SCRIPT_MISMATCH_ID)
        self.assertNotEqual(script_match.SCRIPT_COMPARISON_ID, script_match.SCRIPT_MISMATCH_ID)

    def test_penalties_follow_the_displayed_similarity_thresholds(self):
        for similarity, expected in ((49, 10), (50, 5), (79, 5), (80, 0)):
            with self.subTest(similarity=similarity):
                check = result()
                match = {"status": "MISMATCH" if similarity < 70 else "PARTIAL",
                         "similarity": similarity, "added": []}
                script_match.flag_mismatch(check, match)
                self.assertEqual((check.score, check.raw_score), (100 - expected, 100 - expected))
                self.assertEqual([finding.penalty for finding in check.violations], [expected] if expected else [])

    def test_production_table_matches_voiceover_and_overlays(self):
        script = (
            "Storyline: A presenter visits a construction expo.\n"
            "| **Time** | **Visual / footage** | **Voiceover** | **On-screen text** |\n"
            "| 0:00 | Presenter at the entrance. | We're here at Buildex Alberta! | WELCOME |\n"
            "| 0:03 | Presenter points to the venue. | Come say hi. | BUILDINGS |"
        )
        match = script_match.compare(script, "We're here at Buildex Alberta. Come say hi.", "WELCOME BUILDINGS")
        self.assertEqual((match["similarity"], match["added"], match["missing"]), (100, [], []))

    def test_penalty_cannot_take_score_below_zero(self):
        check = ComplianceCheckResponse(is_compliant=False, score=3, raw_score=3)
        script_match.flag_mismatch(check, {"status": "MISMATCH", "similarity": 7, "added": []})
        self.assertEqual((check.score, check.raw_score), (0, 0))

    def test_matching_video_and_non_script_post_do_not_raise_flags(self):
        self.assertEqual(script_match.assess_video(SCRIPT, SCRIPT, {"transcript_status": "COMPLETED"})["status"], "MATCH")
        self.assertIsNone(script_match.assess_video(None, "Some narration", {"transcript_status": "COMPLETED"}))
        self.assertIsNone(script_match.assess_video(SCRIPT, "", None))


class RetainedBatchScriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_finished_batch_uses_the_retained_approved_version(self):
        self.assertEqual(script_batch_service.batch_status("IN_REVIEW", ["APPROVED"], True), "DONE")
        row = {"id": uuid4(), "script_status": "APPROVED"}
        with patch.object(subs, "fetch_one", AsyncMock(return_value={"body": SCRIPT})) as fetch:
            self.assertEqual(await subs.approved_script(row), SCRIPT)
            self.assertIn("content_script_versions", fetch.await_args.args[0])
            self.assertEqual(fetch.await_args.args[1], row["id"])

    async def test_disconnected_server_is_retryable_for_approved_script_video_drafts(self):
        self.assertIsNotNone(re.search(subs._TRANSIENT_READ_ERROR, "Server disconnected without sending a response.", re.I))
        with patch.object(subs, "fetch_all", AsyncMock(return_value=[])) as fetch:
            self.assertEqual(await subs.retry_failed_reads(), 0)
            for call in fetch.await_args_list:
                self.assertIn("'APPROVED'", call.args[0])
