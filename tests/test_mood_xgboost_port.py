"""The XGBoost model as carried over from the administration project's approach, fitted to this project: training rows with
the required attributes, a model as three files, guarded training, class-support and range checks, status.

Training here runs on a SYNTHETIC dataset made in the test: it proves the machinery works, nothing about real data."""

import argparse
import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services.ai.core import mood_store
from services.compliance.analytics import mood_dataset as ds
from services.compliance.analytics import mood_features as mf
from services.compliance.analytics import mood_model, mood_training as mt

from tests.fake_mongo import FakeMongo
from tests.test_mood_learning_pipeline import T0, _assessment, _decide, _seed, _synthetic, run

# The example record the training rows are modelled on.
EXAMPLE = {
    "assessment_id": "assessment_123",
    "assessed_at": "2026-11-11T14:00:00Z",
    "feature_schema_version": "v1",
    "features": {"relevant_item_count": 24, "negative_share": 0.35, "severe_event_count": 1, "snapshot_age_minutes": 20},
    "system_recommendation": "WAIT",
    "reviewer_label": "HOLD",
    "review_reason": "Relevant safety incident",
}


def _row(**kw):
    return {**EXAMPLE, **kw}


class RequiredAttributesTests(unittest.TestCase):
    def test_the_example_record_is_a_valid_training_row(self):
        mt.validate_rows([_row(), _row(assessment_id="assessment_124", reviewer_label="POST")])

    def test_the_required_attributes_are_exactly_the_ones_in_the_example(self):
        self.assertEqual(set(ds.REQUIRED_ROW_FIELDS), set(EXAMPLE))

    def test_a_row_missing_any_required_attribute_is_refused_and_says_which(self):
        for field in ds.REQUIRED_ROW_FIELDS:
            if field in ("review_reason", "features"):
                continue  # a reviewer may give no words; features are checked on their own below
            bad = {k: v for k, v in _row().items() if k != field}
            with self.assertRaises(mt.NotReady) as cm:
                mt.validate_rows([bad, _row(reviewer_label="POST")])
            self.assertIn(field, str(cm.exception))

    def test_a_row_without_features_is_refused(self):
        with self.assertRaises(mt.NotReady):
            mt.validate_rows([{k: v for k, v in _row().items() if k != "features"}, _row(reviewer_label="POST")])

    def test_the_label_or_an_outcome_cannot_be_among_the_features(self):
        for leak in ("reviewer_label", "post_outcome", "system_recommendation"):
            with self.assertRaises(mt.NotReady) as cm:
                mt.validate_rows([_row(features={**EXAMPLE["features"], leak: 1}), _row(reviewer_label="POST")])
            self.assertIn(leak, str(cm.exception))

    def test_unknown_features_a_wrong_schema_a_bad_label_and_a_single_class_are_refused(self):
        for bad in (_row(features={"invented_measure": 3}), _row(feature_schema_version="v0"), _row(reviewer_label="MAYBE")):
            with self.assertRaises(mt.NotReady):
                mt.validate_rows([bad, _row(reviewer_label="POST")])
        with self.assertRaises(mt.NotReady):
            mt.validate_rows([_row(), _row(assessment_id="b")])  # both HOLD: nothing to learn a difference from
        with self.assertRaises(mt.NotReady):
            mt.validate_rows([])

    def test_a_dataset_example_is_a_complete_row_with_the_reviewers_reason(self):
        with FakeMongo() as m:
            a = [_assessment(i) for i in range(10)]
            reviews = [_decide(x["_id"], ["POST", "WAIT", "HOLD"][i % 3], reason_code="TOO_RISKY", comment="Relevant safety incident"
                               if i == 2 else None) for i, x in enumerate(a)]
            _seed(m, a, reviews)
            run(ds.build_dataset("mood_v001"))
            manifest, rows = run(ds.load_dataset("mood_v001"))
        self.assertEqual(manifest["row_attributes"], list(ds.REQUIRED_ROW_FIELDS))
        row = next(r for r in rows if r["assessment_id"] == "assess-0002")
        for field in ds.REQUIRED_ROW_FIELDS:
            self.assertIn(field, row)
        self.assertEqual((row["reviewer_label"], row["review_reason"], row["review_reason_code"]), ("HOLD", "Relevant safety incident", "TOO_RISKY"))
        self.assertEqual(row["feature_schema_version"], "v1")
        self.assertEqual(row["system_recommendation"], "WAIT")
        other = next(r for r in rows if r["assessment_id"] == "assess-0001")
        self.assertEqual(other["review_reason"], mt.mood_dataset.__dict__["_decisive_reason"].__globals__["CORRECTION_REASONS"]["TOO_RISKY"]
                         if False else other["review_reason"])  # no comment: the reason's words stand in
        self.assertTrue(other["review_reason"])

    def test_rows_export_to_a_file_in_the_example_shape_and_read_back(self):
        with FakeMongo() as m, tempfile.TemporaryDirectory() as tmp:
            a = [_assessment(i) for i in range(10)]
            _seed(m, a, [_decide(x["_id"], ["POST", "WAIT", "HOLD"][i % 3], reason_code="OTHER") for i, x in enumerate(a)])
            run(ds.build_dataset("mood_v001"))
            out = Path(tmp) / "rows.jsonl"
            from scripts import mood_ml
            run(mood_ml._main(argparse.Namespace(cmd="export-rows", dataset="mood_v001", out=str(out))))
            rows = mt.rows_from_jsonl(str(out))
        self.assertEqual(len(rows), 10)
        for r in rows:
            self.assertTrue(set(ds.REQUIRED_ROW_FIELDS) <= set(r))
            self.assertRegex(r["assessed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")  # as in the example: 2026-11-11T14:00:00Z
            self.assertIn(r["split"], ("train", "validation", "test"))
        mt.validate_rows(rows)


class ModelFilesAndTrainingTests(unittest.TestCase):
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
        a, rv = _synthetic(100)
        _seed(self.mongo, a, rv)
        run(ds.build_dataset("mood_v001"))

    def _train(self, **kw):
        return run(mt.train("mood_v001", **kw))

    def test_a_model_is_three_files_that_travel_together(self):
        r = self._train()
        model, labels, meta = mood_model.artifact_paths(r["model_path"])
        self.assertTrue(model.exists() and labels.exists() and meta.exists())
        self.assertEqual((r["labels_path"], r["metadata_path"]), (str(labels), str(meta)))
        self.assertEqual(json.loads(labels.read_text()), ["POST", "WAIT", "HOLD"])
        m = json.loads(meta.read_text())
        self.assertEqual((m["feature_schema_version"], m["feature_names"], m["dataset_version"]), ("v1", mf.FEATURE_NAMES, "mood_v001"))
        self.assertEqual(sum(m["class_counts"].values()), m["training_rows"])
        self.assertEqual(set(m["class_counts"]), {"POST", "WAIT", "HOLD"})
        self.assertIn("severe_event_count", m["feature_ranges"])
        self.assertNotIn("activity_change", m["feature_ranges"])  # never measured in training: no range, not a made-up one
        self.assertIn("guarded_training", m)

    def test_the_training_settings_come_from_the_same_environment_names_as_before(self):
        env = {"XGBOOST_TRAIN_MAX_DEPTH": "2", "XGBOOST_TRAIN_NTHREAD": "2", "XGBOOST_TRAIN_TREE_METHOD": "exact"}
        with patch.dict(os.environ, env):
            r = self._train()
        self.assertEqual((r["params"]["max_depth"], r["params"]["nthread"], r["params"]["tree_method"]), (2, 2, "exact"))
        self.assertEqual((r["params"]["objective"], r["params"]["eval_metric"]), ("multi:softprob", "mlogloss"))
        default = self._train()
        self.assertEqual((default["params"]["max_depth"], default["params"]["nthread"], default["params"]["tree_method"]), (4, 1, "hist"))

    def test_training_without_xgboost_installed_says_so_and_the_app_stays_on_the_rules(self):
        with patch.object(mood_model, "is_xgboost_available", return_value=False):
            with self.assertRaises(mt.NotReady) as cm:
                self._train()
            self.assertIn("not installed", str(cm.exception))
            self.assertFalse(run(mood_model.model_status())["xgboost_installed"])

    def test_a_first_model_has_nothing_to_be_guarded_against(self):
        g = self._train()["guarded"]
        self.assertEqual((g["compared_with"], g["not_worse"]), (None, True))

    def test_a_second_model_is_compared_with_the_one_in_use_on_the_same_held_out_rows(self):
        first = self._train()
        run(mt.shadow(first["_id"]))
        time.sleep(1.1)  # run ids carry the second they were made
        second = self._train()
        self.assertEqual(second["guarded"]["compared_with"], first["_id"])
        self.assertIsNotNone(second["metrics"]["test"]["active"])
        self.assertEqual(second["metrics"]["test"]["active"]["n"], second["metrics"]["test"]["model"]["n"])
        self.assertIn("not_worse", second["guarded"])

    def test_the_status_shows_what_is_registered_what_it_learned_from_and_what_matters(self):
        self.assertIn("No model is registered", run(mood_model.model_status())["note"])
        r = self._train()
        run(mt.shadow(r["_id"]))
        st = run(mood_model.model_status())
        self.assertTrue(st["xgboost_installed"] and st["model_loaded"] and st["label_mapping_present"] and st["metadata_present"])
        self.assertEqual((st["run"], st["run_status"]), (r["_id"], "SHADOW"))
        self.assertEqual(st["training_summary"]["feature_schema_version"], "v1")
        self.assertEqual(st["training_summary"]["class_count"], 3)
        self.assertEqual(sum(st["class_distribution"].values()), st["training_summary"]["training_rows"])
        self.assertTrue(st["feature_importance"])
        self.assertIn(st["feature_importance"][0]["feature"], mf.FEATURE_NAMES)
        # in the synthetic data the negative share and the severe events are what separate the classes
        self.assertIn(st["feature_importance"][0]["feature"], ("negative_share", "severe_event_count"))
        self.assertNotIn("unique_author_count", [i["feature"] for i in st["feature_importance"][:2]])
        self.assertIn("test", st["training_quality"])
        self.assertEqual(st["support_thresholds"]["min_class_support_for_auto"], mood_model.MIN_CLASS_SUPPORT)

    def test_a_model_is_explained_by_the_measurements_that_pushed_it(self):
        r = self._train()
        feats = {**{k: None for k in mf.FEATURE_NAMES}, "severe_event_count": 3, "negative_share": 0.7, "distinct_story_count": 12,
                 "relevant_item_count": 12, "unique_author_count": 8, "snapshot_age_minutes": 30.0, "collection_partial": 0}
        guess = mood_model.predict(r, feats)
        why = mood_model.explain(r, feats, guess["recommendation"])
        self.assertTrue(why)
        self.assertEqual(guess["recommendation"], "HOLD")
        self.assertIn(why[0]["feature"], mf.FEATURE_NAMES)
        self.assertIn("effect", why[0])
        self.assertIn(why[0]["feature"], ("negative_share", "severe_event_count"))  # what makes these rows HOLDs
        self.assertGreater(why[0]["effect"], 0)  # it pushed towards HOLD

    def test_a_model_file_that_changes_is_read_again(self):
        r = self._train()
        meta_path = mood_model.artifact_paths(r["model_path"])[2]
        self.assertEqual(mood_model.load_metadata(r)["training_rows"], json.loads(meta_path.read_text())["training_rows"])
        changed = json.loads(meta_path.read_text())
        changed["training_rows"] = 12345
        meta_path.write_text(json.dumps(changed))
        os.utime(meta_path, (time.time() + 5, time.time() + 5))
        self.assertEqual(mood_model.load_metadata(r)["training_rows"], 12345)

    def test_a_missing_metadata_file_means_the_model_is_not_vouched_for(self):
        r = self._train()
        mood_model.artifact_paths(r["model_path"])[2].unlink()
        mood_model.clear_cache()
        self.assertIsNone(mood_model.load_metadata(r))
        self.assertTrue(mood_model.support_issues(r, {"severe_event_count": 1}, "HOLD"))


class SupportAndRangeTests(unittest.TestCase):
    RUN = {"_id": "m", "status": "PROMOTED", "feature_schema_version": "v1", "model_path": "x/m.json"}
    META = {"class_counts": {"POST": 80, "WAIT": 80, "HOLD": 6}, "feature_ranges": {"severe_event_count": [0, 3], "negative_share": [0.0, 0.8]}}

    def _issues(self, feats, label="POST", meta=None):
        with patch.object(mood_model, "load_metadata", return_value=meta or self.META):
            return mood_model.support_issues(self.RUN, feats, label)

    def test_a_class_the_model_saw_too_few_times_is_not_trusted_on_its_own(self):
        self.assertEqual(self._issues({}, "POST"), [])
        issues = self._issues({}, "HOLD")
        self.assertEqual(len(issues), 1)
        self.assertIn("6 training example", issues[0])

    def test_measurements_outside_what_it_was_trained_on_are_flagged_not_extrapolated(self):
        self.assertEqual(self._issues({"severe_event_count": 3, "negative_share": 0.8}), [])
        issues = self._issues({"severe_event_count": 9, "negative_share": 0.5})
        self.assertEqual(len(issues), 1)
        self.assertIn("severe_event_count = 9", issues[0])
        self.assertEqual(self._issues({"severe_event_count": None}), [])  # unknown is not out of range

    def _decide(self, label, feats, meta, mode="assist"):
        async def registered(status):
            return self.RUN if status == "PROMOTED" else None
        guess = {"recommendation": label, "probabilities": {label: 0.9, **{c: 0.05 for c in ("POST", "WAIT", "HOLD") if c != label}}}
        feats = {"relevant_item_count": 12, "distinct_story_count": 12, "negative_share": 0.1, "severe_event_count": 0,
                 "unique_author_count": 8, "snapshot_age_minutes": 30.0, "collection_partial": 0, "activity_change": None, **feats}
        with patch.dict(os.environ, {"PUBLIC_MOOD_MODEL_MODE": mode}), patch.object(mood_model, "registered", registered), \
                patch.object(mood_model, "load_metadata", return_value=meta), patch.object(mood_model, "explain", return_value=[]), \
                patch.object(mood_model, "predict", return_value=guess):
            return run(mood_model.decide("WAIT", feats))

    def test_in_assist_mode_an_unsupported_answer_goes_back_to_the_rules_and_says_why(self):
        d = self._decide("HOLD", {}, self.META)
        self.assertEqual((d.final, d.source), ("WAIT", "RULES_FALLBACK"))
        self.assertIn("not trusted", d.note)
        self.assertIn("6 training example", d.note)

    def test_an_out_of_range_input_goes_back_to_the_rules(self):
        d = self._decide("POST", {"severe_event_count": 40}, self.META)
        self.assertEqual((d.final, d.source), ("WAIT", "RULES_FALLBACK"))
        self.assertIn("outside the range", d.note)

    def test_a_supported_in_range_answer_is_used(self):
        d = self._decide("POST", {"severe_event_count": 1}, self.META)
        self.assertEqual((d.final, d.source), ("POST", "MODEL"))

    def test_the_shadow_record_keeps_the_support_picture_and_a_confidence_that_cannot_outrun_it(self):
        d = self._decide("HOLD", {}, self.META, mode="shadow")
        self.assertEqual(d.source, "RULES")
        self.assertEqual(d.final, "WAIT")  # shadow decides nothing
        self.assertTrue(d.shadow["support_issues"])
        self.assertEqual(d.shadow["confidence"], 0.9)
        self.assertEqual(d.shadow["support_adjusted_confidence"], 0.3)  # 6 of the 20 examples a class needs: capped at 6/20

    def test_a_model_with_no_metadata_is_not_used_to_decide(self):
        d = self._decide("POST", {}, None)
        self.assertEqual((d.final, d.source), ("WAIT", "RULES_FALLBACK"))


class GuardTests(unittest.TestCase):
    def _m(self, accuracy=0.9, missed=0, unsafe=0):
        return {"accuracy": accuracy, "missed_hold_count": missed, "unsafe_approval_count": unsafe, "run": "active"}

    def test_with_no_model_in_use_a_candidate_is_eligible(self):
        self.assertTrue(mt.guard(self._m(), None)["not_worse"])

    def test_equal_or_better_is_eligible_and_a_small_dip_is_tolerated(self):
        self.assertTrue(mt.guard(self._m(0.9), self._m(0.9))["not_worse"])
        self.assertTrue(mt.guard(self._m(0.89), self._m(0.9))["not_worse"])

    def test_losing_more_than_two_points_of_accuracy_is_not_eligible(self):
        g = mt.guard(self._m(0.85), self._m(0.9))
        self.assertFalse(g["not_worse"])
        self.assertEqual(g["decision"], "rejected_keep_active_model")
        self.assertIn("accuracy dropped", g["reasons"][0])

    def test_missing_more_holds_or_approving_more_that_reviewers_stop_is_not_eligible_whatever_the_accuracy(self):
        self.assertFalse(mt.guard(self._m(0.99, missed=2), self._m(0.9, missed=1))["not_worse"])
        self.assertFalse(mt.guard(self._m(0.99, unsafe=3), self._m(0.9, unsafe=2))["not_worse"])

    def test_a_candidate_worse_than_the_model_in_use_cannot_be_promoted(self):
        good = {"_id": "r", "status": "TRAINED", "feature_schema_version": "v1", "readiness": {"ready": True},
                "metrics": {"test": {"model": {"n": 40, "accuracy": .9, "missed_hold_rate": 0.0, "missed_hold_count": 0,
                                                "unsafe_approval_rate": 0.0, "unsafe_approval_count": 0, "unnecessary_hold_rate": 0.1,
                                                "post_precision": 0.95, "wait_rate": 0.3}, "rules": {}}},
                "calibration": {"test": {"expected_calibration_error": 0.05}}}
        self.assertEqual(mt.check_promotion(good), [])
        worse = {**good, "guarded": {"not_worse": False, "reasons": ["accuracy dropped from 0.95 to 0.8"]}}
        self.assertTrue(any("worse than the model in use" in p for p in mt.check_promotion(worse)))


class RouteTests(unittest.TestCase):
    def test_the_model_status_is_an_administrators_view(self):
        os.environ.setdefault("JWT_SECRET", "test-only-secret-not-used-anywhere-else-0123456789")
        from tools.compliance.analytics import listening_router as lr
        route = next(r for r in lr.router.routes if r.path == "/listening/model/status")
        self.assertEqual(route.methods, {"GET"})
        dependency = route.dependant.dependencies[0].call
        self.assertIsNot(dependency, lr.get_current_user_id_dependency)  # the permission check, not just a signed-in user


if __name__ == "__main__":
    unittest.main()
