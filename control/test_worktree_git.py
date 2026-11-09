import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

import stack_trial as st
import worktree_git as wg


class WorktreeGit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="worktree-side-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "工作树 with spaces"
        self.repo.mkdir()

    def metadata(self, gitdir):
        (self.repo / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    def test_synthetic_gitdirs_choose_the_creating_side(self):
        for gitdir, side, prefix in (
            (r"D:\repo space\.git\worktrees\demo", "windows", [wg.WINDOWS_GIT]),
            ("D:/repo space/.git/worktrees/demo", "windows", [wg.WINDOWS_GIT]),
            (r"\\server\share\repo\.git\worktrees\demo", "windows", [wg.WINDOWS_GIT]),
            ("/mnt\x2fd/repo space/.git/worktrees/demo", "wsl", ["wsl", "-e", "git"]),
            ("/home\x2fwsluser/repo/.git/worktrees/demo", "wsl", ["wsl", "-e", "git"]),
            ("../repo/.git/worktrees/demo", None, [wg.GIT]),
        ):
            with self.subTest(gitdir=gitdir):
                self.metadata(gitdir)
                self.assertEqual(wg.worktree_side(self.repo), side)
                self.assertEqual(wg.side_git(self.repo), prefix)

    def test_main_repo_and_missing_metadata_keep_native_git(self):
        for is_main in (False, True):
            with self.subTest(main=is_main):
                if is_main:
                    (self.repo / ".git").mkdir()
                self.assertIsNone(wg.worktree_side(self.repo))
                self.assertEqual(wg.git_command(self.repo, "status", native_git=st.GIT),
                                 [st.GIT, "-c", "safe.directory=*", "-C", str(self.repo), "status"])

    def test_same_side_uses_native_git_for_status_and_merge_tree(self):
        for platform, gitdir in (("win32", r"D:\repo\.git\worktrees\demo"),
                                 ("linux", "/mnt\x2fd/repo/.git/worktrees/demo")):
            for args in (("status", "--short"), ("merge-tree", "--write-tree", "master", "claude/demo")):
                with self.subTest(platform=platform, args=args), mock.patch.object(wg.sys, "platform", platform):
                    self.metadata(gitdir)
                    command = wg.git_command(self.repo, *args, native_git=st.GIT)
                    self.assertEqual(command, [st.GIT, "-c", "safe.directory=*", "-C", str(self.repo), *args])

    def test_wrong_side_stops_before_subprocess_and_prints_correct_command(self):
        for platform, gitdir, label, prefix in (
            ("win32", "/mnt\x2fd/repo/.git/worktrees/demo", "WSL", "'wsl' '-e' 'git'"),
            ("linux", r"D:\repo\.git\worktrees\demo", "Windows", wg.WINDOWS_GIT),
        ):
            for command in ("status", "merge-tree"):
                with self.subTest(platform=platform, command=command), mock.patch.object(wg.sys, "platform", platform):
                    self.metadata(gitdir)
                    original = (self.repo / ".git").read_bytes()
                    err = io.StringIO()
                    with mock.patch.object(wg.subprocess, "run") as run, contextlib.redirect_stderr(err):
                        self.assertEqual(wg.main([str(self.repo), command]), 2)
                    run.assert_not_called()
                    self.assertIn(f"由 {label} 创建", err.getvalue())
                    self.assertIn(prefix, err.getvalue())
                    self.assertIn(f"'{command}'", err.getvalue())
                    self.assertNotIn("not a git repository", err.getvalue())
                    self.assertEqual((self.repo / ".git").read_bytes(), original)

    def test_recommendation_converts_drive_and_quotes_powershell_paths(self):
        self.metadata("/mnt\x2fd/repo/.git/worktrees/demo")
        with mock.patch.object(wg.sys, "platform", "win32"), \
                mock.patch.object(wg.Path, "resolve", return_value=PureWindowsPath("D:/工作 'repo/demo")):
            with self.assertRaises(ValueError) as err:
                wg.git_command(self.repo, "status", "--short")
        self.assertIn("'/mnt\x2fd/工作 ''repo/demo'", str(err.exception))

        self.metadata(r"D:\repo\.git\worktrees\demo")
        with mock.patch.object(wg.sys, "platform", "linux"), \
                mock.patch.object(wg.Path, "resolve", return_value=Path("/mnt\x2fd/repo space/demo")):
            with self.assertRaises(ValueError) as err:
                wg.git_command(self.repo, "merge-tree", "master", "claude/demo")
        self.assertIn("'D:/repo space/demo'", str(err.exception))

    def test_stack_reports_wrong_side_before_any_trial_or_export(self):
        self.metadata("/mnt\x2fd/repo/.git/worktrees/demo")
        backlog = self.repo / "backlog.md"
        backlog.write_text("", encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(wg.sys, "platform", "win32"), mock.patch.object(st, "BACKLOG", backlog), \
                mock.patch.object(st.subprocess, "run") as run, mock.patch.object(st, "run_tests") as tests, \
                contextlib.redirect_stdout(out):
            self.assertEqual(st.main(["claude/demo"], self.repo), 1)
        run.assert_not_called()
        tests.assert_not_called()
        self.assertIn("由 WSL 创建", out.getvalue())
        self.assertNotIn("CONFLICT", out.getvalue())

    def test_cli_preserves_git_exit_code_and_arguments(self):
        args = ["merge-tree", "--write-tree", "master", "claude/demo"]
        with mock.patch.object(wg.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
            self.assertEqual(wg.main([str(self.repo), *args]), 1)
        self.assertEqual(run.call_args.args[0], wg.git_command(self.repo, *args))

    def test_malformed_metadata_fails_clearly(self):
        (self.repo / ".git").write_text("broken\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "无法识别"):
            wg.git_command(self.repo, "status")


if __name__ == "__main__":
    unittest.main()
