"""Exercise the guard against isolated real git repositories; never switch the production worktree."""
import re
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import tick


class MainWorktreeGuard(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git("init", "-b", "master")
        self.git("-c", "user.name=codex", "-c", "user.email=codex@local", "commit", "--allow-empty", "-m", "fixture")
        self.backlog = self.repo / "projects" / "backlog.md"
        self.backlog.parent.mkdir()
        self.original = ("# tasks\r\n## Arbitros 线\r\n- [todo] Umba：keep\r\n\r\n"
                         "## 组织线\r\n份额：22%\r\n- [todo] 组织：keep\r\n\r\n"
                         "## 生活线\r\n- [todo] 写作：keep\r\n").encode("utf-8")
        self.backlog.write_bytes(self.original)
        for name, value in (("CONTROL", self.repo / "control"), ("BACKLOG", self.backlog)):
            patch = mock.patch.object(tick, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(tick, "log")
        self.log = patch.start()
        self.addCleanup(patch.stop)

    def git(self, *args):
        result = subprocess.run([tick.review_packet.GIT, "-c", "safe.directory=*", "-C", str(self.repo), *args],
                                capture_output=True, text=True, encoding="utf-8", check=True)
        return result.stdout.strip()

    def test_master_does_not_write_or_snapshot(self):
        before = self.backlog.stat().st_mtime_ns
        tick.main_worktree_guard()
        self.assertEqual(self.backlog.read_bytes(), self.original)
        self.assertEqual(self.backlog.stat().st_mtime_ns, before)
        self.assertFalse((self.repo / "projects" / "backlog-history").exists())
        self.log.assert_not_called()

    def test_branch_writes_once_in_organization_with_time_and_preserves_other_sections(self):
        self.git("switch", "-c", "claude/daily-health-line")
        tick.main_worktree_guard()
        data = self.backlog.read_bytes()
        row = tick.parse_backlog(data.decode("utf-8"))["组织线"]["ask"][0][0]
        self.assertIn("当前 claude/daily-health-line", row)
        stamp = re.search(r"发现时间 ([^；]+)", row).group(1)
        self.assertIsNotNone(datetime.fromisoformat(stamp).tzinfo)
        self.assertEqual(data.replace((row + "\r\n").encode("utf-8"), b""), self.original)
        tick.main_worktree_guard()
        self.assertEqual(self.backlog.read_bytes(), data)
        self.assertEqual(self.log.call_count, 1)
        self.assertEqual(len(list((self.repo / "projects" / "backlog-history").glob("backlog-*.md"))), 1)

    def test_answered_branch_and_return_from_master_stay_deduplicated(self):
        self.git("switch", "-c", "claude/daily-health-line")
        tick.main_worktree_guard()
        answered = self.backlog.read_text(encoding="utf-8").replace("[blocked: 等总理]", "[done 11-02：总理答 已核对]")
        self.backlog.write_text(answered, encoding="utf-8")
        self.git("switch", "master")
        tick.main_worktree_guard()
        self.git("switch", "claude/daily-health-line")
        tick.main_worktree_guard()
        self.assertEqual(self.backlog.read_text(encoding="utf-8"), answered)
        self.assertEqual(self.log.call_count, 1)

    def test_different_branches_each_write_once(self):
        for branch in ("claude/a", "claude/b"):
            self.git("switch", "-c", branch)
            tick.main_worktree_guard()
            tick.main_worktree_guard()
        rows = tick.parse_backlog(self.backlog.read_text(encoding="utf-8"))["组织线"]["ask"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.log.call_count, 2)

    def test_detached_head_records_commit_once(self):
        self.git("checkout", "--detach", "HEAD")
        commit = self.git("rev-parse", "HEAD")
        tick.main_worktree_guard()
        tick.main_worktree_guard()
        self.assertIn(f"〔主目录 HEAD：detached@{commit}〕", self.backlog.read_text(encoding="utf-8"))
        self.assertEqual(self.log.call_count, 1)

    def test_missing_section_does_not_write_elsewhere(self):
        self.git("switch", "-c", "claude/a")
        self.backlog.write_text("## Arbitros 线\n- [todo] Umba：keep\n", encoding="utf-8")
        before = self.backlog.read_bytes()
        with self.assertRaisesRegex(ValueError, "组织线"):
            tick.main_worktree_guard()
        self.assertEqual(self.backlog.read_bytes(), before)

    def test_unreadable_git_does_not_make_a_false_alarm(self):
        with mock.patch.object(tick.review_packet, "git", return_value=None), self.assertRaises(OSError):
            tick.main_worktree_guard()
        self.assertEqual(self.backlog.read_bytes(), self.original)

    def test_both_executor_prompt_paths_include_worktree_and_precommit_checks(self):
        for name in ("executor", "executor_general"):
            prompt = tick.PROMPTS[name].format(name="总理", section="组织线", project="incore", item="组织：x",
                                              lessons="", audits="audits", fresh="")
            for required in ("git worktree add -b", "git rev-parse --show-toplevel", "每次提交前", "checkout/switch",
                             r"C:\orghome\wt\<主题>", "不满足就停止提交"):
                self.assertIn(required, prompt)


if __name__ == "__main__":
    unittest.main()
