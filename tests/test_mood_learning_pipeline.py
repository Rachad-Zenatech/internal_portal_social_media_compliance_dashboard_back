"""Steps 9, 11-14: reviews and conflicts, a fixed dataset, training, promotion, and the guarded decision.

Training here runs on a SYNTHETIC dataset made in the test: it proves the pipeline works, nothing about real data."""

import asyncio
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from services.ai.core import mood_store
from services.compliance.analytics import conversation_feedback as fb
from services.compliance.analytics import mood_dataset as ds
from services.compliance.analytics import mood_features as mf
from services.compliance.analytics import mood_model, mood_reviews as mr, mood_training as mt

from tests.fake_mongo import FakeMongo

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


# --- Step 9: reviews -------------------------------------------------------------------------------------------

def _rev(rid, label, reviewer="r1", at=0, resolution=False, retracted=False):
    return {"_id": rid, "reviewer_id": reviewer, "reviewer_label": label, "is_resolution": resolution,
            "reviewed_at": T0 + timedelta(minutes=at), "retracted_at": T0 if retracted else None}


class ResolutionTests(unittest.TestCase):
    def test_no_reviews_is_unreviewed(self):
        self.assertEqual(mr.resolve_decision([]).status, "UNREVIEWED")
        self.assertEqual(mr.resolve_decision([_rev("a", "HOLD", retracted=True)]).status, "UNREVIEWED")

    def test_one_reviewer_or_several_agreeing_is_accepted(self):
        one = mr.resolve_decision([_rev("a", "HOLD")])
        self.assertEqual((one.status, one.label, one.source), ("ACCEPTED", "HOLD", "SINGLE_REVIEWER"))
        two = mr.resolve_decision([_rev("a", "WAIT", "r1"), _rev("b", "WAIT", "r2")])
        self.assertEqual((two.status, two.label, two.source, two.review_ids), ("ACCEPTED", "WAIT", "UNANIMOUS", ["a", "b"]))

    def test_disagreement_is_a_conflict_with_no_label(self):
        res = mr.resolve_decision([_rev("a", "POST", "r1"), _rev("b", "HOLD", "r2")])
        self.assertEqual((res.status, res.label, res.labels), ("CONFLICT", None, ["HOLD", "POST"]))

    def test_a_lead_reviewers_resolution_settles_it(self):
        res = mr.resolve_decision([_rev("a", "POST", "r1"), _rev("b", "HOLD", "r2"), _rev("c", "WAIT", "lead", at=5, resolution=True)])
        self.assertEqual((res.status, res.label, res.source, res.review_ids), ("ACCEPTED", "WAIT", "RESOLUTION", ["c"]))

    def test_a_withdrawn_review_no_longer_counts(self):
        res = mr.resolve_decision([_rev("a", "POST", "r1"), _rev("b", "HOLD", "r2", retracted=True)])
        self.assertEqual((res.status, res.label), ("ACCEPTED", "POST"))


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.mongo = FakeMongo()
        self.mongo.__enter__()
        self.addCleanup(self.mongo.__exit__, None, None, None)
        self.a, self.s, self.r, self.i = uuid4(), uuid4(), uuid4(), uuid4()

    def test_a_decision_is_saved_and_a_changed_mind_keeps_the_earlier_answer(self):
        run(mr.record_decision(self.a, self.s, self.r, "post", "TOO_CAUTIOUS", " fine "))
        run(mr.record_decision(self.a, self.s, self.r, "HOLD", "TOO_RISKY", None))
        (doc,) = self.mongo.docs(mood_store.REVIEWS)
        self.assertEqual((doc["reviewer_label"], doc["reason_code"], doc["retracted_at"]), ("HOLD", "TOO_RISKY", None))
        self.assertEqual([p["reviewer_label"] for p in doc["previous"]], ["POST"])
        self.assertEqual(doc["previous"][0]["comment"], "fine")

    def test_a_decision_must_be_post_wait_or_hold(self):
        with self.assertRaises(ValueError):
            run(mr.record_decision(self.a, self.s, self.r, "MAYBE", "OTHER", None))

    def test_withdrawing_keeps_the_record(self):
        run(mr.record_decision(self.a, self.s, self.r, "POST", "AGREE", None))
        run(mr.retract_decision(self.a, self.r))
        (doc,) = self.mongo.docs(mood_store.REVIEWS)
        self.assertIsNotNone(doc["retracted_at"])
        self.assertEqual(mr.resolve_decision(run(mr.decisions_for(str(self.a)))).status, "UNREVIEWED")

    def test_match_corrections_are_kept_apart_from_the_decision_and_from_each_other(self):
        run(mr.record_decision(self.a, self.s, self.r, "WAIT", "OTHER", None))
        run(mr.record_match_correction(self.a, self.s, self.r, "UNRELATED", self.i, reason_code="DIFFERENT_COMPANY"))
        run(mr.record_match_correction(self.a, self.s, self.r, "WRONG_SENTIMENT", self.i, correct_sentiment="negative"))
        self.assertEqual(len(self.mongo.docs(mood_store.REVIEWS)), 1)
        kinds = {d["kind"]: d for d in self.mongo.docs(mood_store.MATCH_CORRECTIONS)}
        self.assertEqual(set(kinds), {"UNRELATED", "WRONG_SENTIMENT"})
        self.assertEqual(kinds["UNRELATED"]["reason_code"], "DIFFERENT_COMPANY")
        self.assertEqual(kinds["WRONG_SENTIMENT"]["correct_sentiment"], "NEGATIVE")

    def test_a_wrong_sentiment_needs_a_real_sentiment(self):
        with self.assertRaises(ValueError):
            run(mr.record_match_correction(self.a, self.s, self.r, "WRONG_SENTIMENT", self.i, correct_sentiment="angry"))

    def test_putting_an_item_back_withdraws_the_unrelated_marks_only(self):
        run(mr.record_match_correction(self.a, self.s, self.r, "UNRELATED", self.i, reason_code="OTHER"))
        run(mr.record_match_correction(self.a, self.s, self.r, "WRONG_SENTIMENT", self.i, correct_sentiment="NEUTRAL"))
        self.assertEqual(run(mr.retract_match_corrections(self.s, self.i, "UNRELATED")), 1)
        live = {d["kind"]: d["retracted_at"] for d in self.mongo.docs(mood_store.MATCH_CORRECTIONS)}
        self.assertIsNotNone(live["UNRELATED"])
        self.assertIsNone(live["WRONG_SENTIMENT"])

    def test_a_copy_to_the_learning_store_that_fails_never_fails_the_reviewers_action(self):
        run(mr.mirror(AsyncMock(side_effect=RuntimeError("atlas down"))()))  # logged, not raised
        with patch.object(mood_store, "is_enabled", return_value=False):
            ran = []

            async def write():
                ran.append(1)
            run(mr.mirror(write()))
        self.assertEqual(ran, [])


