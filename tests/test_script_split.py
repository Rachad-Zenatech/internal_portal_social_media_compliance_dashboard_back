import unittest

from services.compliance.evaluation.script_split import split_scripts


class SplitScriptsTests(unittest.TestCase):
    def test_numbered_markers_split_the_scripts_and_keep_their_titles(self):
        text = (
            "Script 1: Launch day\nOpen on the hangar doors.\nWe are live.\n\n"
            "Script 2: The team\nMeet the pilots.\n\n"
            "Script 3\nShort teaser."
        )
        self.assertEqual(split_scripts(text), [
            ("Launch day", "Open on the hangar doors.\nWe are live."),
            ("The team", "Meet the pilots."),
            ("Script 3", "Short teaser."),
        ])

    def test_marker_styles_all_count(self):
        text = "Video #1 - First\nBody one.\nReel 2) Second\nBody two.\n# Post 3: Third\nBody three."
        self.assertEqual([t for t, _ in split_scripts(text)], ["First", "Second", "Third"])

    def test_divider_lines_split_the_scripts_and_the_first_line_is_the_title(self):
        text = "Launch day\nWe are live.\n---\nThe team\nMeet the pilots.\n---\nTeaser\nComing soon."
        self.assertEqual(split_scripts(text), [
            ("Launch day", "We are live."), ("The team", "Meet the pilots."), ("Teaser", "Coming soon."),
        ])

    def test_markdown_headings_split_the_scripts(self):
        text = "# Launch day\nWe are live.\n\n# The team\nMeet the pilots."
        self.assertEqual(split_scripts(text), [("Launch day", "We are live."), ("The team", "Meet the pilots.")])

    def test_title_lines_split_the_scripts(self):
        text = "Title: Launch day\nWe are live.\nTitle: The team\nMeet the pilots."
        self.assertEqual([t for t, _ in split_scripts(text)], ["Launch day", "The team"])

    def test_markers_win_over_dividers(self):
        text = "Script 1: A\nOne\n---\nstill one\nScript 2: B\nTwo"
        result = split_scripts(text)
        self.assertEqual([t for t, _ in result], ["A", "B"])
        self.assertIn("still one", result[0][1])

    def test_a_document_with_nothing_to_split_on_is_one_script(self):
        result = split_scripts("Just one script.\nWith two lines.")
        self.assertEqual(result, [("Script 1", "Just one script.\nWith two lines.")])

    def test_a_single_marker_is_not_a_split(self):
        self.assertEqual(len(split_scripts("Script 1: Only\nBody text here.")), 1)

    def test_a_title_page_is_not_a_script_but_a_long_intro_is(self):
        short = "Q4 scripts\nScript 1: A\nOne\nScript 2: B\nTwo"
        self.assertEqual([t for t, _ in split_scripts(short)], ["A", "B"])
        intro = "Opening script\n" + "Welcome to the new facility. " * 10 + "\nScript 1: A\nOne\nScript 2: B\nTwo"
        self.assertEqual(len(split_scripts(intro)), 3)

    def test_empty_chunks_are_dropped_and_line_endings_do_not_matter(self):
        text = "Script 1: A\r\nOne\r\n---\r\n\r\n---\r\nScript 2: B\r\nTwo"
        self.assertEqual([t for t, _ in split_scripts(text)], ["A", "B"])

    def test_nothing_in_nothing_out(self):
        self.assertEqual(split_scripts(""), [])
        self.assertEqual(split_scripts("  \n \n"), [])


if __name__ == "__main__":
    unittest.main()
