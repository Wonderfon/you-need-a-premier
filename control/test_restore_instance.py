"""restore_instance: plan classifies, apply never overwrites and hash-checks, held-back stays out, bundle clones. Temp dirs only."""
import subprocess
import tempfile
import unittest
from pathlib import Path

import restore_instance as ri


def git(*args, cwd):
    subprocess.run([ri._git(), "-c", "safe.directory=*", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   cwd=cwd, check=True, capture_output=True)


class Restore(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t = Path(tmp.name)
        self.out, self.ws = t / "out", t / "ws"
        for rel, text in {"records/a.json": "{}", "projects/backlog.md": "# b\n", "data/owner-todo-ids.json": "{\"D1\": 1}",
                          "input/data/input.sqlite3": "db", "external/claude-memory/m.md": "mem",
                          "external/claude-home/CLAUDE.md": "c", "restore/backup.py": "x", "credentials.txt": "no"}.items():
            f = self.out / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        src = t / "src"
        src.mkdir()
        (src / "README.md").write_text("code", encoding="utf-8")
        (src / "records").mkdir()
        (src / "records" / "a.json").write_text("old committed copy", encoding="utf-8")
        git("init", "-q", "-b", "master", cwd=src)
        git("add", "README.md", "records/a.json", cwd=src)
        git("commit", "-q", "-m", "c", cwd=src)
        (self.out / "git").mkdir()
        git("bundle", "create", str(self.out / "git/incore.bundle"), "--all", cwd=src)

    def test_plan_lists_whitelist_and_holds_the_rest_back(self):
        p = ri.plan(self.out, self.ws)
        self.assertEqual({r["path"] for r in p["files"]}, {"records/a.json", "projects/backlog.md", "data/owner-todo-ids.json",
                                                            "input/data/input.sqlite3"})
        self.assertTrue(all(r["status"] == "install" for r in p["files"]))
        self.assertEqual(p["held_back"], {"external/claude-memory": 1, "external/claude-home": 1, "restore": 1, "credentials.txt": 1})
        self.assertEqual(p["bundles"], ["git/incore.bundle"])
        self.assertFalse(self.ws.exists())  # a plan writes nothing

    def test_apply_installs_hash_checked_and_never_overwrites(self):
        r = ri.apply(self.out, self.ws)
        root = self.ws / "incore"
        self.assertTrue(r["code"].startswith("cloned"))
        self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "code")
        self.assertEqual((root / "records/a.json").read_text(encoding="utf-8"), "{}")
        self.assertEqual({f["path"]: f["status"] for f in r["files"]}["records/a.json"], "replaced-checkout-copy")  # git keeps the old text
        self.assertEqual({f["status"] for f in r["files"]} - {"replaced-checkout-copy"}, {"installed"})
        self.assertFalse((root / "external").exists() or (root / "credentials.txt").exists())
        (root / "projects/backlog.md").write_text("edited on the new machine", encoding="utf-8")
        again = ri.plan(self.out, self.ws)
        self.assertEqual({x["path"]: x["status"] for x in again["files"]}["projects/backlog.md"], "conflict")
        ri.apply(self.out, self.ws)
        self.assertEqual((root / "projects/backlog.md").read_text(encoding="utf-8"), "edited on the new machine")

    def test_a_hand_edited_checkout_file_is_left_alone(self):
        ri.apply(self.out, self.ws)
        f = self.ws / "incore" / "records" / "a.json"
        f.write_text("edited by hand", encoding="utf-8")
        self.assertEqual({x["path"]: x["status"] for x in ri.plan(self.out, self.ws)["files"]}["records/a.json"], "conflict")
        ri.apply(self.out, self.ws)
        self.assertEqual(f.read_text(encoding="utf-8"), "edited by hand")

    def test_apply_runs_from_a_directory_that_is_not_a_repository(self):
        import os
        with tempfile.TemporaryDirectory() as plain:  # a new machine runs this from the workspace root, not from a repo
            here = os.getcwd()
            os.chdir(plain)
            try:
                r = ri.apply(self.out, self.ws)
            finally:
                os.chdir(here)
        self.assertTrue(r["code"].startswith("cloned"))
        self.assertEqual((self.ws / "incore" / "README.md").read_text(encoding="utf-8"), "code")

    def test_a_corrupt_bundle_is_refused(self):
        (self.out / "git/incore.bundle").write_bytes(b"not a bundle")
        with self.assertRaises(RuntimeError):
            ri.apply(self.out, self.ws)

    def test_main_without_yes_is_a_dry_run(self):
        self.assertEqual(ri.main(["apply", str(self.out), str(self.ws)]), 2)
        self.assertFalse(self.ws.exists())

    def test_refuses_a_non_repo_target_that_has_files(self):
        (self.ws / "incore").mkdir(parents=True)
        (self.ws / "incore" / "x.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(SystemExit):
            ri.apply(self.out, self.ws)


if __name__ == "__main__":
    unittest.main()
