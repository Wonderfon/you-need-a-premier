"""Isolated trial checks; no models, production files, network or paid APIs."""
import concurrent.futures
import contextlib
import hashlib
import json
import random
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import shadow_review as shadow
import tick


class ShadowReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.audits = self.root / "incore" / "audits"
        self.audits.mkdir(parents=True)
        (self.audits / "result.md").write_text("Original result 原稿\n## 验证\nPASS", encoding="utf-8")
        self.rules = self.root / "rules.md"
        self.rules.write_text("Do not deploy or contact anyone", encoding="utf-8")
        dates_file = self.root / "life-dates.md"
        dates_file.write_text(
            "## 表 1：测试\n| 项目 | 日期 | 提前 30 天 | 提前 7 天 | 下次核对 |\n| --- | --- | --- | --- | --- |\n"
            "## 表 2：测试\n| 步骤 | 日期窗口 | 下次核对 |\n| --- | --- | --- |\n",
            encoding="utf-8",
        )
        for name, value in {"WORKSPACE": self.root, "DATA": self.data, "RUNS_FILE": self.data / "runs.jsonl",
                            "LIFE_DATES_FILE": dates_file,
                            "LIFE_DATES_REPORTED_FILE": self.root / "dates-reported.json",
                            "LOG_FILE": self.data / "log.jsonl", "RULES_FILE": self.rules, "PAUSE_FILE": self.data / "tick.PAUSE",
                            "BACKLOG": self.root / "backlog.md", "CREATE_NO_WINDOW": 0,
                            "BATCH_MAX": 1}.items():  # one row per review session; the batch tests raise it
            p = mock.patch.object(tick, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(tick.CODEX_PROBE, at=time.time(), used=None, behind=None)  # no live quota probe: the burn mode (10-26) stays off
        p.start()
        self.addCleanup(p.stop)
        tick.BACKLOG.write_text("## Arbitros 线\n- [review] Delta：small patch\n", encoding="utf-8")
        self.now =datetime.now(timezone.utc)
        self.quota = {"at": self.now.isoformat(), "limits": {"primary": {
            "used_percent": 7, "window_minutes": 10080, "resets_at": (self.now + timedelta(days=5)).timestamp()}}}
        self.ex = {"at": self.now.isoformat(), "_at": self.now, "role": "work", "section": "Arbitros 线",
                   "kind": "executor", "item": "Delta：small patch", "project": "delta", "engine": "codex",
                   "ok": True, "cost_usd": 0, "run_file": "executor-1.jsonl"}
        self.packet = {"text": "PRE-REVIEW MATERIAL", "hard": None, "facts": {
            "repo": str(self.root), "refs": {"claude/patch": "a" * 40}, "base": "b" * 40,
            "changed": ["src.py"], "result_files": ["result.md"]}}

    def append_ex(self, ex=None):
        tick.append_jsonl(tick.RUNS_FILE, {k: v for k, v in (ex or self.ex).items() if k != "_at"})

    def rows(self):
        return tick.load_runs()

    def sol(self, text, formal_verdict="VERDICT: pass", real_resident=False):
        """Patch everything outside the scheduler: sol's stream is written by a fake execute, the deputy by a fake run_resident
        (real_resident: run_resident itself runs, its session_run is faked and writes `formal_verdict` as the result)."""
        self.sol_text = text  # a test may change it between rows; a callable gets evidence.md and returns the text

        def fake_execute(tk, evidence, stream):
            events = [{"type": "thread.started", "thread_id": "t1"},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": self.sol_text(evidence.read_text(encoding="utf-8"))
                                                          if callable(self.sol_text) else self.sol_text}},
                      {"type": "turn.completed", "usage": {"input_tokens": 10}}]
            stream.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
            return 0
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(tick, "codex_weekly_used", return_value=7.0))
        self.execute = stack.enter_context(mock.patch.object(shadow, "execute", side_effect=fake_execute))
        stack.enter_context(mock.patch.object(tick.review_packet, "git", return_value="diff"))
        stack.enter_context(mock.patch.object(tick, "review_packet_for", return_value=self.packet))
        stack.enter_context(mock.patch.object(tick, "fresh_usage", return_value=(1, self.now.timestamp() + 9000)))
        self.note = stack.enter_context(mock.patch.object(tick, "note_verdict"))
        if not real_resident:
            self.formal = stack.enter_context(mock.patch.object(tick, "run_resident", return_value=(
                0, self.data / "formal.jsonl", True, formal_verdict, 1)))
            return
        self.deputy_text, self.prompts = formal_verdict, []

        def fake_session(kind, prompt, *args, **kwargs):
            self.prompts.append(prompt)
            stream = self.data / f"deputy-{len(self.prompts)}.jsonl"
            stream.write_text("\n".join(json.dumps(e) for e in (
                {"type": "assistant"}, {"type": "result", "result": self.deputy_text, "total_cost_usd": 0.9,
                                        "usage": {"input_tokens": 100, "output_tokens": 40}})), encoding="utf-8")
            return 0, stream, "sid", False
        stack.enter_context(mock.patch.object(tick, "load_deputies", return_value={}))
        stack.enter_context(mock.patch.object(tick, "update_deputy"))
        stack.enter_context(mock.patch.object(tick, "maybe_compact", return_value=0))
        stack.enter_context(mock.patch.object(tick, "session_run", side_effect=fake_session))

    def review(self, item):
        """A reviewed-later executor row and its [review] line, run through the lane."""
        self.append_ex({**self.ex, "item": item})
        line = "- [review] " + item
        if line not in tick.BACKLOG.read_text(encoding="utf-8"):
            tick.BACKLOG.write_text(tick.BACKLOG.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")
        tick.run_lane_work("work", "Arbitros 线", "review", line, item, 1, 20)
        return line

    def test_gate_is_the_executors_weekly_quota_not_the_old_shadow_window(self):
        self.assertEqual((shadow.DAILY_CAP, tick.CODEX_WEEKLY_MAX), (200, 95.0))
        for i, (used, runs) in enumerate(((95.0, 0), (96.0, 0), (94.9, 1), (None, 1))):  # unreadable runs, as the executors do
            ex = {**self.ex, "item": f"Delta：small patch {i}"}
            self.append_ex(ex)
            with mock.patch.object(tick, "codex_weekly_used", return_value=used), \
                    mock.patch.object(tick.review_packet, "git", return_value="diff"), \
                    mock.patch.object(shadow, "execute", return_value=0) as execute:
                shadow.run(tick, "work", "Arbitros 线", ex["item"], self.packet)
                self.assertEqual(execute.call_count, runs, used)

    def test_weekly_used_reads_the_weekly_window_and_never_guesses_zero(self):
        self.assertEqual(shadow.weekly_used(self.quota, self.now), 7.0)
        future = self.now.timestamp() + 99
        other = {**self.quota, "limits": {"primary": {"used_percent": 3, "window_minutes": 300}, "secondary": {
            "used_percent": 96, "window_minutes": 10080, "resets_at": future}}}
        self.assertEqual(shadow.weekly_used(other, self.now), 96.0)
        old = {**self.quota, "at": (self.now - timedelta(days=2)).isoformat()}  # any age: the window has not reset yet
        self.assertEqual(shadow.weekly_used(old, self.now), 7.0)
        week = lambda **w: {**self.quota, "limits": {"primary": {"window_minutes": 10080, **w}}}
        for value in ({}, None, {"at": self.now.isoformat(), "limits": {}}, {"at": self.now.isoformat(), "limits": {
                "primary": {"used_percent": 3, "window_minutes": 300, "resets_at": future}}},
                week(used_percent=float("nan"), resets_at=future), week(used_percent=5),  # no resets_at
                week(used_percent=5, resets_at=self.now.timestamp() - 1)):  # window already reset
            self.assertIsNone(shadow.weekly_used(value, self.now))

    def test_eligibility_excludes_quant_tick_money_and_credentials(self):
        self.assertTrue(shadow.eligible(tick, "work", self.ex["item"], self.packet, self.ex))
        self.assertFalse(shadow.eligible(tick, "quant", self.ex["item"], self.packet, self.ex))
        for role in ("life", "intelligence"):
            self.assertTrue(shadow.eligible(tick, role, self.ex["item"], self.packet, self.ex))
        for path in ("control/tick.py", "src/broker.py", "orders.py", "quant/notes.md", ".env",
                     "config/token.json", "records/original.json", "input/data/x", "02_secrets/x"):
            packet = {**self.packet, "facts": {**self.packet["facts"], "changed": [path]}}
            self.assertFalse(shadow.eligible(tick, "work", self.ex["item"], packet, self.ex), path)
        for cost in (None, 1.49, 1.5, 2, 4.6, -1, float("nan")):  # no cost limit
            self.assertTrue(shadow.eligible(tick, "work", self.ex["item"], self.packet,
                                            {**self.ex, "engine": "claude", "cost_usd": cost}), cost)
        self.assertFalse(shadow.eligible(tick, "work", self.ex["item"], self.packet, {**self.ex, "ok": False}))

    def test_name_misjudgments_are_released_and_real_credentials_still_blocked(self):
        def why(path):
            packet = {**self.packet, "facts": {**self.packet["facts"], "changed": [path, "src.py"]}}  # src.py: not an .md-only branch
            return shadow.skip_reason(tick, "work", self.ex["item"], packet, self.ex)
        for path in ("control/test_tick.py", "audits/2026-11-04-x-tick.py.txt", "data/tokenizer_v2.json", "data/trainsets_tokens.json",
                     "data/spaced_template_tokenization.json", "projects/local-secretary-v0.md"):
            self.assertIsNone(why(path), path)  # tick.py as a substring, token/secret inside a longer word
        for path in ("control/tick.py", "tick.py", ".env", ".env.local", "token.json", "tokens.txt", "api_token.txt", "github_tokens.json",
                     "client_secret.json", "id_rsa", "a/b.dpapi", "a.key", "02_secrets/x", "my_secret.txt", "credentials.json",
                     "passwords.csv", "records/x.json"):
            self.assertIsNotNone(why(path), path)

    def test_snapshot_of_a_branch_with_thousands_of_files_does_not_pass_them_on_the_command_line(self):
        packet = {**self.packet, "facts": {**self.packet["facts"], "changed": [f"experiments/f{i}.py" for i in range(2000)]}}
        with mock.patch.object(tick.review_packet, "git", return_value="diff") as git:
            shadow.snapshot(tick, self.root / "ev", self.ex["item"], packet, self.ex)
        self.assertLess(sum(len(str(a)) for a in git.call_args.args), 500)  # a pathspec of 800+ paths overran Windows' limit (11-04 OSError)

    def test_sol_first_crash_before_the_evidence_is_frozen_is_retried_twice_only(self):
        self.sol("VERDICT: pass")
        self.append_ex()

        def pending():
            return [c for c in shadow.candidates(tick, tick.parse_backlog("## Arbitros 线\n- [review] Delta：small patch\n"), tick.load_runs())]
        bad = {**self.packet, "facts": {**self.packet["facts"], "result_files": ["x.pem"]}}  # snapshot raises ValueError
        self.assertEqual(len(pending()), 1)
        for n in (1, 2):
            context, reason = shadow.run(tick, "work", "Arbitros 线", self.ex["item"], bad)
            self.assertIsNone(context)
            self.assertEqual(len(pending()), 1, n)  # a crash is not "tried"
        shadow.run(tick, "work", "Arbitros 线", self.ex["item"], bad)
        self.assertEqual(pending(), [])  # the third crash ends it
        self.assertEqual(self.execute.call_count, 0)

    def test_sol_first_retry_after_a_crash_reviews_normally(self):
        self.sol("VERDICT: pass")
        self.append_ex()
        bad = {**self.packet, "facts": {**self.packet["facts"], "result_files": ["x.pem"]}}
        self.assertIsNone(shadow.run(tick, "work", "Arbitros 线", self.ex["item"], bad)[0])
        context, reason = shadow.run(tick, "work", "Arbitros 线", self.ex["item"], self.packet)
        self.assertIsNotNone(context, reason)
        self.assertEqual(self.execute.call_count, 1)
        self.assertEqual(shadow.candidates(tick, tick.parse_backlog("## Arbitros 线\n- [review] Delta：small patch\n"), tick.load_runs()), [])

    def test_primary_review_excludes_owner_facing_opus_and_reviewer_merged_rows(self):
        def ok(item="Delta：small patch", repo=None, changed=("src.py",)):
            packet = {**self.packet, "facts": {**self.packet["facts"], "repo": repo or self.packet["facts"]["repo"], "changed": list(changed)}}
            return shadow.eligible(tick, "work", item, packet, {**self.ex, "item": item})
        self.assertTrue(ok())
        for word in ("给 owner 试用", "过观众试看", "官网改版", "发布前检查", "对外联系人", "上线后验收", "过审当场合并", "owner 试用一周"):
            self.assertFalse(ok(f"Delta：{word}"), word)
        for word in ("不对外联系的小修", "不要发布", "别上线", "无需通知 owner", "禁止对外", "不给 owner 看", "不过观众试看",
                     "给 owner 的报告格式照旧", "owner\x2010-25 终端说的",
                     # owner\x2011-03: internal reports only for the owner to read go to sol first
                     "给owner看", "交给 owner 审", "交 owner 拍板", "通知 owner", "owner看一眼"):
            self.assertTrue(ok(f"Delta：{word}"), word)
        self.assertFalse(ok("Delta：不对外联系，但要发布"))  # a negation covers only the keyword right after it
        self.assertFalse(ok("Delta：画吉祥物" + tick.OPUS_TAG))
        self.assertTrue(ok("Delta：x" + tick.CLAUDE_TAG))  # only Opus-tagged rows are excluded
        for name in ("Thinking Engines", "style_rewriter", "driving-course-sim", "Blade_Heart_Battle_Demo"):
            self.assertFalse(ok(repo=str(self.root / "projects" / name)), name)
        self.assertTrue(ok(repo=str(self.root / "projects" / "Uvox")))
        self.assertFalse(ok(changed=("README.md", "docs/a.MD")))  # a branch that changes only .md: the reviewer may merge it
        self.assertTrue(ok(changed=("README.md", "src.py")))
        self.assertFalse(shadow.eligible(tick, "quant", "量化：x", self.packet, self.ex))

    def test_rows_without_git_changes_need_only_a_result_file(self):
        def why(**facts):
            base = {"repo": None, "refs": {}, "base": None, "changed": [], "result_files": ["result.md"]}
            return shadow.skip_reason(tick, "work", self.ex["item"], {"text": "PRE", "hard": None, "facts": {**base, **facts}}, self.ex)
        self.assertIsNone(why())  # no repo at all
        self.assertIsNone(why(repo=str(self.root)))  # a repo, no branch
        self.assertEqual(why(result_files=[]), "没有结果文件")
        self.assertIn("缺 repo 或 base", why(refs={"claude/x": "a" * 40}, repo=str(self.root)))
        self.assertIsNone(why(refs={"claude/x": "a" * 40}, repo=str(self.root), base="b" * 40))
        self.assertEqual(why(repo=str(self.root / "Thinking Engines")), "审查员可合并的仓库 Thinking Engines")
        facts = {"repo": None, "refs": {}, "base": None, "changed": [], "result_files": ["result.md"]}
        evidence = shadow.snapshot(tick, self.data / "nogit", self.ex["item"], {"text": "PRE", "hard": None, "facts": facts}, self.ex)
        text = evidence.read_text(encoding="utf-8")
        self.assertIn("Original result 原稿", text)
        self.assertIn("no git changes", text)
        self.assertNotIn("```diff", text)
        self.assertNotIn("Repository:", text)
        unsafe = {"text": "PRE", "hard": None, "facts": {**facts, "result_files": ["../secret.md"]}}
        with self.assertRaises(ValueError):
            shadow.snapshot(tick, self.data / "nogit2", self.ex["item"], unsafe, self.ex)

    def test_life_writing_branches_are_eligible_with_deputy_merge_handoff(self):
        for repo in shadow.LIFE_MERGE_REPOS:
            for changed in (["src.py"], ["README.md", "docs/notes.md"]):
                with self.subTest(repo=repo, changed=changed):
                    packet = {**self.packet, "facts": {**self.packet["facts"],
                              "repo": str(self.root / repo), "changed": changed}}
                    self.assertTrue(shadow.eligible(tick, "life", "写作模型：本地小修", packet, self.ex))
                    self.assertFalse(shadow.eligible(tick, "quant", "写作模型：本地小修", packet, self.ex))
                    protected = {**packet, "facts": {**packet["facts"], "changed": ["records/original.md"]}}
                    self.assertFalse(shadow.eligible(tick, "life", "写作模型：本地小修", protected, self.ex))
        for repo in ("Thinking Engines", "driving-course-sim", "Blade_Heart_Battle_Demo"):
            packet = {**self.packet, "facts": {**self.packet["facts"], "repo": str(self.root / repo)}}
            self.assertFalse(shadow.eligible(tick, "life", self.ex["item"], packet, self.ex))
        missing = {**self.packet, "facts": {**self.packet["facts"], "repo": None, "refs": {}}}
        self.assertIn("不能交接合并", shadow.skip_reason(tick, "life", "写作模型：修 claude/patch", missing,
                                                       {**self.ex, "project": "writing"}))
        report = {**missing, "facts": {**missing["facts"], "repo": str(self.root / "style_rewriter"),
                                      "changed": []}}
        self.assertTrue(shadow.eligible(tick, "life", "写作模型：内部报告给 owner 看", report,
                                        {**self.ex, "project": "writing"}))
        self.assertEqual(shadow.life_merge_branches("life", report["facts"]), [])
        worktree = {**self.packet, "facts": {**self.packet["facts"], "repo": str(self.root / "poetry-reading-align"),
                                            "main_repo": str(self.root / "poetry-mask")}}
        self.assertTrue(shadow.eligible(tick, "life", "写作模型：隔离分支", worktree, self.ex))
        self.assertEqual(shadow.life_merge_branches("life", worktree["facts"]), ["claude/patch"])
        other = {**worktree, "facts": {**worktree["facts"], "main_repo": str(self.root / "Blade_Heart_Battle_Demo")}}
        self.assertFalse(shadow.eligible(tick, "life", "项目七：隔离分支", other, self.ex))

    def test_life_audience_and_release_work_stays_with_deputy_even_when_burning(self):
        packet = {**self.packet, "facts": {**self.packet["facts"], "repo": str(self.root / "poetry-mask")}}
        for burning in (False, True):
            with mock.patch.object(tick, "codex_burning", return_value=burning):
                for word in ("观众试看", "给 owner 试用", "对外", "发布", "上线", tick.OPUS_TAG):
                    item = "写作模型：" + word
                    ex = {**self.ex, "role": "life", "section": "生活线", "item": item, "project": "writing"}
                    secs = {"生活线": {"review_rows": [("- [review] " + item, item)]}}
                    self.assertFalse(shadow.eligible(tick, "life", item, packet, ex), (burning, word))
                    self.assertEqual(shadow.candidates(tick, secs, [ex]), [], (burning, word))
        with mock.patch.object(tick, "codex_burning", return_value=False):
            for role in ("life", "work", "uvox", "intelligence", "coordinator"):
                item = "组织：内部报告给 owner 看，交 owner 阅读"
                ex = {**self.ex, "role": role, "item": item, "engine": "claude"}
                self.assertTrue(shadow.eligible(tick, role, item, self.packet, ex), role)
                secs = {tick.LINE_SECTIONS[role]: {"review_rows": [("- [review] " + item, item)]}}
                self.assertEqual(len(shadow.candidates(tick, secs, [ex])), 1, role)

    def test_life_pass_atomically_adds_frozen_merge_branches_once_in_its_own_section(self):
        for repo in shadow.LIFE_MERGE_REPOS:
            item = f"写作模型：{repo} 本地小修，给 owner 看报告"
            self.packet = {**self.packet, "facts": {**self.packet["facts"], "repo": str(self.root / repo),
                           "refs": {f"claude/{repo}-fix": "a" * 40, f"claude/{repo}-docs": "c" * 40}}}
            lines = self.queue([item], role="life", section="生活线")
            tick.BACKLOG.write_text("## 组织线\n- [review] 组织：keep\n" + tick.BACKLOG.read_text(encoding="utf-8")
                                    + "## 量化线\n- [review] 量化：keep\n", encoding="utf-8")
            self.sol("证据 ok\nVERDICT: pass")
            with self.draw(4):
                context, reason = shadow.run(tick, "life", "生活线", item, self.packet)
                self.assertIsNone(reason)
                # The branch may move after the review. Handoff must use the frozen packet, never a new scan.
                self.packet["facts"]["refs"] = {"claude/not-reviewed": "d" * 40}
                self.assertTrue(shadow.primary(tick, "life", "生活线", lines[0], item, context))
                after = tick.BACKLOG.read_text(encoding="utf-8")
                self.assertTrue(shadow.primary(tick, "life", "生活线", lines[0], item, context))
            self.assertEqual(tick.BACKLOG.read_text(encoding="utf-8"), after)
            self.assertIn("sol 审查通过", after)
            self.assertIn("## 组织线\n- [review] 组织：keep\n", after)
            self.assertTrue(after.endswith("## 量化线\n- [review] 量化：keep\n"))
            life = after.split("## 生活线\n")[1].split("## 量化线")[0]
            for suffix in ("fix", "docs"):
                self.assertEqual(life.count(f"- [todo] 写作模型：合并 claude/{repo}-{suffix}（sol 已审过，生活副总理照 10-04 B 合并）"), 1)
            self.assertNotIn("claude/not-reviewed", life)
            self.assertEqual(sum(r.get("primary") == "sol" and r.get("item") == item for r in self.rows()), 1)
            self.formal.assert_not_called()

    def test_life_spot_or_nonpass_has_no_merge_todo_and_lane_pass_does(self):
        self.packet["facts"]["repo"] = str(self.root / "style_rewriter")
        for n, (verdict, pick) in enumerate((("rework", 4), ("blocked", 4), ("pass", 0), ("pass", 4))):
            self.sol("VERDICT: " + verdict)
            self.queue([f"写作模型：patch {n}"], role="life", section="生活线")
            ex = self.rows()[-1]
            rid = shadow.review_id_of("life", "生活线", ex["item"], ex)
            # A fresh block makes each case's spot position independent.
            shadow.update_state(tick, lambda s: s.update(passes=0))
            with self.draw(pick):
                self.assertEqual(tick.sol_first_lane("life", rid), 0)
            text = tick.BACKLOG.read_text(encoding="utf-8")
            self.assertEqual("写作模型：合并 claude/patch" in text, n == 3)
            self.assertEqual("sol 审查通过" in text, n == 3)
            self.formal.assert_not_called()

    def test_life_handoff_refuses_changed_row_without_partial_done(self):
        self.packet["facts"]["repo"] = str(self.root / "poetry-mask")
        item = "写作模型：本地小修"
        line, = self.queue([item], role="life", section="生活线")
        self.sol("VERDICT: pass")
        with self.draw(4):
            context, reason = shadow.run(tick, "life", "生活线", item, self.packet)
            self.assertIsNone(reason)
            tick.BACKLOG.write_text("## 生活线\n" + line + " changed\n", encoding="utf-8")
            self.assertFalse(shadow.primary(tick, "life", "生活线", line, item, context))
            self.assertNotIn("合并 claude/", tick.BACKLOG.read_text(encoding="utf-8"))
            self.assertNotIn("sol 审查通过", tick.BACKLOG.read_text(encoding="utf-8"))
            tick.BACKLOG.write_text("## 生活线\n" + line + "\n", encoding="utf-8")
            self.assertTrue(shadow.primary(tick, "life", "生活线", line, item, context))

    def test_old_life_freeze_without_merge_metadata_stays_with_deputy(self):
        self.packet["facts"]["repo"] = str(self.root / "poetry-mask")
        item = "写作模型：旧冻结材料"
        line, = self.queue([item], role="life", section="生活线")
        self.sol("VERDICT: pass")
        with self.draw(4):
            context, reason = shadow.run(tick, "life", "生活线", item, self.packet)
            self.assertIsNone(reason)
            rows = [json.loads(row) for row in tick.RUNS_FILE.read_text(encoding="utf-8").splitlines()]
            rows[-1].pop("merge_branches")
            tick.RUNS_FILE.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            self.assertFalse(shadow.primary(tick, "life", "生活线", line, item, context))
        self.assertIn(line, tick.BACKLOG.read_text(encoding="utf-8"))
        self.assertNotIn("合并 claude/", tick.BACKLOG.read_text(encoding="utf-8"))

    def test_no_git_row_goes_through_sol_first_review_end_to_end(self):
        self.packet = {"text": "PRE-REVIEW MATERIAL", "hard": None, "facts": {
            "repo": None, "refs": {}, "base": None, "changed": [], "result_files": ["result.md"]}}
        self.sol("读了结果文件\n## 建议的后续\n无\nVERDICT: pass")
        with self.draw(4):
            self.review(self.ex["item"])
        self.formal.assert_not_called()
        self.assertIn("sol 审查通过", tick.BACKLOG.read_text(encoding="utf-8"))

    def test_exclusion_looks_at_changed_files_not_task_text(self):
        item = "组织：小修，不改 control/tick.py、不碰券商和下单代码，资金路径不动"
        self.assertTrue(shadow.eligible(tick, "work", item, self.packet, {**self.ex, "item": item}))
        touched = {**self.packet, "facts": {**self.packet["facts"], "changed": ["src.py", "control/tick.py"]}}
        self.assertFalse(shadow.eligible(tick, "work", item, touched, {**self.ex, "item": item}))
        self.assertFalse(shadow.eligible(tick, "quant", item, self.packet, {**self.ex, "item": item}))
        self.assertFalse(shadow.eligible(tick, "work", item, self.packet, {**self.ex, "item": item, "project": "quant"}))

    def test_atomic_sixty_attempt_cap_duplicates_and_no_expiry(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            reserved = list(pool.map(lambda i: shadow.reserve(tick, str(i), self.now), range(shadow.DAILY_CAP + 20)))
        self.assertEqual(sum(ok for ok, _ in reserved), shadow.DAILY_CAP)
        state = json.loads((self.data / "shadow-review-state.json").read_text(encoding="utf-8"))
        first = state["attempts"][0]["id"]
        self.assertEqual(shadow.reserve(tick, first, self.now + timedelta(days=2)), (False, "重复"))
        self.assertEqual(shadow.reserve(tick, "next-day", self.now + timedelta(days=2)), (True, None))  # 2 days, not 1: the fall-back day has 25 hours, +24 h can still be the same local date
        self.assertEqual(shadow.reserve(tick, "after-the-old-trial", self.now + timedelta(days=30)), (True, None))  # the 14-day trial is gone
        (self.data / "shadow-review-state.json").write_text("invalid", encoding="utf-8")
        with self.assertRaises(ValueError):
            shadow.reserve(tick, "corrupt", self.now)

    def test_freeze_pins_git_and_result_even_after_rework(self):
        repo = self.root / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run([tick.review_packet.GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        (repo / "src.py").write_text("answer = 1\n", encoding="utf-8")
        git("add", "src.py")
        git("-c", "user.name=codex", "-c", "user.email=codex@local", "commit", "-m", "base")
        base = git("rev-parse", "HEAD")
        git("switch", "-c", "claude/patch")
        (repo / "src.py").write_text("answer = 2\n", encoding="utf-8")
        git("add", "src.py")
        git("-c", "user.name=codex", "-c", "user.email=codex@local", "commit", "-m", "patch")
        sha = git("rev-parse", "HEAD")
        facts = {**self.packet["facts"], "repo": str(repo), "base": base, "refs": {"claude/patch": sha}}
        evidence = shadow.snapshot(tick, self.data / "frozen", self.ex["item"], {**self.packet, "facts": facts}, self.ex)
        original = evidence.read_bytes()
        (repo / "src.py").write_text("answer = 3\n", encoding="utf-8")
        git("add", "src.py")
        git("-c", "user.name=codex", "-c", "user.email=codex@local", "commit", "-m", "later rework")
        (self.audits / "result.md").write_text("Overwritten by reviewer", encoding="utf-8")
        self.assertEqual(evidence.read_bytes(), original)
        self.assertIn(sha, original.decode())
        self.assertIn("+answer = 2", original.decode())
        self.assertIn("Original result 原稿", original.decode())
        self.assertNotIn("answer = 3", original.decode())

    def test_real_subprocess_surface_and_no_authoritative_side_effects(self):
        """Run a real child process in place of the model; check command shape, stdin and ledger."""
        stub = self.root / "model_stub.py"
        stub.write_text('''import json, sys
from pathlib import Path
assert "-s" in sys.argv and sys.argv[sys.argv.index("-s")+1] == "read-only"
assert "--add-dir" not in sys.argv
assert 'approval_policy="never"' in sys.argv
assert "evidence.md" in sys.stdin.read()
directory = sys.argv[sys.argv.index("-C")+1]  # production passes /mnt\x2fc/...; WSL reads it, a Windows stub maps it back
if sys.platform == "win32" and directory.startswith("/mnt/"):
    directory = directory[5].upper() + ":" + directory[6:]
directory = Path(directory)
assert "PRE-REVIEW MATERIAL" in (directory/"evidence.md").read_text(encoding="utf-8")
print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"SHADOW ONLY REASON\\nVERDICT: rework"}}))
print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":10}}))
''', encoding="utf-8")
        self.append_ex()
        before = tick.BACKLOG.read_bytes()
        calls = []
        def formal(*args, **kwargs):
            self.assertEqual(len([r for r in self.rows() if r["kind"] == "shadow_review"]), 1)
            self.assertIn("冻结材料", args[2])
            self.assertNotIn("SHADOW ONLY REASON", args[2])  # shadow result isn't given to Opus
            calls.append(args[4])
            (self.data / "unused.jsonl").write_text("", encoding="utf-8")
            tick.record_run("work", "Arbitros 线", "review", "medium", "new", "official", self.ex["item"],
                            1, 0, True, "VERDICT: pass", self.data / "unused.jsonl", metadata={
                                k: args[4][k] for k in ("review_id", "review_snapshot")})
            return 0, self.data / "unused.jsonl", True, "VERDICT: pass", 1
        with mock.patch.object(tick, "CODEX", [sys.executable, "-u", str(stub)]), \
                mock.patch.object(tick, "codex_weekly_used", return_value=7.0), \
                mock.patch.object(tick.review_packet, "git", return_value="diff frozen source"), \
                mock.patch.object(tick, "review_packet_for", return_value=self.packet), \
                mock.patch.object(tick, "fresh_usage", return_value=(1, self.now.timestamp() + 9000)), \
                mock.patch.object(tick, "run_resident", side_effect=formal), \
                mock.patch.object(tick, "note_verdict") as note:
            tick.run_lane_work("work", "Arbitros 线", "review", "- [review] " + self.ex["item"], self.ex["item"], 1, 20)
        note.assert_called_once_with("work", self.ex["item"], "pass")
        self.assertEqual(tick.BACKLOG.read_bytes(), before)
        rows = self.rows()
        shadow_row, formal_row = rows[1:]
        self.assertEqual((shadow_row["kind"], shadow_row["verdict"], shadow_row["ok"]), ("shadow_review", "rework", True))
        self.assertEqual(shadow_row["review_id"], formal_row["review_id"])
        self.assertEqual(shadow_row["cost_usd"], 0)
        self.assertEqual(shadow_row["snapshot_sha256"], hashlib.sha256(Path(calls[0]["review_snapshot"]).read_bytes()).hexdigest())
        self.assertEqual(shadow.summary(rows)["agreement"], 0)

    def test_failures_unknown_quota_and_retry_do_not_repeat_or_block(self):
        self.append_ex()
        with mock.patch.object(tick, "codex_weekly_used", return_value=95.0), mock.patch.object(shadow, "execute") as execute:
            context, reason = shadow.run(tick, "work", "Arbitros 线", self.ex["item"], self.packet)
            self.assertIsNone(context)
            self.assertIn("本周额度", reason)
            execute.assert_not_called()
        with mock.patch.object(tick, "codex_weekly_used", return_value=7.0), \
                mock.patch.object(tick.review_packet, "git", return_value="diff"), \
                mock.patch.object(shadow, "execute", side_effect=OSError("model unavailable")) as execute:
            context, reason = shadow.run(tick, "work", "Arbitros 线", self.ex["item"], self.packet)
            self.assertIn("review_id", context)
            self.assertIsNone(reason)
            self.assertEqual(shadow.run(tick, "work", "Arbitros 线", self.ex["item"], self.packet), (context, None))
            execute.assert_called_once()
        row = self.rows()[-1]
        self.assertEqual((row["kind"], row["ok"], row["verdict"]), ("shadow_review", False, "unknown"))
        self.assertEqual(shadow.summary(self.rows())["paired"], 0)

    def test_run_returns_specific_reasons_for_each_skip_gate(self):
        self.append_ex()
        self.assertEqual(shadow.run(tick, "quant", "量化线", self.ex["item"], self.packet),
                         (None, "不合格：量化线"))
        with mock.patch.object(shadow, "load_state", return_value={"disabled": True}):
            self.assertEqual(shadow.run(tick, "work", "Arbitros 线", self.ex["item"], self.packet), (None, "已禁用"))
        with mock.patch.object(tick, "codex_weekly_used", return_value=95.0):
            quota = shadow.run(tick, "work", "Arbitros 线", self.ex["item"], self.packet)
        self.assertEqual(quota, (None, "本周额度已到上限：95%（上限 95%）"))
        with mock.patch.object(tick, "codex_weekly_used", return_value=7.0), \
                mock.patch.object(shadow, "reserve", return_value=(False, "当日上限")):
            daily = shadow.run(tick, "work", "Arbitros 线", self.ex["item"], self.packet)
        with mock.patch.object(tick, "codex_weekly_used", return_value=7.0), \
                mock.patch.object(shadow, "reserve", return_value=(False, "重复")):
            duplicate = shadow.run(tick, "work", "Arbitros 线", self.ex["item"], self.packet)
        self.assertEqual(daily, (None, f"当日上限（{shadow.DAILY_CAP} 次）"))
        self.assertEqual(duplicate, (None, "重复"))
        logged = [json.loads(line)["reason"] for line in tick.LOG_FILE.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(logged, [quota[1], daily[1], duplicate[1]])

    def test_claude_gate_rechecked_after_shadow_and_retry_reuses_same_snapshot(self):
        self.append_ex()
        with mock.patch.object(tick, "codex_weekly_used", return_value=7.0), \
                mock.patch.object(tick.review_packet, "git", return_value="diff"), \
                mock.patch.object(tick, "review_packet_for", return_value=self.packet), \
                mock.patch.object(shadow, "execute", side_effect=OSError("unavailable")) as execute, \
                mock.patch.object(tick, "fresh_usage", side_effect=[(95, self.now.timestamp() + 9000), (1, self.now.timestamp() + 9000)]), \
                mock.patch.object(tick, "run_resident", return_value=(0, self.data / "formal.jsonl", True, "VERDICT: pass", 1)) as formal, \
                mock.patch.object(tick, "note_verdict"):
            for i in range(2):
                tick.run_lane_work("work", "Arbitros 线", "review", "- [review] " + self.ex["item"], self.ex["item"], 1, 20)
                self.assertEqual(formal.call_count, i)
            execute.assert_called_once()
        self.assertEqual(len([r for r in self.rows() if r["kind"] == "shadow_review"]), 1)
        self.assertIn("shadow_official_deferred", tick.LOG_FILE.read_text(encoding="utf-8"))

    def test_timeout_kills_only_shadow_child(self):
        with mock.patch.object(shadow.subprocess, "Popen") as popen, mock.patch.object(tick, "kill_tree") as kill:
            proc = popen.return_value
            proc.communicate.side_effect = [subprocess.TimeoutExpired("stub", 600), (None, None)]
            code = shadow.execute(tick, self.root / "evidence.md", self.data / "timeout.jsonl")
        self.assertEqual(code, -1)
        kill.assert_called_once_with(proc.pid)
        self.assertEqual(proc.communicate.call_count, 2)

    def test_summary_joins_exact_attempt_and_rejects_failed_or_sonnet_baselines(self):
        rows = []
        for n in range(42):
            at = (self.now + timedelta(seconds=n)).isoformat()
            rows += [{"kind": "shadow_review", "review_id": str(n), "ok": True, "verdict": "pass" if n < 3 else "blocked"},
                     {"kind": "review", "review_id": str(n), "ok": True, "model": "opus", "verdict": "rework", "at": at}]
        # An unrelated pass and a later changed verdict cannot overwrite the original Opus decision.
        rows += [{"kind": "review", "review_id": "0", "ok": True, "model": "opus", "verdict": "pass", "at": self.now.isoformat()},
                 {"kind": "shadow_review", "review_id": "failed", "ok": False, "verdict": "pass"},
                 {"kind": "review", "review_id": "failed", "ok": True, "model": "opus", "verdict": "pass"},
                 {"kind": "shadow_review", "review_id": "sonnet", "ok": True, "verdict": "pass"},
                 {"kind": "review", "review_id": "sonnet", "ok": True, "model": "sonnet", "verdict": "pass"}]
        report = shadow.summary(rows)
        self.assertEqual((report["paired"], report["unpaired_or_failed"], report["opus_rejected"]), (42, 1, 42))
        self.assertEqual((report["non_opus_pairs"], report["non_opus_agreement"]), (1, 1))
        self.assertEqual(report["misses"], ["0", "1", "2"])
        self.assertEqual((report["last40_rejected"], report["last40_misses"]), (40, 1))
        self.assertEqual(shadow.summary([])["agreement"], None)

    def test_shadow_ledger_does_not_change_executor_budget_fairness_or_backoff(self):
        text = "## Arbitros 线\n- [todo] Delta：a\n## 生活线\n- [todo] 驾校模拟器：b\n"
        rows = [{**self.ex, "kind": "shadow_review", "model": shadow.MODEL, "ok": False} for _ in range(50)]
        with mock.patch.object(tick, "CODEX_TIERS", ((shadow.MODEL, ("high",), 1, False),)):
            self.assertIsNotNone(tick.codex_tier("large task", rows))
            self.assertEqual(tick.codex_pick(text, rows, self.now, set()), tick.codex_pick(text, [], self.now, set()))
        self.assertFalse(tick.failed_recently(rows, "Arbitros 线", self.now))
        stats = tick.review_stats.render(rows, [], [], text, tick, self.now)
        self.assertIn("Codex 每次执行（窗口内 0 次）", stats)

    def test_resident_official_ledger_keeps_exact_pair_metadata(self):
        stream = self.data / "official.jsonl"
        stream.write_text(json.dumps({"type": "result", "result": "VERDICT: pass"}), encoding="utf-8")
        context = {"review_id": "execution-unique", "review_snapshot": "immutable-evidence.md"}
        with mock.patch.object(tick, "load_deputies", return_value={}), \
                mock.patch.object(tick, "update_deputy"), \
                mock.patch.object(tick, "session_run", return_value=(0, stream, "official", False)), \
                mock.patch.object(tick, "maybe_compact", return_value=0):
            tick.run_resident("work", "review", "normal formal prompt", {}, {"kind": "review", **context},
                              item=self.ex["item"], section="Arbitros 线")
        row, = self.rows()
        self.assertEqual((row["kind"], row["model"], row["verdict"]), ("review", "opus", "pass"))
        self.assertEqual({k: row[k] for k in context}, context)

    def draw(self, pick):
        """sol passes: the spot check falls on position `pick` of each block of five."""
        return mock.patch.object(shadow, "RNG", mock.Mock(randrange=lambda n: pick))

    def test_sol_pass_becomes_done_without_a_deputy_review(self):
        self.sol("证据 ok\n## 建议的后续\n- 补一个测试\nVERDICT: pass")
        with self.draw(4):
            self.review(self.ex["item"])
        self.formal.assert_not_called()
        self.note.assert_called_once_with("work", self.ex["item"], "pass")
        backlog = tick.BACKLOG.read_text(encoding="utf-8")
        m = re.search(r"- \[done \d\d-\d\d \d\d:\d\d：sol 审查通过，audits/([^\]]+-sol-review\.md)\] " + re.escape(self.ex["item"]), backlog)
        self.assertTrue(m, backlog)
        self.assertNotIn("[review]", backlog)
        audit = (self.audits / m.group(1)).read_text(encoding="utf-8")
        self.assertIn("## 建议的后续", audit)
        self.assertTrue(audit.rstrip().endswith("VERDICT: pass"))
        rows = self.rows()
        executor, shadow_row, review = rows
        self.assertEqual((review["kind"], review["engine"], review["model"], review["verdict"], review["ok"], review["primary"], review["cost_usd"]),
                         ("review", "codex", shadow.MODEL, "pass", True, "sol", 0.0))
        self.assertEqual((review["review_id"], review["audit_file"]), (shadow_row["review_id"], "audits/" + m.group(1)))
        self.assertEqual(tick.kpi.review_join(rows), {0: "pass"})  # the executor's first pass is counted, once
        report = shadow.summary(rows)
        self.assertEqual((report["primary_done"], report["paired"], report["unpaired_or_failed"]), (1, 0, 1))
        stats = tick.review_stats.render(rows, [], [], "## Arbitros 线\n", tick, self.now)
        self.assertIn("Codex 每次执行（窗口内 1 次）", stats)  # the executor run only, not sol's review
        with mock.patch.object(tick, "CODEX_TIERS", ((shadow.MODEL, ("high",), 1, False),)):
            self.assertIsNotNone(tick.codex_tier("large task", rows))  # sol's review does not use the executors' daily cap
            used = lambda s: sum(1 for r in rows if r.get("kind") not in ("shadow_review", "review") and r.get("engine") == "codex" and r.get("section") == s)
            self.assertEqual(used("Arbitros 线"), 1)  # codex_pick's per-line count: the executor run only

    def test_sol_rework_blocked_or_failure_goes_to_the_deputy_who_is_not_shown_it(self):
        self.sol("SOL-REASON\nVERDICT: rework")
        for i, text in enumerate(("SOL-REASON\nVERDICT: rework", "SOL-REASON\nVERDICT: blocked", "no verdict at all")):
            self.sol_text = text
            line = self.review(f"Delta：small patch {i}")
            self.assertIn(line, tick.BACKLOG.read_text(encoding="utf-8"))  # still [review]
            self.assertNotIn("SOL-REASON", self.formal.call_args[0][2])
        self.assertEqual(self.formal.call_count, 3)
        self.assertEqual(shadow.summary(self.rows())["primary_done"], 0)

    def test_every_fifth_pass_is_drawn_for_the_deputy_and_two_spot_reworks_switch_sol_off(self):
        self.sol("VERDICT: pass", formal_verdict="VERDICT: rework")
        with self.draw(0):
            for i in range(6):
                self.review(f"Delta：small patch {i}")
        self.assertEqual(self.formal.call_count, 2)  # passes 0 and 5; the four in between are done
        backlog = tick.BACKLOG.read_text(encoding="utf-8")
        self.assertEqual(backlog.count("sol 审查通过"), 4)
        self.assertEqual([r["verdict"] for r in self.rows() if r["kind"] == "review" and r.get("primary") == "sol"], ["pass"] * 4)
        state = json.loads((self.data / "sol-primary-state.json").read_text(encoding="utf-8"))
        self.assertEqual((state["streak"], state["passes"], state["disabled"]["reason"]), (2, 6, "连续 2 次抽查副总理判 rework"))
        self.review("Delta：small patch 6")
        self.assertEqual((self.execute.call_count, self.formal.call_count), (6, 3))  # switched off: no sol run, deputy reviews
        self.assertIn("- [review] Delta：small patch 6", tick.BACKLOG.read_text(encoding="utf-8"))

    def test_spot_check_pair_enters_the_summary(self):
        self.sol("VERDICT: pass", formal_verdict="VERDICT: rework")
        with self.draw(0):
            self.review("Delta：small patch")
        # the stub deputy of this test writes no ledger row; a real one carries review_id, as test_resident_official_ledger shows
        rows = self.rows()
        review_id = next(r["review_id"] for r in rows if r["kind"] == "shadow_review")
        rows.append({"kind": "review", "model": "opus", "ok": True, "verdict": "rework", "review_id": review_id, "at": self.now.isoformat()})
        report = shadow.summary(rows)
        self.assertEqual((report["paired"], report["spot_pairs"], report["misses"], report["primary_done"]), (1, 1, [review_id], 0))

    def test_decide_draws_one_per_block_and_is_stable_on_retry(self):
        state, answers = {}, []
        with mock.patch.object(shadow, "RNG", random.Random(7)):
            for i in range(20):
                answers.append(shadow.decide(state, str(i)))
            self.assertEqual([answers[k:k + 5].count("spot") for k in range(0, 20, 5)], [1, 1, 1, 1])
            self.assertEqual([shadow.decide(state, str(i)) for i in range(20)], answers)  # asked again: same answer
        self.assertEqual(state["passes"], 20)

    def test_note_spot_streak_resets_on_pass_and_ignores_unsampled_and_unclear(self):
        shadow.update_state(tick, lambda s: s.update(decided={k: "spot" for k in "abcdef"} | {"z": "done"}))
        for rid, verdict, streak in (("a", "rework", 1), ("z", "rework", 1), ("b", "blocked", 1), ("c", "pass", 0),
                                     ("d", "rework", 1), ("d", "rework", 1)):  # repeated verdict of the same row counts once
            shadow.note_spot(tick, rid, verdict)
            self.assertEqual(shadow.load_state(tick).get("streak"), streak, rid)
        self.assertFalse(shadow.load_state(tick).get("disabled"))
        shadow.note_spot(tick, "e", "rework")
        self.assertTrue(shadow.load_state(tick)["disabled"])
        (self.data / "sol-primary-state.json").write_text("{broken", encoding="utf-8")
        self.assertTrue(shadow.load_state(tick)["disabled"])  # unreadable is never read as enabled

    def test_deputy_prompt_lists_sol_done_rows_since_the_last_review_and_report_says_the_switch_off_once(self):
        self.sol("## 建议的后续\n- 补测试\nVERDICT: pass")
        with self.draw(4):
            self.review("Delta：small patch 1")
            self.review("Delta：small patch 2")
        audits = [r["audit_file"] for r in self.rows() if r.get("primary") == "sol"]
        self.assertEqual(len(audits), 2)
        self.sol_text = "SOL-REASON\nVERDICT: rework"
        self.review("Delta：small patch 3")
        prompt = self.formal.call_args[0][2]
        self.assertIn("共 2 条", prompt)
        for item, audit in zip(("small patch 1", "small patch 2"), audits):
            self.assertIn(f"Delta：{item}｜{audit}", prompt)
        self.assertNotIn("SOL-REASON", prompt)
        self.assertIn("建议的后续", prompt)
        self.assertEqual(shadow.followups(tick, "work").count("\n- "), 2)
        tick.append_jsonl(tick.RUNS_FILE, {"at": datetime.now(timezone.utc).isoformat(), "role": "work", "kind": "review", "model": "opus", "ok": True})
        self.assertEqual(shadow.followups(tick, "work"), "")  # the deputy has been through them
        shadow.update_state(tick, lambda s: s.update(disabled={"at": "x", "reason": "连续 2 次抽查副总理判 rework"}))
        prompts = []
        def fake(*args, **kwargs):
            prompts.append(args[2])
            return 0, self.data / "r.jsonl", True, "日报正文", 1
        with mock.patch.object(tick, "codex_report_note", return_value=""), mock.patch.object(tick, "run_resident", side_effect=fake), \
                mock.patch.object(tick.review_stats, "day_facts", return_value="【今日数据】\n"):
            for _ in range(2):
                tick.write_report("coordinator", datetime(2026, 10, 26, 9, 0), "09:00")
        self.assertIn("sol 先审已自动停用", prompts[0])
        self.assertNotIn("sol 先审已自动停用", prompts[1])

    # ---- sol first review while Claude is held back (supervisor entry and the sol-first lane) ----
    def held(self, item=None, **ex):
        """A row waiting for review, its executor run in the ledger; returns (line, review_id, supervisor state)."""
        item = item or self.ex["item"]
        row = {**self.ex, "item": item, **ex}
        self.append_ex(row)
        line = "- [review] " + item
        if line not in tick.BACKLOG.read_text(encoding="utf-8"):
            tick.BACKLOG.write_text(tick.BACKLOG.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")
        return line, shadow.review_id_of(row["role"], row["section"], item, row), {"sample": None, "spawned": {}, "aux": {}}

    def held_fallback(self, st, t=1000.0, used=7.0, busy=False):
        with mock.patch.object(tick, "spawn") as spawn, mock.patch.object(tick, "codex_fallback") as fallback, \
                mock.patch.object(tick, "codex_weekly_used", return_value=used), mock.patch.object(tick, "lock_live", return_value=busy):
            tick.held_fallback(st, t, tick.BACKLOG.read_text(encoding="utf-8"), set())
        return spawn, fallback

    def test_held_supervisor_spawns_one_sol_first_lane_per_row_then_falls_back_to_executors(self):
        line, rid, st = self.held()
        spawn, fallback = self.held_fallback(st)
        spawn.assert_called_once_with(["sol-first", "work", rid])
        fallback.assert_not_called()
        spawn, fallback = self.held_fallback(st, t=1010.0)  # inside the spawn gap
        spawn.assert_not_called()
        spawn, fallback = self.held_fallback(st, t=1200.0)  # this review_id was handed out once: the executors get the slot
        spawn.assert_not_called()
        fallback.assert_called_once()

    def test_held_supervisor_does_not_start_sol_first_when_it_must_not(self):
        cases = {
            "quota full": dict(used=95.0), "no free Codex slot": dict(busy=True),
        }
        for name, kw in cases.items():
            line, rid, st = self.held(f"Delta：small patch {name}")
            spawn, fallback = self.held_fallback(st, **kw)
            spawn.assert_not_called()
            fallback.assert_called_once()
        for item, section, ex in (("量化：x", "量化线", {"role": "quant", "section": "量化线", "project": "quant"}),
                                  ("Delta：画图" + tick.OPUS_TAG, "Arbitros 线", {}), ("Delta：给 owner 试用的页面", "Arbitros 线", {}),
                                  ("Delta：failed run", "Arbitros 线", {"ok": False})):
            tick.BACKLOG.write_text(f"## {section}\n- [review] {item}\n", encoding="utf-8")
            _, _, st = self.held(item, **ex)
            spawn, _ = self.held_fallback(st)
            spawn.assert_not_called()
        line, rid, st = self.held("Delta：already tried")
        tick.BACKLOG.write_text("## Arbitros 线\n" + line + "\n", encoding="utf-8")
        tick.append_jsonl(tick.RUNS_FILE, {"at": self.now.isoformat(), "kind": "shadow_review", "review_id": rid, "ok": False})
        self.assertFalse(self.held_fallback(st)[0].called)  # the same execution is never sol-reviewed twice
        shadow.update_state(tick, lambda s: s.update(disabled={"reason": "x"}))
        line, rid, st = self.held("Delta：after switch-off")
        self.assertFalse(self.held_fallback(st)[0].called)

    def test_sol_first_lane_pass_becomes_done(self):
        line, rid, _ = self.held()
        self.sol("证据 ok\n## 建议的后续\n无\nVERDICT: pass")
        with self.draw(4):
            self.assertEqual(tick.sol_first_lane("work", rid), 0)
        self.formal.assert_not_called()
        self.assertIn("sol 审查通过", tick.BACKLOG.read_text(encoding="utf-8"))
        self.assertNotIn(line, tick.BACKLOG.read_text(encoding="utf-8"))
        kinds = [(r["kind"], r.get("primary")) for r in self.rows()]
        self.assertEqual(kinds, [("executor", None), ("shadow_review", None), ("review", "sol")])
        self.assertFalse(any(tick.lock_live(tick.codex_lock(i)) for i in range(tick.CODEX_SLOTS)))  # the slot is released

    def test_sol_first_lane_non_pass_leaves_the_row_for_the_deputy_who_is_not_shown_it(self):
        for n, (text, pick) in enumerate((("SOL-REASON\nVERDICT: rework", 4), ("SOL-REASON\nVERDICT: blocked", 4), ("VERDICT: pass", 0))):
            line, rid, _ = self.held(f"Delta：small patch {n}")
            self.sol(text)
            before = self.execute.call_count
            with self.draw(pick):
                tick.sol_first_lane("work", rid)
                tick.sol_first_lane("work", rid)  # a second lane for the same execution does nothing
            self.assertEqual(self.execute.call_count, before + 1, n)  # one sol run, however many lanes
            self.assertIn(line, tick.BACKLOG.read_text(encoding="utf-8"))  # still [review]
            self.assertEqual([r["kind"] for r in self.rows() if r.get("review_id") == rid], ["shadow_review"])
            tick.run_lane_work("work", "Arbitros 线", "review", line, f"Delta：small patch {n}", 1, 20)  # Claude is free again
            self.assertNotIn("SOL-REASON", self.formal.call_args[0][2])
            self.assertEqual(self.formal.call_args[0][4]["review_id"], rid)  # paired with the sol row
            self.assertEqual(self.execute.call_count, before + 1)  # the deputy lane reused sol's frozen evidence, no second sol run
            self.formal.reset_mock()

    def test_sol_first_lane_skips_without_packet_quota_or_eligibility(self):
        line, rid, _ = self.held()
        self.sol("VERDICT: pass")
        with mock.patch.object(tick, "review_packet_for", return_value=None):
            tick.sol_first_lane("work", rid)
        with mock.patch.object(tick, "codex_weekly_used", return_value=96.0):
            tick.sol_first_lane("work", rid)
        with mock.patch.object(tick, "review_packet_for", return_value={**self.packet, "hard": "自动返工"}):
            tick.sol_first_lane("work", rid)
        with mock.patch.object(tick, "review_packet_for", return_value={**self.packet, "facts": {**self.packet["facts"], "changed": ["control/tick.py"]}}):
            tick.sol_first_lane("work", rid)  # a path that never goes to sol
        self.assertEqual(tick.sol_first_lane("quant", rid), 2)
        self.execute.assert_not_called()
        self.assertIn(line, tick.BACKLOG.read_text(encoding="utf-8"))
        self.assertEqual([r["kind"] for r in self.rows()], ["executor"])

    # ---- review batching: several [review] rows of a line in one deputy session ----
    def queue(self, items, role="work", section="Arbitros 线"):
        """Executor rows and [review] lines for `items` (the first is the lane's row); returns the lines."""
        lines = []
        for item in items:
            self.append_ex({**self.ex, "item": item, "role": role, "section": section, "project": tick.project_of(item)})
            lines.append("- [review] " + item)
        tick.BACKLOG.write_text(f"## {section}\n" + "\n".join(lines) + "\n", encoding="utf-8")
        return lines

    def batch_lane(self, items, deputy, sol="VERDICT: rework", role="work", section="Arbitros 线", presol=True):
        """Run the lane on the first row. presol: the other rows already had their sol first review (what the row's own lane or
        the sol-first lane does): shadow_review.run, then primary; a sibling sol has not seen is not taken into a batch."""
        self.sol(sol, deputy, real_resident=True)
        lines = self.queue(items, role, section)
        if presol:
            self.presol(role, section, items[1:], lines[1:])
        with mock.patch.object(tick, "BATCH_MAX", 3), self.draw(4):
            tick.run_lane_work(role, section, "review", lines[0], items[0], 1, 20)
        return lines

    def presol(self, role, section, items, lines, packets=None):
        with self.draw(4):
            for item, line in zip(items, lines):
                context, _ = shadow.run(tick, role, section, item, (packets or {}).get(item, self.packet))
                if context:
                    shadow.primary(tick, role, section, line, item, context)

    def review_rows(self):
        return [r for r in self.rows() if r["kind"] == "review"]

    def test_batch_verdict_lines_need_a_number_in_range_and_a_known_word(self):
        text = "x\nVERDICT 1: pass\n  VERDICT 2：Rework\nVERDICT: pass\nVERDICT 4: pass\nVERDICT 3: maybe\nVERDICT two: pass\nVERDICT 1: blocked"
        self.assertEqual(tick.batch_verdicts(text, 3), {1: "blocked", 2: "rework"})  # the last line of a number wins
        self.assertEqual(tick.batch_verdicts("VERDICT: pass", 2), {})
        self.assertEqual(tick.parse_verdict("VERDICT 1: pass"), "unknown")  # a batch reply is not a single verdict

    def test_two_rows_are_reviewed_in_one_session_with_a_verdict_and_a_share_of_the_cost_each(self):
        items = ["Delta：a", "Uvox：b"]
        lines = self.batch_lane(items, "VERDICT 1: pass\nVERDICT 2: rework")
        self.assertEqual(len(self.prompts), 1)
        prompt = self.prompts[0]
        self.assertIn("这次一起审 2 条", prompt)
        self.assertTrue(prompt.index("【第 1 条】Delta：a") < prompt.index("【第 2 条】Uvox：b") < prompt.index("你是"))
        self.assertIn("PRE-REVIEW MATERIAL", prompt)  # each part carries its packet
        self.assertIn("VERDICT <序号>: pass", prompt)
        for item in items:
            self.assertEqual(prompt.count("本条审查的冻结材料："), 2)  # both went through sol first and failed it
        rows = self.review_rows()
        self.assertEqual([(r["item"], r["verdict"], r["ok"], r["cost_usd"], r["batch"]) for r in rows],
                         [("Delta：a", "pass", True, 0.45, 2), ("Uvox：b", "rework", True, 0.45, 2)])
        self.assertTrue(all(r["review_id"] for r in rows))
        self.assertEqual([c.args[1:] for c in self.note.call_args_list], [("Delta：a", "pass"), ("Uvox：b", "rework")])
        self.assertEqual(tick.BACKLOG.read_text(encoding="utf-8").splitlines()[1:], lines)  # the (mock) deputy edited nothing

    def test_three_rows_at_most_and_a_row_without_a_verdict_stays_for_a_single_review(self):
        items = ["Delta：a", "Uvox：b", "Umba：c", "Thinking Engines：d"]
        self.batch_lane(items, "VERDICT 1: pass\nVERDICT 3: blocked")
        prompt = self.prompts[0]
        self.assertIn("这次一起审 3 条", prompt)
        self.assertNotIn("Thinking Engines：d", prompt)  # a fourth row waits
        rows = self.review_rows()
        self.assertEqual([(r["item"], r["verdict"], r["cost_usd"]) for r in rows], [("Delta：a", "pass", 0.45), ("Umba：c", "blocked", 0.45)])
        self.assertEqual(self.note.call_count, 2)
        self.assertIn("review_batch_unjudged", tick.LOG_FILE.read_text(encoding="utf-8"))
        self.assertIn("- [review] Uvox：b", tick.BACKLOG.read_text(encoding="utf-8"))

    def test_a_badly_formatted_reply_leaves_every_row_for_a_single_review(self):
        items = ["Delta：a", "Uvox：b"]
        self.batch_lane(items, "VERDICT: pass\nVERDICT 4: pass\nVERDICT 1: maybe")
        rows = self.review_rows()
        self.assertEqual(len(rows), 1)  # one failed row for the whole batch: no row per item, so no item gets a verdict
        self.assertEqual((rows[0]["ok"], rows[0]["verdict"], rows[0]["cost_usd"], rows[0]["item"].startswith("合批：")), (False, "unknown", 0.9, True))
        self.note.assert_not_called()
        self.assertEqual(tick.BACKLOG.read_text(encoding="utf-8").count("[review]"), 2)

    def test_one_row_goes_the_old_single_way_and_the_quant_line_is_never_batched(self):
        self.batch_lane(["Delta：a"], "VERDICT: pass")
        self.assertNotIn("一起审", self.prompts[0])
        self.assertIn("这一项：Delta：a", self.prompts[0])
        self.assertEqual([(r["item"], r["verdict"], r.get("batch")) for r in self.review_rows()], [("Delta：a", "pass", None)])
        self.assertEqual(tick.parse_verdict(self.deputy_text), "pass")
        # the quant line: two rows, two sessions in turn
        self.batch_lane(["量化：x", "量化：y"], "VERDICT: pass", role="quant", section="量化线")
        self.assertNotIn("一起审", self.prompts[-1])
        self.assertIn("量化：x", self.prompts[-1])
        self.assertNotIn("量化：y", self.prompts[-1])
        # a quant-project row of another line is not taken into a batch either
        self.batch_lane(["Delta：a2", "量化：z"], "VERDICT: pass")
        self.assertNotIn("一起审", self.prompts[-1])

    def test_batching_never_runs_sol_in_the_deputy_lane_and_leaves_out_rows_sol_has_not_seen(self):
        # an eligible sibling without a sol attempt waits for its own lane or the sol-first lane: the deputy reviews one row
        self.batch_lane(["Delta：a", "Uvox：b"], "VERDICT: pass", presol=False)
        self.assertNotIn("一起审", self.prompts[0])
        self.assertEqual(self.execute.call_count, 1)  # sol ran for the lane's own row only
        self.assertIn("- [review] Uvox：b", tick.BACKLOG.read_text(encoding="utf-8"))
        # a sibling sol does not apply to joins without a sol run, with no frozen material
        self.batch_lane(["Delta：c", "Uvox：给 owner 试用的页面"], "VERDICT 1: pass\nVERDICT 2: pass", presol=False)
        self.assertEqual(self.execute.call_count, 1)  # no sol run for the sibling (a fresh patch: only the lane's own row)
        prompt = self.prompts[0]  # (each batch_lane starts a fresh set of patches and prompts)
        self.assertIn("这次一起审 2 条", prompt)
        self.assertEqual(prompt.count("本条审查的冻结材料："), 1)
        self.assertEqual([r.get("review_id") is None for r in self.review_rows()[-2:]], [False, True])
        # sol switched off: every sibling joins, nothing runs
        shadow.update_state(tick, lambda s: s.update(disabled={"reason": "x"}))
        self.batch_lane(["Delta：d", "Uvox：e"], "VERDICT 1: pass\nVERDICT 2: pass", presol=False)
        self.assertEqual(self.execute.call_count, 0)
        self.assertIn("这次一起审 2 条", self.prompts[0])

    def test_a_row_sol_passed_is_not_in_the_batch_and_a_lone_rest_goes_single(self):
        def sol(evidence):  # sol passes only the Uvox row
            return "VERDICT: pass" if "Task: Uvox：b" in evidence else "VERDICT: rework"
        self.batch_lane(["Delta：a", "Uvox：b", "Umba：c"], "VERDICT 1: pass\nVERDICT 2: pass", sol=sol)
        backlog = tick.BACKLOG.read_text(encoding="utf-8")
        self.assertIn("sol 审查通过", backlog)  # the Uvox row is done
        self.assertEqual(self.prompts[0].count("【第"), 2)
        self.assertNotIn("Uvox：b", self.prompts[0].split("你是")[0])
        self.assertTrue(any(r.get("primary") == "sol" and r["item"] == "Uvox：b" for r in self.review_rows()))
        self.assertEqual(sorted(r["item"] for r in self.review_rows() if not r.get("primary")), ["Delta：a", "Umba：c"])
        # now only one row is left for the deputy: the old single prompt
        self.batch_lane(["Delta：e", "Uvox：f"], "VERDICT: pass", sol=lambda ev: "VERDICT: pass" if "Task: Uvox：f" in ev else "VERDICT: rework")
        self.assertNotIn("一起审", self.prompts[-1])
        self.assertIn("这一项：Delta：e", self.prompts[-1])

    def test_rows_of_one_project_are_batched_only_on_different_branches(self):
        packets = {"Uvox：a": self.packet, "Uvox：b": self.packet,
                   "Uvox：c": {**self.packet, "facts": {**self.packet["facts"], "refs": {"claude/other": "c" * 40}}}}
        self.sol("VERDICT: rework", "VERDICT 1: pass\nVERDICT 2: pass", real_resident=True)
        lines = self.queue(["Uvox：a", "Uvox：b"])
        self.presol("work", "Arbitros 线", ["Uvox：b"], lines[1:], packets)
        with mock.patch.object(tick, "BATCH_MAX", 3), mock.patch.object(tick, "review_packet_for", side_effect=packets.get):
            tick.run_lane_work("work", "Arbitros 线", "review", lines[0], "Uvox：a", 1, 20)
        self.assertNotIn("一起审", self.prompts[0])  # same project, same branch: not told apart
        lines = self.queue(["Uvox：a", "Uvox：c"])
        self.presol("work", "Arbitros 线", ["Uvox：c"], lines[1:], packets)
        with mock.patch.object(tick, "BATCH_MAX", 3), mock.patch.object(tick, "review_packet_for", side_effect=packets.get):
            tick.run_lane_work("work", "Arbitros 线", "review", lines[0], "Uvox：a", 1, 20)
        self.assertIn("这次一起审 2 条", self.prompts[1])

    def test_sprint_followups_and_the_first_row_s_hard_packet_still_apply_to_a_batch(self):
        self.sol("VERDICT: rework", "VERDICT 1: pass\nVERDICT 2: pass", real_resident=True)
        lines = self.queue(["Delta：a", "Uvox：b"])
        tick.BACKLOG.write_text("## Arbitros 线\n冲刺：Uvox，版本目标：v\n" + "\n".join(lines) + "\n", encoding="utf-8")
        self.presol("work", "Arbitros 线", ["Uvox：b"], lines[1:])
        tick.append_jsonl(tick.RUNS_FILE, {"at": self.now.isoformat(), "role": "work", "section": "Arbitros 线", "kind": "review", "primary": "sol", "engine": "codex",
                                           "item": "Delta：earlier", "audit_file": "audits/earlier-sol-review.md", "ok": True})
        with mock.patch.object(tick, "BATCH_MAX", 3):
            tick.run_lane_work("work", "Arbitros 线", "review", lines[0], "Delta：a", 1, 20)
        prompt = self.prompts[0]
        self.assertIn(tick.SPRINT_REVIEW, prompt)  # one row of the batch is the sprint project
        self.assertIn("audits/earlier-sol-review.md", prompt)  # followups
        hard = {**self.packet, "hard": "自动返工：没开工"}
        lines = self.queue(["Delta：h", "Uvox：i"])
        with mock.patch.object(tick, "BATCH_MAX", 3), mock.patch.object(tick, "review_packet_for", side_effect=lambda i: hard if i == "Delta：h" else self.packet):
            tick.run_lane_work("work", "Arbitros 线", "review", lines[0], "Delta：h", 1, 20)
        self.assertEqual(len(self.prompts), 1)  # a hard primary is sent back by the script, no session
        self.assertIn("（返工）Delta：h", tick.BACKLOG.read_text(encoding="utf-8"))

    def test_sol_first_lane_is_a_main_subcommand_and_respects_pause(self):
        with mock.patch.object(tick, "sol_first_lane", return_value=0) as lane:
            self.assertEqual(tick.main(["sol-first", "work", "abc"]), 0)
            lane.assert_called_once_with("work", "abc")
            tick.PAUSE_FILE.write_text("", encoding="utf-8")
            tick.main(["sol-first", "work", "abc"])
            lane.assert_called_once()


if __name__ == "__main__":
    unittest.main()
