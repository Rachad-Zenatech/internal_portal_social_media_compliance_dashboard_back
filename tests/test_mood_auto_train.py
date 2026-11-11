"""Training starts by itself when a reviewer's decision changes, and only then; it never blocks, never creates empty
datasets, and never registers or promotes a model. Training uses a SYNTHETIC dataset made in the test."""

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from services.ai.core import mood_store
from services.compliance.analytics import conversation_feedback as fb
from services.compliance.analytics import mood_auto_train as at
from services.compliance.analytics import mood_model

from tests.fake_mongo import FakeMongo
from tests.test_mood_learning_pipeline import FakePool, _assessment, _decide, _seed, _synthetic, run


def _on(**extra):
    return patch.dict(os.environ, {"MOOD_AUTO_TRAIN": "on", **extra})


class AttemptTests(unittest.TestCase):
    """run_once: one attempt, and what it decides to do about what it finds."""

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

    def _datasets(self):
        return self.mongo.docs(mood_store.DATASETS)

    def test_with_nothing_reviewed_it_stops_and_creates_nothing(self):
        out = run(at.run_once("review"))
        self.assertEqual(out["outcome"], "SKIPPED")
        self.assertIn("not enough reviewed data", out["reason"])
        self.assertEqual((self._datasets(), self.mongo.docs(mood_store.EXAMPLES), self.mongo.docs(mood_store.MODEL_RUNS)), ([], [], []))

    def test_with_too_few_examples_it_says_what_is_missing_and_leaves_no_dataset_behind(self):
        a, rv = _synthetic(4)
        _seed(self.mongo, a, rv)
        out = run(at.run_once("review"))
        self.assertEqual(out["outcome"], "SKIPPED")
        self.assertIn("not enough reviewed examples yet", out["reason"])
        self.assertIn("needed", out["reason"])
        self.assertEqual((self._datasets(), self.mongo.docs(mood_store.MODEL_RUNS)), ([], []))  # an unready attempt creates nothing

    def test_without_xgboost_it_skips_and_says_so(self):
        with patch.object(mood_model, "is_xgboost_available", return_value=False):
            out = run(at.run_once("review"))
        self.assertEqual((out["outcome"], out["reason"]), ("SKIPPED", "xgboost isn't installed"))

    def test_with_enough_new_reviewed_data_it_trains_but_registers_and_promotes_nothing(self):
        a, rv = _synthetic(100)
        _seed(self.mongo, a, rv)
        out = run(at.run_once("review"))
        self.assertEqual((out["outcome"], out["run_status"]), ("TRAINED", "TRAINED"))
        self.assertRegex(out["dataset_version"], r"^auto_\d{8}T\d{6}$")
        (dataset,) = self._datasets()
        self.assertEqual((dataset["_id"], dataset["status"], dataset["example_count"]), (out["dataset_version"], "READY", 300))
        (model,) = self.mongo.docs(mood_store.MODEL_RUNS)
        self.assertEqual((model["_id"], model["status"]), (out["run_id"], "TRAINED"))  # not SHADOW, not PROMOTED
        self.assertIsNone(run(mood_model.registered("SHADOW")))
        self.assertIsNone(run(mood_model.registered("PROMOTED")))
        self.assertTrue(Path(model["model_path"]).exists())

    def test_the_same_reviewed_data_does_not_train_again(self):
        a, rv = _synthetic(100)
        _seed(self.mongo, a, rv)
        run(at.run_once("review"))
        again = run(at.run_once("review"))
        self.assertEqual(again["outcome"], "SKIPPED")
        self.assertIn("no new reviewed examples", again["reason"])
        self.assertEqual((len(self._datasets()), len(self.mongo.docs(mood_store.MODEL_RUNS))), (1, 1))

    def test_a_changed_or_added_decision_trains_again_on_a_new_fixed_dataset(self):
        a, rv = _synthetic(100)
        _seed(self.mongo, a, rv)
        first = run(at.run_once("review"))
        time.sleep(1.1)  # dataset versions carry the second they were made
        extra = _assessment(9999)
        _seed(self.mongo, [extra], [_decide(extra["_id"], "WAIT")])
        second = run(at.run_once("review"))
        self.assertEqual(second["outcome"], "TRAINED")
        self.assertNotEqual(second["dataset_version"], first["dataset_version"])
        self.assertEqual({d["_id"] for d in self._datasets()}, {first["dataset_version"], second["dataset_version"]})  # the first is untouched

    def test_a_withdrawn_decision_changes_the_data_and_so_trains_again(self):
        a, rv = _synthetic(100)
        _seed(self.mongo, a, rv)
        run(at.run_once("review"))
        time.sleep(1.1)
        self.mongo[mood_store.REVIEWS].docs[rv[0]["_id"]]["retracted_at"] = mood_store.now()
        self.assertEqual(run(at.run_once("review"))["outcome"], "TRAINED")

    def test_a_failure_is_recorded_not_raised(self):
        a, rv = _synthetic(100)
        _seed(self.mongo, a, rv)
        with patch.object(at.mood_training, "train", AsyncMock(side_effect=RuntimeError("out of memory"))):
            out = run(at.run_once("review"))
        self.assertEqual(out["outcome"], "FAILED")
        self.assertIn("out of memory", out["reason"])
        self.assertEqual(run(at.read_state())["last_attempt"]["outcome"], "FAILED")

    def test_every_attempt_is_recorded_and_shown_in_the_model_status(self):
        run(at.run_once("review"))
        last = run(at.read_state())["last_attempt"]
        self.assertEqual((last["trigger"], last["outcome"]), ("review", "SKIPPED"))
        with _on():
            status = run(mood_model.model_status())
        self.assertTrue(status["auto_training"]["enabled"])
        self.assertEqual(status["auto_training"]["last_attempt"]["outcome"], "SKIPPED")
        with patch.dict(os.environ, {"MOOD_AUTO_TRAIN": "off"}):
            self.assertFalse(run(mood_model.model_status())["auto_training"]["enabled"])