class FakePool:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


class FeedbackMirrorTests(unittest.TestCase):
    """The reviewer's screen is saved in Postgres as before; the same actions also reach the learning collections."""

    def setUp(self):
        self.mongo = FakeMongo()
        self.mongo.__enter__()
        self.addCleanup(self.mongo.__exit__, None, None, None)
        self.pool = FakePool()
        self.a, self.s, self.u = uuid4(), uuid4(), uuid4()
        rows = {"assessment": {"id": self.a, "submission_id": self.s}, "rec": {"rec": "WAIT"}, "item": {"ok": 1}}

        async def fetch_one(sql, *args):
            return rows["rec"] if "recommendation" in sql else (rows["item"] if "assessment_items" in sql else rows["assessment"])
        for p in (patch.object(fb, "fetch_one", fetch_one), patch.object(fb, "get_pool", return_value=self.pool)):
            p.start()
            self.addCleanup(p.stop)

    def test_a_correction_is_a_decision_with_a_reason(self):
        run(fb.correct_recommendation(self.s, self.a, self.u, "HOLD", "TOO_RISKY", "unsafe today"))
        (doc,) = self.mongo.docs(mood_store.REVIEWS)
        self.assertEqual((doc["reviewer_label"], doc["reason_code"], doc["comment"], doc["is_resolution"]),
                         ("HOLD", "TOO_RISKY", "unsafe today", False))
        self.assertEqual(len(self.pool.calls), 1)  # and the screen's own record

    def test_confirming_records_the_assessments_own_recommendation_as_the_decision(self):
        run(fb.confirm_recommendation(self.s, self.a, self.u))
        (doc,) = self.mongo.docs(mood_store.REVIEWS)
        self.assertEqual((doc["reviewer_label"], doc["reason_code"]), ("WAIT", "AGREE"))  # agreement is a label too

    def test_a_lead_reviewers_resolution_is_marked_as_one(self):
        run(fb.correct_recommendation(self.s, self.a, self.u, "POST", "OTHER", None, resolution=True))
        self.assertTrue(self.mongo.docs(mood_store.REVIEWS)[0]["is_resolution"])

    def test_withdrawing_a_decision_withdraws_it_everywhere(self):
        run(fb.correct_recommendation(self.s, self.a, self.u, "HOLD", "OTHER", None))
        run(fb.clear_correction(self.s, self.a, self.u))
        self.assertIsNotNone(self.mongo.docs(mood_store.REVIEWS)[0]["retracted_at"])

    def test_unrelated_and_put_back_reach_the_corrections_collection(self):
        item = uuid4()
        run(fb.mark_unrelated(self.s, self.a, item, self.u, "DIFFERENT_COMPANY", None))
        self.assertEqual(self.mongo.docs(mood_store.MATCH_CORRECTIONS)[0]["kind"], "UNRELATED")
        run(fb.restore_item(self.s, item))
        self.assertIsNotNone(self.mongo.docs(mood_store.MATCH_CORRECTIONS)[0]["retracted_at"])

    def test_wrong_sentiment_is_recorded_for_the_sentiment_analysis_and_validated(self):
        item = uuid4()
        run(fb.mark_wrong_sentiment(self.s, self.a, item, self.u, "negative", "it is an attack story"))
        (doc,) = self.mongo.docs(mood_store.MATCH_CORRECTIONS)
        self.assertEqual((doc["kind"], doc["correct_sentiment"], doc["item_id"]), ("WRONG_SENTIMENT", "NEGATIVE", str(item)))
        self.assertEqual(self.pool.calls, [])  # it changes no assessment
        with self.assertRaises(HTTPException) as cm:
            run(fb.mark_wrong_sentiment(self.s, self.a, item, self.u, "angry", None))
        self.assertEqual(cm.exception.status_code, 422)

    def test_the_reviewers_action_succeeds_even_when_the_learning_store_is_down(self):
        with patch.object(mood_store, "upsert", AsyncMock(side_effect=RuntimeError("atlas down"))):
            run(fb.correct_recommendation(self.s, self.a, self.u, "HOLD", "OTHER", None))
        self.assertEqual(len(self.pool.calls), 1)

    def test_agree_is_a_valid_reason_and_the_reason_lists_cover_both_kinds_of_correction(self):
        self.assertIn("AGREE", fb.CORRECTION_REASONS)
        self.assertIn("DIFFERENT_COMPANY", fb.UNRELATED_REASONS)


