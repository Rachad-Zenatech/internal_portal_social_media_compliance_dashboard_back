"""What is stored for a past post, and what a new post is compared on, must be the same set of text."""

import json
import unittest

from services.compliance.submissions import review_memory_service as rm


def _row(**over):
    base = {
        "submission_id": "11111111-1111-1111-1111-111111111111",
        "version_number": 1,
        "reference_code": "SUB-1",
        "decision": "APPROVE",
        "decision_notes": "ok",
        "decided_at": None,
        "decided_by_name": "Sahil",
        "snapshot": json.dumps({
            "title": "Arizona expansion",
            "caption_text": "Meet us in Mesa.",
            "script_text": "Welcome to the new facility.",
            "brand_code": "ZENADRONE",
            "platforms": ["INSTAGRAM"],
            "content_type": "IMAGE",
        }),
        "transcript": json.dumps([{"text": "Hello"}, {"text": "world"}]),
        "image_text": "ARIZONA EXPANSION Our next major hub",
        "comments": "[]",
        "dismissed_flags": "[]",
        "ratings": "[]",
    }
    base.update(over)
    return base


class StoredPostText(unittest.TestCase):
    def test_a_photo_posts_stored_text_includes_the_text_read_from_its_photos(self):
        doc = rm._doc(_row())
        self.assertIn("ARIZONA EXPANSION Our next major hub", doc.text)

    def test_the_script_and_video_speech_are_stored_too(self):
        doc = rm._doc(_row())
        self.assertIn("Welcome to the new facility.", doc.text)
        self.assertIn("Hello world", doc.text)
        self.assertIn("Meet us in Mesa.", doc.text)

    def test_the_parts_are_in_a_fixed_order(self):
        doc = rm._doc(_row())
        order = [doc.text.index(p) for p in ("Meet us in Mesa.", "Hello world", "Welcome to the new facility.", "ARIZONA EXPANSION")]
        self.assertEqual(order, sorted(order))

    def test_a_post_with_only_photo_text_is_still_stored(self):
        snap = json.dumps({"title": "", "caption_text": "", "script_text": None})
        doc = rm._doc(_row(snapshot=snap, transcript="[]"))
        self.assertIsNotNone(doc)
        self.assertIn("ARIZONA", doc.text)

    def test_a_post_with_nothing_to_match_is_not_stored(self):
        snap = json.dumps({"title": "", "caption_text": ""})
        self.assertIsNone(rm._doc(_row(snapshot=snap, transcript="[]", image_text=None)))

    def test_long_text_is_cut_at_the_limit_not_before(self):
        long_script = "word " * 5000
        doc = rm._doc(_row(snapshot=json.dumps({"title": "T", "caption_text": "c", "script_text": long_script})))
        self.assertLessEqual(len(doc.text), rm.TEXT_CHARS)
        self.assertGreater(len(doc.text), 4000)


class ComparedPostText(unittest.TestCase):
    def test_a_new_post_is_compared_on_the_same_parts(self):
        inp = {
            "title": "Arizona expansion",
            "caption": "Meet us in Mesa.",
            "transcript": [{"text": "Hello"}, {"text": "world"}],
            "script": "Welcome to the new facility.",
            "image_text": "ARIZONA EXPANSION Our next major hub",
        }
        stored = rm._doc(_row())
        self.assertEqual(rm.query_text(inp), f"Arizona expansion\n{stored.text}")

    def test_a_post_without_photos_or_script_matches_as_before(self):
        inp = {"title": "T", "caption": "Caption", "transcript": [{"text": "Spoken"}]}
        self.assertEqual(rm.query_text(inp), "T\nCaption\nSpoken")


if __name__ == "__main__":
    unittest.main()
