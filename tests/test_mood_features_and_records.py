"""Steps 8 and 10: one feature function, and an insert-only record of every assessment made."""

import asyncio
import math
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance.public_mood_model import ConversationMood
from services.ai.core import mood_store
from services.compliance.analytics import conversation_evidence as ce
from services.compliance.analytics import mood_features as mf
from services.compliance.analytics import mood_model, mood_records

from tests.fake_mongo import FakeMongo

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def ev(story, sentiment="NEUTRAL", author="@a", compound=0.0, flags=None, at=NOW, text=None):
    return {"story_key": story, "sentiment": sentiment, "author": author, "compound": compound,
            "crisis_flags": flags or [], "published_at": at, "text": text or story}


class FeatureTests(unittest.TestCase):
    def test_every_measurement_the_plan_asks_for_is_there_and_nothing_else(self):
        self.assertEqual(mf.FEATURE_NAMES, ["relevant_item_count", "distinct_story_count", "negative_share",
                                            "severe_event_count", "unique_author_count", "snapshot_age_minutes",
                                            "collection_partial", "activity_change"])
        self.assertEqual(set(mf.build_features({}, [], {}, NOW)), set(mf.FEATURE_NAMES))

    def test_the_outcome_and_the_label_are_never_features(self):
        self.assertFalse(set(mf.FEATURE_NAMES) & mf.FORBIDDEN_FEATURES)
        self.assertIn("reviewer_label", mf.FORBIDDEN_FEATURES)
        self.assertIn("post_outcome", mf.FORBIDDEN_FEATURES)

    def test_copies_of_one_story_are_counted_once_as_stories_but_all_as_items(self):
        f = mf.build_features({}, [ev("s1", author="@a"), ev("s1", author="@b"), ev("s2", author="@a")], {}, NOW)
        self.assertEqual((f["relevant_item_count"], f["distinct_story_count"], f["unique_author_count"]), (3, 2, 2))

    def test_negative_share_is_among_stories_and_unknown_with_none(self):
        f = mf.build_features({}, [ev("a", "NEGATIVE"), ev("a", "NEGATIVE"), ev("b"), ev("c"), ev("d")], {}, NOW)
        self.assertEqual(f["negative_share"], 0.25)  # 1 of 4 stories (the two copies of "a" are one)
        self.assertIsNone(mf.build_features({}, [], {}, NOW)["negative_share"])  # no evidence: unknown, not 0

    def test_severe_events_are_serious_intensity_or_several_crisis_words(self):
        f = mf.build_features({}, [ev("a", "NEGATIVE", compound=-0.8), ev("b", "NEGATIVE", compound=-0.2, flags=["attack", "killed"]),
                                   ev("c", "NEGATIVE", compound=-0.4, flags=["attack"]), ev("d")], {}, NOW)
        self.assertEqual(f["severe_event_count"], 2)

    def test_evidence_age_and_collection_state_are_unknown_when_not_given_not_zero(self):
        unknown = mf.build_features({}, [ev("a")], {}, NOW)
        self.assertIsNone(unknown["snapshot_age_minutes"])
        self.assertIsNone(unknown["collection_partial"])
        known = mf.build_features({}, [ev("a")], {"collected_at": NOW - timedelta(hours=3), "collection_status": "PARTIAL"}, NOW)
        self.assertEqual((known["snapshot_age_minutes"], known["collection_partial"]), (180.0, 1))
        self.assertEqual(mf.build_features({}, [ev("a")], {"collection_status": "SUCCESS"}, NOW)["collection_partial"], 0)

    def test_missing_activity_history_is_unknown_not_zero_growth(self):
        self.assertIsNone(mf.build_features({}, [ev("a")], {}, NOW)["activity_change"])  # one story: nothing to compare
        stories = [ev(f"s{i}", at=NOW - timedelta(days=d)) for i, d in enumerate([1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 5, 5, 5])]
        # ten stories in the earlier days... none: all but three are yesterday, so the earlier three days are nearly empty
        self.assertIsNone(mf.build_features({}, stories, {}, NOW)["activity_change"])

    def test_activity_change_is_real_when_there_is_history(self):
        earlier = [ev(f"e{i}", at=NOW - timedelta(days=6 - (i % 3))) for i in range(12)]  # 6, 5, 4 days ago: 12 stories
        recent = [ev(f"r{i}", at=NOW - timedelta(days=1 + (i % 3))) for i in range(24)]   # 1, 2, 3 days ago: 24 stories
        f = mf.build_features({}, earlier + recent, {}, NOW)
        self.assertEqual(f["activity_change"], 100.0)

    def test_it_is_deterministic(self):
        e = [ev("a", "NEGATIVE"), ev("b")]
        state = {"collected_at": NOW - timedelta(minutes=30), "collection_status": "SUCCESS"}
        self.assertEqual(mf.build_features({}, e, state, NOW), mf.build_features({}, e, state, NOW))

    def test_unknown_becomes_missing_for_the_model_not_a_value(self):
        row = mf.to_row(mf.build_features({}, [], {}, NOW))
        self.assertEqual(len(row), len(mf.FEATURE_NAMES))
        self.assertTrue(math.isnan(row[mf.FEATURE_NAMES.index("negative_share")]))
        self.assertTrue(math.isnan(row[mf.FEATURE_NAMES.index("activity_change")]))
        self.assertEqual(row[mf.FEATURE_NAMES.index("relevant_item_count")], 0.0)


