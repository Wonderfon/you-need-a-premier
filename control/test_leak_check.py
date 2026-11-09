import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import leak_check as leak


class LeakCheck(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = {**leak.CONFIG, "max_hash_bytes": 32}
        for relative in self.config["watch"]:
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        self.path = self.root / "projects" / "文件.txt"
        self.path.write_text("before", encoding="utf-8")

    def run_code(self, source):
        return leak.check(self.root, [{"cwd": self.root, "argv": [sys.executable, "-c", source]}], self.config)

    def test_test_creates_a_file(self):
        report = self.run_code("from pathlib import Path; Path('projects/new.txt').write_text('new', encoding='utf-8')")
        self.assertEqual(report["changes"], {"created": ["projects/new.txt"], "modified": [], "deleted": []})
        self.assertEqual(report["exit_code"], 2)
        self.assertTrue((self.root / "projects/new.txt").exists())  # checker leaves evidence intact

    def test_same_size_and_mtime_change_is_detected_by_hash(self):
        report = self.run_code("from pathlib import Path; import os; p=next(Path('projects').iterdir()); s=p.stat(); "
                               "p.write_text('AFTER!', encoding='utf-8'); os.utime(p, ns=(s.st_atime_ns,s.st_mtime_ns))")
        self.assertEqual(report["changes"], {"created": [], "modified": ["projects/文件.txt"], "deleted": []})
        self.assertEqual(report["before"]["files"]["projects/文件.txt"]["sha256"], hashlib.sha256(b"before").hexdigest())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "AFTER!")

    def test_no_change(self):
        report = self.run_code("pass")
        self.assertEqual(report["changes"], {"created": [], "modified": [], "deleted": []})
        self.assertEqual(report["exit_code"], 0)
        self.assertFalse(report["summary"]["incomplete"])

    def test_missing_roots_are_listed_and_incomplete(self):
        watched = self.config["watch"]
        for index, missing in enumerate([(name,) for name in watched] + [watched]):
            with self.subTest(missing=missing):
                root = self.root / f"missing-{index}"
                root.mkdir()
                for relative in watched:
                    if relative not in missing:
                        (root / relative).mkdir(parents=True, exist_ok=True)
                report = leak.check(root, [], self.config)
                self.assertEqual(report["before"]["missing_roots"], list(missing))
                self.assertEqual(report["after"]["missing_roots"], list(missing))
                self.assertEqual(report["changes"], {"created": [], "modified": [], "deleted": []})
                self.assertTrue(report["summary"]["incomplete"])
                self.assertEqual(report["exit_code"], 2)

    def test_root_missing_only_before_or_after_is_incomplete(self):
        for source, before, after in (
            ("Path('audits').rmdir()", [], ["audits"]),
            ("Path('audits').mkdir()", ["audits"], []),
        ):
            with self.subTest(source=source):
                report = self.run_code("from pathlib import Path; " + source)
                self.assertEqual(report["before"]["missing_roots"], before)
                self.assertEqual(report["after"]["missing_roots"], after)
                self.assertTrue(report["summary"]["incomplete"])
                self.assertEqual(report["exit_code"], 2)

    def test_deleted_file_and_mtime_only_change(self):
        before = leak.snapshot(self.root, self.config)
        os.utime(self.path, ns=(self.path.stat().st_atime_ns, self.path.stat().st_mtime_ns + 2_000_000_000))
        touched = leak.snapshot(self.root, self.config)
        self.assertEqual(leak.compare(before, touched)["modified"], ["projects/文件.txt"])
        gone = {**touched, "files": {}}
        self.assertEqual(leak.compare(touched, gone)["deleted"], ["projects/文件.txt"])

    def test_ignored_paths_and_large_files_are_never_opened(self):
        for name in ["control/data/ticks/month/run.jsonl", "control/data/backlog-history/old.md", "data/backlog-history/old.md",
                     "data/backups/objects/ab/object", "data/backups/packages/old.zip", "data/backups/snapshots/old.json",
                     "data/.env", "input/data/credentials.json", "projects/large.bin"]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 33)
        original_open = Path.open
        opened = []

        def track(path, *args, **kwargs):
            opened.append(path.relative_to(self.root).as_posix())
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", track):
            report = self.run_code("pass")
        self.assertEqual(opened, ["projects/文件.txt"] * 2)
        self.assertEqual(set(report["before"]["files"]), {"projects/文件.txt", "projects/large.bin"})
        self.assertEqual(report["summary"]["before_unhashed"], 1)
        self.assertIn("control/data/ticks", report["ignore"])
        self.assertIn("lane", report["warning"])

    def test_raw_data_is_only_read_as_bytes(self):
        for name in ("input/data/raw.json", "data/poetry-collection/raw.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xffinvalid json\x00")
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("no decoding")), \
                mock.patch.object(json, "loads", side_effect=AssertionError("no parsing")):
            report = self.run_code("pass")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(len(report["before"]["files"]), 3)

    def test_unreadable_file_and_failed_command_do_not_report_clean(self):
        with mock.patch.object(Path, "open", side_effect=PermissionError):
            report = self.run_code("pass")
        self.assertTrue(report["summary"]["incomplete"])
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(self.run_code("raise SystemExit(7)")["exit_code"], 1)
        report = leak.check(self.root, [{"cwd": self.root, "argv": [str(self.root / "missing-python")]}], self.config)
        self.assertEqual(report["commands"][0]["error"], "FileNotFoundError")
        self.assertEqual(report["exit_code"], 1)

    def test_cli_outputs_report(self):
        # Temporary fake incore with a real unittest command and a watched write.
        control = self.root / "control"
        control.mkdir(exist_ok=True)
        (control / "test_fixture.py").write_text(
            "from pathlib import Path\nimport unittest\nclass Example(unittest.TestCase):\n"
            " def test_write(self):\n  Path('../projects/new.txt').write_text('new', encoding='utf-8')\n", encoding="utf-8")
        input_dir = self.root / "input"
        input_dir.mkdir(exist_ok=True)
        (input_dir / "__init__.py").write_text("VALUE = 3\n", encoding="utf-8")
        (input_dir / "test_fixture.py").write_text(
            "from . import VALUE\nimport unittest\nclass Example(unittest.TestCase):\n"
            " def test_relative_import(self):\n  self.assertEqual(VALUE, 3)\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(Path(leak.__file__).resolve()), "--root", str(self.root)],
                                capture_output=True, text=True, encoding="utf-8",
                                env={**os.environ, "PYTHONIOENCODING": "ascii"})
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["changes"]["created"], ["projects/new.txt"])
        self.assertEqual([r["returncode"] for r in report["commands"]], [0, 0], result.stderr)
        self.assertEqual(result.stderr.count("Ran 1 test"), 2)


if __name__ == "__main__":
    unittest.main()
