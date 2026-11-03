import unittest

from services.compliance import script_match

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


if __name__ == "__main__":
    unittest.main()