def _item(text, sentiment="NEUTRAL", author="@a", n=[0]):
    n[0] += 1
    return {"id": uuid4(), "topic_external_id": "t", "topic_name": "Topic", "network": "X", "author": author,
            "url": f"https://n/{n[0]}", "text": text, "content_hash": ce.content_hash(text), "version": 1,
            "story_key": ce.story_key(text), "sentiment": sentiment, "compound": -0.7 if sentiment == "NEGATIVE" else 0.0,
            "crisis_flags": [], "published_at": NOW, "first_seen_at": NOW, "is_eligible": True, "category": "market"}


def _row(**kw):
    return {"title": "ZenaDrone cargo launch", "caption_text": "Drone delivery for farms", "brand_name": "ZenaTech",
            "brand_code": "ZENATECH", "audience": "INVESTORS", "platforms": ["LINKEDIN"], "script_text": "",
            "transcript_text": "", "text": "ZenaDrone cargo launch", "current_version": 2, **kw}


def _evidence_rows(*items, status="RELEVANT"):
    return [ce.EvidenceRow(item=i, similarity=0.9, margin=0.05, status=status, reason="shared term", supporting=i["text"][:50],
                           story_key=i["story_key"], counted=(status == "RELEVANT"), displayed=(status == "RELEVANT")) for i in items]


def _mood(rec="WAIT"):
    return ConversationMood(status="READY", recommendation=rec, recommendation_reason="r", reason_codes=["REVIEW_REQUIRED"],
                            count=2, retrieved_count=2, assessed_at=NOW)


