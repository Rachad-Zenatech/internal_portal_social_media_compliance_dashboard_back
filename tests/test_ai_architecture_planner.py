"""The CI planner accepts ordinary UTF-8 and UTF-8 files with a BOM."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "ai_architecture_planner", Path(__file__).resolve().parents[1] / ".agents/ai_architecture_planner.py"
)
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


class ArchitecturePlannerEncodingTests(unittest.TestCase):
    def test_plans_python_symbols_with_or_without_bom(self):
        for encoding in ("utf-8", "utf-8-sig"):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "example.py"
                source.write_text("def example():\n    return 1\n", encoding=encoding)
                output = io.StringIO()
                with patch.object(planner, "ROOT", root), patch(
                    "sys.argv", ["planner", "--file", "example.py"]
                ), contextlib.redirect_stdout(output):
                    self.assertEqual(planner.main(), 0)
                plan = json.loads(output.getvalue())["plans"][0]
                self.assertEqual(plan["source_file"], "example.py")
                self.assertEqual(plan["modules"][0]["symbols"], ["example"])


if __name__ == "__main__":
    unittest.main()
