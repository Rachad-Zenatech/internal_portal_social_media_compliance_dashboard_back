"""Step 6: explicit states, reasons and warnings, and reviewer input saved against the exact assessment."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from models.compliance.public_mood_model import ConversationMood
from services.compliance.analytics import conversation_evidence as ce
from services.compliance.analytics import conversation_feedback as fb

DRONES = uuid4()
TOPICS = {"x-delivery": {"id": DRONES, "external_id": "x-delivery", "name": "Drone Delivery",
                         "terms": ["Drone Delivery"], "excluded": []}}
ROW = {"title": "ZenaDrone cargo launch", "caption_text": "Drone delivery for farms", "brand_name": "ZenaTech",
       "audience": "INVESTORS", "platforms": ["LINKEDIN"], "script_text": "", "transcript_text": "",
       "text": "ZenaDrone cargo launch", "current_version": 1}
NOW = datetime.now(timezone.utc)


def _item(text, sentiment="NEUTRAL", n=[0]):
    n[0] += 1
    return {"id": uuid4(), "topic_external_id": "x-delivery", "topic_name": "Drone Delivery", "network": "X",
            "author": "@a", "url": f"https://n/{n[0]}", "text": text, "content_hash": ce.content_hash(text), "version": 1,
            "story_key": ce.story_key(text), "sentiment": sentiment, "published_at": NOW, "is_eligible": True}


def _hit(item, score=0.9):
    return {"score": score, "meta": {"item_id": str(item["id"]), "content_hash": item["content_hash"]}}


def _assess(items, rejected=None):
    hits = [_hit(i, 0.9 - k * 0.001) for k, i in enumerate(items)]
    return ce.assess(hits, {str(i["id"]): i for i in items}, TOPICS, ce.post_context_of(ROW), {DRONES}, rejected)


class ReasonCodeTests(unittest.TestCase):
    def test_a_small_sample_is_flagged_and_needs_review(self):
        _, mood, _ = _assess([_item(f"Drone delivery update {w} expands again", "POSITIVE") for w in ("aa", "bb", "cc")])
        self.assertEqual(mood.status, "READY")
        self.assertIn("SMALL_SAMPLE", mood.reason_codes)
        self.assertIn("REVIEW_REQUIRED", mood.reason_codes)
        self.assertNotEqual(mood.recommendation, "POST")

    def test_a_hold_says_why(self):
        words = ["Apple", "Berry", "Cedar", "Delta", "Echo", "Fox", "Grape", "Hazel", "Iris", "Jade", "Kilo", "Lima"]
        items = [_item(f"Drone delivery {w} outage angers customers terrible {i * 7}", "NEGATIVE") for i, w in enumerate(words)]
        _, mood, _ = _assess(items)
        self.assertEqual(mood.recommendation, "HOLD")
        self.assertIn("HOLD_NEGATIVE", mood.reason_codes)
        self.assertNotIn("SMALL_SAMPLE", mood.reason_codes)

    def test_no_relevant_evidence_is_a_wait_with_its_own_status_and_code(self):
        status, mood, _ = _assess([])
        self.assertEqual((status, mood.status, mood.recommendation), ("NO_RELEVANT_MATCHES", "NO_RELEVANT_MATCHES", "WAIT"))
        self.assertEqual(mood.reason_codes[0], "NO_RELEVANT_EVIDENCE")

    def test_the_api_shape_has_status_reasons_and_matched_items_alongside_the_old_fields(self):
        _, mood, _ = _assess([_item("Drone delivery trial one begins")])
        d = mood.model_dump(mode="json")
        for key in ("status", "recommendation", "reason_codes", "assessed_at", "expires_at", "evidence_version",
                    "matched_items", "matches", "count", "positive_pct", "retrieved_count", "warnings"):
            self.assertIn(key, d)
        self.assertEqual(d["matched_items"], d["matches"])


class ReviewerExclusionTests(unittest.TestCase):
    def test_an_item_a_reviewer_marked_unrelated_is_left_out_and_recorded(self):
        a, b = _item("Drone delivery pilot alpha expands to towns"), _item("Drone delivery pilot bravo reaches farms")
        _, mood, rows = _assess([a, b], rejected={str(a["id"])})
        self.assertEqual(mood.distinct_story_count, 1)
        gone = next(r for r in rows if r.item["id"] == a["id"])
        self.assertEqual((gone.status, gone.counted), ("UNRELATED", False))
        self.assertIn("reviewer", gone.reason)

    def test_marking_an_item_changes_the_cache_key_so_the_assessment_is_redone(self):
        base = ce.cache_key("c", "v1", "m", [DRONES], "t")
        self.assertEqual(base, ce.cache_key("c", "v1", "m", [DRONES], "t", []))
        self.assertNotEqual(base, ce.cache_key("c", "v1", "m", [DRONES], "t", {"item-1"}))


class StateTests(unittest.TestCase):
    """conversation_mood always says which state it is in, and never invents a recommendation."""

    def setUp(self):
        self.ctx = ce.EvidenceContext("SUCCESS", NOW, "COMPLETE", eligible=5, indexed=5)
        self.previous = None
        self.hits = []

        async def evidence_context():
            return self.ctx

        async def fetch_one(sql, *args):
            return None if len(args) == 2 else self.previous  # (submission, key) = lookup; (submission,) = earlier one

        async def search(*a, **k):
            return self.hits

        async def no_ids(*a, **k):
            return set()

        async def nothing(*a, **k):
            return []

        self.enabled = True
        for p in (
            patch.object(ce.store, "is_enabled", lambda: self.enabled),
            patch.object(ce.store, "search", search),
            patch.object(ce, "evidence_context", evidence_context),
            patch.object(ce, "fetch_one", fetch_one),
            patch.object(ce, "get_state", AsyncMock(return_value="v1")),
            patch.object(ce.feedback, "rejected_item_ids", no_ids),
            patch.object(ce.feedback, "excluded_for", nothing),
            patch.object(ce.feedback, "corrections_for", nothing),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _mood(self):
        topics = [{"id": DRONES, "external_id": "x-delivery", "name": "Drone Delivery", "keywords": [], "excluded_keywords": []}]
        return asyncio.run(ce.conversation_mood(ROW, uuid4(), topics, {DRONES}))

    def _previous(self):
        mood = ConversationMood(status="READY", recommendation="POST", recommendation_reason="earlier", count=12,
                                reason_codes=["SMALL_SAMPLE"])
        self.previous = {"status": "READY", "result": mood.model_dump(mode="json")}

    def test_no_text_means_nothing_to_assess(self):
        self.assertIsNone(asyncio.run(ce.conversation_mood({"title": "", "caption_text": ""}, uuid4(), [], set())))

    def test_store_not_configured_is_unavailable_without_a_recommendation(self):
        self.enabled = False
        m = self._mood()
        self.assertEqual((m.status, m.recommendation, m.reason_codes), ("UNAVAILABLE", None, ["STORE_NOT_CONFIGURED"]))

    def test_unfinished_indexing_is_pending_not_an_assessment_on_part_of_the_evidence(self):
        self.ctx = ce.EvidenceContext("SUCCESS", NOW, "INCOMPLETE", eligible=95, indexed=20)
        m = self._mood()
        self.assertEqual((m.status, m.recommendation), ("INDEXING_PENDING", None))
        self.assertIn("INDEXING_INCOMPLETE", m.reason_codes)
        self.assertEqual(m.pending_index_count, 75)

    def test_an_earlier_assessment_is_shown_as_stale_when_a_new_one_cannot_be_made(self):
        self._previous()
        self.ctx = ce.EvidenceContext("SUCCESS", NOW, "INCOMPLETE", eligible=95, indexed=20)
        m = self._mood()
        self.assertEqual(m.status, "STALE")
        self.assertEqual(m.recommendation, "POST")  # the earlier value, plainly labelled as earlier
        self.assertIn("STALE_ASSESSMENT", m.reason_codes)
        self.assertIn("INDEXING_INCOMPLETE", m.reason_codes)
        self.assertTrue(any("earlier assessment" in w for w in m.warnings))

    def test_an_empty_index_and_a_failed_search_are_told_apart(self):
        self.ctx = ce.EvidenceContext("SUCCESS", NOW, "COMPLETE", eligible=0, indexed=0)
        self.assertEqual(self._mood().reason_codes[0], "INDEX_EMPTY")
        self.ctx = ce.EvidenceContext("SUCCESS", NOW, "COMPLETE", eligible=5, indexed=5)
        m = self._mood()
        self.assertEqual((m.status, m.reason_codes[0]), ("UNAVAILABLE", "SEARCH_UNAVAILABLE"))

    def test_an_error_while_assessing_is_unavailable_not_a_crash_or_a_post(self):
        async def boom(*a, **k):
            raise RuntimeError("atlas down")
        with patch.object(ce.store, "search", boom):
            m = self._mood()
        self.assertEqual((m.status, m.recommendation), ("UNAVAILABLE", None))
        self.assertIn("ASSESSMENT_ERROR", m.reason_codes)


class DecorateTests(unittest.TestCase):
    def test_a_partial_collection_and_old_evidence_become_codes_and_warnings(self):
        ctx = ce.EvidenceContext("PARTIAL", NOW - timedelta(hours=100), "COMPLETE", eligible=3, indexed=3)
        m = ce.decorate(ConversationMood(recommendation="WAIT", reason_codes=["REVIEW_REQUIRED"]), ctx, NOW)
        self.assertEqual(m.collection_status, "PARTIAL")
        for code in ("COLLECTION_PARTIAL", "EVIDENCE_AGING", "REVIEW_REQUIRED"):
            self.assertIn(code, m.reason_codes)
        self.assertEqual(len(m.warnings), 2)

    def test_a_clean_recent_collection_adds_nothing(self):
        ctx = ce.EvidenceContext("SUCCESS", NOW - timedelta(hours=2), "COMPLETE", eligible=3, indexed=3)
        m = ce.decorate(ConversationMood(recommendation="WAIT"), ctx, NOW)
        self.assertEqual((m.reason_codes, m.warnings), ([], []))


class FakePool:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


class FeedbackTests(unittest.TestCase):
    """Reviewer input is validated and saved against the exact assessment, which must belong to the post."""

    def _run(self, coro, assessment_row=True, item_row=True):
        pool = FakePool()
        rows = iter([{"id": uuid4(), "submission_id": uuid4()} if assessment_row else None,
                     {"ok": 1} if item_row else None])
        with patch.object(fb, "fetch_one", AsyncMock(side_effect=lambda *a, **k: next(rows))), \
                patch.object(fb, "get_pool", return_value=pool):
            asyncio.run(coro)
        return pool

    def test_an_unknown_reason_is_rejected_before_anything_is_saved(self):
        with self.assertRaises(HTTPException) as cm:
            self._run(fb.mark_unrelated(uuid4(), uuid4(), uuid4(), uuid4(), "BECAUSE", None))
        self.assertEqual(cm.exception.status_code, 422)

    def test_an_assessment_of_another_post_is_not_found(self):
        with self.assertRaises(HTTPException) as cm:
            self._run(fb.mark_unrelated(uuid4(), uuid4(), uuid4(), uuid4(), "DIFFERENT_COMPANY", None), assessment_row=False)
        self.assertEqual(cm.exception.status_code, 404)

    def test_an_item_that_was_not_in_the_assessment_is_not_found(self):
        with self.assertRaises(HTTPException) as cm:
            self._run(fb.mark_unrelated(uuid4(), uuid4(), uuid4(), uuid4(), "DIFFERENT_COMPANY", None), item_row=False)
        self.assertEqual(cm.exception.status_code, 404)

    def test_marking_an_item_unrelated_is_saved_against_that_assessment_with_its_reason(self):
        assessment, item, user = uuid4(), uuid4(), uuid4()
        pool = self._run(fb.mark_unrelated(uuid4(), assessment, item, user, "different_company", "  other firm "))
        sql, args = pool.calls[0]
        self.assertIn("MATCH_UNRELATED", sql)
        self.assertEqual(args, (assessment, item, "DIFFERENT_COMPANY", "other firm", user))

    def test_a_recommendation_correction_needs_a_valid_reason_and_is_saved_per_person(self):
        with self.assertRaises(HTTPException):
            self._run(fb.correct_recommendation(uuid4(), uuid4(), uuid4(), "POST", "NOPE", None))
        assessment, user = uuid4(), uuid4()
        pool = self._run(fb.correct_recommendation(uuid4(), assessment, user, "POST", "TOO_CAUTIOUS", None), item_row=False)
        sql, args = pool.calls[0]
        self.assertIn("RECOMMENDATION_CORRECTION", sql)
        self.assertEqual(args, (assessment, "POST", "TOO_CAUTIOUS", None, user))

    def test_reason_lists_cover_both_an_incorrect_match_and_an_incorrect_decision(self):
        r = fb.reasons()
        self.assertIn("DIFFERENT_COMPANY", r["unrelated"])
        self.assertIn("TOO_RISKY", r["correction"])
        self.assertIn("OTHER", r["unrelated"])
        self.assertIn("OTHER", r["correction"])


if __name__ == "__main__":
    unittest.main()
