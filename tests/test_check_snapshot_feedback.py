"""Only reviewer decisions against the exact saved finding become learning labels."""

import unittest
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from fastapi import HTTPException

from services.compliance.submissions import check_snapshot_feedback as feedback
from services.compliance.submissions import check_snapshot_service as snapshots
from tests.test_check_snapshot_capture import example


class SnapshotFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        row, request, captured, check = example()
        self.post, evaluated = snapshots.inputs(row, request)
        self.check_id, _, self.check, _, _ = snapshots.prepare(self.post, evaluated, captured, check)
        self.finding = self.check["violations"][0]
        self.post_id = UUID(self.post["id"])
        self.rule_id = UUID(self.finding["rule_id"])
        self.finding_id = UUID(self.finding["finding_snapshot_id"])

    def test_selection_rejects_old_or_ambiguous_findings(self):
        with self.assertRaises(HTTPException) as raised:
            feedback.select_finding(self.check, self.rule_id, uuid4())
        self.assertEqual(raised.exception.status_code, 409)
        self.check["violations"].append({**self.finding, "finding_snapshot_id": str(uuid4())})
        with self.assertRaises(HTTPException):
            feedback.select_finding(self.check, self.rule_id)
        self.assertEqual(feedback.select_finding(self.check, self.rule_id, self.finding_id), self.finding)

    async def test_legacy_feedback_cannot_become_an_invented_training_example(self):
        conn = AsyncMock()
        self.assertIsNone(await feedback.validate_link(conn, self.post_id, 1, self.rule_id, {"rule_id": self.rule_id}))
        await feedback.record(conn, None, 1, "CORRECT", None, None, uuid4())
        conn.fetchrow.assert_not_awaited()
        conn.execute.assert_not_awaited()

    async def test_a_racing_recheck_or_version_change_rejects_the_label(self):
        for current_version, snapshot_id in ((1, str(uuid4())), (2, str(self.check_id))):
            conn = AsyncMock()
            conn.fetchrow.return_value = {"current_version": current_version,
                                          "latest_check": {"check_snapshot_id": snapshot_id}}
            with self.assertRaises(HTTPException) as raised:
                await feedback.validate_link(conn, self.post_id, 1, self.rule_id, self.finding)
            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("FOR UPDATE", conn.fetchrow.call_args.args[0])

    async def test_only_identical_finding_and_post_can_be_linked(self):
        saved = {"finding": self.finding, "rule_id": self.rule_id,
                 "submission_id": self.post_id, "check_id": self.check_id}
        for changes in ({}, {"submission_id": uuid4()}, {"rule_id": uuid4()},
                        {"finding": {**self.finding, "penalty": 0}}):
            conn = AsyncMock()
            conn.fetchrow.side_effect = [
                {"current_version": 1, "latest_check": self.check}, {**saved, **changes},
            ]
            if changes:
                with self.assertRaises(HTTPException):
                    await feedback.validate_link(conn, self.post_id, 1, self.rule_id, self.finding)
            else:
                self.assertEqual(await feedback.validate_link(conn, self.post_id, 1, self.rule_id, self.finding),
                                 self.finding_id)

    async def test_evidence_resolution_is_a_correct_flag_and_undo_is_an_event(self):
        conn = AsyncMock()
        await feedback.record(conn, self.finding_id, 1, "CORRECT", "EVIDENCE_ON_FILE", "Document 1", uuid4())
        args = conn.execute.call_args.args
        self.assertEqual(args[3:6], ("CORRECT", "EVIDENCE_SUPPLIED", "EVIDENCE_ON_FILE"))
        conn.fetchrow.return_value = {"finding_snapshot_id": self.finding_id, "id": uuid4(), "rule_number": "R1"}
        await feedback.remove_verdict(conn, self.post_id, 1, self.rule_id, "CORRECT", uuid4())
        self.assertEqual(conn.execute.call_args.args[3:5], ("RETRACTED", "UNDO_CONFIRM"))