# --- Step 11: the fixed dataset ----------------------------------------------------------------------------------

def _assessment(n, label_hint=None, at=None, submission=None, stories=(), mock=False, schema=mf.FEATURE_SCHEMA_VERSION,
                features=None, recommendation="WAIT"):
    aid = f"assess-{n:04d}"
    return {
        "_id": aid, "assessment_id": aid, "submission_id": submission or f"draft-{n:04d}", "draft_version": 1,
        "assessed_at": at or (T0 + timedelta(days=n)), "feature_schema_version": schema, "is_mock": mock,
        "features": features if features is not None else {k: None for k in mf.FEATURE_NAMES} | {"distinct_story_count": n % 7},
        "system_recommendation": recommendation, "status": "READY",
        "evidence": [{"story_key": s, "counted": True} for s in stories],
    }


def _seed(mongo, assessments, reviews):
    for a in assessments:
        mongo[mood_store.ASSESSMENTS].docs[a["_id"]] = a
    for r in reviews:
        mongo[mood_store.REVIEWS].docs[r["_id"]] = {"assessment_id": r["assessment_id"], "retracted_at": None,
                                                    "is_resolution": False, "reviewed_at": T0, **r}


def _decide(aid, label, reviewer="r1", **kw):
    return {"_id": f"{aid}:{reviewer}:decision", "assessment_id": aid, "reviewer_label": label, "reviewer_id": reviewer, **kw}