class RecordTests(unittest.TestCase):
    def _record(self, **kw):
        rows = _evidence_rows(_item("Drone delivery pilot expands", "NEGATIVE"), _item("Another drone story"))
        rows += _evidence_rows(_item("Unrelated office news"), status="UNCERTAIN")
        feats = mf.build_features({}, [mood_records.evidence_dict(r.item) for r in rows if r.status == "RELEVANT"],
                                  {"collected_at": NOW - timedelta(hours=1), "collection_status": "SUCCESS"}, NOW)
        args = dict(assessment_id=uuid4(), submission_id=uuid4(), draft_version=2, row=_row(), evidence_rows=rows, mood=_mood(),
                    features=feats, feature_schema_version=mf.FEATURE_SCHEMA_VERSION, policy_version="conversation-v2:r50",
                    embedding_version="gemini-embedding-2", evidence_version="ev1",
                    state={"collected_at": NOW - timedelta(hours=1), "collection_status": "SUCCESS", "indexing_status": "COMPLETE"},
                    expires_at=NOW + timedelta(hours=24))
        args.update(kw)
        return mood_records.build_assessment_record(**args), rows

    def test_the_record_has_everything_the_plan_asks_for(self):
        rec, _ = self._record()
        for key in ("submission_id", "draft_version", "draft_context", "evidence_refs", "features", "system_recommendation",
                    "reason_codes", "assessed_at", "expires_at", "feature_schema_version", "policy_version"):
            self.assertIn(key, rec)
        self.assertEqual((rec["draft_version"], rec["system_recommendation"], rec["reason_codes"]), (2, "WAIT", ["REVIEW_REQUIRED"]))
        ctx = rec["draft_context"]
        self.assertEqual((ctx["brand_name"], ctx["audience"], ctx["platforms"]), ("ZenaTech", "INVESTORS", ["LINKEDIN"]))
        self.assertIn("cargo launch", ctx["title"])

    def test_evidence_refs_name_every_item_with_its_content_version_and_a_snapshot_keeps_the_text(self):
        rec, rows = self._record()
        self.assertEqual(len(rec["evidence_refs"]), 3)  # candidates that were not relevant are part of what was assessed
        ref = rec["evidence_refs"][0]
        self.assertEqual((ref["item_id"], ref["item_version"], ref["content_hash"]),
                         (str(rows[0].item["id"]), 1, rows[0].item["content_hash"]))
        self.assertEqual({e["relevance_status"] for e in rec["evidence"]}, {"RELEVANT", "UNCERTAIN"})
        self.assertEqual(rec["evidence"][0]["text"], rows[0].item["text"])  # history doesn't depend on the live feed
        self.assertIn("url", rec["evidence"][0])

    def test_the_record_can_be_stored_as_it_is(self):
        rec, _ = self._record()
        import uuid
        import decimal
        def walk(v):
            self.assertNotIsInstance(v, (uuid.UUID, decimal.Decimal))
            if isinstance(v, dict):
                [walk(x) for x in v.values()]
            elif isinstance(v, list):
                [walk(x) for x in v]
        walk(rec)

    def test_features_built_from_the_saved_record_equal_the_ones_saved_with_it(self):
        """Training reads the saved features; this is the guarantee that they are what the live function measured."""
        rec, _ = self._record()
        relevant = [e for e in rec["evidence"] if e["relevance_status"] == "RELEVANT"]
        state = {"collected_at": NOW - timedelta(hours=1), "collection_status": "SUCCESS"}
        self.assertEqual(mf.build_features(rec["draft_context"], relevant, state, NOW), rec["features"])

    def test_reassessing_never_overwrites_an_earlier_record(self):
        first, _ = self._record(assessment_id=uuid4())
        second, _ = self._record(assessment_id=uuid4(), submission_id=first["submission_id"])
        with FakeMongo() as m:
            self.assertTrue(asyncio.run(mood_records.record_assessment(first)))
            self.assertTrue(asyncio.run(mood_records.record_assessment(second)))
            self.assertFalse(asyncio.run(mood_records.record_assessment({**first, "system_recommendation": "POST"})))
            docs = m.docs(mood_store.ASSESSMENTS)
        self.assertEqual(len(docs), 2)
        self.assertEqual(next(d for d in docs if d["_id"] == first["_id"])["system_recommendation"], "WAIT")

    def test_a_problem_saving_the_record_never_stops_the_assessment(self):
        rec, _ = self._record()
        with patch.object(mood_store, "is_enabled", return_value=True), \
                patch.object(mood_store, "insert_once", AsyncMock(side_effect=RuntimeError("atlas down"))):
            self.assertFalse(asyncio.run(mood_records.record_assessment(rec)))
        with patch.object(mood_store, "is_enabled", return_value=False):
            self.assertFalse(asyncio.run(mood_records.record_assessment(rec)))