class TriggerTests(unittest.TestCase):
    """schedule: ask, never wait; one at a time; a burst becomes one run."""

    def setUp(self):
        self.mongo = FakeMongo()
        self.mongo.__enter__()
        self.addCleanup(self.mongo.__exit__, None, None, None)
        at._runner.update(task=None, pending=None, last_start=None)
        self.addCleanup(at._runner.update, task=None, pending=None, last_start=None)
        self.calls = []

    def _spawn(self, returncode=0, delay=0.15, error=None):
        def fake(trigger):
            self.calls.append((trigger, time.monotonic()))
            time.sleep(delay)
            if error:
                raise error
            return subprocess.CompletedProcess([], returncode, "", "boom" if returncode else "")
        return patch.object(at, "_spawn", fake)

    async def _wait_idle(self):
        while at._runner["task"] and not at._runner["task"].done():
            await asyncio.sleep(0.02)

    def _go(self, *schedules, **env):
        async def main():
            for t in schedules:
                at.schedule(t)
            await self._wait_idle()
        with _on(MOOD_AUTO_TRAIN_MIN_MINUTES=env.get("minutes", "0")):
            asyncio.run(main())

    def test_it_does_nothing_when_switched_off_or_there_is_no_vector_database(self):
        async def ask():
            at.schedule("review")
            return at._runner["task"]
        with self._spawn(), patch.dict(os.environ, {"MOOD_AUTO_TRAIN": "off"}):
            self.assertIsNone(asyncio.run(ask()))
        with self._spawn(), _on(), patch.object(mood_store, "is_enabled", return_value=False):
            self.assertIsNone(asyncio.run(ask()))
        self.assertEqual(self.calls, [])

    def test_asking_returns_at_once_and_training_runs_in_its_own_process(self):
        async def main():
            started = time.monotonic()
            at.schedule("review")
            asked_for = time.monotonic() - started
            await self._wait_idle()
            return asked_for
        with self._spawn(delay=0.4), _on(MOOD_AUTO_TRAIN_MIN_MINUTES="0"):
            asked_for = asyncio.run(main())
        self.assertLess(asked_for, 0.1)  # the caller wasn't held up by the 0.4s of training
        self.assertEqual([c[0] for c in self.calls], ["review"])

    def test_the_process_started_is_the_auto_train_command_from_the_repo_root(self):
        with patch.object(at.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as r:
            at._spawn("review")
        args, kwargs = r.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "scripts.mood_ml", "auto-train", "--trigger", "review"])
        self.assertEqual(kwargs["cwd"], str(at.ROOT_DIR))
        self.assertEqual(kwargs["timeout"], at.timeout_seconds())

    def _during_a_run(self, minutes="0", before=2, during=2):
        """Some reviews arrive before a run starts, some while it is running."""
        async def main():
            for _ in range(before):
                at.schedule("review")
            await asyncio.sleep(0.1)  # the run has started (the fake process takes 0.3s)
            for _ in range(during):
                at.schedule("review")
            await self._wait_idle()
        with _on(MOOD_AUTO_TRAIN_MIN_MINUTES=minutes):
            asyncio.run(main())

    def test_reviews_that_arrive_before_a_run_starts_are_folded_into_it(self):
        with self._spawn(delay=0.2):
            self._go("review", "review", "review", "review", "review")
        self.assertEqual(len(self.calls), 1)  # the run reads the data when it starts, so it already has all five

    def test_reviews_that_arrive_during_a_run_cause_exactly_one_more_never_one_each(self):
        with self._spawn(delay=0.3):
            self._during_a_run(before=2, during=3)
        self.assertEqual(len(self.calls), 2)

    def test_runs_are_kept_apart_by_the_minimum_interval(self):
        with self._spawn(delay=0.3):
            self._during_a_run(minutes="0.03", before=1, during=1)  # 1.8 seconds between starts
        self.assertEqual(len(self.calls), 2)
        self.assertGreaterEqual(self.calls[1][1] - self.calls[0][1], 1.7)

    def test_a_failing_training_process_is_recorded_and_never_reaches_the_caller(self):
        with self._spawn(returncode=1):
            self._go("review")
        last = run(at.read_state())["last_attempt"]
        self.assertEqual((last["outcome"], last["trigger"]), ("FAILED", "review"))
        self.assertIn("exited with 1", last["reason"])

    def test_a_training_process_that_takes_too_long_is_stopped_and_recorded(self):
        with self._spawn(error=subprocess.TimeoutExpired("x", 1)):
            self._go("review")
        self.assertIn("took longer than", run(at.read_state())["last_attempt"]["reason"])