class GroupingAndSplitTests(unittest.TestCase):
    def test_assessments_of_one_draft_and_assessments_sharing_a_story_travel_together(self):
        a = [_assessment(1, submission="d1", stories=["s1"]), _assessment(2, submission="d1", stories=["s2"]),
             _assessment(3, submission="d3", stories=["s2"]), _assessment(4, submission="d4", stories=["s9"])]
        g = ds.group_assessments(a)
        self.assertEqual(g["assess-0001"], g["assess-0002"])   # the same draft
        self.assertEqual(g["assess-0002"], g["assess-0003"])   # a story in common
        self.assertNotEqual(g["assess-0001"], g["assess-0004"])

    def _examples(self, n_groups, per_group=1):
        out = []
        for gi in range(n_groups):
            for k in range(per_group):
                out.append({"group_id": f"g{gi}", "assessed_at": T0 + timedelta(days=gi, minutes=k), "reviewer_label": "WAIT", "split": None})
        return out

    def test_oldest_groups_train_then_validate_and_the_newest_is_the_untouched_test(self):
        ex = self._examples(10)
        ds.assign_splits(ex)
        order = [e["split"] for e in sorted(ex, key=lambda e: e["assessed_at"])]
        self.assertEqual(order, ["train"] * 6 + ["validation"] * 2 + ["test"] * 2)

    def test_whole_groups_stay_in_one_split(self):
        ex = self._examples(8, per_group=3)
        ds.assign_splits(ex)
        for g in {e["group_id"] for e in ex}:
            self.assertEqual(len({e["split"] for e in ex if e["group_id"] == g}), 1)
        self.assertEqual({e["split"] for e in ex}, {"train", "validation", "test"})

    def test_a_one_big_group_cannot_starve_a_split(self):
        ex = self._examples(1, per_group=50) + [{"group_id": f"g{i}", "assessed_at": T0 + timedelta(days=5 + i), "reviewer_label": "WAIT", "split": None} for i in range(1, 3)]
        ds.assign_splits(ex)
        self.assertEqual({e["split"] for e in ex}, {"train", "validation", "test"})

    def test_too_few_independent_groups_to_separate_the_periods_is_refused(self):
        with self.assertRaises(ds.DatasetError):
            ds.assign_splits(self._examples(2, per_group=20))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.mongo = FakeMongo()
        self.mongo.__enter__()
        self.addCleanup(self.mongo.__exit__, None, None, None)

    def _reviewed(self, n=10):
        a = [_assessment(i) for i in range(n)]
        _seed(self.mongo, a, [_decide(x["_id"], ["POST", "WAIT", "HOLD"][i % 3]) for i, x in enumerate(a)])
        return a

    def test_only_reviewed_non_mock_resolved_assessments_of_the_right_schema_are_exported_and_each_exclusion_is_counted(self):
        a = [_assessment(i) for i in range(10)] + [_assessment(20, mock=True), _assessment(21), _assessment(22),
                                                   _assessment(23, schema="v0"), _assessment(24, features={})]
        labels = [_decide(x["_id"], "WAIT") for x in a[:10]] + [
            _decide("assess-0020", "WAIT"), _decide("assess-0022", "POST", "r1"), _decide("assess-0022", "HOLD", "r2"),
            _decide("assess-0023", "WAIT"), _decide("assess-0024", "WAIT")]
        _seed(self.mongo, a, labels)
        m = run(ds.build_dataset("mood_v001"))
        self.assertEqual(m["example_count"], 10)
        self.assertEqual(m["excluded"], {"mock_data": 1, "not_reviewed": 1, "reviewers_disagree_unresolved": 1,
                                         "other_feature_schema_or_no_features": 2})
        self.assertEqual((m["status"], m["feature_schema_version"], m["features"]), ("READY", "v1", mf.FEATURE_NAMES))

    def test_the_label_is_the_accepted_reviewer_decision_and_each_example_names_what_it_rests_on(self):
        self._reviewed()
        run(ds.build_dataset("mood_v001"))
        ex = {e["assessment_id"]: e for e in self.mongo.docs(mood_store.EXAMPLES)}
        e = ex["assess-0002"]
        self.assertEqual((e["reviewer_label"], e["label_source"], e["dataset_version"]), ("HOLD", "SINGLE_REVIEWER", "mood_v001"))
        self.assertEqual(e["review_ids"], ["assess-0002:r1:decision"])
        self.assertIn(e["split"], ("train", "validation", "test"))
        self.assertTrue(e["group_id"])

    def test_a_resolved_conflict_is_included_with_the_leads_label(self):
        a = [_assessment(i) for i in range(10)]
        reviews = [_decide(x["_id"], "WAIT") for x in a[1:]]
        reviews += [_decide("assess-0000", "POST", "r1"), _decide("assess-0000", "HOLD", "r2"),
                    _decide("assess-0000", "WAIT", "lead", is_resolution=True, reviewed_at=T0 + timedelta(hours=1))]
        _seed(self.mongo, a, reviews)
        run(ds.build_dataset("mood_v001"))
        e = next(x for x in self.mongo.docs(mood_store.EXAMPLES) if x["assessment_id"] == "assess-0000")
        self.assertEqual((e["reviewer_label"], e["label_source"], e["review_ids"]), ("WAIT", "RESOLUTION", ["assess-0000:lead:decision"]))

    def test_the_inputs_are_the_features_saved_at_assessment_time(self):
        self._reviewed()
        run(ds.build_dataset("mood_v001"))
        manifest, examples = run(ds.load_dataset("mood_v001"))
        by_id = {e["assessment_id"]: e for e in examples}
        self.assertEqual(by_id["assess-0003"]["features"]["distinct_story_count"], 3)
        self.assertEqual(by_id["assess-0003"]["system_recommendation"], "WAIT")  # what the rules said then, for comparison

    def test_the_manifest_records_the_split_the_classes_and_the_period(self):
        self._reviewed(12)
        m = run(ds.build_dataset("mood_v001"))
        self.assertEqual(sum(m["by_split"].values()), 12)
        self.assertEqual(sum(m["by_label"].values()), 12)
        self.assertEqual(set(m["by_split_and_label"]), {"train", "validation", "test"})
        self.assertEqual(set(m["period"]), {"train", "validation", "test"})
        self.assertEqual(m["examples_collection"], mood_store.EXAMPLES)  # membership is separate records, not one document
        self.assertEqual(len(self.mongo.docs(mood_store.EXAMPLES)), 12)

    def test_a_dataset_version_is_fixed_it_is_never_rebuilt(self):
        self._reviewed()
        run(ds.build_dataset("mood_v001"))
        with self.assertRaises(ds.DatasetError):
            run(ds.build_dataset("mood_v001"))
        self.assertEqual(len(self.mongo.docs(mood_store.EXAMPLES)), 10)

    def test_nothing_reviewed_yet_says_so(self):
        _seed(self.mongo, [_assessment(1)], [])
        with self.assertRaises(ds.DatasetError) as cm:
            run(ds.build_dataset("mood_v001"))
        self.assertIn("not_reviewed", str(cm.exception))


# --- Step 12: metrics, readiness, training --------------------------------------------------------------------

