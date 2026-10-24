import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from orgcore import __main__ as cli
from orgcore import config

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo"


def load(name):
    return json.loads((DEMO / name).read_text(encoding="utf-8"))


class DemoInstance(unittest.TestCase):
    def test_demo_loads_and_is_consistent(self):
        instance = config.load_instance(DEMO)
        self.assertEqual(len(instance.org["lines"]), 2)
        self.assertEqual(sum(len(l["projects"]) for l in instance.org["lines"]), 3)
        self.assertEqual(instance.line_projects()["research"], {"demo-notes", "demo-garden"})
        self.assertFalse(instance.policy["live_enabled"])

    def test_example_file_matches_demo_org(self):
        self.assertEqual((ROOT / "examples" / "org.example.json").read_bytes(), (DEMO / "org.json").read_bytes())

    def test_check_command_passes_on_demo(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):  # keep the command's own line out of the test runner's output
            self.assertEqual(cli.main(["check", str(DEMO)]), 0)
        self.assertTrue(out.getvalue().startswith("OK: 2 line(s)"))


class Validation(unittest.TestCase):
    def errors(self, name, data):
        with self.assertRaises(config.ConfigError) as caught:
            config.validate(name, data)
        return caught.exception.errors

    def test_missing_field_names_the_field(self):
        data = load("org.json")
        del data["lines"][0]["projects"][1]["weight"]
        del data["owner_name"]
        found = "\n".join(self.errors("org.json", data))
        self.assertIn("owner_name: missing required field", found)
        self.assertIn("lines[0].projects[1].weight: missing required field", found)

    def test_wrong_types_ranges_and_unknown_fields(self):
        data = load("org.json")
        data["lines"][0]["weight"] = 0
        data["lines"][1]["projects"][0]["weight"] = "heavy"
        data["lines"][1]["id"] = "Has Spaces"
        data["extra"] = 1
        found = "\n".join(self.errors("org.json", data))
        for fragment in ("lines[0].weight: out of range", "lines[1].projects[0].weight: must be a number",
                         "lines[1].id: has an invalid format", "extra: unknown field"):
            self.assertIn(fragment, found)

    def test_booleans_are_not_numbers_and_duplicate_ids_fail(self):
        data = load("org.json")
        data["lines"][0]["weight"] = True
        self.assertIn("must be a number", "\n".join(self.errors("org.json", data)))
        data = load("org.json")
        data["lines"][1]["projects"][0]["id"] = "demo-notes"
        self.assertIn("duplicate id 'demo-notes'", "\n".join(self.errors("org.json", data)))

    def test_policy_rules_cannot_be_weakened_or_skipped(self):
        data = load("policy.json")
        data["spending"]["paid_apis_allowed"] = True
        data["hard_gates"]["deploy"] = False
        data["live_enabled"] = True
        found = "\n".join(self.errors("policy.json", data))
        self.assertIn("paid_apis_allowed: must be false", found)
        self.assertIn("hard_gates.deploy: must be true", found)
        data = load("policy.json")
        data["live_enabled"] = True
        self.assertIn("live_enabled needs live_consent", "\n".join(self.errors("policy.json", data)))
        data["live_consent"] = {"granted_at": "2026-01-05T12:00:00Z", "projects": ["demo-notes"], "task_scope": "one task"}
        config.validate("policy.json", data)

    def test_reporting_formats(self):
        data = load("reporting.json")
        data["report_times"] = ["9:00", "25:00"]
        data["delivery"]["active"] = "email"
        found = "\n".join(self.errors("reporting.json", data))
        self.assertIn("report_times[0]", found)
        self.assertIn("report_times[1]", found)
        self.assertIn("delivery.active: must be one of local", found)
        data = load("reporting.json")
        data["report_times"], data["quiet_hours"] = [], None  # explicit "none" is valid
        config.validate("reporting.json", data)

    def test_setup_accepted_block_needs_hash(self):
        data = load("state/setup.json")
        data["blocks"]["goal"]["status"] = "accepted"
        self.assertIn("blocks.goal: an accepted block needs its hash", "\n".join(self.errors("state/setup.json", data)))
        data["blocks"]["goal"]["hash"] = config.block_hash({"goal": "x"})
        config.validate("state/setup.json", data)


class Loading(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="orgcore-cfg-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        shutil.copytree(DEMO, self.dir / "inst")
        self.inst = self.dir / "inst"

    def test_no_instance_and_no_env_is_an_error_not_a_default(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.resolve_instance(None, environ={})
        self.assertIn("ORGCORE_INSTANCE", str(caught.exception))
        self.assertEqual(config.resolve_instance(None, environ={"ORGCORE_INSTANCE": str(self.inst)}), self.inst.resolve())

    def test_missing_file_and_bad_json_reported_together(self):
        (self.inst / "policy.json").unlink()
        (self.inst / "reporting.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(config.ConfigError) as caught:
            config.load_instance(self.inst)
        text = str(caught.exception)
        self.assertIn("policy.json: file not found", text)
        self.assertIn("reporting.json: not valid JSON", text)

    def test_setup_file_is_optional(self):
        (self.inst / "state" / "setup.json").unlink()
        self.assertIsNone(config.load_instance(self.inst).setup)

    def test_save_never_replaces_saved_answers_by_default(self):
        data = load("reporting.json")
        with self.assertRaises(FileExistsError):
            config.save_file(self.inst, "reporting.json", data)
        data["timezone"] = "UTC"
        config.save_file(self.inst, "reporting.json", data, overwrite=True)
        self.assertEqual(config.load_instance(self.inst).reporting["timezone"], "UTC")

    def test_invalid_save_leaves_file_untouched(self):
        before = (self.inst / "org.json").read_bytes()
        bad = copy.deepcopy(load("org.json"))
        bad["lines"] = []
        with self.assertRaises(config.ConfigError):
            config.save_file(self.inst, "org.json", bad, overwrite=True)
        self.assertEqual((self.inst / "org.json").read_bytes(), before)

    def test_check_command_reports_board_mismatch(self):
        (self.inst / "projects" / "backlog.md").write_text("## Line: research\n- [todo] ghost: x\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["check", str(self.inst)]), 1)
        self.assertIn("line 2: project 'ghost' is not in line 'research'", err.getvalue())


if __name__ == "__main__":
    unittest.main()
