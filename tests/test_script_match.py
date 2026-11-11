import unittest
from types import SimpleNamespace

from services.compliance.evaluation import script_match

SCRIPT = (
    "[SCENE 1] Our drone flies for forty five minutes on a single charge. "
    "It was built for patrol teams who need to see further. "
    "Contact our team to learn more about availability."
)


class ScriptMatchTests(unittest.TestCase):
    def test_same_words_match(self):
        m = script_match.compare(SCRIPT, "Our drone flies for forty-five minutes on a single charge. It was built for patrol teams who need to see further. Contact our team to learn more about availability.")
        self.assertEqual(m["status"], "MATCH")
        self.assertEqual(m["added"], [])
        self.assertEqual(m["missing"], [])

    def test_small_transcription_noise_still_matches(self):
        m = script_match.compare(SCRIPT, "Our drone flies for forty five minutes on a single charge. It was built for the patrol teams who need to see further. Contact our team to learn more about availability")
        self.assertEqual(m["status"], "MATCH")

    def test_added_claim_is_listed(self):
        spoken = SCRIPT.replace("[SCENE 1]", "") + " It is guaranteed to never miss a target in any weather."
        m = script_match.compare(SCRIPT, spoken)
        self.assertTrue(any("never miss a target" in a for a in m["added"]))

    def test_dropped_passage_is_listed(self):
        m = script_match.compare(SCRIPT, "Our drone flies for forty five minutes on a single charge. Contact our team to learn more about availability.")
        self.assertTrue(any("patrol teams" in x for x in m["missing"]))
        self.assertNotEqual(m["status"], "MISMATCH")

    def test_different_video_is_a_mismatch(self):
        m = script_match.compare(SCRIPT, "Welcome back to the channel today we are unboxing the newest phone and testing the camera in low light.")
        self.assertEqual(m["status"], "MISMATCH")

    def test_nothing_to_compare(self):
        self.assertIsNone(script_match.compare("", "hello there"))
        self.assertIsNone(script_match.compare(SCRIPT, "   "))


class FlagMismatchTests(unittest.TestCase):
    def result(self, light="GREEN"):
        return SimpleNamespace(violations=[], review_count=0, is_compliant=True, traffic_light=light, recommendations=[])

    def test_a_mismatch_becomes_a_needs_review_flag_without_points(self):
        match = script_match.compare(SCRIPT, "Welcome back to the channel today we are unboxing the newest phone and testing the camera in low light.")
        r = self.result()
        script_match.flag_mismatch(r, match)
        self.assertEqual(len(r.violations), 1)
        v = r.violations[0]
        self.assertEqual((v.status, v.penalty, v.rule_id), ("NEEDS_REVIEW", 0, script_match.SCRIPT_MISMATCH_ID))
        self.assertEqual((r.review_count, r.is_compliant, r.traffic_light), (1, False, "YELLOW"))
        self.assertTrue(r.recommendations)

    def test_a_red_check_stays_red(self):
        match = script_match.compare(SCRIPT, "Welcome back to the channel today we are unboxing the newest phone and testing the camera in low light.")
        r = self.result("RED")
        script_match.flag_mismatch(r, match)
        self.assertEqual(r.traffic_light, "RED")

    def test_a_matching_or_partial_video_is_not_flagged(self):
        r = self.result()
        script_match.flag_mismatch(r, script_match.compare(SCRIPT, SCRIPT))
        script_match.flag_mismatch(r, {"status": "PARTIAL", "similarity": 80, "added": []})
        script_match.flag_mismatch(r, None)
        self.assertEqual((r.violations, r.review_count, r.is_compliant, r.traffic_light), ([], 0, True, "GREEN"))


if __name__ == "__main__":
    unittest.main()
