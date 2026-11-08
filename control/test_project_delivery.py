import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_delivery as delivery
import review_packet

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.tk = SimpleNamespace(PROJECTS={"demo": (self.repo, ("Demo",))}, PROJECT_ADD_DIRS={},
                                  DATA=self.root / "data", BACKLOG=self.root / "backlog.md",
                                  project_of=lambda item: "demo" if item.startswith("Demo") else "general")
        self.tk.BACKLOG.write_text("", encoding="utf-8")

    def git(self, *args, owner="codex", day="2026-10-10"):
        env = {**os.environ, "GIT_AUTHOR_DATE": day + "T12:00:00+00:00", "GIT_COMMITTER_DATE": day + "T12:00:00+00:00"}
        return subprocess.run([review_packet.GIT, "-c", "safe.directory=*", "-c", f"user.name={owner}",
                               "-c", "user.email=codex@local", "-C", str(self.repo), *args], env=env,
                              check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()

    def init(self):
        self.git("init", "-b", "master")
        (self.repo / "base.txt").write_text("base", encoding="utf-8")
        self.git("add", "base.txt")
        self.git("commit", "-m", "base")

    def branch(self, name, path=None):
        self.git("checkout", "-b", name, "master")
        path = path or name.rsplit("/", 1)[-1] + ".txt"
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(name, encoding="utf-8")
        self.git("add", "--", path)
        self.git("commit", "-m", name)
        return self.git("rev-parse", "HEAD")

    def backlog(self, text):
        self.tk.BACKLOG.write_text(text, encoding="utf-8")

    def project(self):
        return delivery.collect(self.tk, NOW)["projects"][0]

    def test_only_done_completion_tag_resets_age_and_threshold_is_strict(self):
        self.init()
        self.backlog("- [todo] Demo：给 owner\x2010-26：not delivered\n"
                     "- [review] Demo：new work\n"
                     "- [done 10-23；给 owner\x2010-19：试用] Demo：version\n"
                     "- [done-ref；给 owner\x2010-26：history] Demo：old\n"
                     "- [done 10-26] Demo：原文 给 owner\x2010-26：not a receipt\n")
        p = self.project()
        self.assertEqual((p["days_since_delivery"], p["age_exceeded"], p["last_delivery"]), (7, False, "2026-10-19"))
        self.backlog("- [done；给 owner\x2010-18：试用] Demo：version\n")
        self.assertTrue(self.project()["age_exceeded"])

    def test_invalid_future_and_year_boundary_dates(self):
        self.assertIsNone(delivery.tag_date("10-27", NOW.date()))
        self.assertIsNone(delivery.tag_date("02-30", NOW.date()))
        self.assertEqual(delivery.tag_date("12-30", date(2027, 1, 2)), date(2026, 12, 30))

    def test_legacy_done_body_does_not_approve_its_base_branch(self):
        rows = delivery.accepted_rows("- [done 08:05] Demo：claude/fix（基于 claude/unreviewed-base）", self.tk, NOW.date())
        self.assertEqual(set(rows["demo"]["approved"]), {"claude/fix"})

    def test_no_receipt_is_never_and_missing_sources_are_unknown(self):
        self.init()
        p = self.project()
        self.assertEqual((p["days_since_delivery"], p["delivery_state"], p["approved_unmerged"]), (None, "never", 0))
        self.tk.PROJECTS["demo"] = (self.root / "missing", ("Demo",))
        p = self.project()
        self.assertEqual((p["delivery_state"], p["approved_unmerged"]), ("unknown", None))
        self.assertFalse(p["age_exceeded"])
        self.tk.BACKLOG.unlink()
        self.assertIsNone(self.project()["approved_unmerged"])

    def test_approved_branches_exclude_default_rc_duplicates_unreviewed_and_advanced_tips(self):
        self.init()
        rows = []
        for name in ("a", "b", "merged", "rc-child", "advanced"):
            branch = "claude/" + name
            sha = self.branch(branch)
            rows.append(f"- [done：审查通过，{branch} {sha[:7]}] Demo：feature")
        self.git("checkout", "master")
        self.git("merge", "--no-ff", "--no-edit", "claude/merged")
        self.git("checkout", "-b", "claude/demo-rc")
        self.git("merge", "--no-ff", "--no-edit", "claude/rc-child")
        self.branch("claude/unreviewed")
        self.git("checkout", "claude/advanced")
        (self.repo / "new.txt").write_text("unreviewed changes", encoding="utf-8")
        self.git("add", "new.txt")
        self.git("commit", "-m", "not reviewed")
        rows += [rows[0], "- [review] Demo：claude/unreviewed", "- [todo] Demo：claude/demo-rc"]
        self.backlog("\n".join(rows))
        before = self.git("show-ref")
        p = self.project()
        self.assertEqual(p["approved_unmerged"], 2)
        self.assertEqual({b["branch"] for b in p["branches"]}, {"claude/a", "claude/b"})
        self.assertEqual(self.git("show-ref"), before)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_cap_counts_by_project_and_across_repositories(self):
        self.init()
        rows = []
        for i in range(6):
            branch = f"claude/draft-{i}"
            sha = self.branch(branch)
            rows.append(f"- [done：{branch} {sha[:7]}] Demo：draft")
        self.backlog("\n".join(rows[:5]))
        self.assertFalse(self.project()["pause_new_drafts"])
        self.backlog("\n".join(rows))
        self.assertEqual((self.project()["approved_unmerged"], self.project()["pause_new_drafts"]), (6, True))
        # Additional folders in one repository must not double count its branches.
        self.tk.PROJECT_ADD_DIRS = {"demo": (self.repo,)}
        self.assertEqual(self.project()["approved_unmerged"], 6)
        other = self.root / "second"
        subprocess.run([review_packet.GIT, "clone", "--no-hardlinks", str(self.repo), str(other)], check=True,
                       capture_output=True, text=True, encoding="utf-8")
        original, self.repo = self.repo, other
        self.git("branch", "master", "origin/master")
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
        self.repo = original
        # clone only checks out one local branch, so use that branch's accepted tip as another repository.
        self.tk.PROJECT_ADD_DIRS = {"demo": (other,)}
        self.assertEqual(self.project()["approved_unmerged"], 7)

    def test_owner_merge_only_default_first_parent_scoped_to_project(self):
        self.init()
        self.branch("claude/feature", "input/feature.txt")
        self.git("checkout", "master")
        self.git("merge", "--no-ff", "--no-edit", "claude/feature", owner="Demo01", day="2026-10-24")
        self.tk.PROJECTS["input"] = (self.repo / "input", ("Input",))
        result = delivery.collect(self.tk, NOW)["projects"]
        self.assertEqual(result[0]["delivery_state"], "never")  # input merge does not refresh the parent project
        self.assertEqual((result[1]["days_since_delivery"], result[1]["last_delivery"]), (2, "2026-10-24"))
        self.assertIn("owner merge", result[1]["delivery_source"])
        self.branch("claude/newer")  # even a new feature commit does not reset the default delivery
        self.assertEqual(delivery.collect(self.tk, NOW)["projects"][1]["days_since_delivery"], 2)

    def test_default_remote_head_and_missing_default(self):
        self.init()
        self.git("branch", "main")
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
        self.assertEqual(delivery.repo_inventory(self.repo)["default"], "master")
        self.git("branch", "-m", "master", "trunk")
        self.git("branch", "-D", "main")
        self.assertEqual(delivery.repo_inventory(self.repo)["state"], "unknown")

    def test_write_and_render_preserve_unknown_and_errors(self):
        self.init()
        with mock.patch.object(delivery, "collect", return_value=delivery.collect(self.tk, NOW)):
            delivery.write(self.tk)
        data = json.loads((self.tk.DATA / "project-delivery.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["projects"]), 1)
        md = (self.tk.DATA / "project-delivery.md").read_text(encoding="utf-8")
        self.assertIn("| Demo (demo) | 无记录 | 0 |", md)
        with mock.patch.object(review_packet, "git", return_value=None):
            text = delivery.render(delivery.collect(self.tk, NOW))
        self.assertIn("| 未知 | 未知 |", text)


if __name__ == "__main__":
    unittest.main()
