"""A pending creator request can route a post to review, never approve it."""
import copy
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from services.compliance.submissions import creator_dismissal_service as service
from services.compliance.submissions.manual_check_service import require_send_score


class PendingDismissalRoutingTests(unittest.TestCase):
    def setUp(self):
        self.check = {"score": 75, "violations": [
            {"rule_id": "certification", "rule_number": "Rule 26", "penalty": 20, "flagged_terms": ["certified"]},
            {"rule_id": "image", "rule_number": "Rule 9", "penalty": 5, "flagged_terms": ["AI image"]},
        ]}
        self.request = {"rule_id": "certification", "status": "PENDING", "flagged_terms": ["certified"]}

    def test_requested_deduction_can_unlock_review_without_mutating_score(self):
        original = copy.deepcopy(self.check)
        potential = service.pending_send_score(self.check, [self.request])
        self.assertEqual(potential, 95)
        require_send_score(75, pending_score=potential)
        self.assertEqual(self.check, original)

    def test_unrequested_deductions_still_block(self):
        request = {"rule_id": "image", "status": "PENDING", "flagged_terms": ["AI image"]}
        potential = service.pending_send_score(self.check, [request])
        self.assertEqual(potential, 80)
        with self.assertRaises(HTTPException):
            require_send_score(75, pending_score=potential)

    def test_no_request_cannot_unlock(self):
        self.assertEqual(service.pending_send_score(self.check, []), 75)
        with self.assertRaises(HTTPException):
            require_send_score(75)

    def test_rejected_or_changed_finding_cannot_unlock(self):
        for change in ({"status": "REJECTED"}, {"flagged_terms": ["different claim"]}):
            with self.subTest(change=change):
                self.assertEqual(service.pending_send_score(self.check, [{**self.request, **change}]), 75)

    def test_incomplete_and_legacy_checks_cannot_gain_points(self):
        self.assertIsNone(service.pending_send_score({**self.check, "analysis_pending": True}, [self.request]))
        self.assertIsNone(service.pending_send_score({**self.check, "ai_unavailable": True}, [self.request]))
        self.check["violations"][0].pop("penalty")
        self.assertEqual(service.pending_send_score(self.check, [self.request]), 75)

    def test_overlapping_penalties_are_reassigned(self):
        self.check["violations"][1].update(penalty=0, counted_with="Rule 26", uncounted_penalty=10)
        self.check["score"] = 80
        self.assertEqual(service.pending_send_score(self.check, [self.request]), 90)

    def test_zero_point_request_does_not_remove_score_cap(self):
        self.check["score"] = 65
        self.check["violations"][0]["penalty"] = 0
        self.assertEqual(service.pending_send_score(self.check, [self.request]), 65)


class PendingRequestLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_are_scoped_to_creator_and_current_version(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        row = {"id": "post", "current_version": 3, "created_by": "creator"}
        with patch.object(service, "get_pool", return_value=pool):
            self.assertEqual(await service.send_score(row, {"score": 75, "violations": []}), 75)
        query, *args = conn.fetch.call_args.args
        self.assertEqual(args, ["post", 3, "creator"])
        self.assertIn("version_number = $2", query)
        self.assertIn("requested_by = $3", query)
        self.assertIn("status = 'PENDING'", query)

    async def test_passing_score_needs_no_request_lookup(self):
        with patch.object(service, "get_pool") as pool:
            self.assertEqual(await service.send_score({}, {"score": 95}), 95)
            pool.assert_not_called()