class WhatStartsItTests(unittest.TestCase):
    """Reviews, and only reviews: a scrape and the corrections that aren't decisions start nothing."""

    def setUp(self):
        self.mongo = FakeMongo()
        self.mongo.__enter__()
        self.addCleanup(self.mongo.__exit__, None, None, None)
        self.pool = FakePool()
        self.a, self.s, self.u = uuid4(), uuid4(), uuid4()

        async def fetch_one(sql, *args):
            return {"rec": "WAIT"} if "recommendation" in sql else ({"ok": 1} if "assessment_items" in sql else {"id": self.a, "submission_id": self.s})
        self.asked = []
        for p in (patch.object(fb, "fetch_one", fetch_one), patch.object(fb, "get_pool", return_value=self.pool),
                  patch.object(at, "schedule", lambda trigger: self.asked.append(trigger))):
            p.start()
            self.addCleanup(p.stop)

    def test_every_way_a_decision_changes_asks_for_training(self):
        run(fb.correct_recommendation(self.s, self.a, self.u, "HOLD", "TOO_RISKY", None))     # a correction
        run(fb.confirm_recommendation(self.s, self.a, self.u))                                 # a confirmation
        run(fb.correct_recommendation(self.s, self.a, self.u, "WAIT", "OTHER", None, resolution=True))  # a lead's resolution
        run(fb.clear_correction(self.s, self.a, self.u))                                       # a withdrawal
        self.assertEqual(self.asked, ["review"] * 4)

    def test_corrections_to_matching_or_sentiment_do_not_because_they_are_not_decisions(self):
        item = uuid4()
        run(fb.mark_unrelated(self.s, self.a, item, self.u, "DIFFERENT_COMPANY", None))
        run(fb.restore_item(self.s, item))
        run(fb.mark_wrong_sentiment(self.s, self.a, item, self.u, "NEGATIVE", None))
        run(fb.clear_wrong_sentiment(self.s, self.a, item, self.u))
        self.assertEqual(self.asked, [])

    def test_a_scrape_does_not(self):
        os.environ.setdefault("JWT_SECRET", "test-only-secret-not-used-anywhere-else-0123456789")
        from tools.compliance.analytics import listening_router as lr
        with patch.object(lr, "_run_scraper_process", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(lr.svc, "record_conversations", AsyncMock()), patch.object(lr.svc, "list_topics", AsyncMock()), \
                patch.object(lr.svc, "list_trend_opportunities", AsyncMock()), patch.object(lr.svc, "index_conversations", AsyncMock()), \
                patch.object(lr, "_collection_summary", AsyncMock(return_value={})):
            run(lr._run_scrape_and_refresh())
        self.assertEqual(lr._scrape["state"], "finished")
        self.assertEqual(self.asked, [])
        self.assertFalse(hasattr(lr, "mood_auto_train"))  # the scrape code doesn't even know about it

    def test_the_only_places_that_ask_are_the_two_decision_paths(self):
        found = []
        root = Path(at.ROOT_DIR)
        for folder in ("services", "tools", "scripts", "mcp_tools"):
            for path in (root / folder).rglob("*.py"):
                if path.name != "mood_auto_train.py" and "mood_auto_train.schedule(" in path.read_text(encoding="utf-8"):
                    found.append((path.name, path.read_text(encoding="utf-8").count("mood_auto_train.schedule(")))
        self.assertEqual(found, [("conversation_feedback.py", 2)])


if __name__ == "__main__":
    unittest.main()
