"""Public template + private layer (control/layered.py, tick.read_rules). Run: python -m unittest control/test_layered.py"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import instance_config  # noqa: E402
import layered  # noqa: E402
import rules_split_compare as rsc  # noqa: E402
import tick  # noqa: E402

ROLES = layered.CONTROL / "roles"


class Layered(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.layer = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def put(self, name, text):
        (self.layer / "roles").mkdir(exist_ok=True)
        (self.layer / "roles" / name).write_text(text, encoding="utf-8")

    def test_layer_missing_uses_the_template(self):
        self.assertEqual(layered.read_text(ROLES / "work.md", self.layer), (ROLES / "work.md").read_text(encoding="utf-8"))
        self.assertEqual(layered.resolve(ROLES / "work.md", self.layer), ROLES / "work.md")

    def test_layer_present_overrides_the_whole_file(self):
        self.put("work.md", "private work\n")
        self.assertEqual(layered.role_text("work", self.layer), "private work\n")
        self.assertEqual(layered.role_text("quant", self.layer), (ROLES / "quant.md").read_text(encoding="utf-8"))  # other files unaffected

    def test_file_outside_control_is_read_as_is(self):
        other = self.layer / "rules.md"
        other.write_text("x", encoding="utf-8")
        self.assertEqual(layered.read_text(other, self.layer), "x")

    def test_tick_assembles_rules_from_the_layer(self):
        self.put("tick-rules.md", "PRIVATE RULES")
        template = ROLES / "tick-rules.md"  # tick.RULES_FILE is a Windows path under WSL; the code only looks at its folder names
        with mock.patch.object(tick, "RULES_FILE", template), mock.patch.object(layered, "private_dir", return_value=self.layer):
            self.assertEqual(tick.read_rules(), "PRIVATE RULES")
        with mock.patch.object(tick, "RULES_FILE", template), mock.patch.object(layered, "private_dir", return_value=self.layer / "nothing"):
            self.assertEqual(tick.read_rules(), template.read_text(encoding="utf-8"))

    def test_tick_rules_file_outside_control_is_unchanged(self):
        f = self.layer / "rules.md"
        f.write_text("temp rules", encoding="utf-8")
        with mock.patch.object(tick, "RULES_FILE", f):
            self.assertEqual(tick.read_rules(), "temp rules")

    def test_default_location_is_in_backup_scope_and_follows_the_instance_file(self):
        v = instance_config.load(self.layer / "none.json")  # no instance file = defaults
        self.assertTrue(v["private_layer_dir"].endswith(r"incore\data\config-sync\private-layer"))
        custom = self.layer / "i.json"
        custom.write_text('{"private_layer_dir": "D:\\\\x"}', encoding="utf-8")
        self.assertEqual(instance_config.load(custom)["private_layer_dir"], "D:\\x")

    def test_compare_flags_a_changed_layer_file(self):
        self.put("work.md", "changed")
        self.put("tick-rules.md", "changed")
        with mock.patch.object(rsc, "before", lambda role: layered.role_text("tick-rules", self.layer.parent) + "\n\n" + role):  # no git repo in an exported copy (stack_trial)
            rows = {r: (b, a) for r, b, a in rsc.compare(self.layer)}
        self.assertNotEqual(*rows["work"])


if __name__ == "__main__":
    unittest.main()