class AssessmentFlowRecordsTests(unittest.TestCase):
    """conversation_mood with the database and Atlas search faked: every assessment it makes is recorded."""

    def setUp(self):
        mood_model.clear_cache()
        self.mongo = FakeMongo()
        self.mongo.__enter__()
        self.addCleanup(self.mongo.__exit__, None, None, None)
        self.saved, self.version, self.searches = {}, "v1", 0
        self.items = [_item("Drone delivery pilot expands to rural towns"), _item("ZenaTech wins drone contract", "POSITIVE")]
        self.ctx = ce.EvidenceContext("SUCCESS", datetime.now(timezone.utc), "COMPLETE", eligible=2, indexed=2)

        async def search(*a, **k):
            self.searches += 1
            return [{"score": 0.9 - i * 0.01, "meta": {"item_id": str(it["id"]), "content_hash": it["content_hash"]}}
                    for i, it in enumerate(self.items)]

        async def fetch_one(sql, *args):
            return self.saved.get(args[1]) if len(args) == 2 else None

        async def fetch_all(sql, ids):
            return self.items

        async def save(sid, ver, key, ctx_hash, ev, emb, status, mood, rows, exp):
            mood.assessment_id, mood.assessed_at, mood.expires_at = uuid4(), NOW, exp
            self.saved[key] = {"id": mood.assessment_id, "status": status, "result": mood.model_dump(mode="json")}
            return mood

        async def nothing(*a, **k):
            return []

        async def no_ids(*a, **k):
            return set()

        for p in (patch.object(ce.store, "search", search), patch.object(ce, "fetch_one", fetch_one),
                  patch.object(ce, "fetch_all", fetch_all), patch.object(ce, "_save_assessment", save),
                  patch.object(ce, "get_state", AsyncMock(side_effect=lambda k: self.version)),
                  patch.object(ce, "evidence_context", AsyncMock(side_effect=lambda: self.ctx)),
                  patch.object(ce.feedback, "rejected_item_ids", no_ids), patch.object(ce.feedback, "excluded_for", nothing),
                  patch.object(ce.feedback, "corrections_for", nothing)):
            p.start()
            self.addCleanup(p.stop)

    def _mood(self, row=None):
        topics = [{"id": uuid4(), "external_id": "t", "name": "Topic", "keywords": [], "excluded_keywords": []}]
        return asyncio.run(ce.conversation_mood(row or _row(), self.sub, topics, set()))

    sub = uuid4()

    def _records(self):
        return sorted(self.mongo.docs(mood_store.ASSESSMENTS), key=lambda d: d["assessed_at"])

    def test_an_assessment_is_recorded_with_its_features_evidence_and_versions(self):
        mood = self._mood()
        (rec,) = self._records()
        self.assertEqual(rec["_id"], str(mood.assessment_id))
        self.assertEqual((rec["submission_id"], rec["draft_version"]), (str(self.sub), 2))
        self.assertEqual(rec["feature_schema_version"], mf.FEATURE_SCHEMA_VERSION)
        self.assertEqual(rec["policy_version"], ce.policy_version())
        self.assertEqual(set(rec["features"]), set(mf.FEATURE_NAMES))
        self.assertEqual(rec["features"]["collection_partial"], 0)
        self.assertLess(rec["features"]["snapshot_age_minutes"], 5)  # the collection was just now
        self.assertEqual(rec["system_recommendation"], mood.recommendation)
        self.assertEqual(rec["decision"]["source"], "RULES")
        self.assertTrue(rec["evidence"])
        self.assertFalse(rec["is_mock"])

    def test_a_cached_assessment_is_not_recorded_again_but_a_changed_draft_makes_a_new_record(self):
        self._mood()
        self._mood()
        self.assertEqual((self.searches, len(self._records())), (1, 1))  # reused, nothing was evaluated
        self._mood(_row(script_text="a new script"))
        self.assertEqual((self.searches, len(self._records())), (2, 2))  # new assessment, old one untouched
        self.assertEqual(len({r["draft_context"]["script_text"] for r in self._records()}), 2)

    def test_new_evidence_makes_a_new_record_for_an_unchanged_draft(self):
        self._mood()
        self.version = "v2"
        self._mood()
        self.assertEqual(len(self._records()), 2)

    def test_no_relevant_evidence_is_recorded_too_and_a_failure_is_not(self):
        self.items = [_item("Quarterly cafeteria menu")]
        with patch.object(ce, "verify_relevance", return_value=("UNCERTAIN", "no shared term", "")):
            mood = self._mood(_row(title="Brand new topic entirely", text="Brand new topic entirely"))
        self.assertEqual(mood.status, "NO_RELEVANT_MATCHES")
        (rec,) = self._records()
        self.assertEqual((rec["status"], rec["features"]["distinct_story_count"], rec["features"]["negative_share"]),
                         ("NO_RELEVANT_MATCHES", 0, None))
        self.ctx = ce.EvidenceContext("SUCCESS", datetime.now(timezone.utc), "INCOMPLETE", eligible=5, indexed=1)  # unfinished
        self._mood(_row(title="Another draft"))
        self.assertEqual(len(self._records()), 1)  # nothing was evaluated, so nothing is recorded

    def test_a_shadow_models_answer_is_recorded_beside_the_rules_and_changes_nothing(self):
        run = {"_id": "run-1", "status": "SHADOW", "feature_schema_version": mf.FEATURE_SCHEMA_VERSION}
        with patch.object(mood_model, "registered", AsyncMock(side_effect=lambda s: run if s == "SHADOW" else None)), \
                patch.object(mood_model, "predict", return_value={"recommendation": "HOLD", "probabilities": {"POST": .1, "WAIT": .2, "HOLD": .7}}):
            mood = self._mood()
        (rec,) = self._records()
        self.assertEqual(mood.recommendation, rec["system_recommendation"])
        self.assertNotEqual(mood.recommendation, "HOLD")  # the rules still decide
        shadow = rec["decision"]["shadow"]
        self.assertEqual((shadow["model_version"], shadow["status"], shadow["recommendation"]), ("run-1", "OK", "HOLD"))
        self.assertEqual(shadow["probabilities"], {"POST": .1, "WAIT": .2, "HOLD": .7})
        self.assertIn("support_issues", shadow)  # a model with no metadata file is flagged as unvouched-for, not trusted
        self.assertEqual(mood.decision_source, "RULES")


if __name__ == "__main__":
    unittest.main()
