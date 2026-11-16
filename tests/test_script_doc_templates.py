import unittest

from services.compliance.evaluation.script_doc_templates import shared_script_html, text_to_html

SHARED_TEXT = (
    "Storyline: Operator's receipt\n"
    "Hook score: 80.8 (strong)\n"
    "Duration: about 35 seconds\n"
    "Lane: Green lane\n"
    "[0:00] Visual: Laptop | Voiceover: Hello there. | On-screen: HELLO\n"
    "[0:04] Visual: Office | Voiceover: Second | On-screen: "
)


class SharedScriptHtmlTests(unittest.TestCase):
    def test_beats_read_back_from_the_shared_editor_are_drawn_as_the_reels_table(self):
        html = shared_script_html(1, "A script", SHARED_TEXT)
        self.assertIn("<h2>1. A script</h2>", html)
        self.assertIn("<th><p>Visual / footage</p></th>", html)
        self.assertEqual(html.count("<tr>"), 3)  # the header and two beats
        self.assertIn("<td><p>Hello there.</p></td>", html)
        self.assertIn("<p><strong>Storyline:</strong> Operator's receipt</p>", html)
        self.assertIn("<p>Hook score 80.8 (strong) | about 35 seconds | Green lane</p>", html)

    def test_shared_script_without_beats_still_gets_the_empty_reels_table(self):
        html = shared_script_html(2, "Empty", "Duration: about 35 seconds\nLane: Green lane")
        self.assertIn("<h2>2. Empty</h2>", html)
        self.assertEqual(html.count("<tr>"), 6)  # the header and 5 empty beat rows

    def test_text_without_beats_keeps_its_own_layout(self):
        html = text_to_html("T", "Duration: about 35 seconds\nLane: Green lane")
        self.assertNotIn("<table>", html)
        self.assertIn("<p>Duration: about 35 seconds</p>", html)


if __name__ == "__main__":
    unittest.main()
