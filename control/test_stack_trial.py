import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stack_trial as st  # noqa: E402

ROW = ("- [blocked: 等你粘命令] 组织：等你合并的孵化器分支（说明）\n```\nforeach ($b in 'claude/a','claude/b-2')\n{ git merge $b }\n```\n"
       "foreach ($b in 'claude/other')")


class StackTrial(unittest.TestCase):
    def test_branch_list_comes_from_the_row_that_starts_with_the_heading(self):
        old = "- [done-ref] 别的行 foreach ($b in 'claude/x')\n"
        self.assertEqual(st.branches_from_backlog(old + "- [blocked: 等你粘命令] 组织：等你合并的孵化器分支 foreach ($b in 'claude/a','claude/b-2') y\n"),
                         ["claude/a", "claude/b-2"])
        self.assertEqual(st.branches_from_backlog(old + ROW), [])  # the foreach is on a later line, not in the row
        self.assertEqual(st.branches_from_backlog("nothing"), [])

    def test_on_pending_puts_the_row_list_first_and_keeps_each_name_once(self):
        row = "- [blocked: 等你粘命令] 组织：等你合并的孵化器分支 foreach ($b in 'claude/a','claude/b') y\n"
        self.assertEqual(st.pick_branches(["--on-pending", "claude/c", "claude/a", "claude/c"], row), ["claude/a", "claude/b", "claude/c"])
        self.assertEqual(st.pick_branches(["claude/c", "--on-pending"], row), ["claude/a", "claude/b", "claude/c"])  # option position does not matter
        self.assertEqual(st.pick_branches(["--on-pending"], row), ["claude/a", "claude/b"])

    def test_without_the_option_nothing_changes(self):
        row = "- [blocked: 等你粘命令] 组织：等你合并的孵化器分支 foreach ($b in 'claude/a','claude/b') y\n"
        self.assertEqual(st.pick_branches(["claude/c", "claude/a"], row), ["claude/c", "claude/a"])  # the list is not added, no dedupe
        self.assertEqual(st.pick_branches(["--stack-only"], row), ["claude/a", "claude/b"])
        self.assertEqual(st.pick_branches([], "nothing"), [])

    def repo(self):
        d = Path(self._tmp.name)
        before = "\nouter before\nbefore 1\nbefore 2\nbefore 3\n"
        after = "after 1\nafter 2\nafter 3\nouter after\n"
        sh = lambda *a: subprocess.run([st.GIT, "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(d), *a], check=True, capture_output=True)
        sh("init", "-q", "-b", "master")
        (d / "f.txt").write_text(before + "base\n" + after, encoding="utf-8")
        sh("add", "f.txt")
        sh("commit", "-qm", "base")
        for name, fname, text in (("clean", "g.txt", "new\n"), ("c1", "f.txt", "one\n"), ("c2", "f.txt", "two\n")):
            sh("checkout", "-q", "-b", name, "master")
            (d / fname).write_text(before + text + after if fname == "f.txt" else text, encoding="utf-8")
            sh("add", fname)
            sh("commit", "-qm", name)
        sh("checkout", "-q", "master")
        return d

    def go(self, d, *argv):
        st.BACKLOG = d / "backlog.md"
        st.BACKLOG.write_text("", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = st.main([*argv, "--stack-only"], d)
        return rc, out.getvalue()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(setattr, st, "BACKLOG", st.BACKLOG)

    def test_clean_branches_exit_0_and_make_no_ref(self):
        d = self.repo()
        refs = subprocess.run([st.GIT, "-C", str(d), "for-each-ref"], capture_output=True, text=True, encoding="utf-8").stdout
        rc, out = self.go(d, "clean", "c1")
        self.assertEqual(rc, 0)
        self.assertIn("ok clean", out)
        self.assertIn("2 branches, no conflicts", out)
        self.assertEqual(subprocess.run([st.GIT, "-C", str(d), "for-each-ref"], capture_output=True, text=True, encoding="utf-8").stdout, refs)

    def test_jobs_cover_input_notify_runtime_and_root(self):
        root = Path(self._tmp.name)
        for rel in ("control/test_a.py", "backup/test_b.py", "input/test_dashboard.py", "input/test_input.py", "notify/test_send_email.py",
                    "runtime/test_claude_usage.py", "test_records_capture.py"):
            (root / rel).parent.mkdir(exist_ok=True)
            (root / rel).write_text("", encoding="utf-8")
        jobs = {n: (a, c.relative_to(root).as_posix() if c != root.parent else "..", g) for n, a, c, g in st.test_jobs(root)}
        self.assertEqual(jobs["input/test_input"], (["-m", "unittest", f"{root.name}.input.test_input"], "..", "input"))
        self.assertEqual(jobs["input/test_dashboard"][2], "input")
        self.assertEqual(jobs["notify/test_send_email"], (["-m", "unittest", "test_send_email"], "notify", "notify"))
        self.assertEqual(jobs["runtime/test_claude_usage"][1:], ("runtime", "runtime"))
        self.assertEqual(jobs["test_records_capture"], (["-m", "unittest", "test_records_capture"], ".", "root"))
        self.assertTrue({"test_a", "test_b.py", "discord"} <= set(jobs))
        self.assertTrue({"records_capture.py", "runtime"} <= set(st.EXPORT))

    def test_conflict_names_branch_and_file_and_exits_1(self):
        rc, out = self.go(self.repo(), "c1", "c2")
        self.assertEqual(rc, 1)
        self.assertIn("ok c1", out)
        self.assertIn("CONFLICT c2", out)
        self.assertIn("f.txt", out)
        self.assertNotIn("no conflicts", out)

    def test_conflict_reports_upstream_commit_and_numbered_context(self):
        d = self.repo()
        collision = st.git(d, "rev-parse", "--short", "c1")[1]
        refs = st.git(d, "for-each-ref")[1]
        original = (d / "f.txt").read_text(encoding="utf-8")
        rc, out = self.go(d, "c1", "c2")
        self.assertEqual(rc, 1)
        self.assertIn(f"{collision} c1", out)
        self.assertIn("upstream: master + c1", out)
        self.assertIn("conflict lines 6-10; context lines 3-13 (merge result)", out)
        self.assertIn("3: before 1\n4: before 2\n5: before 3", out)
        self.assertIn("11: after 1\n12: after 2\n13: after 3", out)
        self.assertNotIn("outer before", out)
        self.assertNotIn("outer after", out)
        self.assertEqual(st.git(d, "for-each-ref")[1], refs)
        self.assertEqual((d / "f.txt").read_text(encoding="utf-8"), original)

        # The same collision when c1 has already landed on master.
        self.assertEqual(st.git(d, "merge", "--ff-only", "c1")[0], 0)
        rc, out = self.go(d, "c2")
        self.assertEqual(rc, 1)
        self.assertIn(f"{collision} c1", out)
        self.assertIn("upstream: master (", out)
        with mock.patch.object(st, "CONFLICT_CONTEXT_CHARS", 60):
            rc, out = self.go(d, "c2")
        self.assertEqual(rc, 1)
        self.assertIn("conflict context truncated at 60 characters", out)
        self.assertNotIn("11: after 1", out)

    def test_redirected_progress_is_visible_before_tests_finish(self):
        d = self.repo()
        backlog, log = d / "backlog.md", d / "progress.txt"
        backlog.write_text("", encoding="utf-8")
        program = ("import sys\nfrom pathlib import Path\nimport stack_trial as st\n"
                   "st.BACKLOG = Path(sys.argv[1])\n"
                   "st.run_tests = lambda *args: sys.stdin.readline() == 'continue\\n'\n"
                   "sys.exit(st.main(['clean'], st.BACKLOG.parent))\n")
        env = dict(os.environ)
        env.pop("PYTHONUNBUFFERED", None)  # the script itself must enable line buffering
        with log.open("w", encoding="utf-8") as output:
            with subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-c", program, str(backlog)],
                                  cwd=Path(st.__file__).parent, env=env, stdin=subprocess.PIPE,
                                  stdout=output, stderr=subprocess.STDOUT, text=True, encoding="utf-8") as p:
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and p.poll() is None:
                        if log.read_text(encoding="utf-8").startswith("ok clean "):
                            break
                        time.sleep(0.02)
                    self.assertIsNone(p.poll(), log.read_text(encoding="utf-8"))
                    self.assertTrue(log.read_text(encoding="utf-8").startswith("ok clean "))
                finally:
                    p.communicate("continue\n", timeout=10)
                self.assertEqual(p.returncode, 0, log.read_text(encoding="utf-8"))

    def test_reports_untracked_files_the_branch_would_add(self):
        d = self.repo()
        path = d / "g.txt"
        path.write_text("new\n", encoding="utf-8")
        rc, out = self.go(d, "clean")
        self.assertEqual(rc, 0)
        self.assertIn("主目录已有未入库同名文件：clean:g.txt", out)
        self.assertIn("内容相同，可删", out)

        path.write_text("different\n", encoding="utf-8")
        rc, out = self.go(d, "clean")
        self.assertEqual(rc, 0)
        self.assertIn("主目录已有未入库同名文件：clean:g.txt", out)
        self.assertIn("内容不同，交 owner 处理", out)
        self.assertTrue(path.exists())

        # a CRLF copy (what autocrlf=true makes on Windows) is the same content, not a different file
        path.write_bytes(b"new\r\n")
        sh = lambda *a: subprocess.run([st.GIT, "-C", str(d), *a], check=True, capture_output=True)
        sh("config", "core.autocrlf", "true")
        rc, out = self.go(d, "clean")
        self.assertEqual(rc, 0)
        self.assertIn("内容相同，可删", out)
        self.assertNotIn("内容不同", out)


if __name__ == "__main__":
    unittest.main()
