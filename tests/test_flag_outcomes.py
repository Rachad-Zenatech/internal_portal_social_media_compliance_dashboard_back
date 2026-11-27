"""Reviewer decisions remain visible without authorizing an edited post."""
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from services.compliance.submissions import flag_outcome_service as service


class FlagOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_keeps_direct_accept_and_decline_decisions(self):
        submission = types.ModuleType("services.compliance.submissions.submission_service")
        submission._get_owned_row = AsyncMock(return_value={"current_version": 3})
        rows = [dict(rule_id=uuid4(), rule_number="Rule 18", version_number=1,
                     outcome=outcome, reason="EVIDENCE_ON_FILE", note="See the press release",
                     reviewer_name="Reviewer One", decided_at=datetime.now(timezone.utc))
                for outcome in ("DISMISSED", "APPROVED", "REJECTED")]
        with patch.dict(sys.modules, {submission.__name__: submission}), patch.object(
            service, "fetch_all", AsyncMock(return_value=rows)
        ) as fetch:
            results = await service.list_for(uuid4(), uuid4())
        self.assertEqual([r.outcome for r in results], ["DISMISSED", "APPROVED", "REJECTED"])
        self.assertEqual(results[0].reviewer_name, "Reviewer One")
        self.assertEqual(results[1].reason, "The evidence for this claim is on file")
        self.assertEqual(results[2].note, "See the press release")
        self.assertTrue(all(r.version_number == 1 for r in results))
        submission._get_owned_row.assert_awaited_once()
        self.assertNotIn("caption_edited_at", fetch.call_args.args[0])

    async def test_cannot_read_another_users_post(self):
        submission = types.ModuleType("services.compliance.submissions.submission_service")
        submission._get_owned_row = AsyncMock(side_effect=PermissionError("Not allowed"))
        with patch.dict(sys.modules, {submission.__name__: submission}), patch.object(
            service, "fetch_all", AsyncMock()
        ) as fetch:
            with self.assertRaises(PermissionError):
                await service.list_for(uuid4(), uuid4())
            fetch.assert_not_awaited()
