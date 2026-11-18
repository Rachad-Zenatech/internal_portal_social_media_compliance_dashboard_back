"""Imported script content must retain every line and table beat, including long uploads."""
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
shared = types.ModuleType("shared")
# Execute just the existing pure parser with its constants; no database or service configuration is loaded.
source = (ROOT / "services/compliance/evaluation/shared_script_service.py").read_text(encoding="utf-8")
start = source.index("def text_to_beats(")
end = source.index("# --------------------------------------------------------------------------- making", start)
namespace = dict(re=re, MAX_BEATS=80, _clean=lambda s: s.strip(), _FACTS=re.compile(r"^hook score\s*([^|]*)\|\s*([^|]*)\|\s*(.*)$", re.I))
from typing import Tuple, Dict, List
namespace.update(Tuple=Tuple, Dict=Dict, List=List)
exec(source[start:end], namespace)
shared.text_to_beats = namespace["text_to_beats"]
with patch.dict(sys.modules, {"services.compliance.evaluation.shared_script_service": shared}):
    spec = importlib.util.spec_from_file_location("content", ROOT / "services/compliance/evaluation/script_submission_content.py")
    content = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(content)


class ContentTests(unittest.TestCase):
    def test_first_spoken_line_is_never_dropped(self):
        _, rows = content.submission_beats("abc", "First spoken line\nSecond spoken line")
        self.assertEqual([r["voiceover"] for r in rows], ["First spoken line", "Second spoken line"])

    def test_title_is_not_duplicated_as_voiceover(self):
        _, rows = content.submission_beats("abc", "# abc\nFirst spoken line")
        self.assertEqual([r["voiceover"] for r in rows], ["First spoken line"])

    def test_table_and_surrounding_directions_are_preserved(self):
        body = "Storyline: History\nOpening direction\n| Time | Visual | Voiceover | On-screen |\n| --- | --- | --- | --- |\n| 0:00 | Bird | Spoken | TEXT |\nClosing direction"
        details, rows = content.submission_beats("abc", body)
        self.assertEqual(details["Storyline"], "History")
        self.assertEqual([r["voiceover"] for r in rows], ["Opening direction", "Spoken", "Closing direction"])

    def test_extra_table_columns_are_not_discarded(self):
        line = "| 0:00 | Visual | Voiceover | Caption | Extra direction |"
        _, rows = content.submission_beats("abc", line)
        self.assertEqual(rows[0]["voiceover"], line)

    def test_more_than_eighty_beats_are_retained(self):
        _, rows = content.submission_beats("abc", "\n".join(f"Line {i}" for i in range(125)))
        self.assertEqual(len(rows), 125)
        self.assertEqual(rows[-1]["voiceover"], "Line 124")


if __name__ == "__main__":
    unittest.main(verbosity=2)
