import contextlib
import importlib.util
import io
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("make_screenshots", ROOT / "tools" / "make_screenshots.py")
shots = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shots)

NAMES = ("pace-line", "usage-gate", "rehearsal", "setup-status")


class Screenshots(unittest.TestCase):
    def test_the_images_are_well_formed_and_show_no_local_paths(self):
        with tempfile.TemporaryDirectory() as out, contextlib.redirect_stdout(io.StringIO()):
            shots.main(Path(out))
            for name in NAMES:
                text = (Path(out) / f"{name}.svg").read_text(encoding="utf-8")
                ET.fromstring(text)
                self.assertNotIn(tempfile.gettempdir(), text)
                self.assertNotIn(str(ROOT), text)

    def test_the_pace_chart_marks_both_open_and_closed_gate_decisions(self):
        text = shots.pace_chart()
        self.assertIn("#2e8b57", text)
        self.assertIn("#c0392b", text)


if __name__ == "__main__":
    unittest.main()