class MetricTests(unittest.TestCase):
    def test_the_four_questions_are_answered_exactly(self):
        truth = ["HOLD", "HOLD", "WAIT", "WAIT", "POST", "POST", "POST", "POST"]
        pred = ["POST", "HOLD", "POST", "HOLD", "POST", "POST", "WAIT", "POST"]
        m = mt.metrics(truth, pred)
        self.assertEqual((m["missed_hold_count"], m["missed_hold_rate"]), (1, 0.5))       # a HOLD approved
        self.assertEqual((m["unsafe_approval_count"], m["unsafe_approval_rate"]), (2, 0.5))  # HOLD/WAIT approved: 2 of 4
        self.assertEqual((m["unnecessary_hold_count"], m["unnecessary_hold_rate"]), (1, 0.5))  # a WAIT blocked: 1 of 2 HOLDs
        self.assertEqual(m["post_precision"], 0.6)   # 3 of the 5 POSTs were right
        self.assertEqual(m["wait_rate"], 0.125)

    def test_a_rate_with_no_cases_is_unknown_not_zero(self):
        m = mt.metrics(["POST", "POST"], ["POST", "POST"])
        self.assertIsNone(m["missed_hold_rate"])
        self.assertIsNone(m["unnecessary_hold_rate"])
        self.assertEqual(m["post_precision"], 1.0)

    def test_calibration_measures_whether_confidence_means_what_it_says(self):
        sure_and_right = mt.calibration([[0.98, 0.01, 0.01]] * 4, ["POST"] * 4)
        sure_and_wrong = mt.calibration([[0.98, 0.01, 0.01]] * 4, ["HOLD"] * 4)
        self.assertLess(sure_and_right["expected_calibration_error"], 0.05)
        self.assertGreater(sure_and_wrong["expected_calibration_error"], 0.9)
        self.assertIsNone(mt.calibration([], [])["brier"])


def _split_examples(counts):
    out = []
    for split, per_class in counts.items():
        for label, n in per_class.items():
            out += [{"split": split, "reviewer_label": label} for _ in range(n)]
    return out


class ReadinessTests(unittest.TestCase):
    def test_too_few_of_any_class_means_keep_collecting_reviews(self):
        r = mt.readiness(_split_examples({"train": {"POST": 30, "WAIT": 30, "HOLD": 3}, "validation": {"POST": 6, "WAIT": 6, "HOLD": 6},
                                          "test": {"POST": 6, "WAIT": 6, "HOLD": 6}}))
        self.assertFalse(r["ready"])
        self.assertEqual(r["shortfalls"], ["train: HOLD has 3 of 20 needed"])

    def test_enough_of_every_class_in_every_split_is_ready(self):
        r = mt.readiness(_split_examples({s: {c: 30 for c in mt.CLASSES} for s in ("train", "validation", "test")}))
        self.assertTrue(r["ready"])

    def test_a_missing_class_counts_as_zero(self):
        r = mt.readiness(_split_examples({"train": {"POST": 30, "WAIT": 30}, "validation": {"POST": 6, "WAIT": 6, "HOLD": 6},
                                          "test": {"POST": 6, "WAIT": 6, "HOLD": 6}}))
        self.assertIn("train: HOLD has 0 of 20 needed", r["shortfalls"])


