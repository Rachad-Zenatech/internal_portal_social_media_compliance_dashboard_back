"""Frozen inputs, independent run/finding identities and async rule-capture isolation."""

import asyncio
import unittest
from types import SimpleNamespace
from uuid import uuid4

from models.compliance.compliance_model import ComplianceCheckRequest
from services.compliance.evaluation import check_rule_capture
from services.compliance.submissions import check_snapshot_service as snapshots


def example():
    post = {"id": uuid4(), "current_version": 0, "title": "Original", "caption_text": "Original wording",
            "brand_code": "ZENADRONE", "platforms": ["LINKEDIN"], "connection_secret": "exclude"}
    request = ComplianceCheckRequest(text="Original wording")
    rule_id = str(uuid4())
    check = {"score": 75, "rulebook_version": "rule-revision-1", "violations": [
        {"rule_id": rule_id, "rule_number": "R1", "penalty": 25, "flagged_terms": ["wording"],
         "evaluation": {"confidence": 0.8, "decision": "VIOLATION"}},
    ]}
    captured = {"rules": {rule_id: {"id": rule_id, "prohibited_terms": ["wording"]}},
                "request": request.model_dump(mode="json")}
    return post, request, captured, check


class SnapshotCaptureTests(unittest.TestCase):
    def test_inputs_are_detached_before_evaluation(self):
        row, request, _, _ = example()
        media = SimpleNamespace(video=None, images=[{"id": "image-1", "image_text": "Original image text"}],
                                documents=[], script=None, segments=[], visual_segments=[], video_scored=False)
        post, evaluated = snapshots.inputs(row, request, material=media)
        row["title"] = "Edited"
        row["platforms"].append("FACEBOOK")
        request.text = "Edited wording"
        media.images[0]["image_text"] = "Edited image"
        self.assertEqual(post["title"], "Original")
        self.assertEqual(post["platforms"], ["LINKEDIN"])
        self.assertNotIn("connection_secret", post)
        self.assertEqual(evaluated["request"]["text"], "Original wording")
        self.assertEqual(evaluated["media"]["images"][0]["image_text"], "Original image text")

    def test_reruns_have_distinct_ids_and_preserve_score_rules_and_confidence(self):
        row, request, captured, check = example()
        post, evaluated = snapshots.inputs(row, request)
        first = snapshots.prepare(post, evaluated, captured, check)
        second = snapshots.prepare(post, evaluated, captured, check)
        self.assertNotEqual(first[0], second[0])
        self.assertNotEqual(first[3][0][0], second[3][0][0])
        self.assertEqual(first[1], second[1])
        self.assertNotIn("check_snapshot_id", check)
        self.assertEqual(first[2]["score"], 75)
        captured["rules"][check["violations"][0]["rule_id"]]["prohibited_terms"].append("edited")
        # The snapshot payload owns its rule definitions, too.
        self.assertEqual(first[3][0][3]["definition"]["prohibited_terms"], ["wording"])
        self.assertEqual(first[3][0][4]["evaluation"]["confidence"], 0.8)

    def test_each_occurrence_of_one_rule_gets_a_separate_finding_id(self):
        row, request, captured, check = example()
        check["violations"].append({**check["violations"][0], "passage": "A separate finding"})
        post, evaluated = snapshots.inputs(row, request)
        _, _, saved, findings, _ = snapshots.prepare(post, evaluated, captured, check)
        self.assertEqual(len({f[0] for f in findings}), 2)
        self.assertEqual([f[1] for f in findings], [0, 1])
        self.assertTrue(all(v["check_snapshot_id"] == saved["check_snapshot_id"] for v in saved["violations"]))

    def test_builtin_and_zero_finding_checks_are_preserved(self):
        row, request, _, check = example()
        post, evaluated = snapshots.inputs(row, request, script="Original script")
        saved = snapshots.prepare(post, evaluated, {}, check)
        self.assertEqual(saved[3][0][3]["source"], "BUILTIN_OR_DERIVED")
        self.assertEqual(saved[4]["script_body"], "Original script")
        check["violations"] = []
        check["score"] = 100
        saved = snapshots.prepare(post, evaluated, {}, check)
        self.assertEqual(saved[3], [])
        self.assertEqual(saved[2]["score"], 100)


class RuleCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_checks_and_failures_do_not_share_rules(self):
        async def run(text):
            request = ComplianceCheckRequest(text=text)
            rule = SimpleNamespace(id=text, model_dump=lambda **kwargs: {"title": text})
            with check_rule_capture.collecting() as captured:
                await asyncio.sleep(0)
                check_rule_capture.remember([rule], request)
                await asyncio.sleep(0)
                return captured

        first, second = await asyncio.gather(run("one"), run("two"))
        self.assertEqual(first["rules"], {"one": {"title": "one"}})
        self.assertEqual(second["rules"], {"two": {"title": "two"}})
        with self.assertRaises(RuntimeError):
            with check_rule_capture.collecting():
                raise RuntimeError("failed model")
        self.assertIsNone(check_rule_capture._rules.get())
