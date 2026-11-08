import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import review_packet as rp

START = datetime(2020, 1, 5, 12, 0, 0)  # long ago: files made by the test (ctime now) fall outside the window
UNTIL = START + timedelta(hours=2)
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class Packet(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo, self.audits = self.root / "repo", self.root / "audits"
        self.repo.mkdir()
        self.audits.mkdir()
        self.git("init", "-q", "-b", "main")
        self.commit("base.py", when=START - timedelta(days=1))

    def git(self, *args, repo=None, when=None):
        env = ENV if when is None else {**ENV, "GIT_COMMITTER_DATE": when.isoformat(), "GIT_AUTHOR_DATE": when.isoformat()}
        subprocess.run([rp.GIT, "-C", str(repo or self.repo), *args], check=True, capture_output=True, env=env)

    def commit(self, name, when, repo=None):
        root = repo or self.repo
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(name, encoding="utf-8")
        self.git("add", name, repo=root)
        self.git("commit", "-q", "-m", f"add {name}", repo=root, when=when)

    def result(self, name):
        path = self.audits / name
        path.write_text("r", encoding="utf-8")
        os.utime(path, ((START + timedelta(minutes=10)).timestamp(),) * 2)  # created at "now", but the run window is fixed
        return path

    def run_file(self, *tools):
        f = self.root / "run.jsonl"
        f.write_text("\n".join(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": n, "input": i}]}}) for n, i in tools), encoding="utf-8")
        return f

    def build(self, item="x：任务，结果写 audits/<日期>-topic.md", cwd=None, **kw):
        return rp.build(item, cwd or self.repo, self.audits, START, UNTIL, **kw)

    def test_work_done_gives_branch_files_tests_and_no_hard_condition(self):
        self.git("switch", "-q", "-c", "claude/topic")
        self.commit("src/a.py", START + timedelta(minutes=30))
        self.commit("records/owner.json", START + timedelta(minutes=40))
        (self.repo / "test_a.py").write_text("", encoding="utf-8")
        self.result("2026-10-05-topic.md")
        got = self.build("x：任务，分支 `claude/topic`，结果写 audits/<日期>-topic.md", run_file=self.run_file(("Write", {})))
        self.assertIsNone(got["hard"])
        self.assertIsNone(got["facts"]["hard"])
        text = got["text"]
        self.assertIn("claude/topic 比 main 多 2 个提交，改 2 个文件", text)
        self.assertIn("src/a.py", text)
        self.assertIn("⚠ 碰了敏感路径：records/owner.json", text)  # flagged for the reviewer, never an automatic rework
        self.assertIn("python -m unittest", text)
        self.assertIn("结果文件：2026-10-05-topic.md", text)
        self.assertEqual(got["facts"]["refs"]["claude/topic"], rp.git(self.repo, "rev-parse", "claude/topic").strip())
        self.assertEqual(got["facts"]["base"], rp.git(self.repo, "rev-parse", "main").strip())
        self.assertEqual(got["facts"]["result_files"], ["2026-10-05-topic.md"])

    def test_packet_warns_when_main_checkout_is_on_a_topic_branch(self):
        self.git("switch", "-q", "-c", "claude/topic")
        got = self.build()
        self.assertIn("⚠ 仓库主目录停在 claude/topic，不在默认分支", got["text"])

    def test_merge_check_clean_for_every_reference_without_changing_checkout_or_refs(self):
        self.git("branch", "master")
        self.git("switch", "-q", "-c", "claude/topic")
        self.commit("topic.py", START + timedelta(minutes=10))
        branches = ["claude/topic", "claude/two", "claude/three", "claude/four"]
        for branch in branches[1:]:
            self.git("branch", branch)
        self.git("switch", "-q", "master")
        self.commit("master.py", START + timedelta(minutes=20))
        (self.repo / "base.py").write_text("local uncommitted change", encoding="utf-8")
        before = (rp.git(self.repo, "show-ref"), (self.repo / ".git" / "index").read_bytes(),
                  rp.git(self.repo, "symbolic-ref", "HEAD"))
        text = self.build("x：" + "、".join(branches))["text"]
        for branch in branches:
            self.assertIn(f"{branch} 和当前 master 试合并：无冲突", text)
        self.assertEqual(before, (rp.git(self.repo, "show-ref"), (self.repo / ".git" / "index").read_bytes(),
                                  rp.git(self.repo, "symbolic-ref", "HEAD")))
        self.assertEqual((self.repo / "base.py").read_text(encoding="utf-8"), "local uncommitted change")

    def test_merge_check_conflicts_list_paths_including_spaces_and_chinese(self):
        self.commit("中文 file.py", START - timedelta(hours=1))
        self.git("branch", "master")
        self.git("branch", "claude/topic")
        for branch in ("claude/topic", "master"):
            self.git("switch", "-q", branch)
            for name in ("base.py", "中文 file.py"):
                (self.repo / name).write_text(branch, encoding="utf-8")
            self.git("add", "base.py", "中文 file.py")
            self.git("commit", "-q", "-m", branch, when=START + timedelta(minutes=20))
        text = self.build("x：claude/topic")["text"]
        self.assertIn("claude/topic 和当前 master 试合并：冲突文件：base.py、中文 file.py", text)

    def test_merge_check_missing_branch_is_explicit(self):
        self.git("branch", "master")
        text = self.build("x：claude/missing")["text"]
        self.assertIn("claude/missing 和当前 master 试合并：未检查：分支不存在", text)

    def test_merge_check_version_timeout_and_other_errors_remain_unchecked(self):
        self.git("branch", "master")
        self.git("branch", "claude/topic")
        original = rp.subprocess.run
        for outcome, expected in ((subprocess.CompletedProcess([], 129, "", "usage"), "git 版本"),
                                  (subprocess.TimeoutExpired([], 20), "超时"),
                                  (FileNotFoundError(), "git 不可用"),
                                  (subprocess.CompletedProcess([], 128, "", "fatal"), "git 错误")):
            def run(args, **kwargs):
                if "merge-tree" not in args:
                    return original(args, **kwargs)
                self.assertEqual(kwargs["timeout"], 20)
                self.assertEqual(kwargs["creationflags"], rp.NO_WINDOW)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            with self.subTest(expected=expected), mock.patch.object(rp.subprocess, "run", side_effect=run):
                text = self.build("x：claude/topic")["text"]
            self.assertIn(f"claude/topic 和当前 master 试合并：未检查：{expected}", text)

    def test_packet_omits_main_checkout_warning_in_detached_worktree_on_default(self):
        self.git("switch", "-q", "-c", "master")
        self.assertNotIn("仓库主目录停在", self.build()["text"])
        self.git("switch", "-q", "main")
        detached = self.root / "detached"
        self.git("worktree", "add", "--detach", str(detached), "HEAD")
        got = self.build(cwd=detached)
        self.assertNotIn("仓库主目录停在", got["text"])

    def test_frozen_paths_preserve_spaces_and_chinese(self):
        self.git("switch", "-q", "-c", "claude/topic")
        self.commit("src/中文 module.py", START + timedelta(minutes=30))
        got = self.build("x：分支 claude/topic")
        self.assertEqual(got["facts"]["changed"], ["src/中文 module.py"])

    def test_backticked_absolute_repo_path_selects_its_named_branch(self):
        other = self.root / "project with spaces"
        other.mkdir()
        self.git("init", "-q", "-b", "main", repo=other)
        self.commit("base.py", START - timedelta(days=1), repo=other)
        self.git("switch", "-q", "-c", "claude/topic", repo=other)
        self.commit("tests/test_external.py", START + timedelta(minutes=20), repo=other)
        self.commit("src/other.py", START + timedelta(minutes=30), repo=other)
        self.git("switch", "-q", "-c", "claude/unrelated")
        self.commit("src/wrong.py", START + timedelta(minutes=40))

        got = self.build(f"x：在 `{other}` 仓库继续分支 claude/topic")

        self.assertEqual(got["facts"]["repo"], str(other))
        self.assertEqual(set(got["facts"]["refs"]), {"claude/topic"})
        self.assertEqual(got["facts"]["changed"], ["src/other.py", "tests/test_external.py"])
        self.assertIn("测试命令：cd tests; python -m unittest", got["text"])
        self.assertNotIn("src/wrong.py", got["text"])

    def test_missing_named_branch_does_not_fall_back_to_recent_branches(self):
        other = self.root / "target"
        other.mkdir()
        self.git("init", "-q", "-b", "main", repo=other)
        self.commit("base.py", START - timedelta(days=1), repo=other)
        self.git("switch", "-q", "-c", "claude/other", repo=other)
        self.commit("src/target.py", START + timedelta(minutes=30), repo=other)
        self.git("switch", "-q", "-c", "claude/recent")
        self.commit("src/recent.py", START + timedelta(minutes=40))

        got = self.build(f"x：在 `{other}` 仓库找分支 claude/missing")

        self.assertIn(f"行里写的分支在 {other} 仓库没找到：claude/missing", got["text"])
        self.assertEqual(got["facts"]["repo"], str(other))
        self.assertEqual(got["facts"]["refs"], {})
        self.assertEqual(got["facts"]["changed"], [])
        self.assertNotIn("claude/recent 比", got["text"])

    def test_bare_repo_and_worktree_paths_find_poetry_branch_and_common_repo(self):
        poetry = self.root / "poetry-mask"
        poetry.mkdir()
        self.git("init", "-q", "-b", "main", repo=poetry)
        self.commit("base.py", START - timedelta(days=1), repo=poetry)
        worktree = self.root / "poetry-align"
        self.git("worktree", "add", "-b", "claude/align", str(worktree), "main", repo=poetry)
        self.commit("src/align.py", START + timedelta(minutes=30), repo=worktree)
        for path in (poetry, worktree):
            with self.subTest(path=path):
                got = self.build(f"写作模型：在 {path} 仓库继续 claude/align")
                self.assertEqual(got["facts"]["repo"], str(path))
                self.assertEqual(got["facts"]["main_repo"], str(poetry.resolve()))
                self.assertEqual(set(got["facts"]["refs"]), {"claude/align"})
                self.assertEqual(got["facts"]["changed"], ["src/align.py"])

    def test_row_without_named_branch_uses_only_branches_in_its_own_run_record(self):
        self.git("switch", "-q", "-c", "claude/recent")
        self.commit("src/recent.py", START + timedelta(minutes=30))
        self.git("switch", "-q", "-c", "claude/other-executor")
        self.commit("control/test_tick.py", START + timedelta(minutes=35))
        mine = self.root / "run.jsonl"
        mine.write_text('{"cmd": "git switch claude/recent"}\n', encoding="utf-8")

        got = self.build("x：没有写分支的旧式任务", run_file=mine)
        self.assertEqual(set(got["facts"]["refs"]), {"claude/recent"})
        self.assertEqual(got["facts"]["changed"], ["src/recent.py"])

        for run_file in (None, self.root / "readonly-run.jsonl"):  # a read-only inventory row touched no branch
            got = self.build("x：只读盘点，没有分支", run_file=run_file)
            self.assertEqual(got["facts"]["refs"], {})
            self.assertEqual(got["facts"]["changed"], [])
            self.assertNotIn("改动文件", got["text"])

    def test_secretary_files_are_not_protected_but_real_credentials_are(self):
        for ok in ("projects/local-secretary-v0.md", "data/tokenizer_v2.json"):
            self.assertIsNone(rp.PROTECTED.search(ok), ok)
        for bad in ("client_secret.json", "a/my_secret.txt", "credentials.json", "02_secrets/x", "x/token.json", "tokens.txt", "a.pem"):
            self.assertIsNotNone(rp.PROTECTED.search(bad), bad)

    def test_poetry_repo_without_main_freezes_from_sole_root_but_not_multiple_roots(self):
        poetry = self.root / "poetry-mask"
        poetry.mkdir()
        self.git("init", "-q", "-b", "claude/base", repo=poetry)
        self.commit("base.py", START - timedelta(days=1), repo=poetry)
        root = rp.git(poetry, "rev-parse", "HEAD").strip()
        self.git("switch", "-q", "-c", "claude/topic", repo=poetry)
        self.commit("src/new.py", START + timedelta(minutes=30), repo=poetry)
        got = self.build("写作模型：claude/topic", cwd=poetry)
        self.assertEqual(got["facts"]["base"], root)
        self.assertEqual(got["facts"]["changed"], ["src/new.py"])
        self.assertIn("非合并目标", got["text"])
        self.assertIsNone(rp.git(poetry, "rev-parse", "--verify", "-q", "main"))
        self.git("checkout", "-q", "--orphan", "claude/unrelated", repo=poetry)
        self.git("commit", "-q", "--allow-empty", "-m", "other root", repo=poetry, when=START)
        self.assertIsNone(self.build("写作模型：claude/topic", cwd=poetry)["facts"]["base"])

    def test_nothing_produced_is_a_hard_condition(self):
        quiet = self.run_file(("Read", {"file_path": "x"}), ("PowerShell", {"command": "git log 2>&1; ls | Select -First 3"}))
        got = self.build(run_file=quiet)
        self.assertIn("没有新提交", got["hard"])
        self.assertEqual(got["facts"]["hard"], got["hard"])
        self.assertIn("测试命令：未找到", got["text"])
        self.assertIn("结果文件：行里点名的未找到", got["text"])

    def test_anything_uncertain_leaves_the_row_to_the_reviewer(self):
        quiet = self.run_file(("Read", {}))
        self.assertIsNotNone(self.build(run_file=quiet)["hard"])
        self.assertIsNone(self.build(run_file=self.run_file(("Edit", {})))["hard"])  # it wrote something (maybe outside the repo)
        self.assertIsNone(self.build(run_file=self.run_file(("PowerShell", {"command": "'x' | Out-File a.txt"})))["hard"])
        self.assertIsNone(self.build()["hard"])  # no run file: unknown
        self.assertIsNone(self.build(run_file=self.root / "missing.jsonl")["hard"])
        self.result("2026-10-05-topic.md")  # the named result file exists
        self.assertIsNone(self.build(run_file=quiet)["hard"])
        (self.audits / "2026-10-05-topic.md").unlink()
        self.commit("late.py", START + timedelta(minutes=5))  # a commit inside the window
        self.assertIsNone(self.build(run_file=quiet)["hard"])
        self.assertIsNone(rp.build("x：任务", self.root, self.audits, START, UNTIL, run_file=quiet)["hard"])  # not a git repo

    def test_row_naming_no_result_file_needs_an_empty_audits_window(self):
        quiet = self.run_file(("Read", {}))
        self.assertIsNotNone(self.build("x：任务", run_file=quiet)["hard"])
        self.result("other-line.md")  # another line's executor wrote in the window
        self.assertIsNone(self.build("x：任务", run_file=quiet)["hard"])

    def test_only_the_path_after_结果写_is_the_result_file(self):
        # 10-09 Uvox row: cites a background plan (touched in the window by someone else), result path named after 结果写
        row = "Uvox：按新叙事改文案（依据 audits/2026-10-09-breakthrough-plan.md；分支 claude/x）。结果写 audits/<日期>-narrative.md"
        self.result("2026-10-09-breakthrough-plan.md")
        quiet = self.run_file(("Read", {}))
        got = self.build(row, run_file=quiet)
        self.assertEqual(rp.result_refs(row), ["audits/<日期>-narrative.md"])
        self.assertNotIn("breakthrough-plan", got["text"].split("结果文件：")[1].split("\n")[0])
        self.assertIn("结果文件：行里点名的未找到", got["text"])
        self.assertIn("没有新提交", got["hard"])  # the nothing-produced condition is no longer masked
        self.result("2026-10-09-narrative.md")
        self.assertIn("结果文件：2026-10-09-narrative.md", self.build(row, run_file=quiet)["text"])
        self.assertEqual(rp.result_refs("x：见 audits/a.md 和 audits/b.md"), ["audits/a.md", "audits/b.md"])  # no 结果写: as before
        self.assertEqual(rp.result_refs("x：结果写在上面，见 audits/a.md、audits/b.md"), ["audits/a.md"])

    def test_xx_date_placeholder_matches_like_angle_brackets(self):
        # 11-03: intel rows name results as audits/2026-11-xx-<name>.md; 43 rows looked result-less to sol first review
        row = "情报：对照表。结果写 audits/2026-11-xx-intel-table.md"
        self.result("2026-11-02-intel-table.md")
        quiet = self.run_file(("Read", {}))
        self.assertIn("结果文件：2026-11-02-intel-table.md", self.build(row, run_file=quiet)["text"])
        self.assertEqual(rp.result_files("x：结果写 audits/2026-11-xx-intel-table.md", self.audits, START, UNTIL)[0],
                         ["2026-11-02-intel-table.md"])

    def test_test_command_is_found_not_invented(self):
        self.assertEqual(rp.test_command(self.repo), "未找到")
        (self.repo / "control").mkdir()
        (self.repo / "control" / "test_x.py").write_text("", encoding="utf-8")
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8-sig")
        self.assertEqual(rp.test_command(self.repo), "npm test；cd control; python -m unittest（未跑，请重跑）")

    def test_test_command_lists_only_folders_with_changed_files(self):
        for sub in ("control", "input", "backup"):
            (self.repo / sub).mkdir()
            (self.repo / sub / "test_x.py").write_text("", encoding="utf-8")
        both = "python -m unittest"
        self.assertEqual(rp.test_command(self.repo, ("control/tick.py",)), f"cd control; {both}（未跑，请重跑）")
        self.assertEqual(rp.test_command(self.repo, ("control\\tick.py", "input/a.py")),
                         f"cd control; {both}；cd input; {both}（未跑，请重跑）")
        self.assertEqual(rp.test_command(self.repo, ("README.md",)), "未找到")  # only a top-level file, no tests there
        self.assertEqual(rp.test_command(self.repo, ("docs/a.md",)), "未找到")  # a folder without tests
        self.assertIn("cd backup", rp.test_command(self.repo))

    def test_tests_package_runs_from_repository_root(self):
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_example.py").write_text("", encoding="utf-8")
        self.assertEqual(rp.test_command(self.repo, ("tests/test_example.py",)),
                         "python -m unittest（未跑，请重跑）")

    def test_readme_root_instruction_runs_from_repository_root(self):
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "test_example.py").write_text("", encoding="utf-8")
        (self.repo / "README.md").write_text(
            "Run `python -m unittest` from the repository root.", encoding="utf-8")
        self.assertEqual(rp.test_command(self.repo, ("tests/test_example.py",)),
                         "python -m unittest（未跑，请重跑）")

    def test_packet_shows_verification_section_and_codex_last_message(self):
        self.result("2026-10-05-topic.md").write_text(
            "# t\n## 做了什么\nx\n## 验证\n" + "\n".join(f"v{i}" for i in range(30)) + "\n## 后续\nz\n", encoding="utf-8")
        os.utime(self.audits / "2026-10-05-topic.md", ((START + timedelta(minutes=10)).timestamp(),) * 2)
        f = self.root / "codex.jsonl"
        f.write_text("\n".join(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": t}})
                               for t in ("first", "被 .git 只读挡住 " + "x" * 400)), encoding="utf-8")
        text = self.build(run_file=f)["text"]
        self.assertIn("## 验证\nv0", text)
        self.assertIn("v13", text)
        self.assertNotIn("v14", text)  # heading + v0..v13 = 15 lines
        self.assertNotIn("后续", text)
        self.assertIn("Codex 最后一条回复（前 300 字）：被 .git 只读挡住 x", text)
        self.assertNotIn("first", text)
        self.assertEqual(len(rp.codex_final_message(f)), 300)
        self.assertEqual(rp.codex_final_message(self.run_file(("Read", {}))), "")  # a Claude stream: nothing

    def test_run_wrote_ignores_redirects_that_write_nothing(self):
        wrote = lambda cmd: rp.run_wrote(self.run_file(("Bash", {"command": cmd})))
        self.assertFalse(wrote("git status 2>&1 && ls > /dev/null"))
        self.assertTrue(wrote("cat > /tmp/a <<EOF"))
        self.assertTrue(wrote("git commit -m x"))
        f = self.root / "codex.jsonl"
        f.write_text(json.dumps({"type": "item.completed", "item": {"type": "file_change"}}), encoding="utf-8")
        self.assertTrue(rp.run_wrote(f))


class ExplicitPacket(unittest.TestCase):
    setUp = Packet.setUp
    git = Packet.git
    commit = Packet.commit

    def delivery(self, kind="code", tests=None):
        self.base = rp.git(self.repo, "rev-parse", "HEAD").strip()
        self.git("switch", "-q", "-c", "claude/delivery")
        script = self.repo / "test_delivery.py"
        script.write_text("import unittest\nclass Demo(unittest.TestCase):\n"
                          " def test_ok(self): self.assertTrue(True)\n"
                          " @unittest.skip('offline')\n def test_skip(self): pass\n", encoding="utf-8")
        self.git("add", "test_delivery.py")
        self.git("commit", "-q", "-m", "delivery")
        self.sha = rp.git(self.repo, "rev-parse", "HEAD").strip()
        self.report = self.root / "结果 report.md"
        self.report.write_text("verified evidence\n", encoding="utf-8")
        self.manifest = self.root / "delivery.json"
        self.spec = {"version": 1, "kind": kind, "repository": str(self.repo), "baseline": self.base,
                     "commit": self.sha, "results": [{"path": self.report.name, "sha256": rp.file_hash(self.report)}],
                     "tests": ["local"] if tests is None else tests, "tests_reason": "document inspection only"}
        self.commands = self.root / "reviewer.json"
        self.plan = {"local": {"interpreter": sys.executable, "args": ["-m", "unittest", "test_delivery", "-v"],
                               "cwd": ".", "parser": "unittest", "timeout": 30}}
        self.output = self.root / "evidence"

    def collect(self, commands=True, force=False):
        self.manifest.write_text(json.dumps(self.spec, ensure_ascii=False), encoding="utf-8")
        self.commands.write_text(json.dumps(self.plan), encoding="utf-8")
        return rp.collect_manifest(self.manifest, self.output, self.commands if commands else None, force)

    def test_pinned_export_counts_cache_and_no_automatic_verdict(self):
        self.delivery()
        # A dirty source checkout must not replace the pinned test with this failure.
        (self.repo / "test_delivery.py").write_text("raise RuntimeError('dirty source')", encoding="utf-8")
        packet = self.collect()
        facts = packet["facts"]
        self.assertEqual(facts["errors"], [])
        receipt = facts["tests"][0]
        self.assertEqual(receipt["counts"]["passed"], 1)
        self.assertEqual(receipt["counts"]["skipped"], 1)
        self.assertEqual(receipt["exit_code"], 0)
        self.assertIn("skipped=1", " ".join(facts["warnings"]))
        self.assertTrue(Path(receipt["stdout"]).is_file())
        self.assertTrue(Path(receipt["stderr"]).is_file())
        self.assertEqual((self.output / facts["results"][0]["frozen"]).read_bytes(), self.report.read_bytes())
        self.assertTrue(facts["manual_review_required"])
        self.assertIsNone(packet["hard"])
        self.assertNotIn("verdict", packet)
        self.assertFalse((Path(receipt["snapshot"]) / ".git").exists())
        self.assertTrue(self.collect()["facts"]["tests"][0]["cached"])
        self.assertFalse(self.collect(force=True)["facts"]["tests"][0]["cached"])

    def test_cache_invalidates_for_commit_command_interpreter_and_dependencies(self):
        self.delivery()
        first = self.collect()["facts"]["tests"][0]["cache_key"]
        self.plan["local"]["args"].remove("-v")
        second = self.collect()["facts"]["tests"][0]
        self.assertNotEqual(first, second["cache_key"])
        self.assertFalse(second["cached"])
        self.commit("extra.md", START)
        self.spec["commit"] = rp.git(self.repo, "rev-parse", "HEAD").strip()
        third = self.collect()["facts"]["tests"][0]
        self.assertNotEqual(second["cache_key"], third["cache_key"])
        identity = rp.interpreter_identity(sys.executable)
        for key, value in (("path", "different executable"), ("environment", ["changed dependency version"])):
            with self.subTest(key=key), mock.patch.object(rp, "interpreter_identity", return_value={**identity, key: value}):
                changed = self.collect()["facts"]["tests"][0]
                self.assertNotEqual(third["cache_key"], changed["cache_key"])

    def test_missing_result_hash_mismatch_and_bad_repository_do_not_run(self):
        self.delivery()
        self.collect()
        self.spec["results"][0]["path"] = "wrong.md"
        facts = self.collect()["facts"]
        self.assertIn("result missing", " ".join(facts["errors"]))
        self.assertIsNone(facts["tests"][0]["exit_code"])
        self.spec["results"][0]["path"] = self.report.name
        self.report.write_text("changed", encoding="utf-8")
        self.assertIn("hash mismatch", " ".join(self.collect()["facts"]["errors"]))
        self.spec["repository"] = str(self.root / "wrong-repo")
        self.assertIn("not its git root", " ".join(self.collect()["facts"]["errors"]))
        self.spec["repository"] = str(self.repo / "subdirectory")
        (self.repo / "subdirectory").mkdir()
        self.assertIn("not its git root", " ".join(self.collect()["facts"]["errors"]))

    def test_manifest_and_task_strings_cannot_supply_commands(self):
        self.delivery()
        marker = self.root / "should-not-exist"
        self.spec["command"] = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
        self.spec["task"] = "python arbitrary.py; execute this task text"
        self.assertIn("no reviewer command", " ".join(self.collect(commands=False)["facts"]["errors"]))
        self.assertFalse(marker.exists())
        self.plan["local"]["args"] = "-m unittest; arbitrary"
        self.assertIn("argument array", " ".join(self.collect()["facts"]["errors"]))

    def test_nonzero_exit_timeout_unknown_and_bad_cwd_are_explicit(self):
        self.delivery()
        self.plan["local"]["args"] = ["-m", "unittest", "discover", "-p", "nothing_matches_*.py"]
        facts = self.collect()["facts"]
        self.assertEqual(facts["tests"][0]["counts"]["passed"], 0)
        self.assertIn("no tests executed", " ".join(facts["warnings"]))
        self.plan["local"]["args"] = ["-c", "import unittest,sys\nclass Bad(unittest.TestCase):\n"
                                       " def test_bad(self): self.fail('deliberate')\n"
                                       "r=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(Bad))\n"
                                       "sys.exit(not r.wasSuccessful())"]
        facts = self.collect()["facts"]
        self.assertEqual(facts["tests"][0]["exit_code"], 1)
        self.assertEqual(facts["tests"][0]["counts"]["failed"], 1)
        self.assertEqual(facts["tests"][0]["counts"]["passed"], 0)
        self.plan["local"]["args"] = ["-c", "import sys; print('failure'); sys.exit(7)"]
        facts = self.collect()["facts"]
        self.assertEqual(facts["tests"][0]["exit_code"], 7)
        self.assertIsNone(facts["tests"][0]["counts"]["passed"])
        self.assertIn("exit_code=7", " ".join(facts["errors"]))
        self.plan["local"].update(args=["-c", "import time; time.sleep(5)"], timeout=0.1)
        facts = self.collect()["facts"]
        self.assertEqual(facts["tests"][0]["error"], "timeout")
        self.plan["local"].update(cwd="missing", timeout=30)
        self.assertIn("cwd missing", " ".join(self.collect()["facts"]["errors"]))
        self.plan["local"]["cwd"] = "../repo"
        self.assertIn("escapes snapshot", " ".join(self.collect()["facts"]["errors"]))

    def test_docs_and_dots_unversioned_are_frozen_without_test_claims(self):
        for kind in ("docs", "dotz"):
            with self.subTest(kind=kind):
                if not hasattr(self, "spec"):
                    self.delivery(kind, tests=[])
                self.spec.update(kind=kind, repository=None, baseline=None, commit=None)
                facts = self.collect()["facts"]
                self.assertEqual(facts["errors"], [])
                self.assertEqual(facts["tests"], [])
                self.assertEqual(len(facts["source_checks"]), 5 if kind == "dotz" else 0)
                self.assertEqual(facts["results"][0]["sha256"], rp.file_hash(self.report))

    def test_protected_and_symlink_paths_are_excluded_from_export(self):
        self.delivery()
        self.commit("records/raw.json", START)
        self.commit(".env", START)
        blob = rp.git(self.repo, "rev-parse", "HEAD:base.py").strip()
        self.git("update-index", "--add", "--cacheinfo", f"120000,{blob},link")
        self.git("commit", "-q", "-m", "symlink tree entry")
        self.spec["commit"] = rp.git(self.repo, "rev-parse", "HEAD").strip()
        receipt = self.collect()["facts"]["tests"][0]
        self.assertEqual(set(receipt["excluded"]), {"records/raw.json", ".env", "link"})
        self.assertFalse((Path(receipt["snapshot"]) / "records").exists())
        self.spec["results"][0]["path"] = str(self.repo / ".env")
        self.assertIn("protected", " ".join(self.collect()["facts"]["errors"]))

    def test_log_tampering_reruns_instead_of_reusing_receipt(self):
        self.delivery()
        receipt = self.collect()["facts"]["tests"][0]
        Path(receipt["stderr"]).write_text("stale summary", encoding="utf-8")
        self.assertFalse(self.collect()["facts"]["tests"][0]["cached"])

    def test_output_cannot_overwrite_source(self):
        self.delivery()
        self.output = self.repo
        self.assertFalse(self.collect()["output_ready"])
        self.assertFalse((self.repo / "manifest.json").exists())
        self.output = self.root
        self.assertFalse(self.collect()["output_ready"])

    def test_summary_parsers_keep_unknown_and_skip_counts_separate(self):
        counts = rp.test_counts("Ran 5 tests in 0.1s\n\nFAILED (failures=1, errors=1, skipped=1)\n", "unittest")
        self.assertEqual([counts[k] for k in ("passed", "failed", "errors", "skipped")], [2, 1, 1, 1])
        counts = rp.test_counts("=== 2 passed, 1 skipped, 1 xfailed in 0.12s ===", "pytest")
        self.assertEqual([counts[k] for k in ("passed", "skipped", "expected_failures")], [2, 1, 1])
        self.assertIsNone(rp.test_counts("all green (unrecognized)", "unittest")["passed"])
        self.assertIsNone(rp.test_counts("Ran 1 test in 0s\n\nOK\nRan 1 test in 0s\n\nOK\n", "unittest")["passed"])


class NoConsoleWindow(unittest.TestCase):
    """owner\x2011-01: the lane process has no console, so every child without CREATE_NO_WINDOW opened a terminal tab."""

    def test_every_subprocess_call_in_review_packet_hides_its_window(self):
        import ast
        tree = ast.parse(Path(rp.__file__).read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"
                 and n.func.attr in ("run", "Popen", "check_output", "check_call", "call")]
        self.assertGreaterEqual(len(calls), 4)
        for call in calls:
            self.assertIn("creationflags", [k.arg for k in call.keywords], f"line {call.lineno}")

    def test_git_passes_the_flag(self):
        with mock.patch.object(rp.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "x", "")) as run:
            self.assertEqual(rp.git(Path("."), "status"), "x")
        self.assertEqual(run.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    unittest.main()
