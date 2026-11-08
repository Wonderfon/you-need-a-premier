import json
import tempfile
import unittest
from pathlib import Path

import instance_config as ic


class InstanceConfigTests(unittest.TestCase):
    def test_defaults_equal_this_machine(self):
        v = ic.load(Path("does-not-exist.json"))
        self.assertEqual(v["workspace"], r"C:\orghome\orgcore_workspace")
        self.assertEqual(v["wt_root"], r"C:\orghome\wt")
        self.assertEqual(v["claude_exe"], r"C:\orghome\.local\bin\claude.exe")
        self.assertEqual(v["pythonw_exe"], r"C:\orghome\AppData\Local\Programs\Python\Python312\pythonw.exe")
        self.assertEqual(v["wsl_home"], "/home/wsluser")
        self.assertEqual(v["sessions_dir"], r"C:\orghome\.claude\projects\C--orghome-orgcore-workspace")
        self.assertEqual(v["claude_projects"], r"C:\orghome\.claude\projects")
        self.assertEqual(ic.wsl(v["models_dir"]), "/mnt\x2fc/orghome/models")
        self.assertEqual(v["tools_dir"], r"C:\orghome\tools")

    def test_dashboard_labels_default_to_the_owner_display(self):
        v = ic.load(Path("does-not-exist.json"))
        self.assertEqual(v["coordinator_label"], "总理 · Claude Code（Opus 5.5）")
        self.assertEqual(v["role_names"]["coordinator"], "总理")
        self.assertEqual(self._load({"role_names": {"coordinator": "Chief"}})["role_names"], {"coordinator": "Chief"})

    def test_language_defaults_to_zh_here_and_is_set_by_the_instance_file(self):
        self.assertEqual(ic.load(Path("does-not-exist.json"))["language"], "zh")
        self.assertEqual(self._load({"language": "en"})["language"], "en")

    def test_secretary_card_defaults_to_the_owner_display(self):
        v = ic.load(Path("does-not-exist.json"))
        self.assertEqual((v["secretary_card"], v["secretary_model"]), (True, "gpt-6.1-sol"))
        self.assertFalse(self._load({"secretary_card": False})["secretary_card"])

    def _load(self, data, raw=None):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "i.json"
            p.write_text(raw if raw is not None else json.dumps(data), encoding="utf-8")
            return ic.load(p)

    def test_workspace_move_derives_the_rest(self):
        v = self._load({"user_home": r"D:\u", "workspace": r"D:\u\ws", "wsl_user": "bob"})
        self.assertEqual(v["wt_root"], r"D:\u\wt")
        self.assertEqual(v["sessions_dir"], r"D:\u\.claude\projects\D--u-ws")
        self.assertEqual(v["wsl_home"], "/home/bob")
        self.assertEqual(ic.wsl(v["workspace"]), "/mnt\x2fd/u/ws")

    def test_explicit_value_beats_derived(self):
        self.assertEqual(self._load({"wt_root": r"E:\trees"})["wt_root"], r"E:\trees")

    def test_unknown_key_and_bad_json_are_errors_underscore_keys_ignored(self):
        with self.assertRaises(ValueError):
            self._load({"workspce": "x"})
        with self.assertRaises(ValueError):
            self._load(None, raw="{nope")
        self._load({"_comment": "fine"})

    def test_example_file_loads_and_matches_defaults(self):
        ex = ic.CODE_ROOT / "instance" / "example" / "instance.json"
        self.assertEqual(ic.load(ex), ic.load(Path("does-not-exist.json")))

    def test_lines_and_projects_defaults_and_checks(self):
        v = ic.load(Path("does-not-exist.json"))
        self.assertEqual(v["lines"], {"coordinator": "组织线", "work": "Arbitros 线", "uvox": "Beacon Listener 线",
                                      "quant": "量化线", "intelligence": "情报线", "life": "生活线"})
        self.assertIsNone(v["projects"])
        for bad in ({"lines": {"boss": "A"}}, {"lines": {}}, {"lines": {"coordinator": "A", "work": "A"}}, {"lines": {"work": ""}},
                    {"projects": {"p": {"cwd": "x"}}}, {"projects": {"p": {"cwd": "x", "prefixes": []}}}, {"projects": []}):
            with self.assertRaises(ValueError, msg=bad):
                self._load(bad)

    def _probe(self, instance, code):
        """Run `code` in a fresh interpreter whose instance file is `instance` (tick and the input modules read it at import)."""
        import os, subprocess, sys
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "i.json"
            f.write_text(json.dumps(instance), encoding="utf-8")
            done = subprocess.run([sys.executable, "-c", "import sys, json, importlib.util\n"
                                   "def load(name, path):\n s = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(s); sys.modules[name] = m; s.loader.exec_module(m); return m\n"
                                   + code], capture_output=True, text=True, encoding="utf-8", timeout=120,
                                  env={**os.environ, "INCORE_INSTANCE": str(f), "PYTHONPATH": str(ic.CODE_ROOT / "control")})
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_own_lines_and_projects_reach_tick_the_board_and_the_todo_page(self):
        root = ic.CODE_ROOT
        out = self._probe({"lines": {"coordinator": "Organization", "work": "Maya's class"},
                           "projects": {"fractions": {"cwd": "projects\\fractions", "prefixes": ["Fractions"]}}},
                          f"import tick\nd = load('dash', r'{root}/input/dashboard.py')\np = load('pend', r'{root}/input/pending.py')\n"
                          "print(json.dumps({'ls': tick.LINE_SECTIONS, 'sr': tick.SECTION_ROLES, 'pj': sorted(tick.PROJECTS), 'po': tick.project_of('Fractions: x'),"
                          " 'pe': tick.project_of('Uvox: x'), 'ps': d.PLAN_SECTIONS, 'roles': d.ROLES, 'sec': p.SECTIONS, 'leg': p.LEGACY_TARGET, 'names': tick.ROLE_NAMES['work']}))")
        self.assertEqual(out["ls"]["coordinator"], "Organization")
        self.assertEqual(out["ls"]["work"], "Maya's class")
        self.assertTrue(out["ls"]["life"].startswith("（无此线"))  # a role without a line: a title no backlog has
        self.assertEqual(out["sr"]["Maya's class"], "work")
        self.assertEqual(out["pj"], ["fractions", "incore"])
        self.assertEqual((out["po"], out["pe"]), ("fractions", "general"))
        self.assertEqual(list(out["ps"].items()), [("Organization", "coordinator"), ("工作线", "work"), ("Maya's class", "work")])
        self.assertEqual(out["roles"], ["coordinator", "work"])
        self.assertEqual((out["sec"], out["leg"]), (["Organization", "Maya's class"], "Maya's class"))
        self.assertEqual(out["names"], "Arbitros 副总理")

    def test_without_the_keys_tick_keeps_this_machines_lines_and_projects(self):
        out = self._probe({}, "import tick\nprint(json.dumps({'n': len(tick.PROJECTS), 'ls': tick.LINE_SECTIONS}))")
        self.assertGreater(out["n"], 15)
        self.assertEqual(out["ls"]["work"], "Arbitros 线")


if __name__ == "__main__":
    unittest.main()
