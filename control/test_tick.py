import hashlib
import itertools
import http.server
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tick  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

REAL_BACKUP_WATCH = tick.backup_watch
REAL_MACHINE_WATCH_STEP = tick.machine_watch_step
REAL_WEBSITE_WATCH = tick.website_watch
REAL_MAIN_WORKTREE_GUARD = tick.main_worktree_guard
REAL_CODEX_REPORT_NOTE = tick.codex_report_note
REAL_CODEX_WEEKLY_USED = tick.codex_weekly_used
REAL_CODEX_PLAN_SPAWN = tick.codex_plan_spawn

ORIG_SEND_EMAIL = tick.send_email
ORIG_LIFE_PUSHES_ON = tick.LIFE_PUSHES_ON
ORIG_SPAWN = tick.spawn

BACKLOG_TEXT = "\n".join([
    "# header", "- [done 06:45] a", "- [blocked: x] b", "- [todo] first todo", "- [review] the review",
    "- [todo] second todo", "- [each tick] c", "",
])


def event(u, resets):
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": u, "resetsAt": resets}}}})


class TickCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.d = d
        dates_file = d / "life-dates.md"
        dates_file.write_text(
            "## 表 1：测试\n"
            "| 项目 | 日期 | 提前 30 天 | 提前 7 天 | 下次核对 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "## 表 2：测试\n"
            "| 步骤 | 日期窗口 | 下次核对 |\n"
            "| --- | --- | --- |\n",
            encoding="utf-8",
        )
        (d / "rules.md").write_text("RULES", encoding="utf-8")
        (d / "backlog.md").write_text(BACKLOG_TEXT, encoding="utf-8")
        patches = {"DATA": d, "PAUSE_FILE": d / "tick.PAUSE", "LOCK_FILE": d / "tick.lock",
                   "SUPERVISOR_STATE": d / "supervisor-state.json",
                   "USAGE_FILE": d / "usage.jsonl", "LOG_FILE": d / "tick-log.jsonl", "TICKS_DIR": d / "ticks",
                   "BACKLOG": d / "backlog.md", "RULES_FILE": d / "rules.md", "OWNER_LOCK": d / "owner.lock",
                   "BACKLOG_HISTORY": d / "backlog-history",
                   "ASK_LOCK": d / "ask.lock", "ASK_STATE": d / "ask-state.json",
                   "ASK_NOTICE_STATE": d / "ask-notices.json",
                   "OWNER_DATA": d / "owner-data",
                   "DEPUTIES_FILE": d / "deputies.json", "DISCORD_DB": d / "none.sqlite3",
                   "RUNS_FILE": d / "runs.jsonl", "REPORT_FILE": d / "reports.json", "REPORT_LOCK": d / "report.lock",
                   "LIFE_DATES_FILE": dates_file, "LIFE_DATES_REPORTED_FILE": d / "dates-reported.json",
                   "LESSONS_DIR": d / "lessons", "CLAUDE_PROJECTS": d / "claude-projects",
                   "SESSIONS_DIR": d / "sessions", "RECORDS_DIR": d / "records",
                   "REPORT_SLOTS": (), "LIFE_PUSHES_ON": (), "SUPERVISE_MAX_S": 0,  # the supervisor does one step per main()
                   "CODEX_TIERS": (),  # Codex off unless a test turns it on
                   "codex_report_note": lambda now: "",  # the real ones run wsl (CodexQuota calls REAL_CODEX_*)
                   "codex_weekly_used": lambda: None,  # unreadable: no quota gate in the other tests
                   "codex_plan_spawn": lambda *a: None,  # CodexPlan tests call REAL_CODEX_PLAN_SPAWN
                   "BASELINE_MAX": 80.0, "HARD_CEILING": 90.0}  # the scenarios below are written for the 10-01 lines
        for k, v in patches.items():
            p = mock.patch.object(tick, k, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(tick.dots_review, "step")  # it reads the real Drive; test_dots_review.py covers it
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(tick.daily_health, "report_line", return_value="机器和备份：正常")
        p.start()
        self.addCleanup(p.stop)
        self.spawned = []  # no test ever starts a process
        for target, name, kw in ((tick, "spawn", {"side_effect": lambda args: self.spawned.append(list(args))}),
                                 (tick, "backup_watch", {}),  # BackupWatch tests call REAL_BACKUP_WATCH
                                 (tick, "website_watch", {}),  # WebsiteWatch tests call REAL_WEBSITE_WATCH
                                 (tick, "machine_watch_step", {}),  # would sample this machine and write the real data/
                                 (tick, "main_worktree_guard", {}),  # isolated git repos in test_main_worktree_guard
                                 (tick.backlog_archive, "daily", {}),  # archive has isolated real-copy tests
                                 (tick.kpi, "write", {})):
            p = mock.patch.object(target, name, **kw)
            self.mocks = getattr(self, "mocks", {})
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(tick.review_stats, "write")  # would read and write the real control/data
        self.mocks["stats"] = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(tick.project_delivery, "write", return_value={"generated_at_utc": "test", "projects": []})
        self.mocks["delivery"] = p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def logs(self):
        f = self.d / "tick-log.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []

    def backlog(self):
        return (self.d / "backlog.md").read_text(encoding="utf-8")

    def sample(self, pct, frac_of_window=0.5):
        """Patch sample_usage to return pct at a given fraction of the window elapsed."""
        resets = int(time.time() + tick.WINDOW_SECONDS * (1 - frac_of_window))
        return mock.patch.object(tick, "sample_usage", return_value=(pct, resets))


class Gates(TickCase):
    def test_pause(self):
        (self.d / "tick.PAUSE").write_text("")
        with mock.patch.object(tick, "sample_usage") as s:
            tick.main()
        s.assert_not_called()
        self.assertEqual(self.logs()[0]["decision"], "paused")

    def test_busy_live_lock(self):
        (self.d / "tick.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True), \
                mock.patch.object(tick, "sample_usage") as s:
            tick.main()
        s.assert_not_called()
        self.assertEqual(self.logs()[0]["decision"], "busy")
        self.assertEqual((self.d / "tick.lock").read_text(), "4242")

    def test_stale_lock_taken_and_released(self):
        (self.d / "tick.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=False), \
                mock.patch.object(tick, "sample_usage", return_value=None):
            tick.main()
        self.assertEqual(self.logs()[0]["decision"], "usage_unknown")
        self.assertFalse((self.d / "tick.lock").exists())

    def test_lock_released_on_exception(self):
        with mock.patch.object(tick, "sample_usage", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                tick.main()
        self.assertFalse((self.d / "tick.lock").exists())

    def test_usage_unknown_no_event(self):
        done = mock.Mock(stdout="no events here")
        with mock.patch.object(tick.subprocess, "run", return_value=done):
            self.assertIsNone(tick.sample_usage())
        self.assertFalse((self.d / "usage.jsonl").exists())

    def test_sample_usage_appends_and_scales(self):
        done = mock.Mock(stdout=event(0.35, 1790805000))
        with mock.patch.object(tick.subprocess, "run", return_value=done):
            u, r = tick.sample_usage()
        self.assertAlmostEqual(u, 35.0)
        rec = json.loads((self.d / "usage.jsonl").read_text())
        self.assertEqual((rec["source"], rec["resets_at"]), ("tick", 1790805000))

    def test_hard_ceiling(self):
        with self.sample(91, 1.0), mock.patch.object(tick, "run_claude") as r:
            tick.main()
        r.assert_not_called()
        self.assertEqual(self.logs()[0]["decision"], "hard_ceiling")

    def test_pacing(self):
        with self.sample(43, 0.5), mock.patch.object(tick, "run_claude") as r:  # baseline 40, +2 slack
            tick.main()
        r.assert_not_called()
        self.assertEqual(self.logs()[0]["decision"], "pacing")

    def test_baseline_clamp(self):
        now = 1000000
        self.assertEqual(tick.baseline(now, now + 99999), 0)
        self.assertEqual(tick.baseline(now, now - 99999), 80)
        self.assertAlmostEqual(tick.baseline(now, now + 9000), 40)


TEMPLATE = Path(__file__).resolve().parent / "roles" / "tick-rules.md"


class RulesFile(unittest.TestCase):
    """The public template in git (control/roles/tick-rules.md) is checked for structure; the owner's private layer
    (control/layered.py) is checked only for having the same skeleton, never for its words."""

    def template(self):
        return TEMPLATE.read_text(encoding="utf-8")

    def test_template_has_the_three_parts_and_the_authorization_example(self):
        text = self.template()
        for heading in ("## Must follow", "## Authorization exceptions", "## Way of working"):
            self.assertIn(heading, text)
        for part in ("Conditions before acting", "How: `git merge --no-ff --no-edit", "Checks after going live", "Still waits for the owner"):
            self.assertIn(part, text)

    def test_template_has_placeholders_and_no_private_names(self):
        text = self.template()
        for placeholder in ("<OWNER NAME>", "<WORKSPACE>", "<TIMEZONE>"):
            self.assertIn(placeholder, text)
        self.assertIn("kept in your own `records/`", text)

    def test_real_surface_and_loopback_rules_are_in_the_template(self):
        text = self.template()
        self.assertIn("real-surface acceptance: done / not done / not applicable", text)
        self.assertIn("Start-Process http://127.0.0.1:<port>", text)
        self.assertNotIn("http://localhost:<port>", text)
        self.assertIn("put the same file back at that path", text)

    def test_owner_word_sync_rule_is_in_the_template(self):
        text = self.template()
        self.assertIn("owner's words from other channels in the last 24 hours", text)
        self.assertIn("Never write \"if no answer, do X\" or treat silence as consent", text)

    def test_private_layer_keeps_the_same_skeleton(self):
        text = tick.layered.read_text(TEMPLATE)
        if text == self.template():
            self.skipTest("no private layer: the template is in use")
        self.assertGreaterEqual(text.count("\n## "), 2)
        self.assertNotIn("public template", text[:300])


class Parse(TickCase):
    def test_window_skip_before_does_not_hide_expired_and_full_rows_do_not_collide(self):
        text = "## 组织线\n" + "\n".join(
            "- [todo] 组织：" + "长" * 80 + suffix + "〔时段：10:50–13:00〕" for suffix in ("甲", "乙"))
        for clock in ("04:49", "04:49", "13:00", "13:01"):
            tick.parse_backlog(text, datetime.fromisoformat("2026-11-02T" + clock))
        logs = [r for r in self.logs() if r["decision"] == "window_skipped"]
        self.assertEqual(len(logs), 4)
        self.assertEqual([r["phase"] for r in logs], ["before", "before", "expired", "expired"])
        self.assertEqual({r["window_date"] for r in logs}, {"2026-11-02"})

    def test_dots_tail_midrow_and_references_in_both_dispatchers(self):
        tag = tick.dots_watch.DOTS_TAG
        cases = ((f"组织：公开检索 {tag}", False),
                 (f"组织：公开检索 {tag}〔截止：10-26 05:00〕", False),
                 (f"组织：公开检索 {tag}：①列论文；②比较方法", False),
                 (f"组织：公开盘点（只读；{tag}）：逐条给出处", False),
                 (f"组织：`{tag}` 接进调度", True),
                 (f"组织：把{tag}标签接进调度", True),
                 (f"组织：审查员过审后再另排一行{tag}派单。", True))
        runs = [row("coordinate", "组织线", 1), row("plan", "组织线", 0.01, ok=True)]
        for item, available in cases:
            with self.subTest(item=item), mock.patch.object(tick, "CODEX_TIERS", (("test", ("low",), 10, False),)):
                text = f"## 组织线\n- [todo] {item}\n"
                sec = tick.parse_backlog(text)["组织线"]
                self.assertEqual(sec["todo"], [(f"- [todo] {item}", item)] if available else [])
                expected = ("coordinator", "incore") if available else None
                self.assertEqual(tick.choose_lane(text, runs, NOW), expected)
                self.assertEqual(tick.codex_pick(text, [], NOW, set()), expected)

    def test_review_before_todo_skips_and_owner_section(self):
        text = "\n".join(["## 你的指令（最优先）", "- [todo] owner one", "## A", "权重：50%（x）", "- [done] a", "- [blocked: x] b",
                          "- [todo] first", "- [review] the review", "- [todo] second", "- [each tick] c",
                          "- [running 10:00] r", "## B", "权重: 25.5%", "- [todo] b1", "## C", "- [todo] c1", ""])
        secs = tick.parse_backlog(text)
        self.assertEqual(list(secs), ["A", "B", "C"])
        self.assertEqual(secs["A"]["review"], ("- [review] the review", "the review"))
        self.assertEqual([i for _, i in secs["A"]["todo"]], ["first", "second"])
        self.assertEqual([secs[k]["weight"] for k in "ABC"], [50.0, 25.5, 10.0])  # ASCII colon, decimal, default

    def test_time_window_cross_midnight_boundaries_and_skip_log_deduplication(self):
        text = "## Arbitros 线\n- [todo] Uvox：夜跑检查〔时段：23:00–07:00〕\n"
        cases = ((datetime(2026, 10, 26, 22, 59), False), (datetime(2026, 10, 26, 23, 0), True),
                 (datetime(2026, 10, 27, 6, 59), True), (datetime(2026, 10, 27, 7, 0), False))
        with mock.patch.object(tick, "note_malformed") as malformed:
            for index, (now, available) in enumerate(cases):
                sec = tick.parse_backlog(text, now)["Arbitros 线"]
                self.assertEqual(bool(sec["todo"]), available, now)
                if index == 0:
                    tick.parse_backlog(text, now)  # one row and window logs only once for this local date
        malformed.assert_not_called()
        skipped = [row for row in self.logs() if row["decision"] == "window_skipped"]
        self.assertEqual(len(skipped), 2)  # one for each local date/window occurrence, not each parse

    def test_time_window_tag_in_middle_uses_first_match(self):
        text = "## Arbitros 线\n- [todo] Uvox：夜跑检查〔时段：23:00–07:00〕 GPU 夜跑〔时段：08:00–09:00〕\n"
        with mock.patch.object(tick, "note_malformed") as malformed:
            sec = tick.parse_backlog(text, datetime(2026, 10, 26, 8, 30))["Arbitros 线"]
        self.assertEqual(sec["todo"], [])
        malformed.assert_not_called()
        skipped = [row for row in self.logs() if row["decision"] == "window_skipped"]
        self.assertEqual([row["window"] for row in skipped], ["23:00–07:00"])

    def test_time_window_ignores_quoted_tags(self):
        quoted = "「另一行〔时段：23:00–07:00〕」"
        outside = "〔时段：08:00–09:00〕"
        cases = ((quoted, True), ("「〔时段：无效〕」", True), (outside, True),
                 (f"{quoted} {outside}", True), (f"{outside} {quoted}", True),
                 (f"「{outside}」〔时段：23:00–07:00〕", False))
        with mock.patch.object(tick, "note_malformed") as malformed:
            for tags, available in cases:
                with self.subTest(tags=tags):
                    item = f"组织：修标签 {tags}"
                    sec = tick.parse_backlog(f"## 组织线\n- [todo] {item}\n", datetime(2026, 11, 2, 8, 30))["组织线"]
                    self.assertEqual([i for _, i in sec["todo"]], [item] if available else [])
        malformed.assert_not_called()

    def test_time_window_filters_both_dispatchers_but_leaves_untagged_todos_alone(self):
        tagged = "## Arbitros 线\n权重：40%\n- [todo] Uvox：夜跑检查〔时段：23:00–07:00〕\n"
        untagged = tagged.replace("〔时段：23:00–07:00〕", "")
        outside, inside = datetime(2026, 10, 26, 22, 59), datetime(2026, 10, 26, 23, 0)
        runs = [{"kind": "coordinate", "section": "组织线", "_at": outside - timedelta(hours=1)},
                {"kind": "plan", "section": "Arbitros 线", "_at": outside - timedelta(minutes=1), "ok": True}]
        self.assertIsNone(tick.choose_lane(tagged, runs, outside))
        self.assertEqual(tick.choose_lane(tagged, runs, inside), ("work", "uvox"))
        self.assertEqual(tick.choose_lane(untagged, runs, outside), ("work", "uvox"))
        with mock.patch.object(tick, "CODEX_TIERS", (("test", ("low",), 10, False),)):
            self.assertIsNone(tick.codex_pick(tagged, [], outside, set()))
            self.assertEqual(tick.codex_pick(tagged, [], inside, set()), ("work", "uvox"))

    def test_codex_lane_does_not_execute_a_window_todo_outside_it(self):
        line = "- [todo] Uvox：夜跑检查〔时段：23:00–07:00〕"
        (self.d / "backlog.md").write_text(f"## Arbitros 线\n{line}\n", encoding="utf-8")
        with mock.patch.object(tick, "local_datetime", return_value=datetime(2026, 10, 26, 22, 59)), \
                mock.patch.object(tick, "codex_exec") as execute:
            tick.main(["codex", "work", "uvox"])
        execute.assert_not_called()
        self.assertIn(line, self.backlog())

    def test_active_windows_sort_by_end_before_sprint_and_file_order(self):
        text = "\n".join([
            "## Arbitros 线", "冲刺：驾校模拟器", "- [todo] 驾校模拟器：sprint",
            "- [todo] Umba：ordinary", "- [todo] Uvox：later〔时段：23:00–02:00〕",
            "- [todo] Umzy：first〔时段：23:00–00:30〕",
            "- [todo] Delta：tie〔时段：23:00–00:30〕", "",
        ])
        for now in (datetime(2026, 11, 2, 23, 30), datetime(2026, 11, 3, 0, 15)):
            with self.subTest(now=now):
                items = [item.split("：")[0] for _, item in tick.parse_backlog(text, now)["Arbitros 线"]["todo"]]
                self.assertEqual(items, ["Umzy", "Delta", "Uvox", "驾校模拟器", "Umba"])

    def test_window_executor_precedes_review_but_respects_busy_units_and_slots(self):
        text = "\n".join([
            "## 组织线", "- [review] 组织：review", "- [todo] 输入台：ordinary",
            "- [todo] 组织：restore〔时段：10:50–13:00〕〔执行者：Claude〕", "",
        ])
        now = datetime(2026, 11, 2, 12, 55)
        sec = tick.parse_backlog(text, now)["组织线"]
        choose = lambda live, free=True: tick.next_unit(sec, "组织线", "coordinator", [], now, live, free)
        self.assertEqual(choose(set())[:2], ("executor", "incore"))
        self.assertEqual(choose({"coordinator:input"})[:2], ("executor", "incore"))
        self.assertEqual(choose({"coordinator:incore"})[0], "review")
        self.assertEqual(choose({"coordinator:input", "coordinator:general"})[0], "review")
        self.assertEqual(choose(set(), False)[:2], ("executor", "incore"))
        with mock.patch.object(tick, "CODEX_TIERS", (("test", ("low",), 10, False),)):
            self.assertEqual(tick.codex_pick(text, [], now, set()), ("coordinator", "input"))

    def test_same_project_window_is_first_in_both_executor_paths(self):
        text = ("## 组织线\n- [todo] 组织：ordinary\n"
                "- [todo] 组织：timed〔时段：10:50–13:00〕\n")
        now = datetime(2026, 11, 2, 12, 55)
        with mock.patch.object(tick, "local_datetime", return_value=now), \
                mock.patch.object(tick, "lock_units", return_value=set()), \
                mock.patch.object(tick, "run_lane_work") as run:
            (self.d / "backlog.md").write_text(text, encoding="utf-8")
            with self.sample(10):
                self.assertEqual(tick.lane_run("coordinator", "incore"), 0)
            self.assertIn("timed", run.call_args.args[4])
            sec = tick.parse_backlog(text, now)["组织线"]
            self.assertIn("timed", tick.codex_first_todo(sec, "incore")[1])

    def test_sprint_project_review_and_todos_go_first(self):
        text = "\n".join(["## A", "权重：50%", "冲刺：驾校模拟器，版本目标：能开的车", "- [review] Uvox：r1",
                          "- [review] 驾校模拟器：r2", "- [todo] Uvox：t1", "- [todo] 驾校模拟器：t2", "- [todo] 杂项 t3",
                          "- [todo] （返工）驾校模拟器：t4", "## B", "冲刺：无", "- [review] Uvox：b1", "- [todo] b2",
                          "## C", "冲刺：Uvox", "- [review] Delta：c1", "- [review] Umzy：c2", ""])
        secs = tick.parse_backlog(text)
        self.assertEqual((secs["A"]["sprint"], secs["A"]["weight"]), ("driving", 50.0))
        self.assertEqual(secs["A"]["review"][1], "驾校模拟器：r2")
        self.assertEqual([i for _, i in secs["A"]["todo"]], ["驾校模拟器：t2", "（返工）驾校模拟器：t4", "Uvox：t1", "杂项 t3"])
        self.assertIsNone(secs["B"]["sprint"])  # not a project: plain file order
        self.assertEqual(secs["B"]["review"][1], "Uvox：b1")
        self.assertEqual(secs["C"]["review"][1], "Delta：c1")  # sprint has no review row: file order
        self.assertNotIn("reviews", secs["A"])

    def test_next_unit_order(self):
        sec = {"weight": 10, "review": ("- [review] r", "r"), "todo": [("- [todo] t", "t")]}
        unit = lambda live=(), free=True: tick.next_unit(sec, "S", "work", [], NOW, set(live), free)
        self.assertEqual(unit()[0], "review")
        self.assertEqual(unit(free=False), ("executor", "general", "- [todo] t", "t", None))  # deputy unit busy: todo
        sec["review"] = None
        self.assertEqual(unit(), ("executor", "general", "- [todo] t", "t", None))
        self.assertIsNone(unit(live={"work:general"}))  # no plan while a todo exists
        sec["todo"] = []
        self.assertEqual(unit(), ("plan", None, None, None, None))
        self.assertIsNone(unit(free=False))

    def test_starved_project_goes_before_the_sprint_project(self):
        sec = tick.parse_backlog("\n".join(["## A", "冲刺：驾校模拟器", "- [todo] 驾校模拟器：t1", "- [todo] Umba：u1",
                                            "- [todo] 论文：p1", "- [todo] Umba：u2", ""]))["A"]
        run = lambda project, hours: {"kind": "executor", "role": "life", "project": project, "_at": NOW - tick.timedelta(hours=hours)}
        unit = lambda runs, live=(): tick.next_unit(sec, "A", "life", runs, NOW, set(live), True)[1:3]
        # nothing starved: the sprint project first, as before
        self.assertEqual(unit([run("umba", 47), run("thesis", 47)]), ("driving", "- [todo] 驾校模拟器：t1"))
        # past STARVE_H: the longest wait first; never run = infinite
        self.assertEqual(unit([run("umba", 49), run("thesis", 60)])[0], "thesis")
        self.assertEqual(unit([run("umba", 49)])[0], "thesis")
        self.assertEqual(unit([run("umba", 49), run("thesis", 10)]), ("umba", "- [todo] Umba：u1"))  # its first todo
        self.assertEqual(unit([run("umba", 49), run("thesis", 60)], live={"life:thesis"})[0], "umba")  # live units skipped
        self.assertEqual(unit([run("umba", 49), run("thesis", 60), run("umba", 1)])[0], "thesis")  # latest run counts
        # a run of another role does not count, and the line's executor cap still holds
        self.assertEqual(unit([{**run("umba", 1), "role": "work"}, run("thesis", 10)])[0], "umba")
        self.assertIsNone(tick.next_unit(sec, "A", "life", [], NOW, {"life:driving", "life:x"}, True))

    def test_starved_review_goes_before_the_sprint_review(self):
        sec = tick.parse_backlog("\n".join(["## A", "冲刺：驾校模拟器", "- [review] 驾校模拟器：r1", "- [review] 论文：r2",
                                            "- [review] Umba：r3", ""]))["A"]
        run = lambda project, hours: {"kind": "executor", "role": "life", "project": project, "_at": NOW - tick.timedelta(hours=hours)}
        unit = lambda runs, free=True: tick.next_unit(sec, "A", "life", runs, NOW, set(), free)
        self.assertEqual(unit([run("thesis", 23), run("umba", 23)])[3], "驾校模拟器：r1")
        self.assertEqual(unit([run("thesis", 25), run("umba", 30)])[3], "Umba：r3")  # longest wait
        self.assertEqual(unit([run("thesis", 25), run("umba", 3)])[3], "论文：r2")
        self.assertEqual(unit([run("thesis", 25)])[3], "Umba：r3")  # never run
        self.assertIsNone(unit([run("thesis", 25)], free=False))  # deputy unit busy: no review, no todo

    def test_general_rows_starve_by_their_own_text(self):
        sec = tick.parse_backlog("\n".join(["## A", "冲刺：驾校模拟器", "- [todo] 驾校模拟器：t1", "- [todo] Fake Foreigner 修 a",
                                            "- [todo] Hometown 餐单 b", ""]))["A"]
        run = lambda item, hours: {"kind": "executor", "role": "life", "project": "general", "item": item,
                                   "_at": NOW - tick.timedelta(hours=hours)}
        unit = lambda runs: tick.next_unit(sec, "A", "life", runs, NOW, set(), True)
        # one general row ran 1h ago: it no longer keeps the other (never run) alive
        self.assertEqual(unit([run("Fake Foreigner 修 a", 1)])[2:], ("- [todo] Hometown 餐单 b", "Hometown 餐单 b", "general"))
        self.assertEqual(unit([run("Fake Foreigner 修 a", 1), run("（返工）Hometown 餐单 b〔x〕", 49)])[3], "Hometown 餐单 b")
        # both inside STARVE_H: the sprint project stays first, nothing starved
        self.assertEqual(unit([run("Fake Foreigner 修 a", 1), run("Hometown 餐单 b", 47)])[1::3], ("driving", None))
        self.assertEqual(tick.starve_key("（返工）" + "x" * 60), "x" * 50)
        self.assertEqual(tick.starve_key("Umba：a"), "umba")

    def test_starved_field_in_spawn_and_review_decisions(self):
        text = "\n".join(["## 生活线", "冲刺：驾校模拟器", "- [todo] 驾校模拟器：t1", "- [todo] 论文：p1", ""])
        note = {}
        tick.choose_lane(text, [row("coordinate", "组织线", 1)], NOW, set(), set(), note)
        self.assertEqual(note["starved"], "thesis")  # never run: advanced past the sprint project
        run = {"kind": "executor", "role": "life", "project": "thesis", "_at": NOW - tick.timedelta(hours=1)}
        tick.choose_lane(text, [row("coordinate", "组织线", 1), run], NOW, set(), set(), note)
        self.assertIsNone(note["starved"])
        sec = tick.parse_backlog("\n".join(["## A", "冲刺：驾校模拟器", "- [review] 驾校模拟器：r1", "- [review] 论文：r2", ""]))["A"]
        self.assertEqual(tick.next_unit(sec, "A", "life", [], NOW, set(), True)[3:], ("论文：r2", "thesis"))
        self.assertIsNone(tick.next_unit(sec, "A", "life", [{"kind": "executor", "role": "life", "project": "thesis",
                                                             "_at": NOW}], NOW, set(), True)[4])

    def test_cross_line_starve_goes_before_the_score_longest_wait_first(self):
        text = "\n".join(["## Arbitros 线", "权重：40%", "- [todo] w1", "## 组织线", "权重：10%", "- [todo] o1",
                          "## 生活线", "权重：10%", "- [todo] 论文：p1", ""])
        runs = lambda org, life: [row("coordinate", "组织线", 1), row("executor", "Arbitros 线", 1, 0.0),
                                  row("executor", "组织线", org, 9.0), row("executor", "生活线", life, 9.0)]
        note = {}
        pick = lambda r: tick.choose_lane(text, r, NOW, set(), set(), note)
        got = pick(runs(7, 3))
        self.assertEqual((got[0], note["cross_starved"]), (tick.SECTION_ROLES["组织线"], "组织线"))  # 7 h beats the score
        got = pick(runs(8, 9))
        self.assertEqual((got, note["cross_starved"]), (("life", "thesis"), "生活线"))  # longest wait first
        self.assertEqual((pick(runs(5, 5)), note["cross_starved"]), (("work", "general"), None))  # 5 h: the lowest score

    def test_cross_starve_after_6h_beats_the_highest_score(self):
        text = "\n".join(["## Arbitros 线", "权重：40%", "- [todo] w1", "## 生活线", "权重：10%", "- [todo] 论文：p1", ""])
        runs = [row("coordinate", "组织线", 1), row("executor", "Arbitros 线", 1, 0.0),
                row("executor", "生活线", 7, 50.0)]
        note = {}
        got = tick.choose_lane(text, runs, NOW, set(), set(), note)
        self.assertEqual((got, note["cross_starved"]), (("life", "thesis"), "生活线"))

    def test_project_of(self):
        for item, want in (("Uvox：改 x", "uvox"), ("Umba 修 y", "umba"), ("输入台：a", "input"), ("语音试验", "input"),
                           ("量化：校准", "quant"), ("组织：改调度", "incore"), ("（返工）Uvox：改 x", "uvox"),
                           ("（返工） 量化：z", "quant"), ("论文：写", "thesis"), ("Delta：现状盘点", "delta"), ("Umzy：现状盘点", "umzy"), ("项目七：搬入口文档", "jianzhixin"), ("手语：现状盘点", "signlang"), ("Uchu-Deaf 调研", "signlang"), ("每日情报扫描 #3", "intel"), ("驾校模拟器：第一小块", "driving"), ("FactoryAlign：论文提纲 v3", "factoryalign"), ("（返工）FactoryAlign：论文提纲 v3", "factoryalign"), ("杂项", "general"), ("读 Uvox 的代码", "general"),
                           ("组织线", "general")):
            self.assertEqual(tick.project_of(item), want, item)

    def test_kiki_todo_uses_its_executor_without_a_malformed_warning(self):
        item = "Kiki：检查项目计划"
        with mock.patch.object(tick, "note_malformed") as malformed:
            sec = tick.parse_backlog(f"## Arbitros 线\n- [todo] {item}\n")["Arbitros 线"]
        unit = tick.next_unit(sec, "Arbitros 线", "work", [], NOW, set(), True)
        self.assertEqual((unit[0], unit[1], unit[3]), ("executor", "kiki", item))
        self.assertEqual(tick.executor_launch("kiki")["cwd"], tick.PROJECTS["kiki"][0])
        self.assertEqual(tick.executor_launch("kiki")["add_dirs"], tick.EXECUTOR_ADD_DIRS)
        malformed.assert_not_called()

    def test_status_with_trailing_words_is_malformed_not_dispatched(self):
        with mock.patch.object(tick, "note_malformed") as bad:
            sec = tick.parse_backlog("## 组织线\n- [todo 返工 11-01：x] 组织：y\n")["组织线"]
        bad.assert_called_once()
        self.assertEqual(sec["todo"], [])
        with mock.patch.object(tick, "note_malformed") as bad:
            sec = tick.parse_backlog("## 组织线\n- [todo] 组织：y\n")["组织线"]
        bad.assert_not_called()
        self.assertEqual(len(sec["todo"]), 1)

    def test_ask_rows_are_collected_per_section_and_not_dispatched(self):
        text = "\n".join(["## 你的指令（最优先）", "- [blocked: 等总理] 量化：owner section", "## 量化线", "- [todo] 量化：t",
                          "- [blocked: 等总理] 量化：实盘自动下单授权范围，选项 A/B，建议 A，见 audits/x.md",
                          "- [blocked: 等你决定] 量化：别的", "## Arbitros 线", "- [blocked：等总理 ] Uvox：全角冒号", "- [blocked: 等同事] x", ""])
        secs = tick.parse_backlog(text)
        self.assertEqual([i for _, i in secs["量化线"]["ask"]], ["量化：实盘自动下单授权范围，选项 A/B，建议 A，见 audits/x.md"])
        self.assertEqual(secs["量化线"]["ask"][0][0], "- [blocked: 等总理] 量化：实盘自动下单授权范围，选项 A/B，建议 A，见 audits/x.md")
        self.assertEqual([i for _, i in secs["Arbitros 线"]["ask"]], ["Uvox：全角冒号"])  # `等同事` is no question to the coordinator
        self.assertEqual([i for _, i in secs["量化线"]["todo"]], ["量化：t"])
        self.assertEqual(secs["量化线"]["review"], None)
        self.assertNotIn("", secs)
        self.assertEqual(tick.parse_backlog("- [blocked: 等总理] a\n")[""]["ask"], [("- [blocked: 等总理] a", "a")])

    def test_project_extra_dirs(self):
        self.assertEqual(tick.executor_launch("umba")["add_dirs"], tick.EXECUTOR_ADD_DIRS)
        self.assertEqual([p.name for p in tick.executor_launch("signlang")["add_dirs"][-2:]], ["Sign-speak demo", "Uchu-Deaf"])


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=tick.timezone.utc)
LANES = "\n".join(["# h", "## 你的指令（最优先）", "- [todo] owner one", "## Arbitros 线", "权重：40%", "- [todo] w1",
                   "## 量化线", "权重：20%", "- [todo] q1", "## 组织线", "权重：0%", "- [todo] o1", ""])


def row(kind, section, hours, cost=0.0, role=None, **extra):
    at = NOW - tick.timedelta(hours=hours)
    return {"kind": kind, "section": section, "cost_usd": cost, "role": role, "_at": at, **extra}


class ChooseLane(TickCase):
    COORD = row("coordinate", "组织线", 1)  # keeps the coordinate run from being due

    def unit(self, runs=(), text=LANES, busy=(), deputy_busy=()):
        return tick.choose_lane(text, [self.COORD, *runs], NOW, set(busy), set(deputy_busy))

    def choose(self, *args, **kw):
        got = self.unit(*args, **kw)
        return got and got[0]

    def test_failed_lane_backs_off(self):
        self.assertEqual(self.choose([row("review", "Arbitros 线", 0.1, ok=False)]), "quant")
        self.assertEqual(self.choose([row("review", "Arbitros 线", 0.5, ok=False)]), "work")  # 30 min > 20 min backoff
        self.assertEqual(self.choose([row("review", "Arbitros 线", 0.2, ok=False), row("executor", "Arbitros 线", 0.1, ok=True)]),
                         "work")  # only the latest run counts

    def test_deputy_busy_allows_only_executor_work(self):
        self.assertEqual(self.choose(deputy_busy={"work"}), "work")  # w1 is a todo -> executor
        text = LANES.replace("- [todo] w1", "- [review] w1")
        self.assertEqual(self.choose(text=text, deputy_busy={"work"}), "quant")
        self.assertEqual(tick.choose_lane(LANES, [], NOW, set(), {"coordinator"}), ("work", "general"))  # coordinate needs the session

    def test_lowest_cost_over_weight_wins(self):
        self.assertEqual(self.choose(), "work")  # 1/40 < 1/20
        self.assertEqual(self.choose([row("executor", "Arbitros 线", 2, 100.0)]), "quant")  # 101/40 > 1/20
        self.assertEqual(self.choose([row("review", "Arbitros 线", 2, 30.0)]), "quant")  # 31/40 > 1/20
        self.assertEqual(self.choose([row("executor", "量化线", 2, 0.9), row("plan", "量化线", 2, 0.1)]), "work")  # 2/20 > 1/40

    def test_cost_window_and_excluded_kinds(self):
        old = row("executor", "Arbitros 线", 24 * 8, 500.0)  # outside 7 days
        self.assertEqual(self.choose([old, row("owner", "Arbitros 线", 1, 500.0), row("report", "Arbitros 线", 1, 500.0)]), "work")

    def test_zero_weight_never_and_ties_by_file_order(self):
        text = LANES.replace("权重：40%", "权重：20%")
        self.assertEqual(self.choose(text=text), "work")  # tie: file order
        self.assertEqual(self.choose(text=LANES.replace("权重：40%", "权重：0%")), "quant")
        self.assertIsNone(self.choose(text=LANES.replace("权重：40%", "权重：0%").replace("权重：20%", "权重：0%")))

    def test_busy_unit_skipped(self):
        self.assertEqual(self.choose(busy={"work:general"}), "quant")
        self.assertIsNone(self.choose(busy={"work:general", "quant:general"}))
        self.assertEqual(self.unit(busy={"work"}), ("work", "general"))  # the deputy unit is busy, the executor is not

    def test_units_are_roles_or_role_projects(self):
        self.assertEqual(self.unit(), ("work", "general"))
        text = LANES.replace("- [todo] w1", "- [review] w1")
        self.assertEqual(self.unit(text=text), ("work", None))
        self.assertEqual(self.unit(text=LANES.replace("- [todo] w1", "- [todo] Uvox：w1")), ("work", "uvox"))
        self.assertEqual(self.unit(text="## Arbitros 线\n权重：40%\n- [done] x\n"), ("work", None))  # plan

    def test_busy_project_does_not_hold_up_the_lines_other_projects(self):
        text = "## Arbitros 线\n权重：40%\n- [todo] Uvox：a\n- [todo] Uvox：b\n- [todo] Umba：c\n- [todo] 杂项\n"
        self.assertEqual(self.unit(text=text), ("work", "uvox"))  # file order
        self.assertEqual(self.unit(text=text, busy={"work:uvox"}), ("work", "umba"))
        # per-line cap: two live executors of the line block its third, other lines are untouched
        self.assertIsNone(self.unit(text=text, busy={"work:uvox", "work:umba"}))
        self.assertEqual(self.unit(text=text + "## 量化线\n权重：20%\n- [todo] q\n", busy={"work:uvox", "work:umba"}),
                         ("quant", "general"))

    def test_review_and_plan_keep_priority(self):
        text = "## Arbitros 线\n权重：40%\n- [todo] Uvox：a\n- [review] Umba：r\n"
        self.assertEqual(self.unit(text=text), ("work", None))  # review before todo
        self.assertEqual(self.unit(text=text, busy={"work"}), ("work", "uvox"))  # deputy unit busy -> todo
        self.assertEqual(self.unit(text=text, deputy_busy={"work"}), ("work", "uvox"))
        self.assertEqual(self.unit(text="## Arbitros 线\n权重：40%\n- [review] r\n", busy={"work"}), None)  # no plan with a review

    def test_plan_cooldown(self):
        text = "## Arbitros 线\n权重：40%\n- [done] x\n"
        self.assertIsNone(self.choose([row("plan", "Arbitros 线", 1)], text))
        self.assertEqual(self.choose([row("plan", "Arbitros 线", 3)], text), "work")
        self.assertEqual(self.choose([row("plan", "量化线", 1)], text), "work")  # per-section cooldown
        self.assertEqual(self.choose([row("plan", "Arbitros 线", 1)], text + "- [todo] t\n"), "work")  # todo ignores it
        self.assertEqual(self.choose([row("plan", "Arbitros 线", 1)], text + "- [review] t\n"), "work")

    def test_plan_pause_line_holds_plan_until_its_time(self):
        pause = lambda stamp: f"## Arbitros 线\n权重：40%\n空闲补行暂停到：{stamp}（都在等日期）\n- [blocked: 等你决定] x\n"
        later, earlier = "2026-09-30T20:30Z", "2026-09-30T08:00Z"  # NOW is 09-30 12:00 UTC
        self.assertEqual(tick.parse_backlog(pause(later))["Arbitros 线"]["plan_pause"], datetime(2026, 9, 30, 20, 30, tzinfo=tick.timezone.utc))
        self.assertIsNone(self.unit(text=pause(later)))  # before the time: no plan
        self.assertEqual(self.choose(text=pause(earlier)), "work")  # after it: plan again, by itself
        self.assertEqual(self.choose(text=pause(later) + "- [todo] t\n"), "work")  # a todo still dispatches an executor
        self.assertEqual(self.choose(text=pause(later) + "- [review] t\n"), "work")  # so does a review
        with mock.patch.object(tick, "note_malformed") as bad:
            self.assertEqual(self.choose(text=pause("下周三")), "work")  # unparseable: ignored, plan as usual
        bad.assert_called_once()

    def test_review_goes_first_while_the_deputy_session_cache_is_hot(self):
        text = LANES.replace("- [todo] q1", "- [review] q1")  # work would win on score, quant has a review
        hot = lambda hours, **kw: row("review", "量化线", hours, role="quant", **kw)
        self.assertEqual(self.choose(text=text), "work")  # no recent quant run: score order
        self.assertEqual(self.choose([hot(0.9)], text=text), "quant")  # ended 54 min ago: warm
        self.assertEqual(self.choose([hot(56 / 60)], text=text), "work")  # 56 min: the cache has expired
        self.assertEqual(self.choose([row("executor", "量化线", 0.1, role="quant", project="uvox")], text=text), "work")  # other session
        self.assertEqual(self.choose([hot(0.1, auto=True)], text=text), "work")  # scripted, no session
        self.assertEqual(self.choose([hot(0.1, engine="codex")], text=text), "work")
        self.assertEqual(self.choose([hot(0.1)], text=text, deputy_busy={"quant"}), "work")  # session in use
        self.assertEqual(self.choose([row("plan", "量化线", 0.1, role="quant")], text=LANES), "work")  # warm but nothing to review
        self.assertEqual(self.choose([hot(0.1, ok=False)], text=text), "work")  # the failure backoff still applies
        self.assertEqual(tick.choose_lane(text, [row("coordinate", "组织线", 9), hot(0.1)], NOW, set()), ("coordinator", None))

    def test_coordinate_precedence(self):
        self.assertEqual(tick.choose_lane(LANES, [], NOW, set()), ("coordinator", None))  # never run
        self.assertEqual(tick.choose_lane(LANES, [row("coordinate", "组织线", 9)], NOW, set()), ("coordinator", None))
        self.assertEqual(tick.choose_lane(LANES, [row("coordinate", "组织线", 9)], NOW, {"coordinator"}), ("work", "general"))
        self.assertEqual(tick.choose_lane(LANES, [row("coordinate", "组织线", 7)], NOW, set()), ("work", "general"))


class SprintFirst(TickCase):
    """owner\x2011-03: a sprint project's row that has stood as todo/review SPRINT_FIRST_H goes before the score and
    CROSS_STARVE_H. History is the temp BACKLOG_HISTORY that TickCase patches in; nothing here touches control/data."""
    COORD = row("coordinate", "组织线", 1)  # keeps the coordinate run from being due

    def setUp(self):
        super().setUp()
        tick.LOG_FILE.write_text("", encoding="utf-8")  # a readable, empty tick-log: no sprint priority granted lately

    def unit(self, text, runs=(), note=None):
        return tick.choose_lane(text, [self.COORD, *runs], NOW, set(), set(), note)

    def granted(self, section, hours_ago, decision="spawn"):
        at = (NOW - tick.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        with tick.LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": at, "decision": decision, "lane": "quant", "sprint_first": section}, ensure_ascii=False) + "\n")

    @staticmethod
    def text(status="todo", sprint_row="Uvox：冲刺行"):
        """Arbitros 线 (weight 40, wins on score) has a plain Umba row; 量化线 (weight 20) has the sprint row."""
        return ("## Arbitros 线\n权重：40%\n- [todo] Umba：非冲刺行\n"
                f"## 量化线\n权重：20%\n冲刺：Uvox\n- [{status}] {sprint_row}\n")

    def snap(self, text, hours_ago, daily=False):
        d = tick.BACKLOG_HISTORY / "daily" if daily else tick.BACKLOG_HISTORY
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"backlog-{len(list(d.iterdir()))}.md"
        path.write_text(text, encoding="utf-8")
        at = (NOW - tick.timedelta(hours=hours_ago)).timestamp()
        os.utime(path, (at, at))

    def test_sprint_row_standing_5h_goes_before_the_cheaper_line(self):
        text = self.text()
        self.assertEqual(self.unit(text), ("work", "umba"))  # no history: by ratio
        for h in (5, 3, 1):
            self.snap(text, h)
        self.assertEqual(self.unit(text), ("quant", "uvox"))

    def test_standing_1h_or_unknown_keeps_the_ratio_order(self):
        text = self.text()
        self.snap(text, 1)
        self.assertEqual(self.unit(text), ("work", "umba"))  # 1 h < SPRINT_FIRST_H
        self.snap(self.text(sprint_row="Uvox：别的行"), 6)  # older history without this row
        self.assertEqual(self.unit(text), ("work", "umba"))
        shutil.rmtree(tick.BACKLOG_HISTORY)
        self.assertEqual(self.unit(text), ("work", "umba"))  # no history at all

    def test_a_break_in_the_history_restarts_the_count(self):
        text = self.text()
        self.snap(text, 6, daily=True)
        self.snap(self.text(status="running 18:00"), 3)  # running 3 h ago: not standing
        self.snap(text, 1)
        self.assertEqual(self.unit(text), ("work", "umba"))
        self.snap(text, 2)  # a copy between does not bridge the running copy
        self.assertEqual(self.unit(text), ("work", "umba"))

    def test_daily_snapshot_counts_and_review_rows_too(self):
        text = self.text(status="review")
        self.snap(text, 20, daily=True)
        self.snap(text, 0.5)
        self.assertEqual(self.unit(text), ("quant", None))

    def test_unit_not_of_the_sprint_project_is_left_alone(self):
        text = self.text(sprint_row="Umba：别的项目的行")  # the line's sprint is Uvox, its unit is Umba's
        self.snap(text, 8)
        self.assertEqual(self.unit(text), ("work", "umba"))

    def test_the_longest_standing_sprint_row_goes_first(self):
        two = lambda a, b: (f"## Arbitros 线\n权重：40%\n冲刺：Uvox\n- [todo] Uvox：{a}\n"
                            f"## 量化线\n权重：20%\n冲刺：Uvox\n- [todo] Uvox：{b}\n")
        self.snap(two("甲", "乙").replace("Uvox：甲", "Uvox：丁"), 9)  # 乙 stood since 9 h ago
        self.snap(two("甲", "乙"), 5)  # 甲 since 5 h ago
        self.snap(two("甲", "乙"), 2)
        self.assertEqual(self.unit(two("甲", "乙")), ("quant", "uvox"))  # the higher ratio, but standing longer
        shutil.rmtree(tick.BACKLOG_HISTORY)
        for h in (2, 5):
            self.snap(two("甲", "乙"), h)
        self.snap(two("甲", "乙").replace("Uvox：乙", "Uvox：丁"), 12)  # now 甲 stood since 12 h ago, 乙 since 5 h
        self.assertEqual(self.unit(two("甲", "乙")), ("work", "uvox"))

    def test_it_also_goes_before_cross_starvation(self):
        text = self.text()
        for h in (5, 4.5):
            self.snap(text, h)
        starving = [row("executor", "Arbitros 线", 8, role="work", project="umba")]  # Arbitros is CROSS_STARVE_H overdue
        self.assertEqual(self.unit(text, starving), ("quant", "uvox"))
        self.assertEqual(self.unit(self.text(sprint_row="Uvox：别"), starving), ("work", "umba"))  # no history for this row
    def test_a_line_gets_the_priority_once_per_sprint_first_h(self):
        text = self.text()
        self.snap(text, 6)
        note = {}
        self.assertEqual(self.unit(text, note=note), ("quant", "uvox"))
        self.assertEqual(note["sprint_first"], "量化线")
        self.granted("量化线", 1)  # the spawn log says 量化线 had it 1 h ago
        self.assertEqual(self.unit(text, note=note), ("work", "umba"))  # back to the ratio
        self.assertIsNone(note["sprint_first"])
        self.granted("Arbitros 线", 0.5)  # another line's grant does not count
        self.assertEqual(self.unit(text), ("work", "umba"))

    def test_after_sprint_first_h_the_line_may_have_it_again(self):
        text = self.text()
        self.snap(text, 6)
        self.granted("量化线", 4.5)
        self.assertEqual(self.unit(text), ("quant", "uvox"))
        self.granted("量化线", 3.9)
        self.assertEqual(self.unit(text), ("work", "umba"))
        self.granted("量化线", 0.1, decision="pacing")  # only spawn rows count (none other has the field in practice)
        shutil.rmtree(tick.BACKLOG_HISTORY)  # nothing stands any more: untouched
        self.assertEqual(self.unit(text), ("work", "umba"))

    def test_unreadable_log_gives_no_priority(self):
        text = self.text()
        self.snap(text, 6)
        tick.LOG_FILE.unlink()
        self.assertEqual(self.unit(text), ("work", "umba"))

    def test_a_line_that_is_all_sprint_has_the_same_cap(self):
        text = "## Arbitros 线\n权重：40%\n- [todo] Umba：非冲刺行\n## 量化线\n权重：10%\n冲刺：量化\n- [review] 量化：甲\n- [review] 量化：乙\n"
        self.snap(text, 30)
        self.assertEqual(self.unit(text), ("quant", None))
        self.granted("量化线", 0.2)
        self.assertEqual(self.unit(text), ("work", "umba"))  # 乙 has stood 30 h too, but the line just had its turn

class Supervisor(TickCase):
    def test_timed_rows_are_observed_before_unknown_usage_return_and_do_not_edit_backlog(self):
        text = "## 组织线\n- [todo] 组织：恢复〔时段：10:50–13:00〕\n"
        tick.BACKLOG.write_text(text, encoding="utf-8")
        st = {"sample": None, "spawned": {}, "aux": {}}
        with mock.patch.object(tick, "fresh_usage", return_value=None), \
                mock.patch.object(tick, "sample_usage") as sample, \
                mock.patch.object(tick, "latest_usage", return_value=None):
            for clock in ("04:49", "10:50", "13:00", "13:01"):
                tick.supervise_step(st, datetime.fromisoformat("2026-11-02T" + clock).timestamp())
        sample.assert_not_called()
        self.assertEqual(tick.BACKLOG.read_text(encoding="utf-8"), text)
        self.assertEqual([r["phase"] for r in self.logs() if r["decision"].startswith("timed_row_")],
                         ["before", "open", "expired"])
        self.assertFalse(any(args[:1] == ["lane"] for args in self.spawned))

    def test_timed_row_observation_failure_does_not_stop_the_supervisor(self):
        with mock.patch.object(tick.timed_rows, "observe", side_effect=OSError("disk")), self.sample(20):
            tick.supervise_step({"sample": None, "spawned": {}, "aux": {}}, time.time())
        self.assertIn("timed_rows_failed", [r["decision"] for r in self.logs()])
        self.assertTrue(self.spawned)

    def setUp(self):
        super().setUp()
        (self.d / "backlog.md").write_text(LANES, encoding="utf-8")
        self.add_run("coordinate", "组织线", 1)
        self.st = {"sample": None, "spawned": {}, "aux": {}}
        self.t0 = time.time()

    def add_run(self, kind, section, hours, cost=0.0):
        at = (datetime.now(tick.timezone.utc) - tick.timedelta(hours=hours)).isoformat()
        tick.append_jsonl(self.d / "runs.jsonl", {"at": at, "kind": kind, "section": section, "cost_usd": cost})

    def step(self, dt=0.0, pct=10, frac=0.5):
        with self.sample(pct, frac) as s:
            tick.supervise_step(self.st, self.t0 + dt)
        return s

    def lanes(self):
        return [a[1] for a in self.spawned if a[0] == "lane"]

    def lane_args(self):
        return [a[1:] for a in self.spawned if a[0] == "lane"]

    def test_loads_runs_once_per_round_and_reuses_each_snapshot(self):
        first, second, pick_runs, candidate_runs = [], [], [], []
        real_pick = tick.codex_pick

        def record_pick(text, runs, now, live):
            pick_runs.append(runs)
            return real_pick(text, runs, now, live)

        def candidates(mod, backlog, runs):
            candidate_runs.append(runs)
            return []

        with mock.patch.object(tick, "load_runs", side_effect=[first, second]) as loads, \
                mock.patch.object(tick, "codex_plan_spawn", REAL_CODEX_PLAN_SPAWN), \
                mock.patch.object(tick, "CODEX_PLAN_ON", True), \
                mock.patch.object(tick, "codex_pick", side_effect=record_pick), \
                mock.patch.object(tick.shadow_review, "candidates", side_effect=candidates), \
                mock.patch.object(tick, "choose_lane", wraps=tick.choose_lane) as choose:
            self.step(pct=10)
            self.step(dt=601, pct=60)

        self.assertEqual(loads.call_count, 2)
        self.assertEqual(len(choose.call_args_list), 1)
        self.assertIs(choose.call_args.args[1], first)
        self.assertEqual(len(pick_runs), 4)  # plan + fallback, once in each round
        self.assertEqual([runs is first for runs in pick_runs], [True, True, False, False])
        self.assertEqual(len(candidate_runs), 1)
        self.assertIs(candidate_runs[0], second)

    def test_spawns_lane_with_baseline_and_logs(self):
        self.step()
        self.assertEqual(self.lanes(), ["work"])
        self.assertEqual(self.logs()[-1]["decision"], "spawn")
        self.assertNotIn("starved", self.logs()[-1])

    def test_active_window_keeps_usage_gates_in_front(self):
        text = "## 组织线\n- [todo] 组织：restore〔时段：10:50–13:00〕〔执行者：Claude〕\n"
        (self.d / "backlog.md").write_text(text, encoding="utf-8")
        with mock.patch.object(tick, "local_datetime", return_value=datetime(2026, 11, 2, 12, 55)):
            self.step(pct=43, frac=0.5)
            self.assertEqual(self.lanes(), [])
            self.assertEqual(self.logs()[-1]["decision"], "pacing")
            self.step(tick.SAMPLE_EVERY_S + 1, pct=10, frac=0.5)
        self.assertEqual(self.lane_args(), [["coordinator", "incore"]])

    def test_spawn_log_names_the_project_the_starve_rule_advanced(self):
        (self.d / "backlog.md").write_text("## Arbitros 线\n冲刺：驾校模拟器\n- [todo] 驾校模拟器：a\n- [todo] Umba：b\n", encoding="utf-8")
        self.step()
        self.assertEqual(self.lane_args(), [["work", "umba"]])
        self.assertEqual((self.logs()[-1]["decision"], self.logs()[-1]["starved"]), ("spawn", "umba"))

    def test_cross_line_starve_log_and_pacing_gate_stay_in_front(self):
        text = "\n".join(["## Arbitros 线", "权重：40%", "- [todo] w1", "## 生活线", "权重：40%", "- [todo] 论文：p1", ""])
        for sec in ("Arbitros 线", "生活线"):
            self.add_run("executor", sec, 30 if sec == "生活线" else 1, cost=5.0 if sec == "生活线" else 0.0)
        (self.d / "backlog.md").write_text(text, encoding="utf-8")
        self.step(pct=43, frac=0.5)  # the pacing gate is ahead of the cross-line rule: nobody is chosen
        self.assertEqual(self.lanes(), [])
        self.st.update(sample=None, tried=-1e9)
        self.step()
        self.assertEqual(self.lanes(), ["life"])  # work has the lower score, life waited 30 h
        self.assertEqual(self.logs()[-1]["cross_starved"], "生活线")

    def test_spawn_log_names_the_line_the_sprint_rule_advanced(self):
        text = "## Arbitros 线\n权重：40%\n- [todo] w1\n## 量化线\n权重：20%\n冲刺：Uvox\n- [todo] Uvox：冲刺行\n"
        (self.d / "backlog.md").write_text(text, encoding="utf-8")
        (self.d / "tick-log.jsonl").write_text("", encoding="utf-8")  # the grant check needs a readable log
        (self.d / "backlog-history").mkdir()
        snap = self.d / "backlog-history" / "backlog-1.md"
        snap.write_text(text, encoding="utf-8")
        at = time.time() - 6 * 3600
        os.utime(snap, (at, at))
        self.step()
        self.assertEqual(self.lanes(), ["quant"])  # work has the lower score
        self.assertEqual(self.logs()[-1]["sprint_first"], "量化线")

    @mock.patch.object(tick.time, "time", return_value=1790856000)
    def test_pacing_and_ceiling_block_lanes(self, clock):
        # Keep the step and its integer reset sample on the same whole second.
        self.t0 = clock.return_value
        self.step(pct=43, frac=0.5)  # baseline 40 + 2 slack
        self.assertEqual((self.lanes(), self.logs()[-1]["decision"]), ([], "pacing"))
        self.st.update(sample=None, tried=-1e9)
        self.step(pct=91, frac=1.0)
        self.assertEqual((self.lanes(), self.logs()[-1]["decision"]), ([], "hard_ceiling"))
        self.st.update(sample=None, tried=-1e9)
        self.step(pct=42, frac=0.5)  # exactly on baseline + slack is allowed
        self.assertEqual(self.lanes(), ["work"])
        self.assertEqual(self.logs()[-1]["baseline"], 40.0)

    def test_unknown_usage_blocks_lanes_and_retries(self):
        with mock.patch.object(tick, "sample_usage", return_value=None) as s:
            for dt in (0, 30, 61):  # a failed sample is retried after SAMPLE_RETRY_S, not every loop
                tick.supervise_step(self.st, self.t0 + dt)
        self.assertEqual((self.lanes(), s.call_count), ([], 2))
        self.assertEqual(self.logs()[0]["decision"], "usage_unknown")

    def test_sample_interval(self):
        with self.sample(10, 0.5) as s:
            for dt in (0, 30, 599):
                tick.supervise_step(self.st, self.t0 + dt)
            self.assertEqual(s.call_count, 1)
            tick.supervise_step(self.st, self.t0 + 601)
            self.assertEqual(s.call_count, 2)

    def test_newer_usage_from_a_lane_overrides_own_sample(self):
        self.step(0)  # 10 % -> spawns work
        resets = int(self.t0 + tick.WINDOW_SECONDS * 0.5)  # baseline 40
        tick.append_jsonl(self.d / "usage.jsonl", {"observed_at": datetime.fromtimestamp(
            self.t0 + 10, tick.timezone.utc).isoformat(), "utilization": 0.45, "resets_at": resets, "source": "tick"})
        self.step(30)  # own sample still 10 % (not due), but the lane saw 45 % -> no spawn
        self.assertEqual(self.lanes(), ["work"])
        self.assertEqual(self.st["sample"][1][0], 45.0)

    def test_stale_usage_line_ignored(self):
        tick.append_jsonl(self.d / "usage.jsonl", {"observed_at": datetime.fromtimestamp(
            self.t0 - 600, tick.timezone.utc).isoformat(), "utilization": 0.90, "resets_at": 1, "source": "tick"})
        self.step(0)
        self.assertEqual(self.lanes(), ["work"])

    def test_fresh_observation_reused_instead_of_a_haiku_call(self):
        resets = int(time.time() + tick.WINDOW_SECONDS * 0.5)

        def seen(age):
            tick.append_jsonl(self.d / "usage.jsonl", {"observed_at": datetime.fromtimestamp(
                time.time() - age, tick.timezone.utc).isoformat(), "utilization": 0.25, "resets_at": resets,
                "source": "tick:executor"})
        with mock.patch.object(tick, "sample_usage", return_value=(50.0, resets)) as s:
            seen(10)
            self.assertEqual((tick.fresh_usage(), s.call_count), ((25.0, resets), 0))
            seen(tick.REUSE_S + 5)
            self.assertEqual((tick.fresh_usage(), s.call_count), ((50.0, resets), 1))

    def test_max_parallel_counts_deputy_runs_and_executors(self):
        for name in ("work-uvox", "work-umba", "quant"):
            (self.d / f"lane-{name}.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.step()
        self.assertEqual(self.lanes(), [])
        (self.d / "lane-quant.lock").unlink()  # two live: a third unit may start
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.step(1000)
        self.assertEqual(self.lanes(), ["quant"])

    def test_live_unit_skipped(self):
        (self.d / "lane-work-general.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.step()
        self.assertEqual(self.lane_args(), [["quant", "general"]])

    def test_spawn_args_carry_the_project(self):
        (self.d / "backlog.md").write_text(LANES.replace("- [todo] w1", "- [todo] Uvox：w1\n- [todo] Umba：w2")
                                           .replace("- [todo] q1", "- [review] q1"), encoding="utf-8")
        self.step(0)
        self.step(30)
        self.step(60)  # three live: no more
        self.assertEqual(self.lane_args(), [["work", "uvox"], ["work", "umba"], ["quant"]])
        self.assertEqual(sorted(self.st["spawned"]), ["quant", "work:umba", "work:uvox"])

    def test_stale_lane_lock_does_not_count(self):
        (self.d / "lane-work-general.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=False):
            self.step()
        self.assertEqual(self.lanes(), ["work"])

    def test_double_spawn_guard(self):
        self.step(0)
        self.step(30)  # work is live by grace (no lock file yet) -> next lane
        self.step(60)  # both units live (grace) -> nothing left to start
        self.assertEqual(self.lanes(), ["work", "quant"])
        self.step(100)  # work's grace is over (spawned at 0); quant (spawned at 30) is still live
        self.assertEqual(self.lanes(), ["work", "quant", "work"])

    def test_coordinate_precedence(self):
        (self.d / "runs.jsonl").unlink()
        self.step()
        self.assertEqual(self.lanes(), ["coordinator"])

    def test_report_and_owner_spawns_rate_limited_and_lock_aware(self):
        self.step(0)
        self.assertEqual([a for a in self.spawned if a[0] != "lane"], [["report"]])  # no owner item in the backlog
        (self.d / "backlog.md").write_text(OWNER_BACKLOG, encoding="utf-8")
        self.step(30)
        self.assertIn(["owner"], self.spawned)
        self.step(60)
        self.step(301)
        self.step(331)
        self.assertEqual([a[0] for a in self.spawned if a[0] != "lane"], ["report", "owner", "report", "owner"])
        (self.d / "owner.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.step(700)
        self.assertEqual(len([a for a in self.spawned if a == ["owner"]]), 2)

    def test_report_and_owner_spawn_even_when_usage_unknown(self):
        (self.d / "backlog.md").write_text(OWNER_BACKLOG, encoding="utf-8")
        with mock.patch.object(tick, "sample_usage", return_value=None):
            tick.supervise_step(self.st, self.t0)
        self.assertEqual(self.spawned, [["report"], ["owner"]])

    def asks(self):
        return [a for a in self.spawned if a == ["ask"]]

    def test_ask_row_spawns_the_ask_lane_ahead_of_pacing_and_regardless_of_the_lane_limits(self):
        (self.d / "backlog.md").write_text(LANES + "- [blocked: 等总理] 量化：实盘授权范围\n", encoding="utf-8")
        self.step(0, pct=85, frac=0.0)  # 85 % is over the pacing line: no deputy lane, but the question still goes out
        self.assertEqual((self.asks(), self.lanes()), ([["ask"]], []))
        for name in ("work-uvox", "work-umba", "quant"):  # MAX_PARALLEL units live: the ask lane is not one of them
            (self.d / f"lane-{name}.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.step(301, pct=95, frac=0.5)  # over the hard ceiling too: the lane itself declines (AskLane tests)
        self.assertEqual(len(self.asks()), 2)

    def test_ask_spawn_waits_for_a_free_coordinator_and_does_not_double_up(self):
        (self.d / "backlog.md").write_text(LANES + "- [blocked: 等总理] 量化：实盘授权范围\n", encoding="utf-8")
        (self.d / "deputy-coordinator.lock").write_text("4242")
        self.assertEqual(tick.deputy_lock("coordinator").name, "deputy-coordinator.lock")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.step(0)
        self.assertEqual(self.asks(), [])  # the coordinator's session is in use
        (self.d / "deputy-coordinator.lock").unlink()
        (self.d / "ask.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.step(30)
        self.assertEqual(self.asks(), [])  # a run is under way
        (self.d / "ask.lock").unlink()
        self.step(60)
        self.step(90)  # AUX_EVERY_S since the spawn
        self.assertEqual(len(self.asks()), 1)

    def test_no_ask_row_no_ask_spawn(self):
        self.step(0)
        self.assertEqual(self.asks(), [])

    def test_main_worktree_alarm_reaches_ask_lane_in_the_same_loop(self):
        with mock.patch.object(tick.review_packet, "git", return_value="claude/daily-health-line\n"), \
                mock.patch.object(tick, "main_worktree_guard", side_effect=REAL_MAIN_WORKTREE_GUARD):
            self.step(0, pct=85, frac=0.0)  # the existing ask path is independent of pacing
            self.step(30, pct=85, frac=0.0)
        self.assertEqual(self.asks(), [["ask"]])
        self.assertEqual(self.backlog().count("〔主目录 HEAD：claude/daily-health-line〕"), 1)

    def test_main_worktree_guard_failure_does_not_stop_the_loop(self):
        with mock.patch.object(tick, "main_worktree_guard", side_effect=OSError("git unavailable")):
            self.step()
        self.assertEqual(self.lanes(), ["work"])
        self.assertEqual(self.logs()[0]["decision"], "main_worktree_guard_failed")

    def test_pause_and_busy_via_main(self):
        (self.d / "tick.PAUSE").write_text("")
        with mock.patch.object(tick, "sample_usage") as s:
            tick.main([])
        s.assert_not_called()
        self.assertEqual(self.logs()[-1]["decision"], "paused")
        self.assertFalse((self.d / "tick.lock").exists())

    def test_main_supervises_and_releases_lock(self):
        with self.sample(10, 0.5), mock.patch.object(tick.time, "sleep") as sleep:
            tick.main([])
        self.assertEqual(self.lanes(), ["work"])
        sleep.assert_not_called()  # SUPERVISE_MAX_S = 0 ends the loop after one step
        self.assertFalse((self.d / "tick.lock").exists())

    def test_loop_sleeps_between_steps(self):
        with mock.patch.object(tick, "SUPERVISE_MAX_S", 100), self.sample(10, 0.5), \
                mock.patch.object(tick.time, "time", side_effect=itertools.chain([0, 0, 0], itertools.repeat(200))), \
                mock.patch.object(tick.time, "sleep") as sleep:
            tick.supervise()
        sleep.assert_called_once_with(tick.LOOP_S)

    def test_supervisor_start_puts_interrupted_running_rows_back(self):
        start = datetime.now().replace(hour=13, minute=0, second=0, microsecond=0).astimezone()  # naive local first: .astimezone().replace() carries the offset of "now" and breaks on a DST-change day
        ran = {"kind": "executor", "item": "Delta：c", "ok": True, "elapsed_s": 600, "_at": start + tick.timedelta(seconds=600)}
        owner = "【Discord·工作副总理】改 x 〔msg:7 role:work〕"
        rows = ["# h", "## 你的指令（最优先）", f"- [running 10:00] {owner}", "## Arbitros 线", "- [running 11:00] Uvox：a",
                "- [running 12:00] Umba：b", "- [running 13:00] Delta：c", "- [todo] Uvox：later", "## 杂项", "- [running 14:00] ?"]
        text = "\r\n".join(rows) + "\r\n"
        (self.d / "backlog.md").write_bytes(text.encode("utf-8"))
        (self.d / "lane-work-umba.lock").write_text("4242")  # Umba's executor is still alive
        with mock.patch.object(tick, "pid_alive", side_effect=lambda pid: pid == 4242), \
                mock.patch.object(tick, "load_runs", return_value=[ran]):
            self.assertEqual(tick.recover_running(), 3)
            self.assertEqual(tick.recover_running(), 0)  # nothing left to put back
        got = (self.d / "backlog.md").read_bytes().decode("utf-8")
        want = text.replace("[running 10:00] ", "[todo] ").replace("[running 11:00] ", "[todo] ").replace("[running 13:00] ", "[review] ")
        self.assertEqual(got, want)  # the live Umba row and the unrecognised one are untouched; CRLF kept
        self.assertEqual(got.count("\r\n"), got.count("\n"))
        logged = [(e["item"], e["to"]) for e in self.logs() if e["decision"] == "running_recovered"]
        self.assertEqual(logged, [(owner, "todo"), ("Uvox：a", "todo"), ("Delta：c", "review")])
        with mock.patch.object(tick, "pid_alive", side_effect=lambda pid: pid == 4242):  # a live owner lock: the owner row stays
            (self.d / "owner.lock").write_text("4242")
            (self.d / "backlog.md").write_bytes(text.encode("utf-8"))
            tick.recover_running([])
        self.assertIn(f"- [running 10:00] {owner}", (self.d / "backlog.md").read_text(encoding="utf-8"))

    def test_supervisor_start_records_master_commit_and_tick_hash(self):
        with mock.patch.object(tick.review_packet, "git", return_value="a" * 40 + "\n"), \
                mock.patch.object(tick, "supervise_step"):
            tick.supervise()
        record = json.loads((self.d / "supervisor-state.json").read_text(encoding="utf-8"))
        self.assertEqual(record["master_commit"], "a" * 40)
        self.assertEqual(record["tick_sha256"], hashlib.sha256(Path(tick.__file__).resolve().read_bytes()).hexdigest())
        self.assertIsNotNone(datetime.fromisoformat(record["started_at"]).tzinfo)

    def test_supervise_runs_the_recovery_once_at_start(self):
        (self.d / "backlog.md").write_text("## Arbitros 线\n- [running 11:00] Uvox：a\n", encoding="utf-8")
        with mock.patch.object(tick, "pid_alive", return_value=False), self.sample(10, 0.5):
            tick.supervise()
        self.assertIn("- [todo] Uvox：a", self.backlog())
        with mock.patch.object(tick, "recover_running", side_effect=OSError("x")), self.sample(10, 0.5):
            tick.supervise()  # a failing recovery never stops the supervisor
        self.assertEqual([e["decision"] for e in self.logs() if e["decision"] == "running_recover_failed"], ["running_recover_failed"])


class SolFirstSkipReasonLogs(TickCase):
    def test_sol_first_logs_each_returned_skip_reason(self):
        reasons = ("不合格：分支只改 .md", "已禁用", "本周额度已到上限：95%（上限 95%）",
                   "当日上限（60 次）", "重复")
        with mock.patch.object(tick, "codex_weekly_used", return_value=7.0), \
                mock.patch.object(tick, "take_lock", return_value=True), \
                mock.patch.object(tick, "release_lock"), \
                mock.patch.object(tick, "review_packet_for", return_value={"text": "packet", "hard": None}), \
                mock.patch.object(tick.shadow_review, "load_state", return_value={}), \
                mock.patch.object(tick.shadow_review, "run", side_effect=[(None, reason) for reason in reasons]):
            for number, reason in enumerate(reasons):
                item = f"Delta：skip reason {number}"
                executor = {"at": datetime.now(timezone.utc).isoformat(), "role": "work", "section": "Arbitros 线",
                            "kind": "executor", "item": item, "project": "delta", "ok": True,
                            "run_file": f"executor-{number}.jsonl"}
                tick.append_jsonl(tick.RUNS_FILE, executor)
                tick.BACKLOG.write_text(f"## Arbitros 线\n- [review] {item}\n", encoding="utf-8")
                review_id = tick.shadow_review.review_id_of("work", "Arbitros 线", item, executor)
                self.assertEqual(tick.sol_first_lane("work", review_id), 0)
        logged = [row["reason"] for row in self.logs() if row["decision"] == "sol_first_skipped"]
        self.assertEqual(logged, list(reasons))
        self.assertEqual(len(set(logged)), len(reasons))


class Launch(TickCase):
    def run_fake_session(self, scenario, sid="old", resume=True):
        fake = self.d / "fake_claude.py"
        calls = self.d / f"fake_calls_{scenario}.jsonl"
        fake.write_text(
            "import json, sys\n"
            "scenario, call_file, *args = sys.argv[1:]\n"
            "with open(call_file, 'a', encoding='utf-8') as f: f.write(json.dumps(args) + '\\n')\n"
            "resuming = '--resume' in args\n"
            "if scenario == 'transient' and resuming:\n"
            "    sys.stderr.write('x' * (16384) + '\\nTemporary network unavailable\\n')\n"
            "    raise SystemExit(1)\n"
            "if scenario == 'new_failure':\n"
            "    sys.stderr.write('CLI could not initialize the new session\\n')\n"
            "    raise SystemExit(1)\n"
            "if scenario == 'after_work':\n"
            "    print(json.dumps({'type': 'assistant'}), flush=True)\n"
            "    sys.stderr.write('Network failed after the first assistant event\\n')\n"
            "    raise SystemExit(1)\n"
            "if scenario == 'missing' and resuming:\n"
            "    sys.stderr.write('No conversation found for session ID: old\\n')\n"
            "    raise SystemExit(1)\n"
            "if scenario == 'unrecoverable' and resuming:\n"
            "    sys.stderr.write('This session cannot be recovered; session data is corrupted\\n')\n"
            "    raise SystemExit(1)\n"
            "print(json.dumps({'type': 'result', 'is_error': False, 'result': 'fresh session started'}), flush=True)\n",
            encoding="utf-8")

        def fake_argv(_model, _rules, extra=(), *_args, **_kwargs):
            return [sys.executable, str(fake), scenario, str(calls), *extra]

        retried = []
        with mock.patch.object(tick, "build_argv", side_effect=fake_argv), \
                mock.patch.object(tick, "WORKSPACE", self.d), \
                mock.patch.object(tick, "CREATE_NO_WINDOW", tick.CREATE_NO_WINDOW if os.name == "nt" else 0):
            result = tick.session_run("review", "test prompt", f"fake-{scenario}", sid, resume, None,
                                      on_retry=retried.append)
        invocations = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
        files = sorted((self.d / "ticks").rglob(f"*-fake-{scenario}*.jsonl"))
        return result, retried, invocations, files

    def test_resume_start_failure_is_saved_bounded_and_keeps_session(self):
        (code, run_file, sid, resumed), retried, calls, _ = self.run_fake_session("transient")
        self.assertEqual((code, sid, resumed, len(calls), retried), (1, "old", True, 1, []))
        events = [json.loads(line) for line in run_file.read_text(encoding="utf-8").splitlines()]
        error = next(event for event in events if event.get("type") == "stderr")
        self.assertTrue(error["truncated"])
        self.assertLessEqual(len(error["message"].encode("utf-8")), tick.MAX_STDERR_BYTES)
        self.assertIn("Temporary network unavailable", error["message"])

    def test_new_session_start_failure_is_saved_without_retry(self):
        (code, run_file, sid, resumed), retried, calls, _ = self.run_fake_session("new_failure", "new-id", False)
        events = [json.loads(line) for line in run_file.read_text(encoding="utf-8").splitlines()]
        error = next(event for event in events if event.get("type") == "stderr")
        self.assertEqual((code, sid, resumed, len(calls), retried), (1, "new-id", False, 1, []))
        self.assertEqual(error["message"].splitlines(), ["CLI could not initialize the new session"])

    def test_failure_after_work_is_saved_without_retry(self):
        (code, run_file, sid, resumed), retried, calls, _ = self.run_fake_session("after_work")
        self.assertEqual((code, sid, resumed, len(calls), retried), (1, "old", True, 1, []))
        self.assertTrue(tick.final_result(run_file)[2])
        self.assertTrue(any(json.loads(line).get("type") == "stderr"
                            for line in run_file.read_text(encoding="utf-8").splitlines()))

    def test_explicit_missing_session_gets_one_fresh_session_retry(self):
        for scenario, diagnostic in (("missing", "No conversation found"),
                                     ("unrecoverable", "cannot be recovered")):
            with self.subTest(scenario=scenario):
                (code, _, sid, resumed), retried, calls, files = self.run_fake_session(scenario)
                self.assertEqual((code, resumed, len(calls), len(retried)), (0, False, 2, 1))
                self.assertNotEqual(sid, "old")
                self.assertEqual(retried, [sid])
                self.assertEqual(calls[0][:2], ["--resume", "old"])
                self.assertEqual(calls[1][0], "--session-id")
                original = next(path for path in files if "-retry" not in path.name)
                error = next(json.loads(line) for line in original.read_text(encoding="utf-8").splitlines()
                             if json.loads(line).get("type") == "stderr")
                self.assertIn(diagnostic, error["message"])

    def test_argv(self):
        a = tick.build_argv("sonnet", "RULES")
        self.assertEqual(a[:8], [tick.CLAUDE, "--print", "--output-format", "stream-json", "--verbose",
                                 "--model", "sonnet", "--permission-mode"])
        self.assertEqual(a[-2:], ["--append-system-prompt", "RULES"])
        self.assertIn("acceptEdits", a)
        self.assertNotIn("--effort", a)

    def test_only_tools_for_executor_runs(self):
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        self.assertNotIn("--tools", tick.build_argv("sonnet", "RULES"))
        a = tick.build_argv("sonnet", "RULES", tools=["Read", "Edit"], only_tools=True)
        self.assertEqual(a[a.index("--tools"):a.index("--tools") + 3], ["--tools", "Read", "Edit"])
        for kind, want in (("executor", True), ("review", False), ("report", False), ("owner", False),
                           ("lessons", False)):
            with mock.patch.object(tick.subprocess, "Popen", return_value=proc) as popen:
                tick.run_claude(kind, "p", self.d / "t.jsonl", [], None)
            argv = popen.call_args[0][0]
            self.assertEqual("--tools" in argv, want, kind)
            if want:
                i = argv.index("--tools")
                self.assertEqual(argv[i + 1:i + 1 + len(tick.TOOLS)], tick.TOOLS)

    def test_strict_mcp_config_in_every_run(self):
        """Drops the unauthorised claude.ai connectors and their trailing "authorise them" note; remove with owner's authorisation."""
        self.assertIn("--strict-mcp-config", tick.build_argv("sonnet", "RULES"))
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        for kind in ("executor", "review", "report", "owner", "coordinate", "plan", "lessons"):
            with mock.patch.object(tick.subprocess, "Popen", return_value=proc) as popen:
                tick.run_claude(kind, "p", self.d / "t.jsonl")
            self.assertIn("--strict-mcp-config", popen.call_args[0][0], kind)

    def test_effort_in_argv(self):
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        for kind, effort, want in (("review", None, "xhigh"), ("executor", "medium", "medium"),
                                   ("executor", "high", "high"), ("report", None, "xhigh"), ("owner", None, "xhigh"),
                                   ("coordinate", None, "xhigh"), ("plan", None, "xhigh")):
            with mock.patch.object(tick.subprocess, "Popen", return_value=proc) as popen:
                tick.run_claude(kind, "p", self.d / "t.jsonl", [], effort)
            argv = popen.call_args[0][0]
            self.assertEqual(argv[argv.index("--effort") + 1], want, kind)

    def test_compact_at_sets_the_autocompact_percentage(self):
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        with mock.patch.object(tick.subprocess, "Popen", return_value=proc) as popen:
            tick.run_claude("executor", "p", self.d / "t.jsonl", [], "high", compact_at=tick.EXECUTOR_COMPACT_TOKENS)
            env = popen.call_args.kwargs["env"]
            tick.run_claude("review", "p", self.d / "t.jsonl")
        self.assertEqual(env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "25")
        self.assertEqual(env["PATH"], os.environ["PATH"])  # the rest of the environment is kept
        self.assertIsNone(popen.call_args.kwargs["env"])  # no limit: Claude Code's default

    def test_spawn_detached_with_breakaway_then_fallback(self):
        with mock.patch.object(tick, "spawn", ORIG_SPAWN), mock.patch.object(tick.subprocess, "Popen") as popen:
            tick.spawn(["lane", "work"])
        argv, kw = popen.call_args[0][0], popen.call_args.kwargs
        self.assertEqual(argv[0], tick.PYTHONW)
        self.assertEqual(argv[2:], ["lane", "work"])
        self.assertEqual(kw["creationflags"], 0x00000008 | 0x08000000 | 0x01000000)
        self.assertEqual(kw["stdout"], subprocess.DEVNULL)
        with mock.patch.object(tick, "spawn", ORIG_SPAWN), \
                mock.patch.object(tick.subprocess, "Popen", side_effect=[OSError("denied"), mock.Mock()]) as popen:
            tick.spawn(["owner"])
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0x00000008 | 0x08000000)

    def test_timeout_kills_tree(self):
        proc = mock.Mock(pid=777, returncode=1)
        proc.communicate.side_effect = [subprocess.TimeoutExpired("x", 1), ("", "")]
        with mock.patch.object(tick.subprocess, "Popen", return_value=proc), \
                mock.patch.object(tick.subprocess, "run") as run:
            code = tick.run_claude("review", "p", self.d / "ticks" / "t.jsonl")
        self.assertEqual(code, -1)
        self.assertEqual(run.call_args[0][0], ["taskkill", "/PID", "777", "/T", "/F"])
        self.assertEqual(proc.communicate.call_args_list[0].kwargs["timeout"], 2700)

    def test_resume_writes_cost_baseline_first(self):
        cs = lambda total: json.dumps({"type": "cost-state", "startTime": 1, "totalCostUSD": total})
        (self.d / "s9.jsonl").write_text("\n".join([cs(1.5), "{}", cs(28.862), "not json"]), encoding="utf-8")
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        self.assertEqual(tick.session_cost("nope"), 0.0)
        with mock.patch.object(tick, "SESSIONS_DIR", self.d), mock.patch.object(tick.subprocess, "Popen", return_value=proc):
            self.assertEqual(tick.session_cost("s9"), 28.862)
            tick.run_claude("review", "p", self.d / "r.jsonl", ["--resume", "s9"])
            tick.run_claude("review", "p", self.d / "n.jsonl", ["--session-id", "s9"])
        lines = [json.loads(x) for x in (self.d / "r.jsonl").read_text().splitlines()]
        self.assertEqual(lines[1], {"type": "cost_baseline", "session_id": "s9", "total_cost_usd": 28.862})
        self.assertEqual([x["type"] for x in (json.loads(y) for y in (self.d / "n.jsonl").read_text().splitlines())], ["launch"])

    def test_launch_line_is_first_without_the_rules_text(self):
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        with mock.patch.object(tick.subprocess, "Popen", return_value=proc):
            tick.run_claude("review", "SECRET PROMPT", self.d / "l.jsonl", ["--session-id", "s1"])
        text = (self.d / "l.jsonl").read_text()
        launch = json.loads(text.splitlines()[0])
        self.assertEqual((launch["type"], launch["kind"]), ("launch", "review"))
        self.assertIn("--session-id", launch["argv"])
        self.assertIn("<rules 5 chars>", launch["argv"])  # RULES
        self.assertNotIn("SECRET PROMPT", text)
        self.assertNotIn("RULES", text)
        self.assertEqual(tick.final_result(self.d / "l.jsonl")[0], False)  # readers skip the line
        self.assertIsNone(tick.result_stats(self.d / "l.jsonl")["cost_usd"])

    def test_launch_line_records_prompt_length_not_text(self):
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        prompt = "SECRET PROMPT 提示"
        with mock.patch.object(tick.subprocess, "Popen", return_value=proc):
            tick.run_claude("review", prompt, self.d / "pc.jsonl", ["--session-id", "s1"])
        text = (self.d / "pc.jsonl").read_text(encoding="utf-8")
        launch = json.loads(text.splitlines()[0])
        self.assertEqual(launch["prompt_chars"], len(prompt))
        self.assertNotIn("SECRET PROMPT", text)

    def test_cwd_and_add_dir_reach_the_process(self):
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        with mock.patch.object(tick.subprocess, "Popen", return_value=proc) as popen:
            tick.run_claude("executor", "p", self.d / "a.jsonl", ["--resume", "s"], "high", cwd=self.d,
                            add_dirs=(Path("X"), Path("Y")))
            tick.run_claude("review", "p", self.d / "b.jsonl", ["--resume", "s"])
        argv = popen.call_args_list[0][0][0]
        self.assertEqual(argv[argv.index("--resume"):], ["--resume", "s", "--add-dir", "X", "Y"])
        self.assertEqual(popen.call_args_list[0].kwargs["cwd"], str(self.d))
        self.assertNotIn("--add-dir", popen.call_args_list[1][0][0])
        self.assertEqual(popen.call_args_list[1].kwargs["cwd"], str(tick.WORKSPACE))

    def test_session_cost_finds_transcript_under_any_project_dir(self):
        folder = self.d / "claude-projects" / "C--some-other-cwd"
        folder.mkdir(parents=True)
        (folder / "s7.jsonl").write_text(json.dumps({"type": "cost-state", "totalCostUSD": 3.5}), encoding="utf-8")
        self.assertEqual(tick.session_cost("s7"), 3.5)
        self.assertEqual(tick.session_cost("missing"), 0.0)

    def test_update_deputy_merges_executors_per_project(self):
        tick.update_deputy("work", executors={"uvox": {"session_id": "a", "runs": 1}})
        tick.update_deputy("work", executors={"uvox": {"runs": 2}, "umba": {"session_id": "b"}})
        ex = json.loads((self.d / "deputies.json").read_text(encoding="utf-8"))["work"]["executors"]
        self.assertEqual(ex, {"uvox": {"session_id": "a", "runs": 2}, "umba": {"session_id": "b"}})

    def test_executor_timeout_value(self):
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        with mock.patch.object(tick.subprocess, "Popen", return_value=proc):
            self.assertEqual(tick.run_claude("executor", "p", self.d / "t.jsonl"), 0)
        self.assertEqual(proc.communicate.call_args.kwargs["timeout"], 7200)

    def test_executor_gets_the_per_process_budget_cap(self):
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        self.assertNotIn("--max-budget-usd", tick.build_argv("sonnet", "RULES"))
        for kind, role, want in (("executor", "work", "10.0"), ("executor", "quant", "5.0"),
                                 ("review", None, None), ("report", None, None), ("lessons", None, None)):
            with mock.patch.object(tick.subprocess, "Popen", return_value=proc) as popen:
                tick.run_claude(kind, "p", self.d / "t.jsonl", role=role)
            argv = popen.call_args[0][0]
            self.assertEqual(argv[argv.index("--max-budget-usd") + 1] if "--max-budget-usd" in argv else None,
                             want, (kind, role))

    def test_quant_executor_passes_role_to_cli_budget(self):
        with mock.patch.object(tick, "session_run", return_value=(0, self.d / "run.jsonl", "sid", False)) as run, \
                mock.patch.object(tick, "final_result", return_value=(True, "ok", None)), \
                mock.patch.object(tick, "record_run", return_value={"model": "sonnet", "context_tokens": None}):
            tick.run_executor("quant", "quant", "量化：budget test", "general")
        self.assertEqual(run.call_args.kwargs["role"], "quant")

    def test_fork_session_passes_role_to_cli_budget(self):
        on_fork = mock.Mock()
        with mock.patch.object(tick, "run_claude", return_value=0) as run:
            code, run_file, sid, resumed = tick.session_run(
                "executor", "p", "fork", "", False, None,
                fork_from="source-session", on_fork=on_fork, role="uvox")
        self.assertEqual((code, sid, resumed), (0, "", False))
        self.assertEqual(run.call_args.args[3], ["--resume", "source-session", "--fork-session"])
        self.assertEqual(run.call_args.kwargs["role"], "uvox")
        on_fork.assert_called_once_with(run_file)


class InFlight(TickCase):
    """run_claude reads the stream while the process runs: usage.jsonl gets each rate_limit_event when read, and a
    run is stopped at HARD_CEILING."""

    def go(self, kind, during, extra=()):
        """Run run_claude against a fake process whose communicate() calls during(run_file, killed); kill_tree sets
        `killed`. Returns (exit code, kill_tree mock, run_file)."""
        killed, run_file = threading.Event(), self.d / "ticks" / "t.jsonl"
        proc = mock.Mock(pid=4242, returncode=0)
        proc.communicate.side_effect = lambda *a, **k: during(run_file, killed) or ("", "")
        with mock.patch.object(tick, "WATCH_EVERY_S", 0.02), \
                mock.patch.object(tick.subprocess, "Popen", return_value=proc), \
                mock.patch.object(tick, "kill_tree", side_effect=lambda pid: killed.set()) as kill:
            return tick.run_claude(kind, "p", run_file, list(extra)), kill, run_file

    def usage(self):
        f = self.d / "usage.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []

    def write(self, run_file, *lines):
        with run_file.open("ab") as f:
            f.write(("\n".join(lines) + "\n").encode("utf-8"))

    def wait(self, cond):
        end = time.time() + 5
        while not cond() and time.time() < end:
            time.sleep(0.01)

    def test_ceiling_stop_kills_logs_and_stamps_the_read_time(self):
        started = datetime.now(timezone.utc).replace(microsecond=0)
        resets = int(time.time()) + 3600

        def during(run_file, killed):
            self.write(run_file, json.dumps({"type": "assistant"}), event(0.95, resets))
            self.assertTrue(killed.wait(5))

        code, kill, run_file = self.go("review", during)
        self.assertEqual(code, tick.CEILING_STOP)
        kill.assert_called_once_with(4242)
        stop, = [x for x in self.logs() if x["decision"] == "ceiling_stop"]
        self.assertEqual((stop["kind"], stop["u"]), ("review", 95.0))
        rec, = self.usage()  # the final read does not record it a second time
        self.assertEqual((rec["utilization"], rec["resets_at"], rec["source"]), (0.95, resets, "tick:review"))
        self.assertGreaterEqual(datetime.fromisoformat(rec["observed_at"]), started)
        self.assertFalse(tick.final_result(run_file)[0])  # a result event closes the stream: is_error

    def test_events_are_recorded_while_the_run_lives_and_only_once(self):
        resets, seen = int(time.time()) + 3600, {}

        def during(run_file, killed):
            self.write(run_file, event(0.30, resets))
            self.wait(lambda: len(self.usage()) == 1)
            seen["mid_run"] = len(self.usage())  # written before the process ended
            self.write(run_file, event(0.40, resets))
            self.wait(lambda: len(self.usage()) == 2)
            with run_file.open("ab") as f:
                f.write(event(0.50, resets).encode("utf-8"))  # last line without a newline: read at the end

        code, kill, _ = self.go("executor", during)
        self.assertEqual((code, seen["mid_run"]), (0, 1))
        kill.assert_not_called()
        self.assertEqual([r["utilization"] for r in self.usage()], [0.30, 0.40, 0.50])
        self.assertFalse([x for x in self.logs() if x["decision"] == "ceiling_stop"])

    def test_below_the_ceiling_the_run_is_left_alone(self):
        def during(run_file, killed):
            self.write(run_file, event(0.89, 1))
            self.wait(lambda: self.usage())

        code, kill, _ = self.go("review", during)
        self.assertEqual(code, 0)
        kill.assert_not_called()

    def real_events(self, run):
        """The assistant events of a real finished run (test_stopped_run.json: id, model, usage and the length of each
        text / tool_use input / thinking signature; the content itself is same-length filler), one stream line per
        content block like the CLI writes them."""
        out = []
        for ev in run["events"]:
            for b in ev["blocks"]:
                block = ({"type": "thinking", "thinking": "", "signature": "s" * b["n"]} if b["type"] == "thinking"
                         else {"type": "text", "text": "t" * b["n"]} if b["type"] == "text"
                         else {"type": "tool_use", "id": "toolu_x", "name": "Read", "input": {"p": "p" * (b["n"] - 8)}})
                out.append(json.dumps({"type": "assistant", "message": {
                    "id": ev["id"], "model": ev["model"], "usage": ev["usage"], "content": [block]}}))
        return out

    def test_stopped_run_cost_is_estimated_from_the_stream_not_the_transcript(self):
        # Real transcripts only get a cost-state when the process exits normally: this one has the pre-run total and
        # nothing from the killed run. Both runs are real (ticks/ 2026-10-01), resumed, one Opus and one Sonnet.
        runs = json.loads((Path(__file__).parent / "test_stopped_run.json").read_text(encoding="utf-8"))
        for i, run in enumerate(runs):
            sid = f"s{i}"
            (self.d / f"{sid}.jsonl").write_text(json.dumps({"type": "cost-state", "totalCostUSD": run["baseline"]}),
                                                 encoding="utf-8")

            def during(run_file, killed, run=run):
                self.write(run_file, *self.real_events(run), event(0.97, 1))
                self.assertTrue(killed.wait(5))

            with mock.patch.object(tick, "SESSIONS_DIR", self.d):
                code, _, run_file = self.go("executor", during, ["--resume", sid])
            self.assertEqual(code, tick.CEILING_STOP)
            stats = tick.result_stats(run_file)
            self.assertTrue(stats["cost_estimated"])
            self.assertAlmostEqual(stats["cost_usd"] / run["result_cost"], 1.0, delta=0.25, msg=run["run"])
            self.assertNotEqual(stats["cost_usd"], 0)

    def test_estimate_counts_each_message_once(self):
        f = self.d / "e.jsonl"
        use = {"input_tokens": 1000, "cache_read_input_tokens": 100000, "cache_creation_input_tokens": 10000,
               "cache_creation": {"ephemeral_5m_input_tokens": 4000, "ephemeral_1h_input_tokens": 6000}, "output_tokens": 3}
        ev = lambda block: json.dumps({"type": "assistant", "message": {
            "id": "m1", "model": "claude-opus-5-5", "usage": use, "content": [block]}})
        f.write_text("\n".join([ev({"type": "text", "text": "x" * 1000}), ev({"type": "text", "text": "y" * 1000})]),
                     encoding="utf-8")
        # input 1000*4 + 5m 4000*5 + 1h 6000*8 + read 100000*0.2 = 92000; output 0.653 * 2000 chars * 20 = 26120 (per M)
        self.assertAlmostEqual(tick.estimate_stream_cost(f), (92000 + 0.653 * 2000 * 20) / 1e6, places=6)

    def test_estimate_adds_background_cost_of_web_tools_once_per_call(self):
        f = self.d / "w.jsonl"
        use = {"input_tokens": 10, "output_tokens": 1}
        ev = lambda mid, name, tid: json.dumps({"type": "assistant", "message": {"id": mid, "model": "claude-sonnet-5-5", "usage": use,
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": {}}]}})
        f.write_text("\n".join([ev("a", "Read", "1"), ev("b", "Read", "2")]), encoding="utf-8")
        base = tick.estimate_stream_cost(f)
        f.write_text("\n".join([ev("a", "WebFetch", "1"), ev("a", "WebFetch", "1"), ev("b", "WebSearch", "2"), ev("c", "Read", "3")]), encoding="utf-8")
        self.assertAlmostEqual(tick.estimate_stream_cost(f) - base, 2 * tick.WEB_TOOL_USD + (10 * 2 / 1e6) * 0 + 0, delta=1e-4)

    def test_haiku_prices_by_prompt_size_per_request(self):
        f = self.d / "h.jsonl"
        ev = lambda mid, read: json.dumps({"type": "assistant", "message": {"id": mid, "model": "claude-haiku-5-5", "content": [],
                                           "usage": {"input_tokens": 0, "cache_read_input_tokens": read, "output_tokens": 0}}})
        f.write_text("\n".join([ev("a", 100_000), ev("b", 100_001)]), encoding="utf-8")
        self.assertAlmostEqual(tick.estimate_stream_cost(f), (100_000 * 0.01 + 100_001 * 0.05) / 1e6, places=9)
        # the 10-09 A/B run's totals (wt/ab-logs/ab-tm-haiku.jsonl; every request under 100k): Claude Code reported $0.01207905
        p = tick.PRICES["haiku"]
        self.assertAlmostEqual((16 * p[0] + 33377 * p[2] + 195805 * p[3] + 6888 * p[4]) / 1e6, 0.01207905, places=8)

    def test_stopped_resume_is_not_retried_as_a_dead_session(self):
        with mock.patch.object(tick, "run_claude", return_value=tick.CEILING_STOP) as run:
            tick.TICKS_DIR.mkdir(parents=True, exist_ok=True)
            code, _, sid, resumed = tick.session_run("executor", "p", "x", "old", True, None)
        self.assertEqual((code, sid, resumed, run.call_count), (tick.CEILING_STOP, "old", True, 1))


class LaunchReserve(TickCase):
    step, lanes, lane_args, add_run = Supervisor.step, Supervisor.lanes, Supervisor.lane_args, Supervisor.add_run

    def setUp(self):
        super().setUp()
        (self.d / "backlog.md").write_text(LANES, encoding="utf-8")
        self.add_run("coordinate", "组织线", 1)
        self.st = {"sample": None, "spawned": {}, "aux": {}}
        self.t0 = time.time()

    def test_launch_block_rule(self):
        # (u, running): the unit about to start plus those running each need LAUNCH_RESERVE points below the ceiling
        for u, running, want in ((82, 0, None), (82.5, 0, "reserve"), (74, 1, None), (75, 1, "reserve"),
                                 (66, 2, None), (67, 2, "reserve"), (90, 0, "hard_ceiling")):
            self.assertEqual(tick.launch_block(u, 95, running), want, (u, running))  # base 95: pacing out of the way
        self.assertEqual(tick.launch_block(50, 40, 0), "pacing")  # the pacing line comes first
        with mock.patch.object(tick, "LAUNCH_RESERVE", 0.0):
            self.assertIsNone(tick.launch_block(89, 88, 2))

    def test_supervisor_holds_back_when_running_units_leave_no_room(self):
        self.st["spawned"] = {"quant": self.t0}  # one unit running
        self.step(pct=76, frac=1.0)  # pacing allows 82; 76 + 8 * 2 = 92 > 90
        self.assertEqual((self.lanes(), self.logs()[-1]["decision"]), ([], "reserve"))
        self.assertEqual(self.logs()[-1]["running"], 1)
        self.st.update(sample=None, tried=-1e9)
        self.step(pct=74, frac=1.0)  # 74 + 16 = 90 fits
        self.assertEqual(len(self.lanes()), 1)

    def test_lane_holds_back_by_the_units_running_besides_itself(self):
        with mock.patch.object(tick, "baseline", return_value=80.0), self.sample(76, 1.0), \
                mock.patch.object(tick, "lock_units", return_value={"work", "quant"}), \
                mock.patch.object(tick, "run_claude") as run:
            tick.lane_run("work")
        self.assertEqual(self.logs()[-1]["decision"], "reserve")
        self.assertEqual(self.logs()[-1]["running"], 1)  # the other one; `units` includes this lane itself
        run.assert_not_called()


class CodexPlan(TickCase):
    """owner\x2011-01 terminal 「改，直接上线」: Codex with nothing to take gets a deputy plan run that writes rows for it, past
    the pacing line; Codex lanes no longer count toward Claude's MAX_PARALLEL."""
    step, lanes, add_run = Supervisor.step, Supervisor.lanes, Supervisor.add_run
    ONE = "## Arbitros 线\n权重：40%\n- [todo] Uvox：a〔执行者：Claude〕\n"

    def setUp(self):
        super().setUp()
        (self.d / "backlog.md").write_text(self.ONE, encoding="utf-8")
        self.add_run("coordinate", "组织线", 1)
        self.st = {"sample": None, "spawned": {}, "aux": {}}
        self.t0 = time.time()
        for p in (mock.patch.object(tick, "codex_plan_spawn", REAL_CODEX_PLAN_SPAWN), mock.patch.object(tick, "CODEX_PLAN_ON", True)):
            p.start()
            self.addCleanup(p.stop)

    def test_no_plan_while_switched_off(self):  # owner\x2011-04: the live value
        with mock.patch.object(tick, "CODEX_PLAN_ON", False):
            self.step(pct=43, frac=0.5)
        self.assertEqual(self.plans(), [])

    def plans(self):
        return [a[1] for a in self.spawned if a[0] == "codex-plan"]

    def test_plan_spawns_past_the_pacing_line_when_codex_has_nothing(self):
        self.step(pct=43, frac=0.5)  # pacing holds Claude lanes back
        self.assertEqual((self.plans(), self.lanes()), (["work"], []))
        self.assertIn("codex_plan_spawn", [e["decision"] for e in self.logs()])
        self.st.update(sample=None, tried=-1e9)
        self.step(dt=60, pct=43, frac=0.5)  # within CODEX_PLAN_EVERY_S: no second one
        self.assertEqual(self.plans(), ["work"])

    def test_no_plan_when_codex_has_a_row_a_recent_plan_or_the_ceiling(self):
        with mock.patch.object(tick, "codex_pick", return_value=("work", "uvox")):
            self.step(pct=43, frac=0.5)
        self.assertEqual(self.plans(), [])
        self.st.update(sample=None, tried=-1e9, aux={}, spawned={})
        self.step(dt=1, pct=91, frac=1.0)  # hard ceiling (90 in these tests)
        self.assertEqual(self.plans(), [])
        self.add_run("plan", "Arbitros 线", 1)  # inside PLAN_COOLDOWN_S
        self.st.update(sample=None, tried=-1e9, aux={}, spawned={})
        self.step(dt=2, pct=43, frac=0.5)
        self.assertEqual(self.plans(), [])

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_codex_lanes_do_not_fill_claude_max_parallel(self):
        (self.d / "backlog.md").write_text("## 量化线\n权重：40%\n- [todo] 量化：q\n", encoding="utf-8")
        for name in ("codex.lock", "codex-2.lock", "codex-3.lock", "lane-work-delta.lock", "lane-work-umba.lock", "lane-life-writing.lock"):
            (self.d / name).write_text(str(os.getpid()), encoding="utf-8")  # three live Codex lanes
        self.assertEqual(tick.codex_units(), {"work:delta", "work:umba", "life:writing"})
        with mock.patch.object(tick, "codex_plan_spawn", lambda *a: None):
            self.step(pct=10, frac=0.5)
        self.assertEqual(self.lanes(), ["quant"])  # before 11-01 the three Codex lanes filled MAX_PARALLEL

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_lane_runs_past_pacing_but_not_at_the_ceiling(self):
        run = mock.Mock(return_value=(0, self.d / "r.jsonl", True, "ok", 1.0))
        for u, called in ((60, True), (95, False)):
            run.reset_mock()
            with mock.patch.object(tick, "fresh_usage", return_value=(u, int(self.t0) + 3600)), \
                    mock.patch.object(tick, "baseline", return_value=10.0), mock.patch.object(tick, "run_resident", run):
                self.assertEqual(tick.main(["codex-plan", "life"]), 0)
            self.assertEqual(run.called, called, u)
        with mock.patch.object(tick, "fresh_usage", return_value=(60, int(self.t0) + 3600)), \
                mock.patch.object(tick, "baseline", return_value=10.0), mock.patch.object(tick, "run_resident", run):
            tick.main(["codex-plan", "life"])
        self.assertEqual(run.call_args[0][1], "plan")
        self.assertIn("Codex 现在没有能领的行", run.call_args[0][2])
        self.assertIn("生活线", run.call_args[0][2])
        self.assertTrue(self.logs()[-1].get("codex_plan"))


CODEX_STREAM = "\n".join([json.dumps({"type": "thread.started", "thread_id": "th-1"}), "stderr noise",
                          json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done, see audits"}}),
                          json.dumps({"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 700,
                                                                          "cache_write_input_tokens": 0, "output_tokens": 55}})])


TIERS = (("gpt-6-luna", ("max", "xhigh"), 20, True), ("gpt-6.1-sol", ("high",), 10, False))  # shape of tick.CODEX_TIERS (10-05 caps)


class Codex(TickCase):
    """owner\x2010-04: while Claude usage holds launches back, Codex runs executor todos, one at a time; 10-05: luna (max
    else xhigh) only on clear small tasks, ≤20 a day; sol (high) on any todo, ≤10 a day."""
    step, lanes, lane_args, add_run = Supervisor.step, Supervisor.lanes, Supervisor.lane_args, Supervisor.add_run

    def setUp(self):
        super().setUp()
        (self.d / "backlog.md").write_text(LANES, encoding="utf-8")
        self.add_run("coordinate", "组织线", 1)
        self.st = {"sample": None, "spawned": {}, "aux": {}}
        self.t0 = time.time()
        for name, value in (("CODEX_TIERS", TIERS), ("CODEX_PARALLEL_SLOTS", 1),  # the parallel test below is written for the 10-07 value
                            ("CODEX_SLOTS", 3), ("CODEX_BURN_SLOTS", 5), ("CODEX_LOCKS", 5)):  # slot tests: 10-26 values (live: 20, 11-01)
            p = mock.patch.object(tick, name, value)
            p.start()
            self.addCleanup(p.stop)
        for fn in (tick.codex_fallback, tick.held_fallback):  # their `slots` default was bound to the live CODEX_SLOTS at import
            p = mock.patch.object(fn, "__defaults__", (3,))
            p.start()
            self.addCleanup(p.stop)

    def test_tier_by_task_size_and_daily_caps(self):
        used = lambda model, n: [{"_at": datetime.now(tick.timezone.utc), "engine": "codex", "model": model}] * n
        small, big = "Uvox：小修 x（修补类，按 ponytail 做）", "Uvox：阶段 6b 页面"
        self.assertEqual(tick.codex_tier(small, [])[0], "gpt-6-luna")
        self.assertEqual(tick.codex_tier(big, [])[0], "gpt-6.1-sol")  # luna never takes a big task
        self.assertEqual(tick.codex_tier(small, used("gpt-6-luna", 20))[0], "gpt-6.1-sol")  # luna full: sol
        self.assertIsNone(tick.codex_tier(big, used("gpt-6.1-sol", 10)))
        self.assertIsNone(tick.codex_tier(small, used("gpt-6-luna", 20) + used("gpt-6.1-sol", 10)))
        # owner\x2010-06: 不用 ponytail / 从零 is never small; 只读 alone no longer counts
        for item in ("写作模型：从零写，不用 ponytail", "Uvox：从零搭建页面（修补类之外）", "Umba：改两处，两处都只读查证"):
            self.assertEqual(tick.codex_tier(item, [])[0], "gpt-6.1-sol", item)
        self.assertEqual(tick.codex_tier("写作模型：改 x（修补类，按 ponytail 做）", [])[0], "gpt-6-luna")

    def codex(self):
        return [a[1:] for a in self.spawned if a[0] == "codex"]

    def test_codex_runs_while_claude_usage_waits(self):
        self.step(pct=43, frac=0.5)  # pacing blocks Claude
        self.assertEqual((self.lanes(), self.codex()), ([], [["work", "general"]]))
        self.assertEqual(self.logs()[-1]["decision"], "codex_spawn")
        self.step(dt=30, pct=43, frac=0.5)  # the spawn is still within its grace
        self.assertEqual(len(self.codex()), 1)

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_codex_keeps_one_slot_open_next_to_claude(self):
        # owner\x2010-07 terminal: after the FOS video Codex runs in parallel with Claude, one slot while Claude is not held back
        (self.d / "backlog.md").write_text("## Arbitros 线\n权重：40%\n- [todo] Uvox：a\n- [todo] Delta：b\n", encoding="utf-8")
        self.step(pct=10, frac=0.5)  # Claude may run
        claude, codex = self.lane_args(), self.codex()
        self.assertEqual((len(claude), len(codex)), (1, 1))
        self.assertEqual({claude[0][1], codex[0][1]}, {"uvox", "delta"})  # not the unit Claude just took
        self.spawned.clear()
        (self.d / "codex.lock").write_text(str(os.getpid()), encoding="utf-8")  # that Codex run is live
        self.st.update(sample=None, tried=-1e9, aux={}, spawned={})
        self.step(dt=900, pct=10, frac=0.5)
        self.assertEqual(self.codex(), [])  # a second Codex run waits for Claude to be held back
        self.st.update(sample=None, tried=-1e9, aux={}, spawned={})
        self.step(dt=1800, pct=70, frac=0.5)  # held back: both slots may be used
        self.assertEqual(len(self.codex()), 1)

    def test_codex_runs_next_to_a_full_claude_pool(self):
        (self.d / "backlog.md").write_text("## Arbitros 线\n权重：40%\n- [todo] Uvox：a\n", encoding="utf-8")
        with mock.patch.object(tick, "MAX_PARALLEL", 0):
            self.step(pct=10, frac=0.5)
        self.assertEqual((self.lanes(), self.codex()), ([], [["work", "uvox"]]))

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_daily_cap_and_live_codex_hold_it_back(self):
        today = datetime.now(tick.timezone.utc).isoformat()
        for model, n in (("gpt-6-luna", 20), ("gpt-6.1-sol", 10)):
            for _ in range(n):
                tick.append_jsonl(self.d / "runs.jsonl", {"at": today, "kind": "executor", "engine": "codex",
                                                          "model": model, "cost_usd": 0.0})
        self.step(pct=43, frac=0.5)
        self.assertEqual(self.codex(), [])
        (self.d / "runs.jsonl").write_text("", encoding="utf-8")
        (self.d / "codex.lock").write_text(str(os.getpid()), encoding="utf-8")  # one Codex run is live
        self.st.update(sample=None, tried=-1e9)
        self.step(pct=43, frac=0.5)
        self.assertEqual(len(self.codex()), 1)  # owner\x2010-07: a second may start
        self.spawned.clear()
        (self.d / "codex-2.lock").write_text(str(os.getpid()), encoding="utf-8")  # two live
        self.st.update(sample=None, tried=-1e9, aux={}, spawned={})
        self.step(pct=43, frac=0.5)
        self.assertEqual(len(self.codex()), 1)  # owner\x2010-25: a third may start
        self.spawned.clear()
        for lock in ("codex-2.lock", "codex-3.lock"):  # all CODEX_SLOTS (3, owner\x2010-25) live
            (self.d / lock).write_text(str(os.getpid()), encoding="utf-8")
        self.st.update(sample=None, tried=-1e9, aux={}, spawned={})
        self.step(pct=43, frac=0.5)
        self.assertEqual(self.codex(), [])

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_codex_lane_takes_the_second_slot_when_the_first_is_live(self):
        (self.d / "backlog.md").write_text("## Arbitros 线\n- [todo] Uvox：修 x（修补类）\n- [todo] Delta：d（修补类）\n", encoding="utf-8")
        (self.d / "codex.lock").write_text(str(os.getpid()), encoding="utf-8")

        def fake(prompt, run_file, *a, **k):
            run_file.parent.mkdir(parents=True, exist_ok=True)
            run_file.write_text(CODEX_STREAM, encoding="utf-8")
            return 0

        with mock.patch.object(tick, "codex_exec", side_effect=fake):
            self.assertEqual(tick.main(["codex", "work", "uvox"]), 0)
        self.assertIn("- [review] Uvox：修 x", self.backlog())
        self.assertFalse((self.d / "codex-2.lock").exists())  # released
        for lock in ("codex-2.lock", "codex-3.lock"):  # all CODEX_SLOTS live: the lane stays idle
            (self.d / lock).write_text(str(os.getpid()), encoding="utf-8")
        with mock.patch.object(tick, "codex_exec") as ex:
            self.assertEqual(tick.main(["codex", "work", "delta"]), 0)
        ex.assert_not_called()
        self.assertIn("- [todo] Delta：d", self.backlog())

    def test_pick_skips_opus_tagged_and_live_units_and_follows_fair_share(self):
        text = "\n".join(["## Arbitros 线", "权重：40%", "- [todo] Uvox：修 x", "- [todo] Delta：d", "## 量化线", "权重：20%",
                          "- [todo] 量化：y", ""])
        self.assertEqual(tick.codex_pick(text, [], NOW, set()), ("work", "uvox"))
        self.assertEqual(tick.codex_pick(text, [], NOW, {"work:uvox"}), ("work", "delta"))
        self.assertEqual(tick.codex_pick(text, [row("executor", "Arbitros 线", 1, 100.0)], NOW, set()), ("quant", "quant"))
        self.assertIsNone(tick.codex_pick(text.replace("- [todo] Uvox：修 x\n", "").replace("- [todo] Delta：d\n", "")
                                          .replace("量化：y", "量化：y 〔执行者：Opus〕"), [], NOW, set()))
        only_quant = lambda t: t.replace("- [todo] Uvox：修 x\n", "").replace("- [todo] Delta：d\n", "")
        claude_first = text.replace("量化：y", "量化：SEC 抓取 〔执行者：Claude〕\n- [todo] 量化：z")
        self.assertIsNone(tick.codex_pick(only_quant(text.replace("量化：y", "量化：SEC 抓取 〔执行者：Claude〕")), [], NOW, set()))  # nothing untagged left
        # owner\x2010-25 (coordinator): a tagged first todo is skipped, Codex takes the project's next untagged one
        sec = tick.parse_backlog(only_quant(claude_first))["量化线"]
        self.assertEqual(tick.codex_first_todo(sec, "quant")[1], "量化：z")
        self.assertEqual(tick.codex_pick(only_quant(claude_first), [], NOW, set()), ("quant", "quant"))
        self.assertIsNone(tick.executor_model("量化：SEC 抓取 〔执行者：Claude〕"))  # Claude runs it on Sonnet as usual
        haiku_first = text.replace("量化：y", "量化：小修 〔执行者：Haiku〕\n- [todo] 量化：z")
        self.assertEqual(tick.codex_pick(only_quant(haiku_first), [], NOW, set()), ("quant", "quant"))
        opus_first = text.replace("量化：y", "量化：画 〔执行者：Opus〕\n- [todo] 量化：z")
        self.assertEqual(tick.codex_first_todo(tick.parse_backlog(only_quant(opus_first))["量化线"], "quant")[1], "量化：z")

    def test_executor_model_tags(self):
        self.assertEqual(tick.executor_model("TM：小修 ①a ②b 〔执行者：Haiku〕"), "haiku")
        self.assertIsNone(tick.executor_model("（返工）TM：小修 〔执行者：Haiku〕〔审查判返工〕"))  # a rework goes back to Sonnet
        self.assertIsNone(tick.executor_model("TM：小修（10-09 判返工）〔执行者：Haiku〕"))
        self.assertEqual(tick.executor_model("项目七：画面 〔执行者：Opus〕"), "opus")
        self.assertEqual(tick.executor_model("（返工）项目七：画面 〔执行者：Opus〕"), "opus")  # Opus as before
        self.assertIsNone(tick.executor_model("TM：小修"))

    def test_executor_tags_ignore_quoted_references(self):
        for tag, model in ((tick.OPUS_TAG, "opus"), (tick.HAIKU_TAG, "haiku"), (tick.CLAUDE_TAG, None)):
            for tags, available, expected_model in ((f"「另一行{tag}」", True, None), (tag, False, model),
                                                     (f"「{tick.OPUS_TAG}」{tag}", False, model),
                                                     (f"{tag}「{tick.OPUS_TAG}」", False, model)):
                with self.subTest(tags=tags):
                    item = f"组织：修标签 {tags}"
                    todo = (f"- [todo] {item}", item)
                    self.assertEqual(tick.executor_model(item), expected_model)
                    self.assertEqual(tick.codex_first_todo({"todo": [todo]}, "incore"), todo if available else None)
        self.assertEqual(tick.executor_model(f"组织：修标签「{tick.HAIKU_TAG}」{tick.OPUS_TAG}"), "opus")

    def test_pick_skips_tagged_lines_and_a_review_row_does_not_hold_the_project(self):
        opus_first = "\n".join(["## Arbitros 线", "- [todo] Uvox：画吉祥物 〔执行者：Opus〕", "- [todo] Uvox：阶段 6a", "- [todo] Delta：d", ""])
        self.assertEqual(tick.codex_pick(opus_first, [], NOW, set()), ("work", "uvox"))  # owner\x2010-25: the Opus line is skipped, 阶段 6a is taken
        reviewing = "\n".join(["## Arbitros 线", "- [review] Uvox：r", "- [todo] Uvox：t", "- [todo] Delta：d", ""])
        self.assertEqual(tick.codex_pick(reviewing, [], NOW, set()), ("work", "uvox"))  # owner\x2010-25 (coordinator): a review row no longer holds the project
        self.assertEqual(tick.codex_pick(reviewing, [], NOW, {"work:uvox"}), ("work", "delta"))
        opus_only = opus_first.replace("- [todo] Delta：d\n", "").replace("- [todo] Uvox：阶段 6a\n", "")
        self.assertIsNone(tick.codex_pick(opus_only, [], NOW, set()))
        (self.d / "backlog.md").write_text(opus_only, encoding="utf-8")
        with mock.patch.object(tick, "codex_exec") as ex:
            self.assertEqual(tick.main(["codex", "work", "uvox"]), 0)  # the lane itself refuses the Opus line too
        ex.assert_not_called()

    def test_pick_leaves_a_project_to_claude_for_a_day_after_codex_was_sent_back(self):
        at = lambda h: datetime.now(tick.timezone.utc) - tick.timedelta(hours=h)
        ex = lambda item, h, engine=None, p="uvox": {"kind": "executor", "role": "work", "project": p, "engine": engine, "item": item, "_at": at(h)}
        rv = lambda item, h, verdict: {"kind": "review", "item": item, "verdict": verdict, "_at": at(h)}
        A = "Uvox：文案替换，把页面里的旧产品名换成新名字并检查所有语言版本，不要改变布局和图片素材的文件名"
        text = "\n".join(["## Arbitros 线", "权重：40%", "- [todo] " + A, "- [todo] Delta：d", ""])  # the sent-back line itself is back as todo
        # 10-05 shape: Codex does A -> sent back; Claude does B -> passes; the new (返工)A todo still stays with Claude
        shape = [ex(A, 5, "codex"), rv(A, 4, "rework"), ex("Uvox：B", 3), rv("Uvox：B", 2, "pass"), rv("Uvox：C", 1, "pass")]
        pick = lambda runs, now=None: tick.codex_pick(text, runs, now or datetime.now(tick.timezone.utc), set())
        self.assertEqual(pick([]), ("work", "uvox"))
        self.assertEqual(pick(shape), ("work", "delta"))  # other lines passing does not lift it
        self.assertEqual(pick(shape, datetime.now(tick.timezone.utc) + tick.timedelta(hours=18)), ("work", "delta"))  # 23 h after the run
        self.assertEqual(pick(shape, datetime.now(tick.timezone.utc) + tick.timedelta(hours=20)), ("work", "uvox"))  # 25 h after it
        self.assertEqual(pick([ex(A, 5, "codex"), rv(A, 4, "blocked")]), ("work", "delta"))
        self.assertEqual(pick([ex(A, 5, "codex"), rv(A, 4, "pass")]), ("work", "uvox"))
        # 10-06 08:48: the reviewer turned the line back to todo, its verdict not in the ledger yet: that line is held (10-26: only
        # that line; the whole project stayed locked while pacing kept the reviews waiting, and no Codex slot restarted)
        text_a = "\n".join(["## Arbitros 线", "权重：40%", "- [todo] " + A, "- [todo] Uvox：D", "- [todo] Delta：d", ""])
        pick_a = lambda runs: tick.codex_pick(text_a, runs, datetime.now(tick.timezone.utc), set())
        self.assertEqual(tick.codex_first_todo(tick.parse_backlog(text_a)["Arbitros 线"], "uvox", [ex(A, 5, "codex")], role="work")[1], "Uvox：D")
        self.assertEqual(pick_a([ex(A, 5, "codex")]), ("work", "uvox"))  # the project's other line is free
        self.assertIsNone(tick.codex_first_todo(tick.parse_backlog(text_a)["Arbitros 线"], "uvox", [ex(A, 5, "codex"), ex("Uvox：D", 4, "codex")], role="work"))
        self.assertEqual(pick_a([ex(A, 5, "codex"), ex("Uvox：D", 4, "codex")]), ("work", "delta"))  # both lines waiting for review
        # 10-28: sent back holds that line for a day, not the project: the project's other todo still goes to Codex
        self.assertEqual(tick.codex_first_todo(tick.parse_backlog(text_a)["Arbitros 线"], "uvox", [ex(A, 5, "codex"), rv(A, 4, "rework")], role="work")[1], "Uvox：D")
        self.assertEqual(pick_a([ex(A, 5, "codex"), rv(A, 4, "rework")]), ("work", "uvox"))
        # 10-29: auto_rework prefixes the returned todo, but the original Codex item remains held for 24 h
        reworked_text = text_a.replace("- [todo] " + A, "- [todo] （返工）" + A, 1)
        self.assertEqual(tick.codex_first_todo(tick.parse_backlog(reworked_text)["Arbitros 线"], "uvox",
                                                [ex(A, 5, "codex"), rv(A, 4, "rework")], role="work")[1], "Uvox：D")
        self.assertEqual(tick.codex_first_todo(tick.parse_backlog(text_a)["Arbitros 线"], "uvox", [ex(A, 5, "codex"), rv(A, 4, "blocked")], role="work")[1], "Uvox：D")
        self.assertEqual(pick_a([ex(A, 5, "codex"), rv(A, 4, "rework"), ex("Uvox：D", 3, "codex"), rv("Uvox：D", 2, "rework")]), ("work", "delta"))  # both lines sent back
        # the 24 h limit is per line: 25 h after A's rework A is free again
        old = [ex(A, 30, "codex"), rv(A, 29, "rework")]
        self.assertEqual(tick.codex_first_todo(tick.parse_backlog(text_a)["Arbitros 线"], "uvox", old, role="work")[1], A)
        self.assertEqual(pick([ex(A, 5), rv(A, 4, "rework")]), ("work", "uvox"))  # Claude's rework: Codex may try
        self.assertEqual(pick([ex(A, 5, "codex"), rv("Uvox：other line", 4, "rework"), rv(A, 3, "pass")]),
                         ("work", "uvox"))  # not Codex's line
        self.assertEqual(pick([ex(A, 5, "codex", "delta"), rv(A, 4, "rework")]), ("work", "uvox"))  # other project
        # the first review after the run decides, whatever the line's later rounds do
        self.assertEqual(pick([ex(A, 9, "codex"), rv(A, 8, "pass"), rv(A, 4, "rework")]), ("work", "uvox"))
        (self.d / "backlog.md").write_text(text, encoding="utf-8")
        (self.d / "runs.jsonl").write_text("".join(json.dumps({**r, "_at": r["_at"].isoformat(), "at": r["_at"].isoformat()}) + "\n" for r in shape), encoding="utf-8")
        with mock.patch.object(tick, "codex_exec") as codex:
            self.assertEqual(tick.main(["codex", "work", "uvox"]), 0)  # the lane itself refuses it too
        codex.assert_not_called()
    def test_codex_hold_after_a_sent_back_run_is_per_role_and_project(self):
        # `general` exists on the work and life lines: a work-line general run awaiting review must not hold life's
        text = "\n".join(["## Arbitros 线", "权重：40%", "## 生活线", "权重：40%", "- [todo] 订机票（修补类，按 ponytail 做）", ""])
        run = {"kind": "executor", "role": "work", "project": "general", "engine": "codex", "item": "整理桌面（修补类）",
               "_at": datetime.now(tick.timezone.utc) - tick.timedelta(hours=1)}
        sent_back = {"kind": "review", "item": run["item"], "verdict": "rework", "_at": datetime.now(tick.timezone.utc)}
        self.assertEqual(tick._codex_runs([run, sent_back], "general", None, "work"), {run["item"][:60]: "rework"})
        self.assertEqual(tick._codex_runs([run, sent_back], "general", None, "life"), {})
        self.assertEqual(tick.codex_pick(text, [run, sent_back], datetime.now(tick.timezone.utc), set()), ("life", "general"))
        self.assertEqual(tick.codex_unreviewed([run, sent_back], "general", role="work"), set())  # sent back is not 'waiting'
        self.assertEqual(tick.codex_unreviewed([run], "general", role="work"), {run["item"][:60]})
        self.assertEqual(tick.codex_unreviewed([run], "general", role="life"), set())

    def test_small_means_小修_in_the_title_only(self):
        small = ["Uvox 乌贼舞台小修（修补类，按 ponytail 做；…）", "量化：C1 小修两处（按 ponytail 做；…）",
                 "Uvox 导出页去掉样式副本（修补类，按 ponytail 做；…）", "小修 x（不带标记）", "情报：每日情报扫描"]
        big = ["Uvox 冲刺版本合流与一键试用版（把分支合进主线，做一键试用；乌贼舞台小修若还没通过就先不等它）",
               "Uvox：小修（从零搭建，按 ponytail 做）", "Uvox：改版（不用 ponytail）"]
        for item in small:
            self.assertTrue(tick.CODEX_SMALL.search(item), item)
        for item in big:
            self.assertFalse(tick.CODEX_SMALL.search(item), item)

    def test_pick_spreads_over_sections_by_codex_runs_today(self):
        text = "\n".join(["## Arbitros 线", "权重：40%", "- [todo] Uvox：a", "## 量化线", "权重：20%", "- [todo] 量化：y", ""])
        used = lambda sec, n: [{**row("executor", sec, 1, 0.0), "engine": "codex", "_at": datetime.now(tick.timezone.utc)}] * n
        self.assertEqual(tick.codex_pick(text, used("Arbitros 线", 1), NOW, set()), ("quant", "quant"))
        self.assertEqual(tick.codex_pick(text, used("Arbitros 线", 1) + used("量化线", 1), NOW, set()), ("work", "uvox"))

    def test_codex_lane_runs_the_todo_and_records_it(self):
        (self.d / "backlog.md").write_text("## Arbitros 线\n- [todo] Uvox：修 x（修补类）\n- [todo] Delta：d\n", encoding="utf-8")
        efforts, prompts = [], []

        def fake(prompt, run_file, cwd, effort, model, extra=(), gpu=False):
            efforts.append((model, effort))
            prompts.append(prompt)
            run_file.parent.mkdir(parents=True, exist_ok=True)
            if effort == "max":  # the model refuses the effort before any turn
                run_file.write_text("error: unsupported reasoning effort", encoding="utf-8")
                return 1
            run_file.write_text(CODEX_STREAM, encoding="utf-8")
            return 0

        with mock.patch.object(tick, "codex_exec", side_effect=fake):
            self.assertEqual(tick.main(["codex", "work", "uvox"]), 0)
        self.assertEqual(efforts, [("gpt-6-luna", "max"), ("gpt-6-luna", "xhigh")])
        self.assertTrue(prompts[0].startswith(tick.CODEX_PREAMBLE.replace(tick.CODEX_SANDBOX_DIR + "/venv", tick.CODEX_SANDBOX_DIR + "/venv-work-uvox") + "RULES"))
        self.assertIn("--python /home\x2fwsluser/.cache/codex-sandbox/venv-work-uvox/bin/python", prompts[0])  # own venv per unit: two runs at a time
        self.assertIn("任务：Uvox：修 x（修补类）。", prompts[0])
        self.assertEqual(self.backlog(), "## Arbitros 线\n- [review] Uvox：修 x（修补类）\n- [todo] Delta：d\n")
        row, = [json.loads(x) for x in (self.d / "runs.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(x).get("engine")]
        self.assertEqual((row["kind"], row["model"], row["effort"], row["session_id"], row["ok"], row["project"]),
                         ("executor", "gpt-6-luna", "xhigh", "th-1", True, "uvox"))
        self.assertEqual((row["cost_usd"], row["input_tokens"], row["cache_read_tokens"], row["output_tokens"]),
                         (0.0, 900, 700, 55))
        ran = [x for x in self.logs() if x["decision"] == "ran"][0]
        self.assertEqual((ran["engine"], ran["effort"], ran["exit_code"]), ("codex", "xhigh", 0))
        for lock in ("codex.lock", "lane-work-uvox.lock"):
            self.assertFalse((self.d / lock).exists())

    def test_codex_preamble_tells_how_to_run_python_in_wsl(self):
        # the sandbox's seccomp filter blocks the vsock that WSL interop needs, so *.exe cannot start (measured 10-05)
        uv = "/home\x2fwsluser/.local/bin/uv"
        self.assertIn("git -c user.name=codex -c user.email=codex@local commit", tick.CODEX_PREAMBLE)  # no author identity in WSL git
        for need in ("UtilBindVsockAnyPort", "python3", "UV_CACHE_DIR=/home\x2fwsluser/.cache/codex-sandbox/uv-cache", f"{uv} venv --clear /home\x2fwsluser/.cache/codex-sandbox/venv", f"{uv} pip install --python /home\x2fwsluser/.cache/codex-sandbox/venv/bin/python"):
            self.assertIn(need, tick.CODEX_PREAMBLE)

    def test_codex_preamble_asks_for_windows_portable_tests(self):
        # 10-15: a WSL-only pass hid 1 failure + 1 error on Windows (bare git, /mnt/c path, cp1252)
        for need in ("review_packet.GIT", "裸 git", "/mnt/c", 'encoding="utf-8"', "只在 WSL 跑过，Windows 未跑"):
            self.assertIn(need, tick.CODEX_PREAMBLE)

    def test_no_idle_prompts_and_owner_reader_sentence(self):
        # owner\x2010-15 "以后不要让它停下来": the coordinator must write todos for an idle line; the evening summary reports idle time
        self.assertIn("「空转」", tick.PROMPTS["coordinate"])
        self.assertIn("超过 2 小时", tick.PROMPTS["coordinate"])
        self.assertIn("必须替它写 todo", tick.PROMPTS["coordinate"])
        self.assertIn("空转时长", tick.REPORT_PROMPT_COORDINATOR[1])
        self.assertNotIn("空转时长", tick.REPORT_PROMPT_COORDINATOR[0])
        # every deputy's report (work/quant/life, intelligence, coordinator) and the Discord owner reply say who reads it
        reader = "他的注意力最贵：第一眼要看懂，不写给审查员看的标签"
        for prompt in (*tick.REPORT_PROMPT, *tick.REPORT_PROMPT_INTELLIGENCE, *tick.REPORT_PROMPT_COORDINATOR, tick.PROMPTS["owner"]):
            self.assertIn(reader, prompt)
        self.assertIn("本地 23:00–08:00 不发进度", tick.PROMPTS["review"])
        self.assertIn("写进审查文件，由 09:00 日报带出", tick.PROMPTS["review"])
        self.assertIn("生产事故、owner 当时主动来问的回复照发", tick.PROMPTS["review"])
        for prompt in tick.REPORT_PROMPT_INTELLIGENCE:
            self.assertIn("情报 MM-DD｜要动 N 条，线索 M 条", prompt)
            self.assertIn("最多 5 条，有截止日的排前", prompt)
            self.assertIn("线索：", prompt)
            self.assertIn("末尾标注交给哪条线", prompt)
            self.assertIn("今天没有要你动的事", prompt)
            self.assertNotIn("要你决定：……", prompt)
            self.assertIn("不要写总览、把握、未核、A 档/B 档", prompt)

    def test_codex_plan_prompt_checks_ready_to_start(self):
        prompt = tick.PROMPTS["codex_plan"]
        self.assertTrue(all(text in prompt for text in (
            "依赖的审查或合并已完成才写 todo",
            "Git 元数据须在 Codex 沙箱内可写",
            "执行者标签只放在任务行末尾",
        )))

    def test_codex_exec_argv(self):
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        with mock.patch.object(tick.subprocess, "Popen", return_value=proc) as popen:
            tick.codex_exec("p", self.d / "c.jsonl", Path(r"C:\orghome\project files\U"), "max")
        argv = popen.call_args[0][0]
        self.assertEqual(argv[:3], tick.CODEX)
        for pair in (["-m", "gpt-6-luna"], ["-c", 'model_reasoning_effort="max"'], ["-s", "workspace-write"],
                     ["-C", "/mnt\x2fc/orghome/project files/U"], ["--add-dir", "/mnt\x2fc/orghome/wt"], ["--add-dir", "/home\x2fwsluser/.cache/codex-sandbox"]):
            self.assertIn(pair, [argv[i:i + 2] for i in range(len(argv) - 1)])
        self.assertEqual(argv[-1], "-")  # the prompt goes in on stdin
        self.assertEqual(proc.communicate.call_args[0][0], "p".encode("utf-8"))

    def test_codex_exec_gpu_drops_the_sandbox(self):
        """The sandbox blocks the GPU; only a 〔显卡〕 line runs without it (owner\x2011-01)."""
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        for gpu in (False, True):
            with mock.patch.object(tick.subprocess, "Popen", return_value=proc) as popen:
                tick.codex_exec("p", self.d / "c.jsonl", Path(r"C:\orghome\U"), "max", gpu=gpu)
            pairs = [popen.call_args[0][0][i:i + 2] for i in range(len(popen.call_args[0][0]) - 1)]
            self.assertEqual(["-s", "danger-full-access"] in pairs, gpu)
            self.assertEqual(["-s", "workspace-write"] in pairs, not gpu)

    def test_run_codex_passes_gpu_from_the_tag(self):
        seen, prompts = [], []

        def fake_exec(prompt, run_file, *_args, **kwargs):
            run_file.parent.mkdir(parents=True, exist_ok=True)
            run_file.write_text("", encoding="utf-8")
            prompts.append(prompt)
            seen.append(kwargs.get("gpu"))
            return 1

        with mock.patch.object(tick, "codex_exec", side_effect=fake_exec), \
                mock.patch.object(tick, "codex_result", return_value=(None, False, "", {})), \
                mock.patch.object(tick, "TICKS_DIR", self.d), mock.patch.object(tick, "RUNS_FILE", self.d / "runs.jsonl"):
            for item in ("写作模型：生成 〔显卡〕", "写作模型：普通", "写作模型：引用「另一行〔显卡〕」",
                         "写作模型：引用「〔显卡〕」〔显卡〕", "写作模型：生成〔显卡〕「引用〔显卡〕」"):
                tick.run_codex("life", "生活线", item, "writing", ("gpt-6.1-sol", ("high",), 1, False))
        self.assertEqual(seen, [True, False, False, True, True])
        self.assertIn("python3 control/llama_wsl.py start|bench|stop", prompts[0])
        self.assertIn("用完必须 stop", prompts[0])
        self.assertNotIn("llama_wsl.py", prompts[1])

    def test_codex_exec_makes_the_projects_git_dir_writable(self):
        """workspace-write mounts .git read-only (`git switch -c` failed); a worktree's git dir is in the main repo."""
        main, wt = self.d / "main", self.d / "wt"
        (main / ".git" / "worktrees" / "w").mkdir(parents=True)
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'w'}\n", encoding="utf-8")
        (main / "sub").mkdir()  # a project dir inside a repo, like incore/input
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        inherited_git = tick.git_common_dir(self.d)  # sandbox temp roots may themselves be inside a repository
        for cwd, git in ((main, main / ".git"), (wt, main / ".git"), (main / "sub", main / ".git"), (self.d, inherited_git)):
            with mock.patch.object(tick.subprocess, "Popen", return_value=proc) as popen:
                tick.codex_exec("p", self.d / "c.jsonl", cwd, "max")
            argv = popen.call_args[0][0]
            dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
            self.assertEqual(tick.wsl_path(git) in dirs, git is not None)
            self.assertEqual(len(dirs), 3 + (git is not None))  # audits, wt, the sandbox dir

    def test_codex_exec_adds_the_projects_other_folders_and_their_git_dirs(self):
        """writing = style_rewriter + projects/self-media-lab (one project, one session)."""
        lab = tick.WORKSPACE / "projects" / "self-media-lab"
        self.assertIn(lab, tick.PROJECT_ADD_DIRS["writing"])
        cwd, other = self.d / "writer", self.d / "lab"
        for d in (cwd, other):
            (d / ".git").mkdir(parents=True)
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        with mock.patch.object(tick, "PROJECTS", {**tick.PROJECTS, "writing": (cwd, ("写作模型",))}), \
                mock.patch.dict(tick.PROJECT_ADD_DIRS, {"writing": (other,)}), \
                mock.patch.object(tick.subprocess, "Popen", return_value=proc) as popen:
            tick.run_codex("life", "生活线", "写作模型：x（修补类）", "writing", ("gpt-6-luna", ("xhigh",), 20, True))
        argv = popen.call_args[0][0]
        dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        for d in (other, other / ".git", cwd / ".git"):
            self.assertIn(tick.wsl_path(d), dirs)
        self.assertEqual(len(dirs), len(set(dirs)))


OWNER_LINE = "- [todo] 【Discord·工作副总理】改一下 〔msg:55 role:work〕"
OWNER_BACKLOG = "\n".join(["# h", "", "## 你的指令（最优先）", OWNER_LINE, "", "## 线", "- [review] the review",
                           "- [todo] plain todo", ""])


def stream(result="OK text", is_error=False, worked=True, **extra):
    lines = [json.dumps({"type": "assistant"})] if worked else []
    lines.append(json.dumps({"type": "result", "is_error": is_error, "result": result, **extra}))
    return "\n".join(lines)


class Owner1025Constants(unittest.TestCase):
    """owner\x2010-25 (records\x2f2026-10-25-owner-terminal-efficiency-codex-dotz.json): more Codex, lower effort. Live values, unpatched."""

    def test_codex_more_effort_down_constants(self):
        # owner\x2011-01 terminal: 20 Codex slots in every mode (records\x2f2026-11-01-owner-terminal-codex-slots-20-now.json)
        # owner\x2011-04 terminal: 10, and no codex-plan runs (records\x2f2026-11-04-owner-terminal-codex-slots-10-no-plan.json)
        self.assertEqual((tick.CODEX_SLOTS, tick.CODEX_PARALLEL_SLOTS, tick.CODEX_BURN_SLOTS, tick.CODEX_LOCKS), (10, 10, 10, 10))
        self.assertFalse(tick.CODEX_PLAN_ON)
        self.assertEqual([(m, cap) for m, _, cap, _ in tick.CODEX_TIERS], [("gpt-6-luna", 40), ("gpt-6.1-sol", 200)])
        self.assertEqual(tick.CODEX_TIERS[1][1], ("xhigh", "high"))
        self.assertEqual(tick.DEPUTY_EFFORTS, {"work": "medium", "uvox": "medium", "life": "xhigh", "intelligence": "high", "coordinator": "high"})
        self.assertEqual([tick.deputy_effort(r, "review") for r in ("quant", "life", "coordinator")], ["xhigh", "xhigh", "high"])  # quant not lowered; life xhigh (owner\x2011-06)
        self.assertEqual(tick.EXECUTOR_EFFORTS, ["medium"])
        self.assertEqual(tick.COORDINATOR_COMPACT_TOKENS, 300_000)
        self.assertEqual((tick.shadow_review.DAILY_CAP, tick.shadow_review.SPOT_EVERY, tick.shadow_review.SPOT_STREAK), (200, 5, 2))
        self.assertEqual((tick.CODEX_RESETS, tick.CODEX_WEEKLY_MAX, tick.CODEX_PROBE_S),
                         (("2026-11-30 15:00", "2026-12-01 17:33"), 95.0, 300))
        self.assertEqual((tick.deputy_effort("life", "lessons"), tick.deputy_effort("coordinator", "lessons")), ("medium", "medium"))  # lessons keep EFFORTS


class CodexQuota(TickCase):
    """Owner 11-01: reset reminders use the current weekly window and the earliest card's exact expiry."""

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(tick, "codex_report_note", REAL_CODEX_REPORT_NOTE),
                  mock.patch.object(tick, "codex_weekly_used", REAL_CODEX_WEEKLY_USED),
                  mock.patch.dict(tick.CODEX_PROBE, at=-1e9, used=None)):
            p.start()
            self.addCleanup(p.stop)

    def probe(self, obs):
        """Patch the session probe to return obs (or raise it)."""
        return mock.patch.object(tick.shadow_review, "observe_quota", side_effect=obs if isinstance(obs, Exception) else None,
                                 return_value=None if isinstance(obs, Exception) else obs)

    def note(self, obs, day=datetime(2026, 10, 26, 9, 0)):
        tick.CODEX_PROBE.update(at=-1e9, used=None)
        with self.probe(obs):
            return tick.codex_report_note(day)

    def obs(self, pct, age_s=0, resets_in=86400):
        at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        return {"at": at.isoformat(), "limits": {"secondary": {"used_percent": pct, "window_minutes": 10080,
                                                               "resets_at": time.time() + resets_in}}}

    def test_note_text_threshold_and_unreadable(self):
        now = datetime(2026, 11, 2, 9, 0).astimezone()

        def observation(used, hours):
            return {"limits": {"secondary": {"used_percent": used, "window_minutes": 10080,
                                             "resets_at": now.timestamp() + hours * 3600}}}

        self.assertIn("Codex 周额度 95%，离自然重置 2.1 天，建议现在按 11-30 到期那张，净得约 24.7 点",
                      self.note(observation(95, 50), now))
        self.assertIn("Codex 周额度 100%", self.note(observation(100, 50), now))
        self.assertEqual(self.note(observation(95, 20), now), "")
        self.assertEqual(self.note(observation(94.9, 50), now), "")
        for unreadable in ({}, observation(96, -1), RuntimeError("wsl down")):
            self.assertEqual(self.note(unreadable, now), "")
        with self.probe({}) as probe:
            self.assertEqual(tick.codex_report_note(datetime(2026, 12, 1, 17, 33)), "")
            probe.assert_not_called()

    def test_expiring_card_negative_net_says_not_to_press(self):
        now = datetime(2026, 11, 30, 9, 0).astimezone()
        observation = {"limits": {"secondary": {"used_percent": 70, "window_minutes": 10080,
                                                "resets_at": now.timestamp() + 20 * 3600}}}
        self.assertIn("不按，让它过期（11-30 到期那张", self.note(observation, now))

    def test_gate_holds_new_codex_runs_at_95_unreadable_runs_and_the_probe_is_cached(self):
        text = "## Arbitros 线\n权重：40%\n- [todo] Uvox：修 x（修补类）\n"
        (self.d / "backlog.md").write_text(text, encoding="utf-8")
        p = mock.patch.object(tick, "CODEX_TIERS", TIERS)
        p.start()
        self.addCleanup(p.stop)
        fresh = lambda: ({"sample": None, "spawned": {}, "aux": {}}, tick.CODEX_PROBE.update(at=-1e9, used=None))[0]
        with self.probe(self.obs(96)) as probe:  # a day-old reading is fine; at 95 or more nothing new starts
            st = fresh()
            tick.codex_fallback(st, time.time(), text, set())
            tick.codex_fallback(st, time.time() + 100, text, set())
            self.assertEqual((self.spawned, probe.call_count), ([], 1))  # one probe inside CODEX_PROBE_S
            self.assertEqual([x["decision"] for x in self.logs()], ["codex_quota_full"])  # logged once, not every loop
            tick.CODEX_PROBE.update(at=-1e9)
            with mock.patch.object(tick, "codex_exec") as ex:
                self.assertEqual(tick.main(["codex", "work", "uvox"]), 0)  # the lane itself holds too
            ex.assert_not_called()
            self.assertIn("- [todo] Uvox：修 x", self.backlog())
        for obs in (self.obs(94.9), {}, RuntimeError("wsl down")):  # below the line, or unreadable: runs as before
            self.spawned.clear()
            with self.probe(obs):
                tick.codex_fallback(fresh(), time.time(), text, set())
            self.assertEqual(self.spawned, [["codex", "work", "uvox"]], obs)

    def test_both_report_slots_put_the_note_on_the_first_fact_line_for_coordinator_only(self):
        prompts = {}

        def fake(role, kind, prompt, *a, **k):
            prompts[role] = prompt
            return 0, Path("x"), True, "text", 0

        with mock.patch.object(tick, "codex_report_note", return_value="NOTE。"), \
                mock.patch.object(tick, "REPORT_SLOTS", ("09:00", "21:00")), \
                mock.patch.object(tick, "run_resident", side_effect=fake), \
                mock.patch.object(tick.review_stats, "day_facts", return_value="【今日数据】\n"):
            for slot in tick.REPORT_SLOTS:
                for role in tick.ROLE_NAMES:
                    tick.write_report(role, datetime(2026, 11, 2, int(slot[:2]), 0), slot)
                self.assertTrue(prompts["coordinator"].startswith(tick.resident_prefix("coordinator") + "NOTE。\n【今日数据】"))
                for role in set(tick.ROLE_NAMES) - {"coordinator"}:
                    self.assertNotIn("NOTE", prompts[role])


class MachineWatchStep(TickCase):
    def test_supervisor_samples_through_the_hook_and_a_failure_is_only_logged(self):
        with mock.patch.object(tick.machine_watch, "step") as step, mock.patch.object(tick, "MACHINE_HEALTH", self.d / "m.jsonl"):
            st = {}
            REAL_MACHINE_WATCH_STEP(st, 123.0)
            step.assert_called_once_with(st["machine"], 123.0, self.d / "m.jsonl")
            step.side_effect = OSError("disk")
            REAL_MACHINE_WATCH_STEP(st, 124.0)  # must not raise
        self.assertIn("machine_watch_failed", [x["decision"] for x in self.logs()])

    def test_supervise_step_calls_the_machine_hook(self):
        self.supervise_calls = []
        self.mocks["machine_watch_step"].side_effect = lambda st, t: self.supervise_calls.append(t)
        with mock.patch.object(tick, "fresh_usage", return_value=None) as fresh, \
                mock.patch.object(tick, "latest_usage", return_value=None), \
                mock.patch.object(tick, "sample_usage") as sample:  # hook test: no real Windows quota CLI
            tick.supervise_step({"sample": None, "spawned": {}, "aux": {}}, 500.0)
        fresh.assert_called_once_with()
        sample.assert_not_called()
        self.assertEqual(self.supervise_calls, [500.0])


class OwnerPost(TickCase):
    def test_post_discord_prefix_uses_sender_when_given(self):
        with mock.patch.object(tick.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            tick.post_discord("life", "x", sender="总理")
            tick.post_discord("life", "y")
        sent = [c.args[0][c.args[0].index("--message") + 1] for c in run.call_args_list]
        self.assertEqual(sent, ["[总理] x", "[生活副总理] y"])

    def test_post_discord_chunks_and_argv(self):
        with mock.patch.object(tick.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(tick.post_discord("life", "字" * 4000))
        sent = [c.args[0] for c in run.call_args_list]
        self.assertEqual(len(sent), 3)
        for argv in sent:
            self.assertEqual(argv[2:5], ["send", "--channel", "life"])
            self.assertEqual(argv[-2], "--message")
            self.assertLessEqual(len(argv[-1]), 1900)
            self.assertTrue(argv[-1].startswith("[生活副总理] "))
        self.assertEqual("".join(a[-1][len("[生活副总理] "):] for a in sent), "字" * 4000)
        self.assertEqual(Path(sent[0][0]).parts[-4:], ("discord", ".venv", "Scripts", "python.exe"))
        self.assertEqual(Path(sent[0][1]).parts[-2:], ("discord", "gateway.py"))

    def test_reply_flag_and_hold_timeout(self):
        with mock.patch.object(tick.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            tick.post_discord("work", "x")
            tick.post_discord("work", "x", reply=True)
            tick.post_discord("quant", "x", hold=False)
        (notice, nk), (reply, rk), (nohold, hk) = [(c.args[0], c.kwargs) for c in run.call_args_list]
        self.assertNotIn("--reply", notice)
        self.assertEqual(notice[notice.index("--line") + 1], "Arbitros 线")
        self.assertNotIn("--no-hold", notice)
        self.assertGreater(nk["timeout"], 15 * 60)  # outlasts the gateway's hold
        self.assertIn("--reply", reply)
        self.assertNotIn("--line", reply)
        self.assertEqual(rk["timeout"], 120)
        self.assertEqual(nohold[nohold.index("--line") + 1], "量化线")  # the supervisor loop: tagged, never held
        self.assertIn("--no-hold", nohold)
        self.assertEqual(hk["timeout"], 120)

    def test_failed_send_returns_false(self):
        with mock.patch.object(tick.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(tick.post_discord("work", "x"))

    def test_attachment_lines_become_files_on_last_chunk(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            ok, cred, big = root / "报告.pdf", root / "my_token.txt", root / "big.bin"
            ok.write_text("x"); cred.write_text("x")
            with big.open("wb") as h:
                h.truncate(tick.MAX_UPLOAD_MB * 1024 * 1024 + 1)
            text = f"结果如下\n附件：{ok}\n附件: {cred}\n附件：{big}\n附件：{root / 'none.txt'}\n附件：{Path(d).anchor}Windows\\win.ini\n" + "字" * 2500
            with mock.patch.object(tick, "ATTACH_ROOTS", (root,)), \
                    mock.patch.object(tick.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
                self.assertTrue(tick.post_discord("work", text))
        sent = [c.args[0] for c in run.call_args_list]
        self.assertEqual(len(sent), 2)
        self.assertNotIn("--file", sent[0])
        self.assertEqual(sent[1][-2:], ["--file", str(ok)])
        self.assertEqual(sent[1].count("--file"), 1)
        body = sent[0][sent[0].index("--message") + 1] + sent[1][sent[1].index("--message") + 1]
        self.assertNotIn("附件：", body.replace("没有发送", ""))
        for reason in ("疑似凭据文件", "超过 10 MB", "文件不存在", "不在允许的目录内"):
            self.assertIn(reason, body)

    def test_attachment_only_message_still_posts(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d).resolve() / "a.txt"
            f.write_text("x")
            with mock.patch.object(tick, "ATTACH_ROOTS", (f.parent,)), \
                    mock.patch.object(tick.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
                self.assertTrue(tick.post_discord("life", f"附件：{f}"))
        self.assertEqual(run.call_args.args[0][-3:], [tick.ROLE_NAMES["life"].join(["[", "] "]), "--file", str(f)])


class OwnerLane(TickCase):
    def setUp(self):
        super().setUp()
        (self.d / "backlog.md").write_text(OWNER_BACKLOG, encoding="utf-8")
        self.calls, self.posts, self.efforts, self.replies = [], [], [], []
        self.outcomes = []  # (exit_code, stream_text) consumed per run_claude call
        p = mock.patch.object(tick, "post_discord", side_effect=lambda r, m, reply=False: self.posts.append((r, m)) or self.replies.append(reply) or True)
        p.start()
        self.addCleanup(p.stop)

    def fake(self, kind, prompt, run_file, extra=(), effort=None, model=None, compact_at=None, role=None):
        self.calls.append((kind, prompt, list(extra)))
        self.efforts.append(effort)
        run_file.parent.mkdir(parents=True, exist_ok=True)
        code, text = self.outcomes.pop(0) if self.outcomes else (0, stream())
        run_file.write_text(text, encoding="utf-8")
        self.seen_running = self.backlog(), json.loads((self.d / "deputies.json").read_text(encoding="utf-8"))
        return code

    def run_main(self, pct=75, frac=0.0, argv=("owner",)):
        with self.sample(pct, frac), mock.patch.object(tick, "run_claude", side_effect=self.fake):
            return tick.main(list(argv))

    def deputies(self):
        return json.loads((self.d / "deputies.json").read_text(encoding="utf-8"))

    def test_owner_ignores_70_pacing(self):
        self.run_main(85, 0.0, argv=["owner"])  # 85 % is above the 80 % pacing line, below the ceiling
        self.assertEqual([c[0] for c in self.calls], ["owner"])
        self.assertEqual([x["decision"] for x in self.logs()], ["owner_ran"])
        self.assertEqual(self.efforts, ["medium"])  # owner\x2010-04: the work deputy thinks at medium
        self.assertEqual(self.replies, [True])  # the answer to the owner is a reply: the gateway never holds it

    def test_owner_run_in_ledger(self):
        self.run_main(10, 0.5, argv=["owner"])
        rec = json.loads((self.d / "runs.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((rec["kind"], rec["role"], rec["model"], rec["effort"], rec["session"]),
                         ("owner", "work", "opus", "medium", "new"))

    def test_owner_only_argv_skips_normal_lane(self):
        self.run_main(10, 0.5, argv=["owner"])
        self.assertEqual([c[0] for c in self.calls], ["owner"])

    def test_hard_ceiling_leaves_item_and_notifies_once(self):
        self.run_main(95, 0.5, argv=["owner"])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.replies, [True])
        self.assertEqual(self.posts[0][0], "work")
        self.assertRegex(self.posts[0][1], r"额度已到 90% 硬线，\d\d:\d\d（America/New_York）后处理。")
        self.assertIn(OWNER_LINE + "〔已通知额度〕", self.backlog())
        self.run_main(95, 0.5, argv=["owner"])
        self.assertEqual(len(self.posts), 1)

    def test_new_session_then_resume_and_deputies_json(self):
        self.run_main(10, 0.5, argv=["owner"])
        kind, prompt, extra = self.calls[0]
        self.assertEqual(extra[0], "--session-id")
        self.assertIn("你是Arbitros 副总理（常驻副总理）", prompt)
        self.assertIn("频道直接给你的指令：改一下。", prompt)
        running_text, running_dep = self.seen_running
        self.assertRegex(running_text, r"- \[running \d\d:\d\d\] 【Discord·工作副总理】")
        self.assertEqual(running_dep["work"]["status"], "running")
        self.assertEqual(running_dep["work"]["current"]["message_id"], "55")
        dep = self.deputies()["work"]
        self.assertEqual((dep["session_id"], dep["status"], dep["current"], dep["runs"]), (extra[1], "idle", None, 1))
        self.assertEqual((dep["last"]["message_id"], dep["last"]["ok"], dep["last"]["summary"]), ("55", True, "OK text"))
        # second message resumes the same session
        (self.d / "backlog.md").write_text(OWNER_BACKLOG.replace("55", "56"), encoding="utf-8")
        self.run_main(10, 0.5, argv=["owner"])
        self.assertEqual(self.calls[1][2], ["--resume", extra[1]])
        self.assertEqual(self.deputies()["work"]["runs"], 2)

    def test_missing_session_retries_once_with_new_id(self):
        (self.d / "deputies.json").write_text(json.dumps({"work": {"session_id": "old", "created_at": "x",
                                              "status": "idle", "current": None, "last": None, "runs": 4}}))
        missing = json.dumps({"type": "stderr", "message": "No conversation found for session ID: old"})
        self.outcomes = [(1, missing + "\n" + stream("failure", True, worked=False)), (0, stream("fine"))]
        self.run_main(10, 0.5, argv=["owner"])
        self.assertEqual(self.calls[0][2], ["--resume", "old"])
        self.assertEqual(self.calls[1][2][0], "--session-id")
        dep = self.deputies()["work"]
        self.assertEqual((dep["session_id"], dep["runs"]), (self.calls[1][2][1], 5))
        self.assertNotEqual(dep["session_id"], "old")
        self.assertIn("- [done", self.backlog())

    def test_failed_resume_after_work_is_not_retried(self):
        (self.d / "deputies.json").write_text(json.dumps({"work": {"session_id": "old", "runs": 0}}))
        self.outcomes = [(1, stream("boom", True))]
        self.run_main(10, 0.5, argv=["owner"])
        self.assertEqual(len(self.calls), 1)
        self.assertIn("- [blocked: 执行失败", self.backlog())

    def test_done_rewrite_and_result_posted(self):
        self.outcomes = [(0, stream("最终回复"))]
        self.run_main(10, 0.5, argv=["owner"])
        self.assertEqual(self.posts, [("work", "最终回复")])
        self.assertRegex(self.backlog(), r"- \[done \d\d:\d\d\] 【Discord·工作副总理】改一下 〔msg:55 role:work〕")
        self.assertNotIn("[todo] 【Discord", self.backlog())
        self.assertEqual(self.logs()[0]["decision"], "owner_ran")

    def test_failure_and_timeout_blocked_with_note(self):
        self.outcomes = [(-1, "")]
        self.run_main(10, 0.5, argv=["owner"])
        self.assertRegex(self.backlog(), r"- \[blocked: 执行失败 \d\d:\d\d，见 ticks/\S+-owner-work\.jsonl\] 【Discord")
        self.assertIn("执行失败（超时）", self.posts[0][1])
        self.assertFalse(self.deputies()["work"]["last"]["ok"])

    def test_items_processed_one_at_a_time_fifo(self):
        two = OWNER_BACKLOG.replace(OWNER_LINE, OWNER_LINE + "\n" + OWNER_LINE.replace("55", "56").replace("改一下", "第二条"))
        (self.d / "backlog.md").write_text(two, encoding="utf-8")
        self.run_main(10, 0.5, argv=["owner"])
        self.assertEqual(len(self.calls), 2)
        self.assertIn("改一下", self.calls[0][1])
        self.assertIn("第二条", self.calls[1][1])

    def test_unknown_role_blocked_without_run(self):
        (self.d / "backlog.md").write_text("- [todo] 【Discord·x】hi 〔msg:1 role:nobody〕\n", encoding="utf-8")
        self.run_main(10, 0.5, argv=["owner"])
        self.assertEqual(self.calls, [])
        self.assertIn("- [blocked: 无法识别角色", self.backlog())

    def test_owner_lock_independent_of_tick_lock(self):
        (self.d / "tick.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.run_main(10, 0.5, argv=["owner"])
        self.assertEqual([c[0] for c in self.calls], ["owner"])
        self.assertEqual((self.d / "tick.lock").read_text(), "4242")
        self.assertFalse((self.d / "owner.lock").exists())

    def test_owner_lock_busy(self):
        (self.d / "owner.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.run_main(10, 0.5, argv=["owner"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.logs()[0]["decision"], "owner_busy")

    def test_pause_file_blocks_owner_lane(self):
        (self.d / "tick.PAUSE").write_text("")
        self.run_main(10, 0.5, argv=["owner"])
        self.assertEqual(self.calls, [])

    def test_eastern_time(self):
        self.assertEqual(tick.eastern_hhmm(1790769600), "08:00")  # 2026-09-30 12:00Z, EDT
        self.assertEqual(tick.eastern_hhmm(1768478400), "07:00")  # 2026-01-15 12:00Z, EST


ASK_ROW = "- [blocked: 等总理] 量化：实盘自动下单授权范围。选项 A/B，我建议 A，见 audits/x.md"
ASK_BACKLOG = "\n".join(["# h", "", "## 量化线", "- [todo] 量化：t", ASK_ROW,
                         "## Arbitros 线", "- [blocked: 等总理] Uvox：要不要先发 v2", ""])


class AskLane(TickCase):
    def setUp(self):
        super().setUp()
        (self.d / "backlog.md").write_text(ASK_BACKLOG, encoding="utf-8")
        self.calls, self.answer = [], None  # answer(backlog_text) -> new text: what the coordinator does to the backlog
        self.posts = []
        p = mock.patch.object(tick, "post_discord",
                              side_effect=lambda role, message, **kw: self.posts.append((role, message, kw)) or True)
        p.start()
        self.addCleanup(p.stop)

    def answered(self, text):
        return (text.replace(ASK_ROW, ASK_ROW.replace("[blocked: 等总理]", "[done 10-31 14:50：总理答 可定，建议先做A。]"))
                .replace("- [blocked: 等总理] Uvox：要不要先发 v2",
                         "- [blocked: 等你决定] Uvox：要不要先发 v2（总理建议：先补本地预览，owner 看过再发）"))

    def fake(self, kind, prompt, run_file, extra=(), effort=None, model=None, compact_at=None, role=None):
        self.calls.append((kind, prompt, list(extra), model, effort))
        run_file.parent.mkdir(parents=True, exist_ok=True)
        code = getattr(self, "code", 0)
        run_file.write_text(stream("总理答复" if code == 0 else "boom", code != 0), encoding="utf-8")
        if self.answer:
            (self.d / "backlog.md").write_text(self.answer(self.backlog()), encoding="utf-8")
        return code

    def go(self, pct=10, frac=0.5):
        with self.sample(pct, frac), mock.patch.object(tick, "run_claude", side_effect=self.fake):
            return tick.main(["ask"])

    def freeze_tick_time(self, hour, minute=15):
        now = datetime.now().astimezone().replace(hour=hour, minute=minute, second=0, microsecond=0)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(now.timestamp(), tz)

        return now, mock.patch.object(tick, "datetime", FrozenDateTime)

    def state(self):
        return json.loads((self.d / "ask-state.json").read_text(encoding="utf-8")) if (self.d / "ask-state.json").exists() else {}

    def test_one_coordinator_run_answers_every_ask_row(self):
        self.go()
        self.assertEqual(len(self.calls), 1)
        kind, prompt, extra, model, effort = self.calls[0]
        self.assertEqual((kind, model, effort), ("ask", "opus", "high"))  # owner\x2010-25: coordinator at high
        self.assertIn("你是总理", prompt)
        self.assertIn("2 条问题", prompt)
        self.assertIn("1. （量化线）" + ASK_ROW, prompt)
        self.assertIn("2. （Arbitros 线）- [blocked: 等总理] Uvox：要不要先发 v2", prompt)
        for want in ("[done ", "[blocked: 等你决定]", "coordinator-log.md", "Discord"):
            self.assertIn(want, prompt)
        rec = json.loads((self.d / "runs.jsonl").read_text(encoding="utf-8"))  # the coordinator's cost, in the 组织线
        self.assertEqual((rec["kind"], rec["role"], rec["section"], rec["model"]), ("ask", "coordinator", "组织线", "opus"))
        self.assertEqual(self.logs()[-1]["decision"], "ask_ran")
        self.assertFalse((self.d / "ask.lock").exists() or (self.d / "deputy-coordinator.lock").exists())

    def test_it_ignores_the_pacing_line_but_not_the_hard_ceiling(self):
        self.go(85, 0.0)  # over the 80 % pacing line, under the ceiling
        self.assertEqual(len(self.calls), 1)
        self.calls.clear()
        (self.d / "ask-state.json").unlink()
        self.go(95, 0.5)  # HARD_CEILING is 90 in these tests
        self.assertEqual(self.calls, [])
        self.assertEqual(self.logs()[-1]["decision"], "hard_ceiling")
        self.assertEqual(self.state(), {})  # nothing was tried

    def test_an_answered_row_is_not_asked_again(self):
        self.answer = lambda t: t.replace(ASK_ROW, ASK_ROW.replace("[blocked: 等总理]", "[done 10-05 20:00：总理答 A]")) \
            .replace("- [blocked: 等总理] Uvox", "- [blocked: 等你决定] Uvox")
        self.go()
        self.assertEqual(self.state(), {})
        self.assertEqual(tick.ask_pending(self.backlog(), time.time() + 10 * 3600), [])
        self.go()
        self.assertEqual(len(self.calls), 1)  # the second lane run found nothing to do

    def test_new_deputy_question_is_relayed_to_coordinator_once(self):
        before = "## 生活线\n- [todo] 生活：先做准备\n"
        item = "写作模型：" + "问" * 205
        after = before + f"- [blocked: 等总理] {item}\n## Arbitros 线\n- [blocked: 等总理] Umba：别线的问题\n"
        (self.d / "backlog.md").write_text(before, encoding="utf-8")

        def fake_session_run(*_args, **_kwargs):
            run_file = self.d / "deputy-run.jsonl"
            run_file.write_text("{}\n", encoding="utf-8")
            (self.d / "backlog.md").write_text(after, encoding="utf-8")
            return 0, run_file, "session", False

        with mock.patch.object(tick, "owner_words_block", return_value=""), \
                mock.patch.object(tick.pending, "replies_block", return_value=""), \
                mock.patch.object(tick, "session_run", side_effect=fake_session_run), \
                mock.patch.object(tick, "final_result", return_value=(True, "done", {})), \
                mock.patch.object(tick, "record_run", return_value={}), \
                mock.patch.object(tick, "maybe_compact", return_value=None), \
                mock.patch.object(tick, "local_datetime", return_value=datetime(2026, 11, 1, 10, tzinfo=timezone.utc)):
            tick.run_resident("life", "review", "prompt", {}, {})
            self.assertIn(["ask-notice"], self.spawned)
            tick.main(["ask-notice"])
            tick.main(["ask-notice"])

        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][0], "coordinator")
        self.assertEqual(self.posts[0][1], "【转总理】写作模型：" + "问" * 200 + "…")
        self.assertEqual(self.posts[0][2], {"hold": False})

    def test_answer_notices_are_batched_to_the_owning_line_channels(self):
        _, clock = self.freeze_tick_time(10)
        long_item = ASK_ROW.replace("授权范围", "问" * 50)
        (self.d / "backlog.md").write_text(ASK_BACKLOG.replace(ASK_ROW, long_item) +
                                               "\n## 生活线\n- [blocked: 等总理] 生活：这行本次没有答复\n",
                                               encoding="utf-8")
        long_answer = "答" * 320
        self.answer = lambda text: (text.replace(long_item, long_item.replace(
            "[blocked: 等总理]", f"[done 10-31 14:50：总理答 {long_answer}]"))
            .replace("- [blocked: 等总理] Uvox：要不要先发 v2",
                     "- [blocked: 等你决定] Uvox：要不要先发 v2（总理建议：先补本地预览，owner 看过再发）"))
        with clock:
            self.go()
            self.assertIn(["ask-notice"], self.spawned)
            tick.main(["ask-notice"])
        self.assertEqual([role for role, _, _ in self.posts], ["quant", "work"])
        self.assertEqual(len(self.posts), 2)  # one combined post per affected channel
        quant_message = self.posts[0][1]
        self.assertTrue(quant_message.startswith("【总理答复】"))
        self.assertIn("量化｜问题：" + ("实盘自动下单" + "问" * 50)[:40] + "…", quant_message)
        self.assertIn("答复：" + "答" * 300 + "…", quant_message)
        self.assertIn("projects/backlog.md 第 5 行", quant_message)
        self.assertIn("projects/backlog.md 第 7 行", self.posts[1][1])
        self.assertTrue(all(call[2] == {"hold": False, "sender": "总理"} for call in self.posts))  # answers read as the coordinator's

    def test_recent_unanswered_owner_message_delays_only_its_channel_and_deduplicates(self):
        now, clock = self.freeze_tick_time(10)
        db_path = tick.DISCORD_DB
        received = now.astimezone(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as db:
            db.executescript("CREATE TABLE inbound(message_id TEXT PRIMARY KEY, received_at TEXT, role TEXT);"
                             "CREATE TABLE direct_requests(message_id TEXT PRIMARY KEY, status TEXT);")
            db.execute("INSERT INTO inbound VALUES('owner-1', ?, 'work')", (received,))
            db.execute("INSERT INTO direct_requests VALUES('owner-1', 'queued')")
            db.commit()
        self.answer = self.answered
        with clock:
            self.go()
            tick.main(["ask-notice"])
            self.assertEqual([role for role, _, _ in self.posts], ["quant"])
            self.assertEqual(len(tick.pending_report_ask_notices("work")), 0)  # collision is a next-tick retry
            with closing(sqlite3.connect(db_path)) as db:
                db.execute("UPDATE direct_requests SET status='answered' WHERE message_id='owner-1'")
                db.commit()
            tick.main(["ask-notice"])
            tick.main(["ask-notice"])
        self.assertEqual([role for role, _, _ in self.posts], ["quant", "work"])

    def test_quiet_hour_answers_are_added_to_the_09_report_and_cleared_after_send(self):
        self.answer = self.answered
        rows = [(section, line) for section, sec in tick.parse_backlog(self.backlog()).items()
                for line, _ in sec["ask"]]
        quiet = datetime(2026, 10, 31, 23, 0)
        tick.queue_ask_answer_notices(rows, self.answered(self.backlog()), quiet)
        tick.send_pending_ask_notices(quiet)
        self.assertEqual(self.posts, [])
        quant_notices = tick.pending_report_ask_notices("quant")
        self.assertEqual(len(quant_notices), 1)
        with mock.patch.object(tick, "REPORT_SLOTS", ("09:00", "21:00")), \
                mock.patch.object(tick, "run_resident", return_value=(0, self.d / "report.jsonl", True,
                                                                        "早报正文", 1)), \
                mock.patch.object(tick.review_stats, "day_facts", return_value=""):
            text, reason = tick.write_report("quant", datetime(2026, 10, 31, 9, 0), "09:00", quant_notices)
        self.assertEqual(reason, "")
        self.assertIn("【总理答复（夜间延后）】", text)
        self.assertIn("可定，建议先做A", text)
        tick.acknowledge_report_ask_notices([n["fingerprint"] for n in quant_notices])
        self.assertEqual(tick.pending_report_ask_notices("quant"), [])

    def test_quiet_deputy_question_is_added_to_the_coordinator_morning_report(self):
        before = "## 生活线\n- [todo] 生活：先做准备\n"
        after = before + "- [blocked: 等总理] 生活：诗词提案需要总理建议\n"
        quiet = datetime(2026, 10, 31, 2, 0)
        tick.queue_deputy_question_notices("life", before, after, quiet)
        notices = tick.pending_report_ask_notices("coordinator")
        self.assertEqual(len(notices), 1)
        self.assertEqual(self.posts, [])
        with mock.patch.object(tick, "REPORT_SLOTS", ("09:00", "21:00")), \
                mock.patch.object(tick, "run_resident", return_value=(0, self.d / "report.jsonl", True,
                                                                        "早报正文", 1)), \
                mock.patch.object(tick.review_stats, "day_facts", return_value=""):
            text, reason = tick.write_report("coordinator", datetime(2026, 10, 31, 9, 0), "09:00", notices)
        self.assertEqual(reason, "")
        self.assertIn("【转总理（夜间延后）】", text)
        self.assertIn("【转总理】生活：诗词提案需要总理建议", text)
        tick.acknowledge_report_ask_notices([n["fingerprint"] for n in notices])
        self.assertEqual(tick.pending_report_ask_notices("coordinator"), [])

    def test_send_failure_is_logged_once_and_does_not_escape(self):
        _, clock = self.freeze_tick_time(10)
        self.answer = self.answered
        with clock, mock.patch.object(tick, "post_discord", side_effect=RuntimeError("offline")) as send:
            self.go()
            tick.main(["ask-notice"])
            tick.main(["ask-notice"])
        self.assertEqual(send.call_count, 2)
        statuses = [entry["status"] for entry in tick.load_ask_notices()["attempted"].values()]
        self.assertEqual(statuses, ["failed", "failed"])
        self.assertEqual(self.logs()[-1]["decision"], "ask_notice_failed")

    def test_an_unanswered_row_waits_then_is_tried_once_more_then_goes_to_the_owner(self):
        self.go()
        self.assertEqual([x["tries"] for x in self.state().values()], [1, 1])
        t = time.time()
        self.assertEqual(tick.ask_pending(self.backlog(), t), [])  # backoff
        self.assertEqual(len(tick.ask_pending(self.backlog(), t + tick.FAIL_BACKOFF_S + 5)), 2)
        with mock.patch.object(tick.time, "time", return_value=t + tick.FAIL_BACKOFF_S + 5):
            self.go()
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("等总理", self.backlog())
        self.assertIn("- [blocked: 等你决定] 量化：实盘自动下单授权范围。选项 A/B，我建议 A，见 audits/x.md（总理 2 次没答复，见 ticks/", self.backlog())
        self.assertEqual(self.state(), {})
        self.go()
        self.assertEqual(len(self.calls), 2)

    def test_a_ceiling_stop_does_not_use_up_a_try(self):
        self.code = tick.CEILING_STOP
        self.go()
        self.assertEqual([x["tries"] for x in self.state().values()], [0, 0])
        self.assertIn("等总理", self.backlog())

    def test_busy_coordinator_or_running_lane_means_no_run(self):
        for lock in ("deputy-coordinator.lock", "ask.lock"):
            (self.d / lock).write_text("4242")
            with mock.patch.object(tick, "pid_alive", return_value=True):
                self.go()
            (self.d / lock).unlink()
        self.assertEqual(self.calls, [])
        self.assertEqual([x["decision"] for x in self.logs()], ["deputy_busy", "ask_busy"])
        (self.d / "deputies.json").write_text(json.dumps({"coordinator": {"current": {"kind": "terminal"}}}))  # owner has it open
        self.go()
        self.assertEqual(self.calls, [])

    def test_no_ask_rows_no_run_and_pause_blocks(self):
        (self.d / "backlog.md").write_text("## 量化线\n- [todo] 量化：t\n", encoding="utf-8")
        self.go()
        (self.d / "backlog.md").write_text(ASK_BACKLOG, encoding="utf-8")
        (self.d / "tick.PAUSE").write_text("")
        self.go()
        self.assertEqual(self.calls, [])


class ReportLane(TickCase):
    """Twice-daily reports (09:00 / 21:00): six deputies back to back, Discord each, one combined email per slot."""
    ROLES = ("coordinator", "work", "uvox", "quant", "intelligence", "life")  # run order and email order

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(tick, "REPORT_SLOTS", ("09:00", "21:00")),
                  mock.patch.object(tick.review_stats, "day_facts", return_value="【今日数据】\n")):  # day_facts would read the real control/data
            p.start()
            self.addCleanup(p.stop)
        (self.d / "deputies.json").write_text(json.dumps({"uvox": {"session_id": "uvox-session", "runs": 0}}), encoding="utf-8")  # forked already
        self.calls, self.posts, self.emails, self.all = [], [], [], []
        self.results = {}  # role -> stream text
        self.email_ok = self.discord_ok = True
        self.now = datetime(2026, 9, 30, 9, 5)
        for name, fn in (("report_now", lambda: self.now),
                         ("post_discord", lambda r, m, **k: self.posts.append((r, m)) or self.discord_ok),
                         ("send_email", lambda s, b: self.emails.append((s, b)) or self.email_ok)):
            q = mock.patch.object(tick, name, side_effect=fn)
            q.start()
            self.addCleanup(q.stop)

    def role_of(self, prompt):
        return next(r for r, n in tick.ROLE_NAMES.items() if prompt.startswith(f"你是{n}（"))

    def fake(self, kind, prompt, run_file, extra=(), effort=None, model=None, compact_at=None, role=None):
        role = self.role_of(prompt)
        self.all.append(kind)
        if kind == "report":
            self.calls.append((kind, prompt, role))
        run_file.parent.mkdir(parents=True, exist_ok=True)
        run_file.write_text(self.results.get(role, stream(f"{role}完成X")), encoding="utf-8")
        return 0

    def go(self, now=None, pct=10, argv=("report",)):
        self.now = now or self.now
        with self.sample(pct, 0.5), mock.patch.object(tick, "run_claude", side_effect=self.fake):
            return tick.main(list(argv))

    def state(self):
        day = json.loads((self.d / "reports.json").read_text(encoding="utf-8"))["2026-09-30"]
        return {k: v for k, v in day.items() if not k.startswith("text:")}

    def test_report_date_write_stays_out_of_workspace_dates_path(self):
        dates_file = Path(tick.LIFE_DATES_FILE)
        reported_file = Path(tick.LIFE_DATES_REPORTED_FILE)
        self.assertEqual(dates_file.parent, self.d)
        self.assertEqual(reported_file.parent, self.d)
        dates_file.write_text(
            "## 表 1：测试\n"
            "| 项目 | 日期 | 提前 30 天 | 提前 7 天 | 下次核对 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| 临时测试提醒 | 2026-10-07 | 2026-09-07 | 2026-09-30 |  |\n"
            "## 表 2：测试\n"
            "| 步骤 | 日期窗口 | 下次核对 |\n"
            "| --- | --- | --- |\n",
            encoding="utf-8",
        )
        workspace = self.d / "pretend-workspace"
        production_record = workspace / "incore" / "projects" / "life" / "dates-reported.json"
        with mock.patch.object(tick, "WORKSPACE", workspace):
            self.go()
        self.assertTrue(reported_file.exists())
        self.assertEqual(json.loads(reported_file.read_text(encoding="utf-8")), [
            {"table": 1, "name": "临时测试提醒", "reminder_date": "2026-09-07"},
            {"table": 1, "name": "临时测试提醒", "reminder_date": "2026-09-30"},
        ])
        self.assertFalse(production_record.exists())

    def test_09_report_includes_queued_answer_and_marks_it_reported(self):
        rows = [("量化线", ASK_ROW)]
        answered = ASK_BACKLOG.replace(ASK_ROW,
                                       ASK_ROW.replace("[blocked: 等总理]", "[done 10-31 02:00：总理答 先走A。]"))
        tick.queue_ask_answer_notices(rows, answered, datetime(2026, 10, 31, 2, 0))
        (self.d / "deputies.json").write_text(json.dumps({"quant": {"session_id": "quant-session"}}),
                                               encoding="utf-8")
        self.now = datetime(2026, 10, 31, 9, 5)
        with mock.patch.object(tick, "REPORT_ROLES", ("quant",)), \
                mock.patch.object(tick, "fresh_usage", return_value=(10, int(time.time() + 10000))), \
                mock.patch.object(tick, "run_resident", return_value=(0, self.d / "report.jsonl", True,
                                                                       "早报", 1)):
            self.go(argv=("report",))
        text = json.loads((self.d / "reports.json").read_text(encoding="utf-8"))["2026-10-31"]["text:quant@09:00"]
        self.assertIn("【总理答复（夜间延后）】", text)
        self.assertIn("答复：先走A", text)
        self.assertEqual(tick.pending_report_ask_notices("quant"), [])

    def slot_state(self, slot, **over):
        return {**{f"{r}@{slot}": "sent" for r in self.ROLES}, f"email@{slot}": "sent", **over}

    def push(self, on=("lunch",), now=datetime(2026, 9, 30, 11, 31)):
        self.now = now
        with mock.patch.object(tick, "LIFE_PUSHES_ON", on), mock.patch.object(tick, "REPORT_SLOTS", ()):
            self.go()

    def test_life_push_slots_from_config(self):
        with mock.patch.object(tick, "LIFE_PUSHES_ON", ("morning", "lunch", "dinner", "night")):
            self.assertEqual(tick.push_slots(), {"push:morning": "08:30", "push:lunch": "11:30", "push:dinner": "18:00",
                                                 "push:night": "23:00"})
        with mock.patch.object(tick, "LIFE_MEALS", {"lunch": "13:00"}), mock.patch.object(tick, "LIFE_MEAL_LEAD_MIN", 45), \
                mock.patch.object(tick, "LIFE_PUSHES_ON", ("lunch", "night")):
            self.assertEqual(tick.push_slots(), {"push:lunch": "12:15", "push:night": "23:00"})
        self.assertEqual(tick.push_slots(), {})  # TickCase turns the pushes off
        with mock.patch.object(tick, "LIFE_PUSHES_ON", ORIG_LIFE_PUSHES_ON):  # owner\x2010-12: lunch, dinner, night; no 08:30 briefing
            self.assertEqual(tick.push_slots(), {"push:lunch": "11:30", "push:dinner": "18:00", "push:night": "23:00"})

    def test_no_morning_briefing_at_0830(self):
        with mock.patch.object(tick, "LIFE_PUSHES_ON", ORIG_LIFE_PUSHES_ON):
            self.go(datetime(2026, 9, 30, 8, 35))
            self.go(datetime(2026, 9, 30, 8, 55))
        self.assertEqual((self.calls, self.posts, self.emails), ([], [], []))

    def test_life_push_fires_on_time_discord_only(self):
        self.push()
        self.assertEqual(self.state(), {"push:lunch": "sent"})
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][0], "life")
        self.assertTrue(self.posts[0][1].startswith("午饭提示 09-30\n"))
        self.assertEqual(self.emails, [])
        self.assertIn("午饭前 30 分钟", self.calls[0][1])

    def test_life_push_not_due_before_its_time(self):
        self.push(now=datetime(2026, 9, 30, 11, 29))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.posts, [])

    def test_life_push_caught_up_late_is_labelled(self):
        self.push(now=datetime(2026, 9, 30, 12, 5))
        self.assertEqual(self.state(), {"push:lunch": "sent"})
        self.assertTrue(self.posts[0][1].startswith("午饭提示 09-30（补发，原定 11:30）\n"))

    def test_life_push_too_late_is_missed_not_sent(self):
        self.push(now=datetime(2026, 9, 30, 13, 5))
        self.assertEqual(self.state(), {"push:lunch": "missed"})
        self.assertEqual((self.calls, self.posts), ([], []))

    def test_life_push_once_per_day_per_slot(self):
        self.push(on=("lunch", "night"))
        self.push(on=("lunch", "night"), now=datetime(2026, 9, 30, 11, 50))
        self.assertEqual(len(self.posts), 1)
        self.push(on=("lunch", "night"), now=datetime(2026, 9, 30, 23, 10))
        self.assertEqual(self.state(), {"push:lunch": "sent", "push:night": "sent"})
        self.assertEqual([p[1].split(" ")[0] for p in self.posts], ["午饭提示", "收工提醒"])
        self.push(on=("lunch", "night"), now=datetime(2026, 9, 30, 23, 40))
        self.assertEqual(len(self.posts), 2)

    def test_life_push_skip_reply_sends_nothing_and_is_done(self):
        self.results["life"] = stream("skip")
        self.push()
        self.assertEqual((self.state(), self.posts), ({"push:lunch": "skipped"}, []))

    def test_life_push_overlong_reply_is_failed_and_not_posted(self):
        self.results["life"] = stream("收工提醒没有发出去。" + "说明" * tick.PUSH_MAX_CHARS)
        self.push()
        self.assertEqual((self.state(), self.posts), ({"push:lunch": "failed"}, []))
        self.assertIn(("report", "回复过长，疑似不是提醒"), [(x["decision"], x["reason"]) for x in self.logs()])
        self.results["life"] = stream("字" * tick.PUSH_MAX_CHARS)  # the limit itself is still a reminder
        self.push(now=datetime(2026, 9, 30, 11, 50))
        self.assertEqual((self.state(), len(self.posts)), ({"push:lunch": "sent"}, 1))

    def test_push_prompts_tell_the_deputy_not_to_send(self):
        self.assertEqual(set(tick.PUSH_PROMPTS), set(tick.PUSH_NAMES))
        for name, prompt in tick.PUSH_PROMPTS.items():
            self.assertIn("不要自己调用 gateway.py", prompt, name)
            self.assertNotIn("发到他的 Discord", prompt, name)

    def test_life_push_failure_retries_next_tick(self):
        self.results["life"] = stream("")
        self.push()
        self.assertEqual((self.state(), self.posts), ({"push:lunch": "failed"}, []))
        self.results["life"] = stream("吃这个")
        self.push(now=datetime(2026, 9, 30, 11, 50))
        self.assertEqual((self.state(), len(self.posts)), ({"push:lunch": "sent"}, 1))

    def test_pushes_and_reports_share_the_lane_in_time_order(self):
        with mock.patch.object(tick, "LIFE_PUSHES_ON", ("lunch",)):
            self.go(datetime(2026, 9, 30, 11, 31))  # the 09:00 slot catches up first, then the lunch push
        self.assertEqual(self.state(), {**self.slot_state("09:00"), "push:lunch": "sent"})
        self.assertEqual([p[1].split("\n")[0] for p in self.posts],
                         ["日报 09-30 09:00（补发）"] * 6 + ["午饭提示 09-30"])
        self.assertEqual(len(self.emails), 1)

    def test_morning_slot_five_discord_posts_and_one_combined_email(self):
        self.go()
        self.assertEqual(self.state(), self.slot_state("09:00"))
        self.assertEqual([c[2] for c in self.calls], list(self.ROLES))
        self.assertEqual(self.posts, [(r, f"日报 09-30 09:00\n{r}完成X") for r in self.ROLES])
        self.assertEqual(len(self.emails), 1)
        subject, body = self.emails[0]
        self.assertEqual(subject, "日报 09-30 09:00")
        names = [tick.ROLE_NAMES[r] for r in ("coordinator", "work", "uvox", "quant", "intelligence", "life")]  # 总理、Arbitros、Beacon Listener、量化、情报、生活
        self.assertEqual(body, "\n\n".join(f"【{n}】\n{r}完成X" for n, r in zip(names, self.ROLES)))

    def test_both_slots_a_day_ten_posts_two_emails(self):
        self.go()
        self.go(datetime(2026, 9, 30, 21, 5))
        self.assertEqual(self.state(), {**self.slot_state("09:00"), **self.slot_state("21:00")})
        self.assertEqual(len(self.calls), 12)
        self.assertEqual([p[1].split("\n")[0] for p in self.posts], ["日报 09-30 09:00"] * 6 + ["日报 09-30 21:00"] * 6)
        self.assertEqual([e[0] for e in self.emails], ["日报 09-30 09:00", "日报 09-30 21:00"])
        self.assertTrue(all(body.count("【") == 6 for _, body in self.emails))

    def test_not_due_before_the_slot_or_between_slots_once_done(self):
        self.go(datetime(2026, 9, 30, 8, 55))
        self.assertEqual((self.calls, self.posts, self.emails), ([], [], []))
        self.go(datetime(2026, 9, 30, 9, 0))
        self.go(datetime(2026, 9, 30, 9, 5))
        self.go(datetime(2026, 9, 30, 15, 0))
        self.assertEqual((len(self.calls), len(self.posts), len(self.emails)), (6, 6, 1))

    def test_rerun_after_all_sent_sends_nothing_more(self):
        self.go()
        self.go(datetime(2026, 9, 30, 9, 10))
        self.go(datetime(2026, 9, 30, 10, 30))
        self.assertEqual((len(self.calls), len(self.posts), len(self.emails)), (6, 6, 1))

    def test_reports_within_an_hour_are_not_labelled_later_ones_are_buhua(self):
        self.go(datetime(2026, 9, 30, 9, 59))
        self.assertTrue(all(p[1].startswith("日报 09-30 09:00\n") for p in self.posts))
        self.assertEqual(self.emails[0][0], "日报 09-30 09:00")

    def test_report_more_than_an_hour_late_is_labelled_buhua_on_discord_and_email(self):
        self.go(datetime(2026, 9, 30, 10, 30))
        self.assertTrue(all(p[1].startswith("日报 09-30 09:00（补发）\n") for p in self.posts))
        self.assertEqual(self.emails[0][0], "日报 09-30 09:00（补发）")
    def test_morning_not_sent_after_the_evening_slot_starts_marked_missed(self):
        self.go(datetime(2026, 9, 30, 21, 5))
        self.assertEqual(self.state(), {**{f"{r}@09:00": "missed" for r in self.ROLES}, "email@09:00": "missed", **self.slot_state("21:00")})
        self.assertEqual([c[2] for c in self.calls], list(self.ROLES))
        self.assertEqual([e[0] for e in self.emails], ["日报 09-30 21:00"])  # the evening ones are on time, not 补发

    def test_morning_report_still_unsent_at_2100_is_dropped_not_run_late(self):
        (self.d / "deputy-work.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.go()
        (self.d / "deputy-work.lock").unlink()
        self.go(datetime(2026, 9, 30, 21, 5))
        self.assertEqual((self.state()["work@09:00"], self.state()["work@21:00"]), ("missed", "sent"))
        work = [c for c in self.calls if c[2] == "work"]
        self.assertEqual(len(work), 1)
        self.assertIn("晚报", work[0][1])
    def test_email_waits_for_every_deputy_then_goes_at_90_minutes_with_wei_chu(self):
        (self.d / "deputy-work.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.go()
            self.assertEqual((len(self.posts), self.emails), (5, []))  # work is busy: the other five are out, email waits
            self.go(datetime(2026, 9, 30, 9, 50))
            self.assertEqual(self.emails, [])
            self.go(datetime(2026, 9, 30, 10, 35))
        self.assertEqual(len(self.emails), 1)
        subject, body = self.emails[0]
        self.assertEqual(subject, "日报 09-30 09:00（补发）")
        self.assertIn("【Arbitros 副总理】\n未出", body)
        self.assertEqual(body.count("完成X"), 5)
        (self.d / "deputy-work.lock").unlink()
        self.go(datetime(2026, 9, 30, 10, 40))  # work finally runs: Discord only, no second email
        self.assertEqual(len(self.emails), 1)
        self.assertEqual(self.posts[-1], ("work", "日报 09-30 09:00（补发）\nwork完成X"))
        self.assertEqual(self.state(), self.slot_state("09:00"))

    def test_failed_report_is_retried_and_the_email_waits_for_it(self):
        self.results["quant"] = stream("")
        self.go()
        self.assertEqual(self.state()["quant@09:00"], "failed")
        self.assertEqual((len(self.posts), self.emails), (5, []))
        self.results.pop("quant")
        self.go(datetime(2026, 9, 30, 9, 10))
        self.assertEqual(self.state(), self.slot_state("09:00"))
        self.assertEqual(len(self.emails), 1)
        self.assertIn("【量化副总理】\nquant完成X", self.emails[0][1])

    def test_discord_failure_retries_the_post_without_a_new_run(self):
        self.discord_ok = False
        self.go()
        self.assertEqual(self.state(), {f"{r}@09:00": "failed" for r in self.ROLES})
        self.assertEqual((len(self.calls), self.emails), (6, []))
        self.discord_ok = True
        self.go(datetime(2026, 9, 30, 9, 10))
        self.assertEqual(self.state(), self.slot_state("09:00"))
        self.assertEqual(len(self.calls), 6)  # the stored text was re-posted
        self.assertEqual(len(self.posts), 12)
        self.assertEqual(len(self.emails), 1)

    def test_email_failure_retries_next_tick_and_is_sent_once(self):
        self.email_ok = False
        self.go()
        self.assertEqual(self.state(), self.slot_state("09:00", **{"email@09:00": "failed"}))
        self.assertIn("email", [x for x in self.logs() if x["decision"] == "report"][-1]["reason"])
        self.assertEqual((len(self.posts), len(self.emails)), (6, 1))
        self.email_ok = True
        self.go(datetime(2026, 9, 30, 9, 10))
        self.assertEqual(self.state(), self.slot_state("09:00"))
        self.assertEqual((len(self.calls), len(self.posts), len(self.emails)), (6, 6, 2))  # one failed attempt, one sent
        self.go(datetime(2026, 9, 30, 9, 15))
        self.assertEqual(len(self.emails), 2)

    def test_skip_still_sends_short_report(self):
        self.results["life"] = stream(" skip。\n")
        self.go()
        self.assertEqual(self.state(), self.slot_state("09:00"))
        self.assertIn("【生活副总理】\n这段时间暂无实质进展，流程正常运行。", self.emails[0][1])

    def test_hard_ceiling_fails_without_run_then_retries(self):
        self.go(pct=90)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state(), {f"{r}@09:00": "failed" for r in self.ROLES})
        self.assertEqual(self.emails, [])
        self.go(datetime(2026, 9, 30, 9, 10), pct=50)
        self.assertEqual(self.state(), self.slot_state("09:00"))

    def test_ceiling_not_pacing(self):
        self.go(pct=89)  # above the 80 % pacing line but below the ceiling
        self.assertEqual(self.state(), self.slot_state("09:00"))

    def test_old_days_are_pruned_so_texts_do_not_pile_up(self):
        old = {f"2026-09-{d:02d}": {"life@09:00": "sent"} for d in range(1, 21)}
        (self.d / "reports.json").write_text(json.dumps(old), encoding="utf-8")
        self.go()
        days = json.loads((self.d / "reports.json").read_text(encoding="utf-8"))
        self.assertEqual((len(days), "2026-09-30" in days, "2026-09-01" in days), (14, True, False))

    def test_read_only_argv_and_timeout(self):
        proc = mock.Mock(pid=1, returncode=0)
        proc.communicate.return_value = ("", "")
        with mock.patch.object(tick.subprocess, "Popen", return_value=proc) as popen:
            tick.run_claude("report", "p", self.d / "t.jsonl")
        argv = popen.call_args[0][0]
        i = argv.index("--allowedTools")
        self.assertEqual(argv[i + 1:i + 4], ["Read", "Glob", "Grep"])
        self.assertEqual(argv[i + 4], "--append-system-prompt")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(proc.communicate.call_args.kwargs["timeout"], 900)

    def test_pause_stops_reports(self):
        (self.d / "tick.PAUSE").write_text("")
        self.go()
        self.assertEqual(self.calls, [])

    def test_email_argv(self):
        with mock.patch.object(tick.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(ORIG_SEND_EMAIL("日报 09-30 09:00", '正文 "引号"'))
        argv = run.call_args[0][0]
        self.assertEqual(Path(argv[0]).parts[-4:], ("discord", ".venv", "Scripts", "python.exe"))
        self.assertEqual(Path(argv[1]).parts[-2:], ("notify", "send_email.py"))
        self.assertEqual(argv[2:], ["日报 09-30 09:00", '正文 "引号"'])
        self.assertNotIn("shell", run.call_args.kwargs)


    def test_an_unforked_beacon_listener_deputy_is_missed_not_started_blank_and_the_email_still_goes(self):
        (self.d / "deputies.json").write_text(json.dumps({"uvox": {"session_id": None, "fork_from": None}}), encoding="utf-8")
        self.go()
        self.assertEqual([c[2] for c in self.calls], [r for r in self.ROLES if r != "uvox"])
        self.assertEqual(self.state(), self.slot_state("09:00", **{"uvox@09:00": "missed"}))
        self.assertEqual(len(self.emails), 1)
        self.assertIn("【Beacon Listener 副总理】\n未出", self.emails[0][1])
        self.assertNotIn("uvox", [r for r, _ in self.posts])

class Resident(TickCase):
    """Fake run_claude plumbing shared by the resident-session and lane tests."""

    def setUp(self):
        super().setUp()
        self.calls, self.efforts, self.models, self.outcomes, self.during, self.launch, self.roles = [], [], [], [], [], [], []
        self.compact_at = []
        for name, fn in (("post_discord", lambda r, m, **k: True), ("send_email", lambda s, b: True)):
            q = mock.patch.object(tick, name, side_effect=fn)
            q.start()
            self.addCleanup(q.stop)
        q = mock.patch.object(tick.review_packet, "build", return_value={"text": "", "hard": None, "facts": {}})
        q.start()  # the packet reads the real project folders; its own tests patch it again
        self.addCleanup(q.stop)

    def fake(self, kind, prompt, run_file, extra=(), effort=None, model=None, cwd=None, add_dirs=(), compact_at=None,
             role=None):
        self.calls.append((kind, prompt, list(extra)))
        self.roles.append(role)
        self.efforts.append(effort)
        self.models.append(model)
        self.compact_at.append(compact_at)
        self.launch.append((cwd, add_dirs))
        self.during.append(self.backlog())
        run_file.parent.mkdir(parents=True, exist_ok=True)
        code, text = self.outcomes.pop(0) if self.outcomes else (0, stream("done"))
        run_file.write_text(text, encoding="utf-8")
        return code

    def go(self, argv, pct=10):
        with self.sample(pct, 0.5), mock.patch.object(tick, "run_claude", side_effect=self.fake):
            return tick.main(list(argv))

    def deputies(self):
        return json.loads((self.d / "deputies.json").read_text(encoding="utf-8"))

    def ledger(self):
        f = self.d / "runs.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


ROLE_BACKLOG = "\n".join(["## Arbitros 线", "- [review] w review", "- [todo] w todo", "", "## 量化线", "- [todo] q todo", ""])
USAGE = {"input_tokens": 9, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 90,
         "output_tokens": 77, "iterations": [{"input_tokens": 1, "cache_read_input_tokens": 5000,
                                              "cache_creation_input_tokens": 1},
                                             {"input_tokens": 3, "cache_read_input_tokens": 200,
                                              "cache_creation_input_tokens": 10}]}


class LaneRuns(Resident):
    def lane(self, role, text=ROLE_BACKLOG, **kw):
        (self.d / "backlog.md").write_text(text, encoding="utf-8")
        return self.go(["lane", role], **kw)

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_review_in_resident_session_with_ledger_row(self):
        self.outcomes = [(0, stream("looks fine\nVERDICT: rework", usage=USAGE, total_cost_usd=0.5))]
        self.lane("work")
        kind, prompt, extra = self.calls[0]
        self.assertEqual((kind, extra[0], self.efforts[0]), ("review", "--session-id", "medium"))
        self.assertTrue(prompt.startswith("你是Arbitros 副总理，验收你这条线的这一项：w review。"))
        self.assertIn("VERDICT: pass、VERDICT: rework 或 VERDICT: blocked", prompt)
        self.assertIn(tick.REVIEW_AS_TRIAL, prompt)
        dep = self.deputies()["work"]
        self.assertEqual((dep["session_id"], dep["status"], dep["runs"], dep["last"]["kind"]), (extra[1], "idle", 1, "review"))
        self.assertEqual(self.backlog(), ROLE_BACKLOG)  # the deputy edits backlog, not tick
        row, = self.ledger()
        self.assertEqual(set(row), {"at", "role", "section", "kind", "model", "effort", "session", "session_id", "item",
                                    "elapsed_s", "exit_code", "ok", "cost_usd", "input_tokens", "cache_read_tokens",
                                    "cache_creation_tokens", "output_tokens", "context_tokens", "verdict", "project", "run_file",
                                    "num_turns", "cache_write_5m_tokens", "cache_write_1h_tokens"})
        self.assertEqual((row["role"], row["section"], row["kind"], row["model"], row["effort"], row["session"]),
                         ("work", "Arbitros 线", "review", "opus", "medium", "new"))
        self.assertEqual((row["session_id"], row["item"], row["exit_code"], row["ok"], row["verdict"], row["project"]),
                         (extra[1], "w review", 0, True, "rework", None))
        self.assertEqual((row["cost_usd"], row["input_tokens"], row["cache_read_tokens"], row["cache_creation_tokens"],
                          row["output_tokens"]), (0.5, 9, 9000, 90, 77))
        self.assertEqual(row["context_tokens"], 213)  # final iteration only, not the summed totals
        ran = [x for x in self.logs() if x["decision"] == "ran"][0]
        self.assertEqual((ran["role"], ran["effort"], ran["kind"], ran["model"]), ("work", "medium", "review", "opus"))
        for lock in ("lane-work.lock", "deputy-work.lock"):
            self.assertFalse((self.d / lock).exists())

    def executor_row(self, item="w review"):
        at = (datetime.now(tick.timezone.utc) - tick.timedelta(hours=1)).isoformat()
        tick.append_jsonl(self.d / "runs.jsonl", {"at": at, "kind": "executor", "role": "work", "project": "delta",
                                                  "item": item, "elapsed_s": 600, "run_file": None})

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_review_prompt_starts_with_the_packet(self):
        self.executor_row()
        self.outcomes = [(0, stream("ok\nVERDICT: pass"))]
        with mock.patch.object(tick.review_packet, "build", return_value={"text": "【材料】\n", "hard": None, "facts": {}}) as build:
            self.lane("work")
        self.assertTrue(self.calls[0][1].startswith("【材料】\n你是Arbitros 副总理，验收你这条线的这一项：w review。"))
        self.assertEqual(build.call_args.args[0], "w review")
        self.assertEqual(build.call_args.args[1], tick.PROJECTS["delta"][0])  # the executor run's project folder

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_hard_condition_sends_the_row_back_without_a_review_run(self):
        self.executor_row()
        why = "自动返工：没有新提交"
        with mock.patch.object(tick.review_packet, "build", return_value={"text": "", "hard": why, "facts": {}}):
            self.lane("work")
        self.assertEqual(self.calls, [])  # no LLM
        self.assertIn(f"- [todo] （返工）w review〔{why}〕\n", self.backlog())
        self.assertIn("- [todo] w todo", self.backlog())
        review, = [r for r in self.ledger() if r["kind"] == "review"]
        self.assertEqual((review["verdict"], review["model"], review["cost_usd"], review["project"], review["auto"]),
                         ("rework", "script", 0.0, "general", True))
        self.assertEqual([x["reason"] for x in self.logs() if x["decision"] == "auto_rework"], [why])
        self.lane("work", self.backlog())  # the rework line is a todo again: the executor takes it, no review
        self.assertEqual([c[0] for c in self.calls], ["executor"])
        self.assertEqual(self.roles, ["work"])

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_unknown_executor_run_or_failing_packet_reviews_as_before(self):
        self.outcomes = [(0, stream("ok\nVERDICT: pass")), (0, stream("ok\nVERDICT: pass"))]
        self.lane("work")  # no executor row in the ledger
        self.assertTrue(self.calls[0][1].startswith("你是Arbitros 副总理"))
        self.executor_row()
        with mock.patch.object(tick.review_packet, "build", side_effect=OSError("boom")):
            self.lane("work")
        self.assertTrue(self.calls[1][1].startswith("你是Arbitros 副总理"))
        self.assertEqual([x["decision"] for x in self.logs() if x["decision"] == "review_packet_failed"], ["review_packet_failed"])

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_quant_review_keeps_its_effort_and_prompt(self):
        self.lane("quant", "## 量化线\n- [review] 量化：q\n")
        kind, prompt, _ = self.calls[0]
        self.assertEqual((kind, self.efforts[0]), ("review", "xhigh"))
        self.assertNotIn(tick.REVIEW_AS_TRIAL, prompt)  # owner\x2010-04: he is still learning quant

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_sprint_review_prompt_only_for_the_sprint_project(self):
        at = (datetime.now(tick.timezone.utc) - tick.timedelta(hours=1)).isoformat()  # Delta ran recently: not starved
        tick.append_jsonl(self.d / "runs.jsonl", {"at": at, "kind": "executor", "role": "work", "project": "delta"})
        with mock.patch.object(tick, "BATCH_MAX", 1):  # one row per session here; batching has its own tests
            self.lane("work", "## Arbitros 线\n冲刺：Uvox，版本目标：v\n- [review] Delta：d\n- [review] Uvox：u\n")
        self.assertIn("这一项：Uvox：u。", self.calls[0][1])
        self.assertTrue(self.calls[0][1].endswith(tick.SPRINT_REVIEW))
        delivery_pos = self.calls[0][1].index("补行前的项目交付核对（本次刷新）")
        self.assertLess(delivery_pos, self.calls[0][1].index(tick.REVIEW_AS_TRIAL))
        self.assertLess(delivery_pos, self.calls[0][1].index(tick.SPRINT_REVIEW))
        self.lane("work", "## Arbitros 线\n冲刺：Uvox，版本目标：v\n- [review] Delta：d\n")
        self.assertNotIn(tick.SPRINT_REVIEW, self.calls[1][1])
        self.assertTrue(self.calls[1][1].endswith(tick.REVIEW_AS_TRIAL))
        self.assertIn("明天冲刺", tick.REPORT_PROMPT[1])  # deputies propose the next sprint in the evening report; the owner approves
        self.assertNotIn("明天冲刺", tick.REPORT_PROMPT[0])

    def test_verdict_parsing_and_context_fallbacks(self):
        self.assertEqual(tick.parse_verdict("x\nVERDICT: pass\n"), "pass")
        self.assertEqual(tick.parse_verdict("VERDICT: pass\nlater\nVERDICT: BLOCKED"), "blocked")
        self.assertEqual(tick.parse_verdict("VERDICT: maybe"), "unknown")
        self.assertEqual(tick.parse_verdict("no verdict"), "unknown")
        f = self.d / "s.jsonl"
        f.write_text("\n".join([json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 2, "cache_read_input_tokens": 30, "cache_creation_input_tokens": 4}}}),
            json.dumps({"type": "result", "usage": {"input_tokens": 50, "cache_read_input_tokens": 900}})]),
            encoding="utf-8")
        self.assertEqual(tick.result_stats(f)["context_tokens"], 36)  # last assistant message, no iterations
        f.write_text(json.dumps({"type": "result", "usage": {"input_tokens": 50, "cache_read_input_tokens": 900}}),
                     encoding="utf-8")
        self.assertEqual(tick.result_stats(f)["context_tokens"], 950)  # totals as last resort
        f.write_text("{}", encoding="utf-8")
        self.assertIsNone(tick.result_stats(f)["context_tokens"])

    def test_result_cost_is_delta_from_baseline_and_compact_post_tokens(self):
        f = self.d / "s.jsonl"
        base = json.dumps({"type": "cost_baseline", "session_id": "s", "total_cost_usd": 28.862})
        f.write_text("\n".join([base, json.dumps({"type": "result", "total_cost_usd": 29.104})]), encoding="utf-8")
        self.assertAlmostEqual(tick.result_stats(f)["cost_usd"], 0.242, places=6)
        f.write_text(json.dumps({"type": "result", "total_cost_usd": 0.5}), encoding="utf-8")
        self.assertEqual(tick.result_stats(f)["cost_usd"], 0.5)  # new session: no baseline
        f.write_text("{}", encoding="utf-8")
        self.assertIsNone(tick.result_stats(f)["cost_usd"])
        boundary = json.dumps({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"post_tokens": 7777}})
        f.write_text("\n".join([boundary, json.dumps({"type": "result", "usage": {}})]), encoding="utf-8")
        self.assertEqual(tick.result_stats(f)["context_tokens"], 7777)

    def test_result_stats_has_turns_and_both_cache_write_tiers(self):
        f = self.d / "s.jsonl"
        usage = {"cache_creation_input_tokens": 30, "cache_creation": {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 20}}
        f.write_text("\n".join([json.dumps({"type": "launch", "argv": []}),
                                json.dumps({"type": "result", "num_turns": 9, "usage": usage})]), encoding="utf-8")
        stats = tick.result_stats(f)
        self.assertEqual((stats["num_turns"], stats["cache_write_5m_tokens"], stats["cache_write_1h_tokens"]), (9, 10, 20))
        f.write_text(json.dumps({"type": "result", "usage": {}}), encoding="utf-8")
        stats = tick.result_stats(f)
        self.assertEqual((stats["num_turns"], stats["cache_write_5m_tokens"], stats["cache_write_1h_tokens"]), (None, None, None))

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_non_review_ledger_has_no_verdict(self):
        self.lane("work", "## Arbitros 线\n- [done] x\n")
        row, = self.ledger()
        self.assertEqual((row["kind"], row["verdict"], row["item"]), ("plan", None, ""))

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_plan_then_resume_then_cooldown_idle(self):
        self.lane("work", "## Arbitros 线\n- [done] x\n")
        sid = self.calls[0][2][1]
        self.assertEqual((self.calls[0][0], self.calls[0][2][0]), ("plan", "--session-id"))
        self.assertTrue(self.calls[0][1].startswith("你是Arbitros 副总理。Arbitros 线 目前没有可执行任务。"))
        self.lane("work", "## Arbitros 线\n- [review] r\n")
        self.assertEqual(self.calls[1][2], ["--resume", sid])
        self.assertEqual(self.deputies()["work"]["runs"], 2)
        self.lane("work", "## Arbitros 线\n- [done] x\n")  # plan ran < 2 h ago
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.logs()[-1]["decision"], "idle")

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_executor_resident_session_effort_alternation_and_backlog(self):
        for _ in range(2):  # other lanes' runs do not move quant's alternation
            tick.append_jsonl(self.d / "runs.jsonl", {"at": tick.now_iso(), "kind": "executor", "role": "work"})
        self.lane("quant")
        self.assertEqual(self.calls[0][0], "executor")
        sid = self.calls[0][2][1]
        self.assertEqual((self.calls[0][2][0], self.efforts[0]), ("--session-id", "medium"))  # owner\x2010-25: executors always medium
        self.assertIn("你是量化副总理这条线（量化线）的通用执行者", self.calls[0][1])
        self.assertIn("任务：q todo。", self.calls[0][1])
        self.assertNotIn("lessons", self.calls[0][1])  # no lessons file yet
        self.assertIn("不要读整份 incore/projects/backlog.md", self.calls[0][1])
        self.assertTrue(self.calls[0][1].endswith("（新会话）"))
        self.assertEqual(self.launch[0], (tick.WORKSPACE, tick.EXECUTOR_ADD_DIRS))
        self.assertIn("- [running ", self.during[0])
        self.assertRegex(self.during[0], r"- \[running \d\d:\d\d\] q todo")
        self.assertIn("- [review] q todo", self.backlog())
        dep = self.deputies()["quant"]
        self.assertIsNone(dep["session_id"])  # the deputy's own session is separate
        self.assertNotIn("executor", dep)
        ex = dep["executors"]["general"]
        self.assertEqual((ex["session_id"], ex["runs"], ex["effort"], ex["item_preview"], ex["exit_code"]),
                         (sid, 1, "medium", "q todo", 0))
        self.assertEqual((ex["project"], ex["last_item"], ex["reworks_in_row"], ex["retire"]),
                         ("general", "q todo", 0, False))
        self.assertTrue(ex["finished_at"])
        self.lane("quant", "## 量化线\n- [todo] q2\n")
        self.assertEqual((self.calls[1][2], self.efforts[1]), (["--resume", sid], "medium"))
        self.assertFalse(self.calls[1][1].endswith("（新会话）"))
        self.lane("quant", "## 量化线\n- [todo] q3\n")
        self.assertEqual((self.calls[2][2], self.efforts[2]), (["--resume", sid], "medium"))
        ex = self.deputies()["quant"]["executors"]["general"]
        self.assertEqual((ex["runs"], ex["session_id"]), (3, sid))
        rows = [r for r in self.ledger() if r.get("role") == "quant"]
        self.assertEqual([(r["effort"], r["session"], r["model"]) for r in rows],
                         [("medium", "new", "sonnet"), ("medium", "resume", "sonnet"), ("medium", "resume", "sonnet")])
        self.assertFalse((self.d / "lane-quant.lock").exists())

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_executor_opus_tag_runs_on_opus(self):
        self.lane("quant", f"## 量化线\n- [todo] q 画面 {tick.OPUS_TAG}\n")
        self.lane("quant", "## 量化线\n- [todo] q2\n")  # the same resident session, untagged: back to the default
        self.assertEqual(self.models, ["opus", None])
        self.assertEqual([r["model"] for r in self.ledger()], ["opus", "sonnet"])
        self.assertEqual([r["model"] for r in self.logs() if r.get("decision") == "ran"], ["opus", "sonnet"])

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_executor_haiku_tag_runs_in_its_own_new_session(self):
        self.lane("quant", "## 量化线\n- [todo] q1\n")  # the project's Sonnet session
        before = dict(self.deputies()["quant"]["executors"]["general"])
        self.lane("quant", f"## 量化线\n- [todo] q 小修 {tick.HAIKU_TAG}\n")
        self.assertEqual(self.calls[1][2][0], "--session-id")  # never resumes the project's session
        self.assertNotEqual(self.calls[1][2][1], before["session_id"])
        after = self.deputies()["quant"]["executors"]["general"]
        self.assertEqual({k: after.get(k) for k in ("session_id", "runs", "created_at", "context_tokens")},
                         {k: before.get(k) for k in ("session_id", "runs", "created_at", "context_tokens")})
        self.lane("quant", "## 量化线\n- [todo] q3\n")  # the next Sonnet run resumes the project's session as before
        self.assertEqual(self.calls[2][2], ["--resume", before["session_id"]])
        self.assertEqual(self.models, [None, "haiku", None])
        self.assertEqual([r["model"] for r in self.ledger()], ["sonnet", "haiku", "sonnet"])

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_executor_compacted_in_place_above_threshold(self):
        big = stream("ok", usage={"iterations": [{"input_tokens": 1, "cache_read_input_tokens": tick.EXECUTOR_COMPACT_TOKENS,
                                                  "cache_creation_input_tokens": 0}]}, total_cost_usd=0.3)
        self.outcomes = [(0, big), (0, stream("", total_cost_usd=0.1))]
        self.lane("quant")
        sid = self.calls[0][2][1]
        self.assertEqual(self.calls[1], ("compact", "/compact", ["--resume", sid]))
        self.assertEqual(self.models[1], "sonnet")
        self.assertEqual(self.launch[1], self.launch[0])  # compaction resumes in the cwd the session was created in
        self.assertIsNone(self.deputies()["quant"]["executors"]["general"]["context_tokens"])
        compact = self.ledger()[-1]
        self.assertEqual((compact["kind"], compact["model"], compact["session_id"], compact["cost_usd"], compact["ok"]),
                         ("compact", "sonnet", sid, 0.1, True))
        self.assertEqual(self.logs()[-2]["decision"], "compacted")
        self.lane("quant", "## 量化线\n- [todo] q2\n")  # same session continues after compaction
        self.assertEqual(self.calls[2][2], ["--resume", sid])
        self.assertEqual(len(self.calls), 3)  # small context: no second compaction
        self.assertEqual(self.deputies()["quant"]["executors"]["general"]["runs"], 2)

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_executor_not_compacted_at_exactly_the_threshold(self):
        (self.d / "deputies.json").write_text(json.dumps({"quant": {"executors": {"general": {
            "session_id": "old", "context_tokens": 10, "runs": 4}}}}))
        self.outcomes = [(0, stream("ok", usage={"iterations": [{"cache_read_input_tokens": tick.EXECUTOR_COMPACT_TOKENS}]}))]
        self.lane("quant")
        self.assertEqual([c[0] for c in self.calls], ["executor"])
        self.assertEqual(self.calls[0][2], ["--resume", "old"])
        ex = self.deputies()["quant"]["executors"]["general"]
        self.assertEqual((ex["runs"], ex["context_tokens"]), (5, tick.EXECUTOR_COMPACT_TOKENS))

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_deputy_session_compacted_with_its_model(self):
        self.outcomes = [(0, stream("VERDICT: pass", usage={"iterations": [{"cache_read_input_tokens": tick.COMPACT_TOKENS + 1}]}))]
        self.lane("work")
        sid = self.calls[0][2][1]
        self.assertEqual((self.calls[1], self.models[1]), (("compact", "/compact", ["--resume", sid]), "opus"))
        dep = self.deputies()["work"]
        self.assertEqual((dep["session_id"], dep["context_tokens"], dep["status"]), (sid, None, "idle"))
        self.assertEqual([r["kind"] for r in self.ledger()], ["review", "compact"])
        self.assertFalse((self.d / "deputy-work.lock").exists())

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_review_ran_log_carries_starved_only_when_the_rule_advanced_it(self):
        self.outcomes = [(0, stream("VERDICT: pass", usage=USAGE))]
        self.lane("work", "## Arbitros 线\n冲刺：驾校模拟器\n- [review] 驾校模拟器：r1\n- [review] Umba：r2\n")
        ran = [x for x in self.logs() if x["decision"] == "ran"][0]
        self.assertEqual((ran["kind"], ran["item"], ran["starved"]), ("review", "Umba：r2", "umba"))
        self.outcomes = [(0, stream("VERDICT: pass", usage=USAGE))]
        self.lane("work")  # plain review row: no starve rule
        self.assertNotIn("starved", [x for x in self.logs() if x["decision"] == "ran"][-1])

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_life_on_opus_and_intelligence_on_sonnet_both_high(self):
        big = {"iterations": [{"cache_read_input_tokens": tick.COMPACT_TOKENS + 1}]}
        self.outcomes = [(0, stream("VERDICT: pass", usage=big))]
        self.lane("life", "## 生活线\n- [review] l review\n")
        self.assertEqual([(c[0], m, e) for c, m, e in zip(self.calls, self.models, self.efforts)],
                         [("review", "opus", "xhigh"), ("compact", "opus", None)])  # owner\x2010-25 high; 10-27 back on Opus; 11-06 xhigh
        self.assertEqual([r["model"] for r in self.ledger()], ["opus", "opus"])
        self.assertEqual([x["model"] for x in self.logs() if x["decision"] == "ran"], ["opus"])
        self.assertEqual(self.compact_at, [tick.COMPACT_TOKENS, None])  # auto-compacts during the run at its limit
        self.lane("intelligence", "## 情报线\n- [review] i review\n")
        self.assertEqual(self.models[-1], "sonnet")
        self.lane("work")
        self.assertEqual(self.models[-1], "opus")
        self.lane("quant", "## 量化线\n- [todo] q todo\n")
        self.assertEqual((self.calls[-1][0], self.compact_at[-1]), ("executor", tick.EXECUTOR_COMPACT_TOKENS))

    def test_compact_limits_deputy_coordinator_executor(self):
        row = lambda n: {"context_tokens": n, "session_id": "s", "section": "组织线"}
        with mock.patch.object(tick, "run_claude", side_effect=self.fake):
            self.assertEqual(tick.maybe_compact("coordinator", row(tick.COMPACT_TOKENS + 1), "opus", "c"),
                             tick.COMPACT_TOKENS + 1)  # the coordinator keeps the larger limit
            self.assertEqual(self.calls, [])
            tick.maybe_compact("coordinator", row(tick.COORDINATOR_COMPACT_TOKENS + 1), "opus", "c")
            tick.maybe_compact("quant", row(tick.COMPACT_TOKENS + 1), "opus", "q")
            tick.maybe_compact("quant", row(tick.COMPACT_TOKENS + 1), "sonnet", "e", "general")  # executor: 250k
        self.assertEqual([c[0] for c in self.calls], ["compact", "compact"])

    def test_executor_dead_resume_retries_once_with_new_session(self):
        (self.d / "deputies.json").write_text(json.dumps({"quant": {"executors": {"general": {
            "session_id": "old", "context_tokens": 10, "runs": 4, "created_at": "x"}}}}))
        missing = json.dumps({"type": "stderr", "message": "No conversation found for session ID: old"})
        self.outcomes = [(1, missing + "\n" + stream("failure", True, worked=False)), (0, stream("fine"))]
        with mock.patch.object(tick, "pid_alive", return_value=False):
            self.lane("quant")
        self.assertEqual(self.calls[0][2], ["--resume", "old"])
        self.assertEqual(self.calls[1][2][0], "--session-id")
        new = self.calls[1][2][1]
        ex = self.deputies()["quant"]["executors"]["general"]
        self.assertEqual((ex["session_id"], ex["runs"], ex["exit_code"]), (new, 1, 0))
        self.assertNotEqual(new, "old")
        self.assertEqual((self.ledger()[-1]["session"], self.ledger()[-1]["session_id"]), ("new", new))
        self.assertIn("- [review] q todo", self.backlog())
        self.assertTrue(self.calls[0][1] == self.calls[1][1])

    def test_executor_failure_after_work_not_retried_and_timeout_reason(self):
        (self.d / "deputies.json").write_text(json.dumps({"quant": {"executors": {"general": {"session_id": "old"}}}}))
        self.outcomes = [(-1, stream("x", True))]
        with mock.patch.object(tick, "pid_alive", return_value=False):
            self.lane("quant")
        self.assertEqual(len(self.calls), 1)
        ran = [x for x in self.logs() if x["decision"] == "ran"][0]
        self.assertEqual((ran["reason"], ran["exit_code"]), ("timeout", -1))
        self.assertFalse(self.ledger()[-1]["ok"])
        self.assertIn("- [review] q todo", self.backlog())

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_ceiling_stopped_executor_line_goes_back_to_todo_and_resumes_next_run(self):
        self.outcomes = [(tick.CEILING_STOP, stream("ceiling_stop", True, cost_estimated=True, total_cost_usd=1.2))]
        self.lane("quant")
        self.assertIn("- [todo] q todo", self.backlog())  # text unchanged, no review
        self.assertNotIn("[review] q todo", self.backlog())
        ran = [x for x in self.logs() if x["decision"] == "ran"][0]
        self.assertEqual((ran["reason"], ran["exit_code"]), ("ceiling_stop", tick.CEILING_STOP))
        row, = [r for r in self.ledger() if r["kind"] == "executor"]
        self.assertEqual((row["cost_usd"], row["cost_estimated"]), (1.2, True))
        sid = self.calls[0][2][1]
        self.lane("quant")  # the same line again: the same session
        self.assertEqual(self.calls[1][2], ["--resume", sid])
        self.assertIn("- [review] q todo", self.backlog())

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_budget_cut_executor_line_still_goes_to_review(self):
        self.outcomes = [(1, stream("budget", True, subtype="error_max_budget_usd"))]
        self.lane("quant")
        self.assertIn("- [review] q todo", self.backlog())
        ran = [x for x in self.logs() if x["decision"] == "ran"][0]
        self.assertEqual((ran["reason"], ran["exit_code"]), ("", 1))

    def test_deputy_busy_exits_before_sampling_but_executor_ignores_deputy_lock(self):
        (self.d / "backlog.md").write_text("## Arbitros 线\n- [review] w review\n", encoding="utf-8")
        (self.d / "deputy-work.lock").write_text("4242")
        (self.d / "deputy-quant.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True), mock.patch.object(tick, "sample_usage") as s:
            tick.main(["lane", "work"])
        s.assert_not_called()
        self.assertEqual((self.calls, self.logs()[-1]["decision"]), ([], "idle"))  # only the busy deputy could take it
        self.assertEqual((self.d / "deputy-work.lock").read_text(), "4242")
        self.assertFalse((self.d / "lane-work.lock").exists())
        (self.d / "backlog.md").write_text(ROLE_BACKLOG, encoding="utf-8")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.lane("quant")
        self.assertEqual([c[0] for c in self.calls], ["executor"])

    def project_lane(self, role, project, text, **kw):
        (self.d / "backlog.md").write_text(text, encoding="utf-8")
        return self.go(["lane", role, project], **kw)

    TWO = "## Arbitros 线\n- [todo] Uvox：a\n- [todo] Umba：b\n"

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_project_lane_runs_its_own_todo_in_the_project_cwd(self):
        self.project_lane("work", "umba", self.TWO)
        kind, prompt, extra = self.calls[0]
        self.assertEqual((kind, extra[0]), ("executor", "--session-id"))
        self.assertIn("你是Arbitros 副总理这条线（Arbitros 线）里项目 umba 的常驻执行者，当前工作目录就是这个项目的目录", prompt)
        self.assertIn("任务：Umba：b。", prompt)
        self.assertEqual(self.launch[0], (tick.PROJECTS["umba"][0], tick.EXECUTOR_ADD_DIRS))
        self.assertIn("- [todo] Uvox：a", self.backlog())  # the other project's todo is untouched
        self.assertIn("- [review] Umba：b", self.backlog())
        ex = self.deputies()["work"]["executors"]["umba"]
        self.assertEqual((ex["project"], ex["session_id"], ex["runs"]), ("umba", extra[1], 1))
        row, = self.ledger()
        self.assertEqual((row["kind"], row["project"], row["role"]), ("executor", "umba", "work"))
        self.assertEqual(self.logs()[-1]["project"], "umba")
        self.assertTrue(self.logs()[-1]["decision"] == "ran")
        for lock in ("lane-work-umba.lock", "lane-work.lock"):
            self.assertFalse((self.d / lock).exists())
        self.project_lane("work", "umba", "## Arbitros 线\n- [todo] Uvox：a\n")  # none for this project: idle
        self.assertEqual((len(self.calls), self.logs()[-1]["decision"]), (1, "idle"))

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_lessons_file_is_named_in_the_prompt_only_when_it_exists(self):
        self.project_lane("work", "umba", self.TWO)
        self.assertNotIn("umba.md", self.calls[0][1])
        (self.d / "lessons").mkdir()
        (self.d / "lessons" / "umba.md").write_text("x", encoding="utf-8")
        self.project_lane("work", "umba", "## Arbitros 线\n- [todo] （返工）Umba：b\n")
        self.assertIn(str(self.d / "lessons" / "umba.md"), self.calls[1][1])
        self.assertEqual(self.calls[1][2], ["--resume", self.calls[0][2][1]])  # same session continues

    def test_unknown_project_argument_is_refused(self):
        self.assertEqual(tick.lane_run("work", "nope"), 2)
        self.assertEqual(self.calls, [])

    def test_plain_lane_falls_back_to_the_first_free_executor_unit(self):
        (self.d / "lane-work-uvox.lock").write_text("4242")
        seen = {}

        def fake(*args, **kw):
            seen["locks"] = sorted(p.name for p in self.d.glob("lane-*.lock"))
            return self.fake(*args, **kw)
        (self.d / "backlog.md").write_text(self.TWO, encoding="utf-8")
        with mock.patch.object(tick, "pid_alive", return_value=True), self.sample(10, 0.5), \
                mock.patch.object(tick, "run_claude", side_effect=fake):
            tick.main(["lane", "work"])
        self.assertEqual(seen["locks"], ["lane-work-umba.lock", "lane-work-uvox.lock"])  # role lock given up
        self.assertIn("任务：Umba：b。", self.calls[0][1])  # uvox's unit was busy
        self.assertEqual(self.launch[0][0], tick.PROJECTS["umba"][0])
        self.assertIn("umba", self.deputies()["work"]["executors"])

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_plain_lane_prefers_deputy_work_over_the_fallback(self):
        self.lane("work", "## Arbitros 线\n- [review] Uvox：r\n- [todo] Uvox：a\n")
        self.assertEqual(self.calls[0][0], "review")
        self.lane("work", "## Arbitros 线\n- [done] x\n")  # plan only without todos
        self.assertEqual(self.calls[1][0], "plan")
        self.lane("work", "## Arbitros 线\n- [done] x\n")  # cooldown
        self.assertEqual(len(self.calls), 2)

    def test_lane_run_enforces_line_and_global_caps_for_an_old_supervisor(self):
        for name in ("lane-work-uvox.lock", "lane-work-umba.lock"):
            (self.d / name).write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.project_lane("work", "general", "## Arbitros 线\n- [todo] 杂项\n")
            self.assertEqual((self.calls, self.logs()[-1]["decision"]), ([], "capped"))  # third executor of the line
            self.assertIn("- [todo] 杂项", self.backlog())
            self.project_lane("quant", "general", "## 量化线\n- [todo] q\n")  # 3 live: another line may take the last slot
            self.assertEqual([c[0] for c in self.calls], ["executor"])
        (self.d / "lane-quant-general.lock").write_text("4242")
        (self.d / "lane-work-uvox.lock").unlink()
        (self.d / "lane-intelligence.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.project_lane("life", "general", "## 生活线\n- [todo] l\n")
        self.assertEqual((len(self.calls), self.logs()[-1]["decision"]), (1, "capped"))  # 4 live units
        self.assertFalse((self.d / "lane-life-general.lock").exists())

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_haiku_row_verdicts_do_not_count_toward_the_project_session(self):
        self.project_lane("work", "uvox", "## Arbitros 线\n- [todo] Uvox：a\n")
        f, h = self.d / "deputies.json", "Uvox：h 〔执行者：Haiku〕"
        dep = self.deputies()
        dep["work"]["executors"]["uvox"].update(reworks_in_row=1, last_item=h)
        f.write_text(json.dumps(dep), encoding="utf-8")
        ex = lambda: self.deputies()["work"]["executors"]["uvox"]
        for verdict in ("rework", "pass", "rework"):
            self.outcomes = [(0, stream(f"x\nVERDICT: {verdict}"))]
            self.lane("work", f"## Arbitros 线\n- [review] {h}\n")
            self.assertEqual((ex()["reworks_in_row"], ex()["retire"]), (1, False))  # neither counted nor reset
        tick.note_verdict("work", "Uvox：h 〔执行者：Haiku〕（返工）", "rework")  # a row sent back to Sonnet counts again
        dep = self.deputies()
        dep["work"]["executors"]["uvox"]["last_item"] = "Uvox：h 〔执行者：Haiku〕（返工）"
        f.write_text(json.dumps(dep), encoding="utf-8")
        with mock.patch.object(tick, "DEPUTIES_FILE", f):
            tick.note_verdict("work", "Uvox：h 〔执行者：Haiku〕（返工）", "rework")
        self.assertEqual(ex()["reworks_in_row"], 2)

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_verdicts_drive_reworks_in_row_and_retirement_with_lessons(self):
        self.project_lane("work", "uvox", "## Arbitros 线\n- [todo] Uvox：a\n")
        old = self.calls[0][2][1]
        ex = lambda: self.deputies()["work"]["executors"]["uvox"]
        self.assertEqual((ex()["reworks_in_row"], ex()["retire"], ex()["last_item"]), (0, False, "Uvox：a"))

        def review(item, verdict):
            self.outcomes = [(0, stream(f"x\nVERDICT: {verdict}"))]
            self.lane("work", f"## Arbitros 线\n- [review] {item}\n")
        review("Uvox：a", "rework")
        self.assertEqual((ex()["reworks_in_row"], ex()["retire"]), (1, False))
        review("Other：z", "rework")  # not this session's item
        review("Uvox：a", "blocked")
        review("Uvox：a", "unknown")
        self.assertEqual((ex()["reworks_in_row"], ex()["retire"]), (1, False))
        review("Uvox：a", "pass")
        self.assertEqual(ex()["reworks_in_row"], 0)
        review("Uvox：a", "rework")
        review("Uvox：a", "rework")
        self.assertEqual((ex()["reworks_in_row"], ex()["retire"], ex()["session_id"]), (2, True, old))
        n = len(self.calls)
        self.outcomes = [(0, stream("- 先读 app.py\n- 跑 pytest -x")), (0, stream("done"))]
        self.project_lane("work", "uvox", "## Arbitros 线\n- [todo] （返工）Uvox：a\n")
        lessons, executor = self.calls[n], self.calls[n + 1]
        self.assertEqual((lessons[0], lessons[2], self.models[n], self.efforts[n]), ("lessons", ["--resume", old], None, "medium"))
        self.assertEqual(self.launch[n][0], tick.PROJECTS["uvox"][0])  # resumed from the cwd it was created in
        self.assertIn("不超过 5 行", lessons[1])
        text = (self.d / "lessons" / "uvox.md").read_text(encoding="utf-8")
        self.assertIn("跑 pytest -x", text)
        self.assertRegex(text, r"## \d{4}-\d\d-\d\d（会话 " + old[:8])
        self.assertEqual((executor[0], executor[2][0]), ("executor", "--session-id"))
        self.assertNotEqual(executor[2][1], old)
        self.assertIn("uvox.md", executor[1])  # the new session is told to read the lessons
        self.assertTrue(executor[1].endswith("（新会话）"))
        new = ex()
        self.assertEqual((new["session_id"], new["retire"], new["reworks_in_row"], new["runs"]), (executor[2][1], False, 0, 1))
        self.assertEqual([(r["session_id"], r["runs"]) for r in new["retired"]], [(old, 1)])
        lrow = next(r for r in self.ledger() if r["kind"] == "lessons")
        self.assertEqual((lrow["project"], lrow["session_id"], lrow["model"], lrow["effort"], lrow["session"], lrow["ok"]),
                         ("uvox", old, "sonnet", "medium", "resume", True))
        self.assertIn("retired", [x["decision"] for x in self.logs()])

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_failed_lessons_run_still_replaces_the_session(self):
        (self.d / "deputies.json").write_text(json.dumps({"work": {"executors": {"uvox": {
            "session_id": "old", "runs": 2, "retire": True, "reworks_in_row": 2}}}}))
        self.outcomes = [(1, stream("boom", True)), (0, stream("done"))]
        self.project_lane("work", "uvox", "## Arbitros 线\n- [todo] Uvox：a\n")
        self.assertFalse((self.d / "lessons" / "uvox.md").exists())
        ex = self.deputies()["work"]["executors"]["uvox"]
        self.assertNotEqual(ex["session_id"], "old")
        self.assertEqual(ex["retired"][0]["session_id"], "old")
        self.assertFalse([r for r in self.ledger() if r["kind"] == "lessons"][0]["ok"])

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_gates_pacing_ceiling_unknown(self):
        self.lane("work", pct=43)
        self.assertEqual((self.calls, self.logs()[-1]["decision"]), ([], "pacing"))
        self.lane("work", pct=95)
        self.assertEqual(self.logs()[-1]["decision"], "hard_ceiling")
        with mock.patch.object(tick, "sample_usage", return_value=None):
            tick.main(["lane", "work"])
        self.assertEqual((self.calls, self.logs()[-1]["decision"]), ([], "usage_unknown"))
        for lock in ("lane-work.lock", "deputy-work.lock"):
            self.assertFalse((self.d / lock).exists())

    def test_lane_lock_busy_exits_quietly(self):
        (self.d / "lane-work.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True), mock.patch.object(tick, "sample_usage") as s:
            tick.main(["lane", "work"])
        s.assert_not_called()
        self.assertEqual(self.logs(), [])
        self.assertEqual((self.d / "lane-work.lock").read_text(), "4242")

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_idle_logged_and_other_sections_ignored(self):
        tick.append_jsonl(self.d / "runs.jsonl", {"at": tick.now_iso(), "kind": "plan", "section": "Arbitros 线"})
        self.lane("work", "## Arbitros 线\n- [running 10:00] x\n- [done] y\n")  # plan cooldown
        self.lane("life", ROLE_BACKLOG)  # no 生活线 section at all
        self.assertEqual(self.calls, [])
        self.assertEqual([x["decision"] for x in self.logs()], ["idle", "idle"])

    def test_pause_blocks_lane(self):
        (self.d / "tick.PAUSE").write_text("")
        self.lane("work")
        self.assertEqual(self.calls, [])

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_coordinate_run_first_then_section_work(self):
        text = "## 组织线\n- [todo] o1\n"
        self.lane("coordinator", text)
        kind, prompt, extra = self.calls[0]
        self.assertEqual((kind, extra[0], self.efforts[0]), ("coordinate", "--session-id", "high"))
        self.assertTrue(prompt.startswith("你是总理。跨线复盘（每 8 小时一次）：先读 control/data/review-stats.md"))
        self.assertIn("和 control/data/kpi.md", prompt)
        self.mocks["write"].assert_called_once_with()  # kpi.md regenerated first
        self.mocks["stats"].assert_called_once_with(tick)  # and the stats table
        self.mocks["delivery"].assert_called_once_with(tick)
        self.assertIn("project-delivery.md", prompt)
        self.assertIn("超过 7 天或 5 条", prompt)
        self.assertEqual(self.backlog(), text)
        row, = self.ledger()
        self.assertEqual((row["kind"], row["role"], row["section"], row["model"]), ("coordinate", "coordinator", "组织线", "opus"))
        self.assertEqual(self.deputies()["coordinator"]["last"]["kind"], "coordinate")
        self.lane("coordinator", text)  # not due again: the section's todo runs in the executor session
        self.assertEqual(self.calls[1][0], "executor")
        self.assertEqual(self.mocks["write"].call_count, 1)

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_kpi_failure_does_not_stop_coordinate(self):
        self.mocks["write"].side_effect = OSError("disk")
        self.lane("coordinator", "## 组织线\n- [todo] o1\n")
        self.assertEqual(self.calls[0][0], "coordinate")
        self.assertIn("kpi_failed", [x["decision"] for x in self.logs()])

    @mock.patch.object(tick, "pid_alive", side_effect=lambda pid: pid == os.getpid())
    def test_delivery_failure_is_visible_and_does_not_stop_coordinate(self, _alive):
        self.mocks["delivery"].side_effect = OSError("disk")
        self.lane("coordinator", "## 组织线\n- [todo] o1\n")
        self.assertIn("本次项目交付核对失败", self.calls[0][1])
        self.assertIn("project_delivery_failed", [x["decision"] for x in self.logs()])

    @mock.patch.object(tick, "pid_alive", side_effect=lambda pid: pid == os.getpid())
    def test_work_plan_gets_fresh_delivery_cap_before_adding_tasks(self, _alive):
        self.lane("work", "## Arbitros 线\n")
        self.assertEqual(self.calls[0][0], "plan")
        self.assertIn("补行前的项目交付核对（本次刷新）", self.calls[0][1])
        self.assertIn("暂停自发新草样", self.calls[0][1])
        self.mocks["delivery"].assert_called_once_with(tick)


    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_stats_failure_does_not_stop_coordinate(self):
        self.mocks["stats"].side_effect = KeyError("x")
        self.lane("coordinator", "## 组织线\n- [todo] o1\n")
        self.assertEqual(self.calls[0][0], "coordinate")
        self.assertIn("review_stats_failed", [x["decision"] for x in self.logs()])


class ResidentRuns(Resident):
    def setUp(self):
        super().setUp()
        q = mock.patch.object(tick.review_stats, "day_facts", return_value="【今日数据】\n")  # reads the real control/data
        self.facts = q.start()
        self.addCleanup(q.stop)
        (self.d / "deputies.json").write_text(json.dumps({"uvox": {"session_id": "uvox-session", "runs": 0}}), encoding="utf-8")  # forked already

    def test_owner_and_review_contend_for_role_lock(self):
        (self.d / "backlog.md").write_text(OWNER_BACKLOG, encoding="utf-8")
        (self.d / "deputy-work.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.go(["owner"])
        self.assertEqual(self.calls, [])
        self.assertIn("- [todo] 【Discord", self.backlog())
        self.assertEqual(self.logs()[-1]["decision"], "deputy_busy")

    def test_owner_takes_and_releases_role_lock(self):
        (self.d / "backlog.md").write_text(OWNER_BACKLOG, encoding="utf-8")
        seen = {}

        def fake(kind, prompt, run_file, extra=(), effort=None, model=None, compact_at=None, role=None):
            seen["lock"] = (self.d / "deputy-work.lock").exists()
            return self.fake(kind, prompt, run_file, extra, effort, model, compact_at=compact_at, role=role)
        with self.sample(10, 0.5), mock.patch.object(tick, "run_claude", side_effect=fake):
            tick.main(["owner"])
        self.assertTrue(seen["lock"])
        self.assertFalse((self.d / "deputy-work.lock").exists())

    def lane_at(self, when, slots=("09:00", "21:00")):
        with mock.patch.object(tick, "REPORT_SLOTS", slots), mock.patch.object(tick, "report_now", return_value=when):
            self.go(["report"])

    def reports(self):
        """role -> (prompt, extra) of the report runs so far (the last one per role)."""
        return {next(r for r, n in tick.ROLE_NAMES.items() if c[1].startswith(f"你是{n}（")): (c[1], c[2])
                for c in self.calls if c[0] == "report"}

    def test_report_resumes_role_session_and_creates_entry(self):
        self.lane_at(datetime(2026, 9, 30, 9, 30))
        reports = self.reports()
        self.assertEqual(sorted(reports), sorted(tick.ROLE_NAMES))  # all five deputies, one run each
        self.assertEqual(len([c for c in self.calls if c[0] == "report"]), 6)
        self.assertEqual(reports["life"][1][0], "--session-id")
        self.assertEqual(reports["uvox"][1], ["--resume", "uvox-session"])  # never a blank session for the forked role
        self.assertTrue(reports["uvox"][0].startswith("你是Beacon Listener 副总理（常驻副总理），负责Beacon Listener 线。"))
        self.assertTrue(reports["life"][0].startswith("你是生活副总理（常驻副总理），负责生活线。"))
        self.assertEqual(self.deputies()["life"]["last"]["kind"], "report")
        life, = [r for r in self.ledger() if r["role"] == "life"]
        self.assertEqual((life["kind"], life["effort"]), ("report", "xhigh"))
        self.assertEqual({r["kind"] for r in self.ledger()}, {"report"})

    def test_morning_report_asks_for_last_night_evening_report_for_the_daytime_and_blockers(self):
        self.lane_at(datetime(2026, 9, 30, 9, 30))
        self.lane_at(datetime(2026, 9, 30, 21, 30))
        morning = {r: c[1] for r, c in zip(("coordinator", "work", "uvox", "quant", "intelligence", "life"), self.calls[:6])}
        evening = {r: c[1] for r, c in zip(("coordinator", "work", "uvox", "quant", "intelligence", "life"), self.calls[6:])}
        for role in morning:
            self.assertIn("昨晚", morning[role])
            self.assertNotIn("打开命令", morning[role])
            if role == "intelligence":
                self.assertIn("给谁｜做什么｜截止", evening[role])  # decisions are part of the action list
                self.assertNotIn("要你决定：", evening[role])
            else:
                self.assertIn("打开命令", evening[role])  # every blocker: what, options, recommendation, open command
            self.assertIn("去睡", evening[role])  # he clears the blockers, then sleeps
            if role == "coordinator":
                self.assertIn("今天白天的计划", morning[role])
            if role in ("work", "uvox", "quant", "life"):
                self.assertIn("接下来 12 小时要推进的项目（早报写白天）", morning[role])
                self.assertIn("接下来 12 小时要推进的项目（晚报写今晚）", evening[role])
                self.assertIn("暂停中：", morning[role])
        self.assertIn("09-29 21:00 以来", morning["work"])  # the window the facts block and the wording cover
        self.assertIn("09-30 09:00 以来", evening["work"])
        self.assertNotIn("明天冲刺", morning["work"])
        self.assertIn("明天冲刺", evening["work"])
        self.assertIn("用量概况", evening["coordinator"])
        self.assertNotIn("用量概况", morning["coordinator"])
        self.assertIn("情报 MM-DD｜要动 N 条，线索 M 条", morning["intelligence"])
        self.assertIn("情报 MM-DD｜要动 N 条，线索 M 条", evening["intelligence"])
        self.assertNotIn("情报 MM-DD｜A 档", morning["intelligence"])

    def test_report_prompt_carries_the_script_facts_for_the_window_and_role(self):
        self.lane_at(datetime(2026, 9, 30, 9, 30))
        self.lane_at(datetime(2026, 9, 30, 21, 30))
        asked = [(c.args[1], c.kwargs["since"]) for c in self.facts.call_args_list]
        self.assertEqual(asked, [(None, datetime(2026, 9, 29, 21, 0)), ("Arbitros 线", datetime(2026, 9, 29, 21, 0)),
                                 ("Beacon Listener 线", datetime(2026, 9, 29, 21, 0)),
                                 ("量化线", datetime(2026, 9, 29, 21, 0)), ("生活线", datetime(2026, 9, 29, 21, 0)),
                                 (None, datetime(2026, 9, 30, 9, 0)), ("Arbitros 线", datetime(2026, 9, 30, 9, 0)),
                                 ("Beacon Listener 线", datetime(2026, 9, 30, 9, 0)),
                                 ("量化线", datetime(2026, 9, 30, 9, 0)), ("生活线", datetime(2026, 9, 30, 9, 0))])  # no intelligence
        self.assertEqual(self.facts.call_args.args[0], datetime(2026, 9, 30).date())
        prompt = self.reports()["life"][0]
        self.assertIn("负责", prompt.split("【今日数据】")[0])
        self.assertTrue(prompt.split("【今日数据】\n")[1].startswith("【机器和备份】\n"))  # the machine block precedes the template
        self.assertIn("今日数据", tick.REPORT_PROMPT[0])
        self.assertIn("今日数据", tick.REPORT_PROMPT[1])

    def test_intelligence_report_has_no_facts_block_and_a_failing_block_does_not_stop_reports(self):
        self.facts.side_effect = KeyError("x")
        self.lane_at(datetime(2026, 9, 30, 9, 30))
        self.assertEqual(sorted(self.reports()), sorted(tick.ROLE_NAMES))
        self.assertTrue(all("今日数据】" not in p for p, _ in self.reports().values()))
        self.assertIn("report_facts_failed", [x["decision"] for x in self.logs()])
        self.assertNotIn("intelligence", [x.get("role") for x in self.logs() if x["decision"] == "report_facts_failed"])

    def test_intelligence_report_is_decision_centred(self):
        self.lane_at(datetime(2026, 9, 30, 9, 30))
        prompt = self.reports()["intelligence"][0]
        self.assertIn("intel-standard.md", prompt)
        self.assertIn("intel-decisions.md", prompt)
        self.assertNotIn("今日完成", prompt)

    def test_sunday_line_only_for_the_coordinators_evening_report_on_sunday(self):
        def prompts(when):
            self.calls.clear()
            self.lane_at(when)
            return {r: p for r, (p, _) in self.reports().items()}
        sunday, monday = datetime(2026, 10, 11, 9, 30), datetime(2026, 10, 12, 9, 30)
        self.assertNotIn("feedback-log.md", prompts(sunday)["coordinator"])  # morning
        evening = prompts(datetime(2026, 10, 11, 21, 30))
        self.assertIn("feedback-log.md", evening["coordinator"])
        self.assertNotIn("feedback-log.md", evening["life"])
        self.assertNotIn("feedback-log.md", prompts(datetime(2026, 10, 12, 21, 30))["coordinator"])

    def test_report_retries_when_role_locked(self):
        (self.d / "deputy-life.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.lane_at(datetime(2026, 9, 30, 9, 30))
        self.assertEqual(sorted(self.reports()), ["coordinator", "intelligence", "quant", "uvox", "work"])
        state = json.loads((self.d / "reports.json").read_text(encoding="utf-8"))["2026-09-30"]
        self.assertNotIn("life@09:00", state)
        self.assertNotIn("email@09:00", state)  # the email waits for the sixth
        (self.d / "deputy-life.lock").unlink()
        self.lane_at(datetime(2026, 9, 30, 9, 35))
        self.assertEqual(len([c for c in self.calls if c[0] == "report"]), 6)
        self.assertEqual(json.loads((self.d / "reports.json").read_text(encoding="utf-8"))["2026-09-30"]["email@09:00"], "sent")

    def hold_coordinator(self, kind="terminal"):
        entry = {"session_id": "owner-terminal-session", "status": "running", "runs": 17,
                 "current": {"kind": kind, "started_at": "2026-09-30T08:00:00+00:00"},
                 "last": {"kind": "coordinate"}, "context_tokens": 12345}
        deputies = self.deputies()
        deputies["coordinator"] = entry
        (self.d / "deputies.json").write_text(json.dumps(deputies), encoding="utf-8")
        (self.d / "deputy-coordinator.lock").write_text("4242", encoding="utf-8")
        return entry

    def test_terminal_locked_report_uses_new_session_and_sends_once_per_slot(self):
        entry = self.hold_coordinator()
        with mock.patch.object(tick, "pid_alive", return_value=True), \
                mock.patch.object(tick, "post_discord", return_value=True) as post, \
                mock.patch.object(tick, "send_email", return_value=True) as email:
            for hour in (9, 21):
                self.lane_at(datetime(2026, 9, 30, hour, 5))
                self.lane_at(datetime(2026, 9, 30, hour, 10))
                self.assertEqual(self.deputies()["coordinator"], entry)
                self.assertEqual((self.d / "deputy-coordinator.lock").read_text(encoding="utf-8"), "4242")
            (self.d / "deputy-coordinator.lock").unlink()
            self.lane_at(datetime(2026, 9, 30, 21, 15))  # terminal closes: no new report or duplicate post
        coord = [c for c in self.calls if c[1].startswith(tick.resident_prefix("coordinator"))]
        self.assertEqual(len(coord), 2)
        self.assertEqual(len(self.calls), 12)
        self.assertTrue(all(c[2][0] == "--session-id" and c[2][1] != entry["session_id"] for c in coord))
        self.assertNotEqual(coord[0][2][1], coord[1][2][1])
        self.assertEqual(sum(c.args[0] == "coordinator" for c in post.call_args_list), 2)
        self.assertEqual(email.call_count, 2)
        rows = [r for r in self.ledger() if r["role"] == "coordinator"]
        self.assertEqual([r["slot"] for r in rows], ["09:00", "21:00"])
        self.assertTrue(all((r["session"], r["source"], r["model"], r["effort"]) ==
                            ("new", "report-when-locked", tick.deputy_model("coordinator", "report"),
                             tick.deputy_effort("coordinator", "report")) for r in rows))

    def test_terminal_report_post_retry_reuses_saved_text_after_terminal_closes(self):
        self.hold_coordinator()
        with mock.patch.object(tick, "REPORT_ROLES", ("coordinator",)), \
                mock.patch.object(tick, "pid_alive", return_value=True), \
                mock.patch.object(tick, "post_discord", return_value=False) as post, \
                mock.patch.object(tick, "send_email", return_value=True) as email:
            self.lane_at(datetime(2026, 9, 30, 9, 5))
            state = json.loads((self.d / "reports.json").read_text(encoding="utf-8"))["2026-09-30"]
            self.assertEqual(state["coordinator@09:00"], "failed")
            self.assertEqual(state["text:coordinator@09:00"], "done")
            email.assert_not_called()
            (self.d / "deputy-coordinator.lock").unlink()
            post.return_value = True
            self.lane_at(datetime(2026, 9, 30, 9, 10))
            self.lane_at(datetime(2026, 9, 30, 9, 15))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0], post.call_args_list[1])
        self.assertEqual(email.call_count, 1)

    def test_coordinator_automatic_or_unknown_lock_holder_still_waits(self):
        with mock.patch.object(tick, "REPORT_ROLES", ("coordinator",)), \
                mock.patch.object(tick, "pid_alive", return_value=True):
            for kind in ("coordinate", "review", "plan", "owner", "report", None):
                with self.subTest(kind=kind):
                    entry = self.hold_coordinator(kind)
                    self.lane_at(datetime(2026, 9, 30, 9, 5))
                    self.assertEqual(self.calls, [])
                    self.assertEqual(self.deputies()["coordinator"], entry)
                    state = json.loads((self.d / "reports.json").read_text(encoding="utf-8"))["2026-09-30"]
                    self.assertNotIn("coordinator@09:00", state)
                    self.assertNotIn("email@09:00", state)

    def test_terminal_report_keeps_usage_gate_and_preserves_foreign_lock_on_error(self):
        entry = self.hold_coordinator()
        with mock.patch.object(tick, "REPORT_ROLES", ("coordinator",)), \
                mock.patch.object(tick, "pid_alive", return_value=True):
            for sampled in (None, (tick.HARD_CEILING, 0)):
                with self.subTest(sampled=sampled), mock.patch.object(tick, "fresh_usage", return_value=sampled):
                    self.lane_at(datetime(2026, 9, 30, 9, 5))
                    self.assertEqual(self.calls, [])
            with mock.patch.object(tick, "write_report", side_effect=RuntimeError("draft failed")):
                with self.assertRaisesRegex(RuntimeError, "draft failed"):
                    self.lane_at(datetime(2026, 9, 30, 9, 10))
        self.assertFalse((self.d / "report.lock").exists())
        self.assertEqual((self.d / "deputy-coordinator.lock").read_text(encoding="utf-8"), "4242")
        self.assertEqual(self.deputies()["coordinator"], entry)

    def test_terminal_report_has_same_facts_context_and_deferred_notices(self):
        self.hold_coordinator()
        notice = {"kind": "answer", "fingerprint": "night-answer"}
        with mock.patch.object(tick, "REPORT_ROLES", ("coordinator",)), \
                mock.patch.object(tick, "pid_alive", return_value=True), \
                mock.patch.object(tick, "owner_words_block", return_value="OWNER WORDS") as words, \
                mock.patch.object(tick.pending, "replies_block", return_value="PENDING REPLIES"), \
                mock.patch.object(tick, "pending_report_ask_notices", return_value=[notice]), \
                mock.patch.object(tick, "format_ask_notice", return_value="NIGHT ANSWER"), \
                mock.patch.object(tick, "acknowledge_report_ask_notices") as ack:
            self.lane_at(datetime(2026, 9, 30, 9, 5))
        prompt = self.calls[0][1]
        for fragment in ("【今日数据】", "【机器和备份】", "OWNER WORDS", "PENDING REPLIES",
                         "事实包截止 2026-09-30 09:05:00", "不续接或修改那个会话"):
            self.assertIn(fragment, prompt)
        words.assert_called_once_with(self.backlog(), "coordinator")
        self.facts.assert_called_once_with(datetime(2026, 9, 30).date(), None, since=datetime(2026, 9, 29, 21))
        state = json.loads((self.d / "reports.json").read_text(encoding="utf-8"))["2026-09-30"]
        self.assertIn("NIGHT ANSWER", state["text:coordinator@09:00"])
        ack.assert_called_once_with(["night-answer"])

    def test_failed_terminal_draft_retries_in_another_new_session(self):
        entry = self.hold_coordinator()
        self.outcomes = [(1, stream("", is_error=True))]
        with mock.patch.object(tick, "REPORT_ROLES", ("coordinator",)), \
                mock.patch.object(tick, "pid_alive", return_value=True), \
                mock.patch.object(tick, "post_discord", return_value=True) as post:
            self.lane_at(datetime(2026, 9, 30, 9, 5))
            state = json.loads((self.d / "reports.json").read_text(encoding="utf-8"))["2026-09-30"]
            self.assertEqual(state["coordinator@09:00"], "failed")
            self.assertNotIn("text:coordinator@09:00", state)
            post.assert_not_called()
            self.lane_at(datetime(2026, 9, 30, 9, 10))
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(self.calls[0][2][1], self.calls[1][2][1])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.deputies()["coordinator"], entry)


class Terminal(Resident):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(tick, "SESSIONS_DIR", self.d)
        p.start()
        self.addCleanup(p.stop)
        (self.d / "deputies.json").write_text(json.dumps({"work": {"session_id": "s1", "runs": 4}}))
        self.t0 = 1_790_000_000.0

    def entry(self, dt, text="", ctx=1000, **extra):
        ts = datetime.fromtimestamp(self.t0 + dt, tick.timezone.utc).isoformat().replace("+00:00", "Z")
        return json.dumps({"type": "assistant", "timestamp": ts, "effort": "xhigh", "message": {
            "model": "claude-opus-5-5", "content": [{"type": "text", "text": text}] if text else [],
            "usage": {"input_tokens": 1, "cache_read_input_tokens": ctx, "cache_creation_input_tokens": 10,
                      "output_tokens": 5}}, **extra})

    def write(self, *lines):
        (self.d / "s1.jsonl").write_text("\n".join(lines), encoding="utf-8")

    def test_begin_then_end_records_ledger_and_dashboard(self):
        tick.main(["terminal", "work", "begin"])
        self.assertEqual((self.deputies()["work"]["status"], self.deputies()["work"]["current"]["kind"]),
                         ("running", "terminal"))
        self.write(self.entry(-50, "old reply"),  # before the conversation: ignored
                   self.entry(10, "first"), self.entry(20, "", ctx=2000), self.entry(30, "final answer", ctx=3000),
                   json.dumps({"type": "cost-state", "startTime": (self.t0 - 999) * 1000, "totalCostUSD": 9.0}),
                   json.dumps({"type": "cost-state", "startTime": (self.t0 + 1) * 1000, "totalCostUSD": 0.42}))
        tick.main(["terminal", "work", "end", str(self.t0)])
        row, = self.ledger()
        self.assertEqual((row["kind"], row["role"], row["section"], row["session_id"], row["model"], row["effort"]),
                         ("terminal", "work", "Arbitros 线", "s1", "claude-opus-5-5", "xhigh"))
        self.assertEqual((row["cost_usd"], row["output_tokens"], row["cache_read_tokens"], row["context_tokens"]),
                         (0.42, 15, 6000, 3011))
        dep = self.deputies()["work"]
        self.assertEqual((dep["status"], dep["current"], dep["runs"], dep["last"]["kind"], dep["last"]["summary"]),
                         ("idle", None, 5, "terminal", "final answer"))
        self.assertEqual(self.calls, [])  # small context: no compaction
        self.assertEqual(self.logs()[-1]["decision"], "terminal")

    def test_end_without_reply_records_nothing(self):
        tick.main(["terminal", "work", "begin"])
        self.write(self.entry(-50, "old"))
        tick.main(["terminal", "work", "end", str(self.t0)])
        self.assertEqual(self.ledger(), [])
        self.assertEqual((self.deputies()["work"]["status"], self.deputies()["work"]["runs"]), ("idle", 4))

    def test_large_context_compacted_and_works_while_paused(self):
        (self.d / "tick.PAUSE").write_text("")
        self.write(self.entry(5, "hi", ctx=tick.COMPACT_TOKENS + 1))
        with mock.patch.object(tick, "run_claude", side_effect=self.fake):
            tick.main(["terminal", "work", "end", str(self.t0)])
        self.assertEqual((self.calls, self.models), ([("compact", "/compact", ["--resume", "s1"])], ["opus"]))
        self.assertEqual([r["kind"] for r in self.ledger()], ["terminal", "compact"])
        self.assertIsNone(self.deputies()["work"]["context_tokens"])

    def test_compact_returns_post_tokens(self):
        self.write(self.entry(5, "hi", ctx=tick.COMPACT_TOKENS + 1))
        boundary = json.dumps({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"post_tokens": 4321}})

        def fake(kind, prompt, run_file, extra=(), effort=None, model=None):
            run_file.parent.mkdir(parents=True, exist_ok=True)
            run_file.write_text(boundary + "\n" + json.dumps({"type": "result", "usage": {}}), encoding="utf-8")
            return 0
        with mock.patch.object(tick, "run_claude", side_effect=fake):
            tick.main(["terminal", "work", "end", str(self.t0)])
        self.assertEqual(self.deputies()["work"]["context_tokens"], 4321)

    def test_duplicate_message_ids_count_once_and_cost_is_a_delta(self):
        cs = lambda total: json.dumps({"type": "cost-state", "startTime": (self.t0 - 999) * 1000, "totalCostUSD": total})
        dup = json.loads(self.entry(10, "first"))
        dup["message"]["id"] = "m1"
        other = json.loads(self.entry(20, "second", ctx=2000))
        other["message"]["id"] = "m2"
        self.write(cs(5.0), self.entry(-50, "old"), cs(11.0), json.dumps(dup), json.dumps({**dup, "uuid": "b"}),
                   json.dumps(other), cs(11.4), cs(11.75))
        stats = tick.transcript_stats(self.d / "s1.jsonl", self.t0)
        self.assertEqual((stats["output_tokens"], stats["cache_read_tokens"]), (10, 3000))  # m1 once, m2 once
        self.assertAlmostEqual(stats["cost_usd"], 0.75, places=6)  # 11.75 - 11.0, startTime is ignored
        self.write(cs(11.0), self.entry(10, "first"))
        self.assertIsNone(tick.transcript_stats(self.d / "s1.jsonl", self.t0)["cost_usd"])  # no cost-state after the reply

    def test_begin_compacts_a_large_session_first(self):
        self.write(self.entry(-50, "old", ctx=tick.COMPACT_TOKENS + 5))
        with mock.patch.object(tick, "run_claude", side_effect=self.fake):
            tick.main(["terminal", "work", "begin", str(self.t0)])
        self.assertEqual(self.calls, [("compact", "/compact", ["--resume", "s1"])])
        self.assertEqual(self.deputies()["work"]["current"]["since"], self.t0)

    def test_compact_boundary_resets_context(self):
        boundary = json.dumps({"type": "system", "subtype": "compact_boundary"})
        self.write(self.entry(-50, "old", ctx=tick.COMPACT_TOKENS + 5), boundary)
        with mock.patch.object(tick, "run_claude", side_effect=self.fake):
            tick.main(["terminal", "work", "begin", str(self.t0)])
        self.assertEqual(self.calls, [])  # already compacted
        self.write(self.entry(5, "a", ctx=tick.COMPACT_TOKENS + 5), boundary)  # owner typed /compact at the end
        with mock.patch.object(tick, "run_claude", side_effect=self.fake):
            tick.main(["terminal", "work", "end", str(self.t0)])
        self.assertEqual((self.calls, self.ledger()[0]["context_tokens"]), ([], 0))

    def test_recover_takes_the_lock_or_waits(self):
        self.write(self.entry(5, "closed by X"))
        (self.d / "deputy-work.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            tick.main(["terminal", "work", "recover", str(self.t0)])  # someone else holds the session: retry later
        self.assertEqual(self.ledger(), [])
        with mock.patch.object(tick, "pid_alive", return_value=False):
            tick.main(["terminal", "work", "recover", str(self.t0)])
        self.assertEqual(self.ledger()[0]["kind"], "terminal")
        self.assertEqual(self.deputies()["work"]["last"]["summary"], "closed by X")
        self.assertFalse((self.d / "deputy-work.lock").exists())

    def test_supervisor_recovers_closed_window_and_keeps_its_session_free(self):
        (self.d / "backlog.md").write_text(LANES.replace("- [todo] w1", "- [review] w1"), encoding="utf-8")
        tick.append_jsonl(self.d / "runs.jsonl", {"at": tick.now_iso(), "kind": "coordinate", "section": "组织线"})
        (self.d / "deputies.json").write_text(json.dumps({"work": {"session_id": "s1", "current": {
            "kind": "terminal", "since": 123.0, "started_at": "2026-09-30T22:30:05+00:00"}}}))
        st = {"sample": None, "spawned": {}, "aux": {}}
        with self.sample(10, 0.5):
            tick.supervise_step(st, time.time())
        self.assertIn(["terminal", "work", "recover", "123.0"], self.spawned)
        self.assertEqual([a[1] for a in self.spawned if a[0] == "lane"], ["quant"])  # work's review waits

    # ---- a project agent's session (talk.ps1 <role> <project>) ----
    def agent(self, **extra):
        """work has an uvox agent whose transcript lives under a project dir (where claude files it)."""
        (self.d / "deputies.json").write_text(json.dumps({"work": {"session_id": "s1", "runs": 4, "executors": {
            "uvox": {"session_id": "e1", "runs": 2, "finished_at": "x", **extra}}}}))
        return self.d / "claude-projects" / "slug" / "e1.jsonl"

    def write_agent(self, *lines):
        path = self.agent()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")

    def test_agent_begin_then_end_records_a_project_row_and_leaves_the_deputy_alone(self):
        self.write_agent()
        tick.main(["terminal", "work", "begin", str(self.t0), "uvox"])
        dep = self.deputies()["work"]
        self.assertEqual((dep["executors"]["uvox"]["current"]["kind"], dep.get("current"), dep.get("status")),
                         ("terminal", None, None))
        self.write_agent(self.entry(-50, "old"), self.entry(10, "hi"), self.entry(30, "agent answer", ctx=3000))
        tick.main(["terminal", "work", "end", str(self.t0), "uvox"])
        row, = self.ledger()
        self.assertEqual((row["kind"], row["role"], row["project"], row["session_id"], row["context_tokens"]),
                         ("terminal", "work", "uvox", "e1", 3011))
        dep = self.deputies()["work"]
        ex = dep["executors"]["uvox"]
        self.assertEqual((ex["current"], ex["runs"], ex["context_tokens"], ex["last"]["kind"], ex["last"]["summary"],
                          ex["session_id"]), (None, 3, 3011, "terminal", "agent answer", "e1"))
        self.assertEqual((dep["runs"], dep.get("current")), (4, None))
        self.assertEqual(self.calls, [])

    def test_agent_end_without_reply_only_clears_current(self):
        self.agent(current={"kind": "terminal"})
        tick.main(["terminal", "work", "end", str(self.t0), "uvox"])
        self.assertEqual((self.ledger(), self.deputies()["work"]["executors"]["uvox"]["current"]), ([], None))

    def test_agent_compacts_at_the_executor_limit_in_its_cwd(self):
        self.write_agent(self.entry(5, "hi", ctx=tick.EXECUTOR_COMPACT_TOKENS + 1))
        with mock.patch.object(tick, "run_claude", side_effect=self.fake):
            tick.main(["terminal", "work", "begin", str(self.t0), "uvox"])
            tick.main(["terminal", "work", "end", str(self.t0), "uvox"])
        self.assertEqual(self.calls, [("compact", "/compact", ["--resume", "e1"])] * 2)
        self.assertEqual((self.models, {c for c, _ in self.launch}), (["sonnet"] * 2, {tick.PROJECTS["uvox"][0]}))
        self.assertEqual([(r["kind"], r["project"]) for r in self.ledger()],
                         [("compact", "uvox"), ("terminal", "uvox"), ("compact", "uvox")])  # begin, end, end's own

    def test_agent_recover_respects_its_lane_lock(self):
        self.write_agent(self.entry(5, "closed by X"))
        (self.d / "lane-work-uvox.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            tick.main(["terminal", "work", "recover", str(self.t0), "uvox"])
        self.assertEqual(self.ledger(), [])
        with mock.patch.object(tick, "pid_alive", return_value=False):
            tick.main(["terminal", "work", "recover", str(self.t0), "uvox"])
        self.assertEqual(self.ledger()[0]["project"], "uvox")
        self.assertFalse((self.d / "lane-work-uvox.lock").exists())

    def test_unknown_project_is_refused(self):
        self.assertEqual(tick.main(["terminal", "work", "begin", str(self.t0), "nope"]), 2)

    def test_supervisor_recovers_a_closed_agent_window_and_keeps_the_agent_free(self):
        (self.d / "backlog.md").write_text(LANES.replace("- [todo] w1", "- [todo] Uvox w1"), encoding="utf-8")
        tick.append_jsonl(self.d / "runs.jsonl", {"at": tick.now_iso(), "kind": "coordinate", "section": "组织线"})
        self.agent(current={"kind": "terminal", "since": 123.0, "started_at": "2026-09-30T22:30:05+00:00"})
        st = {"sample": None, "spawned": {}, "aux": {}}
        with self.sample(10, 0.5):
            tick.supervise_step(st, time.time())
        self.assertIn(["terminal", "work", "recover", "123.0", "uvox"], self.spawned)
        self.assertNotIn(["lane", "work", "uvox"], self.spawned)  # the agent waits for recovery

    def test_launch_prints_how_the_scheduler_starts_an_agent(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(tick.main(["launch", "work", "signlang"]), 0)
        info = json.loads(out.getvalue())
        self.assertEqual((info["cwd"], info["model"]), (str(tick.PROJECTS["signlang"][0]), tick.MODELS["executor"]))
        self.assertEqual(info["add_dirs"], [str(d) for d in tick.executor_launch("signlang")["add_dirs"]])
        self.assertEqual(tick.main(["launch", "work", "nope"]), 2)


class OwnerRepliesInPrompt(Resident):
    def run_role(self, role):
        seen = []

        def fake(kind, prompt, run_file, extra=(), effort=None, model=None, compact_at=None, role=None):
            seen.append(prompt)
            run_file.parent.mkdir(parents=True, exist_ok=True)
            run_file.write_text(stream("ok"), encoding="utf-8")
            return 0
        with mock.patch.object(tick, "run_claude", side_effect=fake):
            tick.run_resident(role, "review", "BASE PROMPT", {}, {})
        return seen[0]

    def test_new_replies_of_the_role_line_ride_along_until_marked_done(self):
        (self.d / "owner-data").mkdir()
        rows = [{"at": "2026-10-19T01:00:00Z", "kind": "reply", "id": "a1", "section": "Arbitros 线", "head": "Uvox：首页主张选哪句",
                 "option": "B", "note": "用 B", "status": "new"},
                {"at": "2026-10-19T01:01:00Z", "kind": "reply", "id": "q1", "section": "量化线", "head": "量化：别的", "option": "A", "note": "", "status": "new"}]
        (self.d / "owner-data" / "owner-replies.json").write_text(json.dumps({"records": rows}, ensure_ascii=False), encoding="utf-8")
        prompt = self.run_role("work")
        self.assertTrue(prompt.startswith("BASE PROMPT"))
        for want in ("Uvox：首页主张选哪句", "选的选项：B", "说明：用 B", "pending.py done a1"):
            self.assertIn(want, prompt)
        self.assertNotIn("量化：别的", prompt)  # only this line's replies
        self.assertNotIn("回复", self.run_role("intelligence"))
        self.assertEqual(tick.pending.mark_done(tick.OWNER_DATA, "a1"), 1)
        handled = self.run_role("work")
        self.assertTrue(handled.startswith("BASE PROMPT"))
        self.assertNotIn("pending.py done a1", handled)  # handled: not carried again; delivery inventory still rides along


class OwnerWordsInPrompt(Resident):
    @staticmethod
    def message_id(when, sequence=0):
        millis = int(when.timestamp() * 1000)
        return str(((millis - tick.DISCORD_EPOCH_MS) << 22) | sequence)

    def run_role(self, role, backlog, kind="review"):
        (self.d / "backlog.md").write_text(backlog, encoding="utf-8")
        seen = []

        def fake(run_kind, prompt, run_file, extra=(), effort=None, model=None, compact_at=None, role=None):
            seen.append(prompt)
            run_file.parent.mkdir(parents=True, exist_ok=True)
            run_file.write_text(stream("ok"), encoding="utf-8")
            return 0

        with mock.patch.object(tick, "deputy_ready", return_value=True), \
                mock.patch.object(tick, "run_claude", side_effect=fake):
            tick.run_resident(role, kind, "BASE PROMPT", {}, {})
        return seen[0]

    def test_recent_other_channel_words_reach_coordinator_deputy_and_report_but_not_their_own_line(self):
        now = datetime.now(timezone.utc)
        life = f"- [done 19:00] 【Discord·生活副总理】先测 4B 〔msg:{self.message_id(now - timedelta(hours=1))} role:life〕"
        work = f"- [done 19:00] 【Discord·Arbitros 副总理】选 A 〔msg:{self.message_id(now - timedelta(minutes=30), 1)} role:work〕"
        old = f"- [done 19:00] 【Discord·生活副总理】旧回复 〔msg:{self.message_id(now - timedelta(hours=25), 2)} role:life〕"
        backlog = "\n".join(["## 你的指令（最优先）", life, work, old, "## 生活线", "- [todo] x", ""])

        coordinator = self.run_role("coordinator", backlog, "coordinate")
        deputy_report = self.run_role("work", backlog, "report")
        life_review = self.run_role("life", backlog, "review")

        self.assertIn(life, coordinator)
        self.assertIn(work, coordinator)
        self.assertIn(life, deputy_report)  # another line's words reach a deputy's report
        self.assertNotIn(work, deputy_report)  # work does not repeat its own channel
        self.assertIn(work, life_review)
        self.assertNotIn(life, life_review)  # life does not repeat its own channel
        for prompt in (coordinator, deputy_report, life_review):
            self.assertNotIn(old, prompt)

    def test_terminal_owner_words_are_included_and_more_than_30_are_counted(self):
        now = datetime.now().astimezone()
        terminal = now - timedelta(hours=1)
        terminal_line = f'- [done {terminal:%H:%M}] 【终端】owner {terminal:%m-%d %H:%M} 原话「先测」'
        self.assertIn(terminal_line, tick.owner_words_block(
            f"## 你的指令（最优先）\n{terminal_line}\n## 组织线\n", "work", now))

        recent = now.astimezone(timezone.utc)
        lines = [f"- [done 19:00] 【Discord·生活副总理】指令 {i} 〔msg:{self.message_id(recent, i)} role:life〕"
                 for i in range(32)]
        block = tick.owner_words_block("## 你的指令（最优先）\n" + "\n".join(lines) + "\n## 组织线\n", "work", now)
        self.assertEqual(sum(line.startswith("- [") for line in block.splitlines()), 30)
        self.assertNotIn(lines[0], block)
        self.assertIn(lines[-1], block)
        self.assertIn("另有 2 条。", block)

    def test_recent_coordinator_answers_reach_only_their_line_prompt(self):
        now = datetime.now().astimezone()
        fresh, stale, owner, other = "写作模型：第 3 步问题", "生活：旧问题", "自媒体：方向选择", "FactoryAlign：跨线问题"
        fresh_done = f"- [done {(now - timedelta(minutes=5)):%m-%d %H:%M}：总理答 按建议继续做] {fresh}"
        stale_done = f"- [done {(now - timedelta(hours=25)):%m-%d %H:%M}：总理答 旧答复] {stale}"
        owner_answer = f"- [blocked: 等你决定] {owner}（总理建议：A，先做可逆草样）"
        other_done = f"- [done {(now - timedelta(minutes=5)):%m-%d %H:%M}：总理答 合并进 Arbitros] {other}"

        def snapshot(when, content):
            history = self.d / "backlog-history"
            history.mkdir(exist_ok=True)
            stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            (history / f"backlog-{stamp}.md").write_text(content, encoding="utf-8")

        before = "\n".join(["## 生活线", f"- [blocked: 等总理] {fresh}", f"- [blocked: 等总理] {stale}",
                            f"- [blocked: 等总理] {owner}", "## Arbitros 线", f"- [blocked: 等总理] {other}", ""])
        after_stale = "\n".join(["## 生活线", f"- [blocked: 等总理] {fresh}", stale_done,
                                f"- [blocked: 等总理] {owner}", "## Arbitros 线", f"- [blocked: 等总理] {other}", ""])
        current = "\n".join(["## 生活线", fresh_done, stale_done, owner_answer,
                             "## Arbitros 线", other_done, ""])
        snapshot(now - timedelta(hours=26), before)
        snapshot(now - timedelta(hours=23), after_stale)
        snapshot(now - timedelta(minutes=6), after_stale)
        snapshot(now - timedelta(minutes=1), current)

        life = self.run_role("life", current, "review")
        discord = self.run_role("life", current, "owner")
        work = self.run_role("work", current, "plan")

        self.assertIn("总理最近24小时给本线的答复", life)
        self.assertIn("写作模型", life)
        self.assertIn("按建议继续做", life)
        self.assertIn("先做可逆草样", life)
        self.assertIn("写作模型", discord)
        self.assertIn("incore/projects/backlog.md:2", life)
        self.assertNotIn("旧答复", life)
        self.assertNotIn("FactoryAlign", life)
        self.assertIn("FactoryAlign", work)
        self.assertNotIn("写作模型", work)

    def test_timed_coordinator_answers_do_not_need_snapshots(self):
        now = datetime.now().astimezone()
        recent = f"- [done {(now - timedelta(hours=2)):%m-%d %H:%M}：总理答 {('按建议继续做（保留括号）' + '补充' * 100)}] 写作模型：第 3 步问题"
        stale = f"- [done {(now - timedelta(hours=25)):%m-%d %H:%M}：总理答 25 小时前的答复] 生活：旧问题"
        undated = "- [blocked: 等你决定] 自媒体：方向选择（总理建议：A，先做可逆草样）"
        current = "\n".join(["## 生活线", recent, stale, undated, ""])

        block = tick.coordinator_answers_block(current, "life", now)

        self.assertIn("写作模型", block)
        self.assertIn("按建议继续做（保留括号）", block)
        self.assertNotIn("25 小时前的答复", block)
        self.assertNotIn("自媒体", block)  # an undated owner decision without a previous snapshot is not attributed
        excerpt = block.split("答复摘录（前200字）：", 1)[1].split("｜全文见", 1)[0]
        self.assertEqual(len(excerpt), 200)
        self.assertFalse((self.d / "backlog-history").exists())

    def test_long_terminal_words_are_cut_and_point_to_record(self):
        records = self.d / "records"
        records.mkdir(exist_ok=True)
        now = datetime.now().astimezone()
        name = f"{now:%Y-%m-%d}-owner-terminal-long.json"
        (records / name).write_text(json.dumps({"recorded_at": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
                                                "owner_verbatim": "长" * 500}, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(tick, "RECORDS_DIR", records):
            block = tick.owner_words_block("## 你的指令（最优先）\n## 组织线\n", "work", now)
        line = next(row for row in block.splitlines() if name[:-5] in row)
        words = line.split("：", 1)[1]
        suffix = f"…（全文 records/{name}）"
        self.assertTrue(words.endswith(suffix))
        self.assertEqual(len(words) - len(suffix), 300)

    def test_terminal_records_reach_every_prompt_and_old_ones_do_not(self):
        records = self.d / "records"
        records.mkdir(exist_ok=True)
        now = datetime.now().astimezone()
        recent = now - timedelta(hours=1)
        rounded = recent.replace(minute=recent.minute // 10 * 10, second=0, microsecond=0)
        old = now - timedelta(hours=30)
        cases = (
            ("recorded-at", {"recorded_at": recent.isoformat(timespec="seconds"), "owner_verbatim": "终端 recorded_at"}, "终端 recorded_at"),
            ("recorded-at-verbatim", {"recorded_at": recent.isoformat(timespec="seconds"), "speaker": "owner", "verbatim": ""}, "终端原话字段兼容"),
            ("time-local-iso", {"time_local": recent.replace(tzinfo=None).isoformat(timespec="seconds")}, "终端 ISO time_local"),
            ("date-hh-mx", {"date": f"{rounded:%Y-%m-%d}", "time_local": f"{rounded:%H}:{rounded.minute // 10}x"}, "终端 HH:Mx"),
            ("date-hh-mm", {"date": f"{recent:%Y-%m-%d}", "time_local": f"{recent:%H:%M}"}, "终端 HH:MM"),
            ("date-only", {"date": f"{now:%Y-%m-%d}"}, "终端当天无钟点"),
            ("filename-date-only", {}, "终端文件名日期"),
            ("old", {"recorded_at": old.isoformat(timespec="seconds")}, "终端超过24小时"),
        )
        for name, fields, words in cases:
            rec = {**fields}
            rec["verbatim" if "verbatim" in rec else "owner_verbatim"] = words
            (records / f"{now:%Y-%m-%d}-owner-terminal-{name}.json").write_text(
                json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        (records / "2026-10-30-owner-discord-x.json").write_text(
            json.dumps({"date": f"{now:%Y-%m-%d}", "owner_verbatim": "非终端"}), encoding="utf-8")
        (records / f"{now:%Y-%m-%d}-owner-terminal-non-owner.json").write_text(
            json.dumps({"recorded_at": recent.isoformat(timespec="seconds"), "speaker": "assistant",
                        "verbatim": "非 owner 原话"}, ensure_ascii=False), encoding="utf-8")
        backlog = "## 你的指令（最优先）\n## 组织线\n- [todo] x\n"
        with mock.patch.object(tick, "RECORDS_DIR", records):
            for role in tick.LINE_SECTIONS:
                kind = "coordinate" if role == "coordinator" else "review"
                prompt = self.run_role(role, backlog, kind)
                for _, _, words in cases[:-1]:
                    self.assertIn(words, prompt)
                self.assertNotIn(cases[-1][2], prompt)
                self.assertNotIn("非 owner 原话", prompt)
                self.assertNotIn("非终端", prompt)


class BackupWatch(TickCase):
    def setUp(self):
        super().setUp()
        self.proc, self.status = self.d / "watcher-process.json", self.d / "status.json"
        for k, v in {"BACKUP_PROCESS": self.proc, "BACKUP_STATUS": self.status,
                     "BACKUP_ALERT_FILE": self.d / "backup-alert.json"}.items():
            p = mock.patch.object(tick, k, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(tick, "pid_alive", return_value=True)
        self.alive = p.start()
        self.addCleanup(p.stop)
        self.post = mock.patch.object(tick, "post_discord", return_value=True)
        self.posted = self.post.start()
        self.addCleanup(self.post.stop)
        self.st, self.t = {}, 1000.0
        self.proc.write_text(json.dumps({"ProcessId": 4242}), encoding="utf-8")
        self.write_status()

    def write_status(self, age_s=30, **extra):
        checked = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        self.status.write_text(json.dumps({"stage": "local_snapshot_verified", "checked_at": checked.isoformat(),
                                           **extra}), encoding="utf-8")

    def step(self, dt=0):
        self.t += dt
        REAL_BACKUP_WATCH(self.st, self.t)

    def test_healthy_sends_nothing(self):
        self.step()
        self.posted.assert_not_called()

    def test_process_gone_alerts_once_to_coordinator(self):
        self.alive.return_value = False
        self.step()
        self.posted.assert_not_called()
        self.step(tick.BACKUP_CHECK_S)
        self.posted.assert_called_once()
        self.step(tick.BACKUP_CHECK_S)
        self.posted.assert_called_once()
        self.assertEqual(self.posted.call_args[0][0], "coordinator")
        self.assertIn("4242", self.posted.call_args[0][1])
        self.assertIs(self.posted.call_args.kwargs.get("hold"), False)  # the supervisor loop never waits on the gateway

    def test_process_down_confirmation_rearms_after_recovery(self):
        self.alive.return_value = False
        self.step()
        self.posted.assert_not_called()
        self.alive.return_value = True
        self.step(tick.BACKUP_CHECK_S)
        self.alive.return_value = False
        self.step(tick.BACKUP_CHECK_S)
        self.posted.assert_not_called()
        self.step(tick.BACKUP_CHECK_S)
        self.posted.assert_called_once()

    def test_process_down_confirmation_restarts_when_pid_changes(self):
        self.alive.return_value = False
        self.step()
        self.proc.write_text(json.dumps({"ProcessId": 4343}), encoding="utf-8")
        self.step(tick.BACKUP_CHECK_S)
        self.posted.assert_not_called()
        self.step(tick.BACKUP_CHECK_S)
        self.posted.assert_called_once()

    def test_pid_file_written_with_bom_is_read(self):
        # 10-21: the live file starts with a BOM; read as plain utf-8 the PID was "unknown" and every restart re-alerted
        self.proc.write_bytes(b"\xef\xbb\xbf" + json.dumps({"ProcessId": 4242}).encode("utf-8"))
        self.alive.side_effect = lambda pid: pid == 4242
        self.assertNotIn("process_down", tick.backup_faults(datetime.now(timezone.utc)))

    def test_missing_pid_file_counts_as_down(self):
        self.proc.unlink()
        self.alive.side_effect = lambda pid: pid > 0  # pid 0 is never alive in the real check
        self.assertIn("process_down", tick.backup_faults(datetime.now(timezone.utc)))

    def test_status_errors(self):
        self.write_status(staging_error="drive disconnected")
        self.assertEqual(list(tick.backup_faults(datetime.now(timezone.utc))), ["staging_error"])
        self.write_status()
        self.status.write_text(json.dumps({"stage": "backup_failed", "error": "boom",
                                           "checked_at": datetime.now(timezone.utc).isoformat()}))
        self.assertEqual(list(tick.backup_faults(datetime.now(timezone.utc))), ["backup_failed"])

    def fail_round(self, n):
        """A failed round as the watcher writes it: stage backup_failed with that round's own checked_at."""
        self.status.write_text(json.dumps({"stage": "backup_failed", "error": "busy",
                                           "checked_at": (datetime.now(timezone.utc) - timedelta(seconds=30 - n)).isoformat()}))

    def test_backup_failed_one_round_does_not_alert_even_if_seen_twice(self):
        self.fail_round(1)
        self.step()
        self.step(tick.BACKUP_CHECK_S)  # the same failed status is still there
        self.posted.assert_not_called()
        self.write_status()
        self.step(tick.BACKUP_CHECK_S)
        self.posted.assert_not_called()

    def test_backup_failed_two_rounds_in_a_row_alert_once(self):
        self.fail_round(1)
        self.step()
        self.fail_round(2)
        self.step(tick.BACKUP_CHECK_S)
        self.fail_round(3)
        self.step(tick.BACKUP_CHECK_S)
        self.posted.assert_called_once()
        self.assertIn("本地备份失败", self.posted.call_args[0][1])

    def test_backup_failed_count_starts_over_after_a_healthy_check(self):
        self.fail_round(1)
        self.step()
        self.write_status()
        self.step(tick.BACKUP_CHECK_S)
        self.fail_round(2)
        self.step(tick.BACKUP_CHECK_S)
        self.posted.assert_not_called()

    def test_other_kinds_still_alert_on_the_first_check(self):
        self.alive.return_value = False
        self.write_status(staging_error="drive disconnected", age_s=601)
        self.step()
        self.assertEqual(self.posted.call_count, 1)
        for kind in ("Drive 暂存失败", "已超过"):
            self.assertIn(kind, self.posted.call_args[0][1])
        self.assertNotIn("备份进程不在了", self.posted.call_args[0][1])

    def test_arbitros_pull_failure_is_its_own_alert_kind(self):
        self.write_status(arbitros_error="ssh demo01-server exit 255: timed out")
        faults = tick.backup_faults(datetime.now(timezone.utc))
        self.assertEqual(list(faults), ["arbitros_error"])
        self.assertIn("255", faults["arbitros_error"])
        self.write_status()  # the next successful pull drops the key: the fault clears
        self.assertEqual(tick.backup_faults(datetime.now(timezone.utc)), {})

    def test_arbitros_failure_alerts_only_when_the_retry_also_failed(self):
        def ok_at(minutes):
            return {"ok_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()}
        now = datetime.now(timezone.utc)
        self.write_status(arbitros_error="ssh demo01-server: TimeoutExpired", arbitros=ok_at(60))  # one failed pull, last success 1 h ago
        self.assertEqual(tick.backup_faults(now), {})
        self.write_status(arbitros_error="ssh demo01-server: TimeoutExpired", arbitros=ok_at(76))  # the 10-minute retry failed too
        self.assertEqual(list(tick.backup_faults(now)), ["arbitros_error"])
        self.write_status(arbitros=ok_at(1))  # recovered
        self.assertEqual(tick.backup_faults(now), {})
        self.write_status(arbitros_error="ssh demo01-server: TimeoutExpired", arbitros=ok_at(26 * 60))  # no success in 26 h
        self.assertEqual(list(tick.backup_faults(now)), ["arbitros_error"])

    def test_arbitros_alert_rearms_after_recovery(self):
        old = {"ok_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}
        self.write_status(arbitros_error="x", arbitros=old)
        self.step()
        self.write_status()
        self.step(tick.BACKUP_CHECK_S)
        self.write_status(arbitros_error="x", arbitros=old)
        self.step(tick.BACKUP_CHECK_S)
        self.assertEqual(self.posted.call_count, 2)

    def test_stale_check_over_ten_minutes(self):
        self.write_status(age_s=601)
        self.assertEqual(list(tick.backup_faults(datetime.now(timezone.utc))), ["stale"])
        self.write_status(age_s=590)
        self.assertEqual(tick.backup_faults(datetime.now(timezone.utc)), {})

    def test_unreadable_status(self):
        self.status.write_text("{")
        self.assertEqual(list(tick.backup_faults(datetime.now(timezone.utc))), ["status_unreadable"])

    def test_recovery_rearms_and_new_kind_alerts_again(self):
        self.write_status(staging_error="x")
        self.step()
        self.write_status(staging_error="x", age_s=700)  # same fault persists, plus stale: only stale is new
        self.step(tick.BACKUP_CHECK_S)
        self.assertEqual(self.posted.call_count, 2)
        self.assertNotIn("Drive", self.posted.call_args[0][1])
        self.write_status()
        self.step(tick.BACKUP_CHECK_S)  # healthy: re-armed
        self.write_status(staging_error="y")
        self.step(tick.BACKUP_CHECK_S)
        self.assertEqual(self.posted.call_count, 3)

    def test_failed_send_is_retried(self):
        self.write_status(staging_error="x")
        self.posted.return_value = False
        self.step()
        self.posted.return_value = True
        self.step(tick.BACKUP_CHECK_S)
        self.assertEqual(self.posted.call_count, 2)
        self.step(tick.BACKUP_CHECK_S)
        self.assertEqual(self.posted.call_count, 2)

    def test_alert_survives_restart_and_check_is_throttled(self):
        self.write_status(staging_error="x")
        self.step()
        self.step(5)  # throttled: no second look
        self.st = {}  # supervisor restarted
        self.step(tick.BACKUP_CHECK_S)
        self.posted.assert_called_once()

    def test_supervise_step_runs_the_check(self):
        with mock.patch.object(tick, "backup_watch") as w, self.sample(10, 0.5):
            tick.supervise_step({"sample": None, "spawned": {}, "aux": {}}, time.time())
        w.assert_called_once()


class WebsiteWatch(TickCase):
    """The health URL is a local HTTP server: no outside network."""
    def setUp(self):
        super().setUp()
        self.code = 200

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(h):
                h.send_response(self.code)
                h.send_header("Location", "/elsewhere")
                h.end_headers()

            def log_message(h, *args):
                pass
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/health"
        for k, v in {"WEBSITE_URL": self.url, "WEBSITE_ALERT_FILE": self.d / "website-alert.json"}.items():
            p = mock.patch.object(tick, k, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(tick, "post_discord", return_value=True)
        self.posted = p.start()
        self.addCleanup(p.stop)
        self.st, self.t = {}, 1000.0

    def step(self):
        self.t += tick.WEBSITE_CHECK_S
        REAL_WEBSITE_WATCH(self.st, self.t)

    def test_status_200_503_and_nobody_listening(self):
        self.assertEqual(tick.website_status(self.url), (200, ""))
        self.code = 503
        self.assertEqual(tick.website_status(self.url), (503, ""))
        self.code = 301  # redirects are not followed
        self.assertEqual(tick.website_status(self.url), (301, ""))
        self.server.shutdown()
        self.server.server_close()
        code, err = tick.website_status(self.url)
        self.assertIsNone(code)
        self.assertTrue(err)

    def test_alert_after_two_bad_checks_once_and_rearms_when_healthy(self):
        self.step()
        self.code = 503
        self.step()  # one bad check: nothing yet
        self.posted.assert_not_called()
        self.step()
        self.posted.assert_called_once()
        self.assertIs(self.posted.call_args.kwargs.get("hold"), False)
        self.assertEqual(self.posted.call_args[0][0], "coordinator")
        self.assertTrue(self.posted.call_args[0][1].startswith("官网告警："))
        self.step()  # still down: no second line
        self.posted.assert_called_once()
        self.code = 200
        self.step()
        self.code = 503
        self.step()
        self.step()  # down again after recovery: a new line
        self.assertEqual(self.posted.call_count, 2)

    def test_check_is_throttled_and_a_failed_send_is_retried(self):
        self.code = 503
        self.posted.return_value = False
        self.step()
        self.step()
        self.posted.return_value = True
        REAL_WEBSITE_WATCH(self.st, self.t + 5)  # inside the interval: no look
        self.assertEqual(self.posted.call_count, 1)
        self.step()
        self.assertEqual(self.posted.call_count, 2)

    def test_supervise_step_runs_the_check(self):
        with mock.patch.object(tick, "website_watch") as w, self.sample(10, 0.5):
            tick.supervise_step({"sample": None, "spawned": {}, "aux": {}}, time.time())
        w.assert_called_once()


if __name__ == "__main__":
    unittest.main()