def _synthetic(n_per_class=100):
    """Assessments whose features really do separate the classes (a HOLD has severe events, a POST has many calm
    stories, a WAIT is in between), reviewed by one reviewer each, spread over time, every one its own group."""
    a, reviews, i = [], [], 0
    for label in ("POST", "WAIT", "HOLD"):
        for k in range(n_per_class):
            f = {"relevant_item_count": 12 + (k % 5), "distinct_story_count": 12 + (k % 5), "negative_share": 0.05 + 0.02 * (k % 4),
                 "severe_event_count": 0, "unique_author_count": 8, "snapshot_age_minutes": 30.0, "collection_partial": 0,
                 "activity_change": None}
            if label == "WAIT":
                f.update(distinct_story_count=4 + (k % 4), relevant_item_count=5 + (k % 4), negative_share=0.3 + 0.02 * (k % 4))
            if label == "HOLD":
                f.update(severe_event_count=2 + (k % 3), negative_share=0.6 + 0.02 * (k % 4), distinct_story_count=10 + (k % 5))
            x = _assessment(i, features=f, recommendation="WAIT", at=T0 + timedelta(hours=i))  # the rules always said WAIT
            a.append(x)
            reviews.append(_decide(x["_id"], label))
            i += 1
    # interleave the labels over time so every period holds every class
    order = sorted(range(len(a)), key=lambda j: (j % n_per_class, j))
    for t, j in enumerate(order):
        a[j]["assessed_at"] = T0 + timedelta(hours=t)
    return a, reviews


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.mongo = FakeMongo()
        self.mongo.__enter__()
        self.addCleanup(self.mongo.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(mood_model, "MODEL_DIR", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        mood_model.clear_cache()

    def test_with_too_few_examples_no_model_is_trained(self):
        a, rv = _synthetic(4)
        _seed(self.mongo, a, rv)
        run(ds.build_dataset("mood_v001"))
        with self.assertRaises(mt.NotReady) as cm:
            run(mt.train("mood_v001"))
        self.assertIn("Keep collecting reviews", str(cm.exception))
        self.assertEqual(self.mongo.docs(mood_store.MODEL_RUNS), [])

    def test_a_model_trained_on_too_little_is_only_an_experiment_and_can_never_be_promoted(self):
        a, rv = _synthetic(10)
        _seed(self.mongo, a, rv)
        run(ds.build_dataset("mood_v001"))
        r = run(mt.train("mood_v001", allow_small=True))
        self.assertEqual(r["status"], "EXPERIMENT")
        self.assertTrue(any("too few" in p for p in mt.check_promotion(r)))
        with self.assertRaises(mt.NotReady):
            run(mt.promote(r["_id"], "A. Reviewer"))

    def test_training_learns_from_older_reviews_and_is_judged_on_the_newest_against_the_rules(self):
        a, rv = _synthetic(100)
        _seed(self.mongo, a, rv)
        manifest = run(ds.build_dataset("mood_v001"))
        r = run(mt.train("mood_v001"))
        self.assertEqual((r["status"], r["algorithm"], r["dataset_version"], r["feature_schema_version"]),
                         ("TRAINED", "xgboost", "mood_v001", "v1"))
        self.assertTrue(Path(r["model_path"]).exists())
        self.assertEqual(r["features"], mf.FEATURE_NAMES)
        self.assertTrue(r["readiness"]["ready"])
        test = r["metrics"]["test"]
        self.assertEqual(test["model"]["n"], manifest["by_split"]["test"])
        self.assertGreater(test["model"]["accuracy"], test["rules"]["accuracy"])        # the rules said WAIT to everything
        self.assertLess(test["model"]["missed_hold_rate"], test["rules"]["missed_hold_rate"])
        self.assertIn("expected_calibration_error", r["calibration"]["test"])
        self.assertEqual(len(self.mongo.docs(mood_store.MODEL_RUNS)), 1)

    def test_the_run_can_be_reloaded_and_predicts_from_the_features(self):
        a, rv = _synthetic(100)
        _seed(self.mongo, a, rv)
        run(ds.build_dataset("mood_v001"))
        r = run(mt.train("mood_v001"))
        hold = mood_model.predict(r, {**{k: None for k in mf.FEATURE_NAMES}, "severe_event_count": 3, "negative_share": 0.7,
                                      "distinct_story_count": 12, "relevant_item_count": 12, "unique_author_count": 8,
                                      "snapshot_age_minutes": 30.0, "collection_partial": 0})
        self.assertEqual(hold["recommendation"], "HOLD")
        self.assertAlmostEqual(sum(hold["probabilities"].values()), 1.0, places=2)

    def test_a_missing_value_is_handled_as_missing(self):
        a, rv = _synthetic(100)
        _seed(self.mongo, a, rv)
        run(ds.build_dataset("mood_v001"))
        r = run(mt.train("mood_v001"))
        guess = mood_model.predict(r, {k: None for k in mf.FEATURE_NAMES})  # everything unknown: still answers
        self.assertIn(guess["recommendation"], mt.CLASSES)


def _good_run():
    ok = {"n": 40, "accuracy": .9, "missed_hold_rate": 0.0, "missed_hold_count": 0, "unsafe_approval_rate": 0.02,
          "unsafe_approval_count": 0, "unnecessary_hold_rate": 0.1, "post_precision": 0.95, "wait_rate": 0.3}
    return {"_id": "run-1", "status": "TRAINED", "feature_schema_version": "v1", "readiness": {"ready": True},
            "metrics": {"test": {"model": ok, "rules": {**ok, "missed_hold_count": 2}}},
            "calibration": {"test": {"expected_calibration_error": 0.05}}}


class PromotionTests(unittest.TestCase):
    def test_a_run_that_meets_every_criterion_can_be_promoted(self):
        self.assertEqual(mt.check_promotion(_good_run()), [])

    def test_each_criterion_blocks_on_its_own(self):
        cases = {
            "missed HOLD": ("missed_hold_rate", 0.1), "unsafe approval": ("unsafe_approval_rate", 0.2),
            "unnecessary HOLD": ("unnecessary_hold_rate", 0.5), "POST precision": ("post_precision", 0.5),
            "WAIT rate": ("wait_rate", 0.9), "test examples": ("n", 10),
        }
        for label, (key, value) in cases.items():
            run_ = _good_run()
            run_["metrics"]["test"]["model"][key] = value
            problems = mt.check_promotion(run_)
            self.assertTrue(any(label.split()[0].lower() in p.lower() for p in problems), (label, problems))
        bad = _good_run()
        bad["calibration"]["test"]["expected_calibration_error"] = 0.4
        self.assertTrue(any("calibration" in p for p in mt.check_promotion(bad)))

    def test_a_rate_that_cant_be_judged_blocks_rather_than_passes(self):
        r = _good_run()
        r["metrics"]["test"]["model"]["missed_hold_rate"] = None   # no HOLD cases in the test period
        self.assertTrue(any("can't be judged" in p for p in mt.check_promotion(r)))

    def test_it_must_miss_no_more_holds_than_the_rules_do(self):
        r = _good_run()
        r["metrics"]["test"]["rules"]["missed_hold_count"] = 0
        r["metrics"]["test"]["model"]["missed_hold_count"] = 1
        self.assertTrue(any("misses more HOLDs" in p for p in mt.check_promotion(r)))

    def test_a_model_for_another_feature_schema_is_blocked(self):
        r = _good_run()
        r["feature_schema_version"] = "v0"
        self.assertTrue(any("feature schema" in p for p in mt.check_promotion(r)))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.mongo = FakeMongo()
        self.mongo.__enter__()
        self.addCleanup(self.mongo.__exit__, None, None, None)
        mood_model.clear_cache()
        self.mongo[mood_store.MODEL_RUNS].docs["run-1"] = {**_good_run(), "created_at": T0}
        self.mongo[mood_store.MODEL_RUNS].docs["run-2"] = {**_good_run(), "_id": "run-2", "created_at": T0 + timedelta(days=1)}

    def status(self, rid):
        return self.mongo[mood_store.MODEL_RUNS].docs[rid]["status"]

    def test_promotion_needs_a_named_approver(self):
        with self.assertRaises(mt.NotReady):
            run(mt.promote("run-1", "  "))
        self.assertEqual(self.status("run-1"), "TRAINED")

    def test_promoting_records_who_and_on_what_criteria_and_retires_the_previous_model(self):
        run(mt.promote("run-1", "A. Reviewer"))
        run(mt.promote("run-2", "B. Lead"))
        self.assertEqual((self.status("run-1"), self.status("run-2")), ("RETIRED", "PROMOTED"))
        doc = self.mongo[mood_store.MODEL_RUNS].docs["run-2"]
        self.assertEqual(doc["promoted_by"], "B. Lead")
        self.assertEqual(doc["criteria"]["max_missed_hold_rate"], 0.0)

    def test_a_run_that_fails_the_criteria_is_refused_and_unchanged(self):
        self.mongo[mood_store.MODEL_RUNS].docs["run-1"]["metrics"]["test"]["model"]["missed_hold_rate"] = 0.3
        with self.assertRaises(mt.NotReady) as cm:
            run(mt.promote("run-1", "A. Reviewer"))
        self.assertIn("missed HOLD", str(cm.exception))
        self.assertEqual(self.status("run-1"), "TRAINED")

    def test_shadow_changes_no_decision_and_one_model_shadows_at_a_time(self):
        run(mt.shadow("run-1"))
        run(mt.shadow("run-2"))
        self.assertEqual((self.status("run-1"), self.status("run-2")), ("RETIRED", "SHADOW"))

    def test_retiring_takes_a_model_out_of_use_at_once(self):
        run(mt.promote("run-1", "A. Reviewer"))
        self.assertEqual(run(mood_model.registered("PROMOTED"))["_id"], "run-1")
        run(mt.retire("run-1"))
        self.assertIsNone(run(mood_model.registered("PROMOTED")))

    def test_disagreements_list_where_the_shadow_model_and_the_rules_differ(self):
        def with_decision(n, rules, shadow, shadow_status="OK"):
            return {**_assessment(n), "decision": {"final": rules, "source": "RULES",
                                                    "shadow": {"status": shadow_status, "recommendation": shadow, "probabilities": {}}}}
        for a in (with_decision(1, "WAIT", "WAIT"), with_decision(2, "WAIT", "HOLD"), with_decision(3, "POST", "WAIT", "ERROR")):
            self.mongo[mood_store.ASSESSMENTS].docs[a["_id"]] = a
        _seed(self.mongo, [], [_decide("assess-0002", "HOLD")])
        rows = run(mt.disagreements())
        self.assertEqual([(r["assessment_id"], r["rules"], r["shadow"], r["reviewer"]) for r in rows],
                         [("assess-0002", "WAIT", "HOLD", "HOLD")])


# --- Steps 13-14: the guarded decision -------------------------------------------------------------------------

def _features(**kw):
    base = {"relevant_item_count": 12, "distinct_story_count": 12, "negative_share": 0.05, "severe_event_count": 0,
            "unique_author_count": 8, "snapshot_age_minutes": 60.0, "collection_partial": 0, "activity_change": None}
    return {**base, **kw}


GUESS = {"recommendation": "POST", "probabilities": {"POST": 0.8, "WAIT": 0.15, "HOLD": 0.05}}


class DecisionTests(unittest.TestCase):
    def _decide(self, rules, feats=None, mode="shadow", runs=None, guess=GUESS, predict_error=None, floor=None,
                ranges=None, metadata=None):
        runs = runs if runs is not None else {"SHADOW": {"_id": "m1", "status": "SHADOW", "feature_schema_version": "v1"}}

        async def registered(status):
            return runs.get(status)

        env = {"PUBLIC_MOOD_MODEL_MODE": mode, "PUBLIC_MOOD_MODEL_MIN_CONFIDENCE": str(floor or 0)}
        meta = {"class_counts": {"POST": 60, "WAIT": 60, "HOLD": 60}, "feature_ranges": ranges or {}}
        with patch.dict("os.environ", env), patch.object(mood_model, "registered", registered), \
                patch.object(mood_model, "load_metadata", return_value=metadata if metadata is not None else meta), \
                patch.object(mood_model, "explain", return_value=[]), \
                patch.object(mood_model, "predict", side_effect=predict_error, return_value=guess):
            return run(mood_model.decide(rules, feats or _features()))

    PROMOTED = {"PROMOTED": {"_id": "m2", "status": "PROMOTED", "feature_schema_version": "v1"}}

    def test_with_no_model_the_rules_decide(self):
        d = self._decide("WAIT", runs={})
        self.assertEqual((d.final, d.source, d.shadow), ("WAIT", "RULES", None))

    def test_off_ignores_any_model(self):
        self.assertEqual(self._decide("WAIT", mode="off", runs=self.PROMOTED).source, "RULES")

    def test_shadow_computes_the_models_answer_but_the_rules_decide(self):
        d = self._decide("WAIT", mode="shadow")
        self.assertEqual((d.final, d.source), ("WAIT", "RULES"))
        self.assertEqual(d.shadow["recommendation"], "POST")

    def test_a_promoted_model_decides_only_in_assist_mode(self):
        self.assertEqual(self._decide("WAIT", mode="shadow", runs=self.PROMOTED).final, "WAIT")
        d = self._decide("WAIT", mode="assist", runs=self.PROMOTED)
        self.assertEqual((d.final, d.source), ("POST", "MODEL"))

    def test_a_shadow_only_model_never_decides_even_in_assist_mode(self):
        self.assertEqual(self._decide("WAIT", mode="assist").source, "RULES")

    def test_a_rules_hold_stands_over_the_model(self):
        d = self._decide("HOLD", mode="assist", runs=self.PROMOTED)
        self.assertEqual((d.final, d.source, d.guards), ("HOLD", "MODEL", ["RULES_HOLD_STANDS"]))

    def test_missing_or_stale_evidence_caps_the_model_at_wait(self):
        for feats, why in ((_features(distinct_story_count=0), "no verified"), (_features(snapshot_age_minutes=None), "unknown"),
                           (_features(snapshot_age_minutes=60 * 72), "72 hours")):
            d = self._decide("WAIT", feats, mode="assist", runs=self.PROMOTED)
            self.assertEqual(d.final, "WAIT", feats)
            self.assertIn("INSUFFICIENT_EVIDENCE_CAPS_AT_WAIT", d.guards)
            self.assertIn(why, d.note)

    def test_the_model_can_be_more_cautious_than_the_rules_without_any_guard(self):
        d = self._decide("POST", mode="assist", runs=self.PROMOTED, guess={"recommendation": "HOLD", "probabilities": {"POST": .1, "WAIT": .1, "HOLD": .8}})
        self.assertEqual((d.final, d.guards), ("HOLD", []))

    def test_a_model_for_another_feature_schema_is_not_used_and_the_decision_says_so(self):
        runs = {"PROMOTED": {"_id": "old", "status": "PROMOTED", "feature_schema_version": "v0"}}
        d = self._decide("WAIT", mode="assist", runs=runs)
        self.assertEqual((d.final, d.source), ("WAIT", "RULES_FALLBACK"))
        self.assertIn("feature schema", d.note)

    def test_if_the_model_cannot_run_the_rules_decide_and_say_so(self):
        d = self._decide("WAIT", mode="assist", runs=self.PROMOTED, predict_error=RuntimeError("corrupt model file"))
        self.assertEqual((d.final, d.source), ("WAIT", "RULES_FALLBACK"))
        self.assertIn("couldn't run", d.note)
        self.assertEqual(d.shadow["status"], "ERROR")

    def test_a_hesitant_model_defers_to_the_rules_when_a_confidence_floor_is_set(self):
        shaky = {"recommendation": "POST", "probabilities": {"POST": 0.4, "WAIT": 0.35, "HOLD": 0.25}}
        d = self._decide("WAIT", mode="assist", runs=self.PROMOTED, guess=shaky, floor=0.6)
        self.assertEqual((d.final, d.source), ("WAIT", "RULES_FALLBACK"))

    def test_the_registry_being_down_is_just_the_rules(self):
        async def boom(status):
            raise RuntimeError("atlas down")
        with patch.dict("os.environ", {"PUBLIC_MOOD_MODEL_MODE": "assist"}), patch.object(mood_model, "registered", boom):
            d = run(mood_model.decide("HOLD", _features()))
        self.assertEqual((d.final, d.source), ("HOLD", "RULES_FALLBACK"))

    def test_the_decision_is_saved_in_full_for_later_review(self):
        rec = self._decide("WAIT").as_record()
        self.assertEqual((rec["source"], rec["final"], rec["mode"], rec["shadow"]["model_version"]), ("RULES", "WAIT", "shadow", "m1"))


if __name__ == "__main__":
    unittest.main()
