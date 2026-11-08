import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from unittest import mock

import review_stats as rs


NOW = datetime(2026, 10, 30, 20, tzinfo=timezone.utc)
WIN = r"C:\sandbox\incore"


class DailyPaths(unittest.TestCase):
    def test_missed_window_is_one_line_scoped_read_only_and_clears_on_completion(self):
        local = NOW.astimezone().replace(tzinfo=None)
        day = (local - timedelta(days=1)).strftime("%Y-%m-%d")
        row = f"- [todo] 组织：到点恢复〔时段：10:50–13:00〕〔窗口日期：{day}〕"
        backlog = "## 组织线\n" + row + "\n"
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        text = self.render(backlog)
        self.assertEqual(text.count("到点行错过：原定 10:50、窗口已过、还没执行"), 1)
        self.assertNotIn("到点行错过", self.render(backlog.replace("[todo]", "[done 13:01]")))
        other = rs.daily([], backlog, local.date(), "生活线", root=self.root, now=NOW)
        self.assertNotIn("到点行错过", other)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.since = NOW.astimezone().replace(tzinfo=None) - timedelta(hours=8)

    def put(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def render(self, backlog, runs=(), **kwargs):
        return rs.daily(list(runs), backlog, NOW.astimezone().date(), "组织线", since=self.since,
                        root=self.root, now=NOW, windows_root=WIN, **kwargs)

    def record(self, name, path, words, **extra):
        return self.put("records/" + name, json.dumps({"context": path, "owner_verbatim": words, **extra},
                                                     ensure_ascii=False, indent=2))

    def test_answered_card_uses_existing_rule_and_latest_exact_reply_without_writing(self):
        self.put("audits/a/same.html", "result")
        self.record("2026-10-29-first.json", "audits/a/same.html", "先看")
        latest = self.record("2026-10-30-new.json", "audits/a/same.html", "先测，别上线")
        self.record("2026-10-30-other.json", "audits/b/same.html", "无关")
        backlog = "## 组织线\n- [done 10-29；给 owner\x2010-29：看，audits/a/same.html] 组织：成果\n"
        run = {"kind": "review", "verdict": "pass", "section": "组织线", "item": "组织：成果", "_at": NOW}
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with mock.patch.object(rs.backlog_io, "cited_by", wraps=rs.backlog_io.cited_by) as rule:
            text = self.render(backlog, [run])
        self.assertTrue(rule.called)
        self.assertIn("已答卡／owner 已看（清卡判定；不表示办完）", text)
        self.assertIn(latest.name + ":3", text)
        self.assertNotIn("other.json", text)
        self.assertIn(r"Start-Process 'C:\sandbox\incore\audits\a\same.html'", text)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_path_identity_sent_only_and_window_filter_keep_same_named_files(self):
        for rel in ("audits/a/same.html", "audits/b/same.html"):
            self.put(rel, rel)
        slot = self.since.strftime("%H:%M")
        date = self.since.date().isoformat()
        self.put("control/data/reports.json", json.dumps({date: {
            f"work@{slot}": "sent", f"text:work@{slot}": r"Start-Process 'C:\sandbox\incore\audits\a\same.html'",
            f"life@{slot}": "failed", f"text:life@{slot}": "audits/b/same.html",
            "push:lunch": "sent", "text:push:lunch": "audits/b/same.html"}}, ensure_ascii=False))
        text = self.render("## 组织线\n- [blocked: 等你决定] 组织：a audits/a/same.html\n"
                           "- [blocked: 等你决定] 组织：b audits/b/same.html\n")
        self.assertEqual(text.count("本时段已报（"), 1)
        self.assertEqual(text.count("本时段未报（"), 1)
        self.assertIn("同名不同文件：", text)
        self.assertIn("不消重", text)
        # Moving the only sent entry outside this period must not claim a repeat.
        self.since += timedelta(minutes=1)
        self.assertNotIn("本时段已报（", self.render("## 组织线\n- [blocked: 等你决定] 组织：a audits/a/same.html\n"))

    def test_missing_paths_files_and_windows_mapping_are_explicit(self):
        text = self.render("## 组织线\n- [blocked: 等你决定] 组织：没有路径\n"
                           "- [blocked: 等你决定] 组织：缺文件 audits/absent.html\n")
        for word in ("缺路径", "缺文件", "不可生成", "没有可确认的新结果", "是否已报未知"):
            self.assertIn(word, text)
        self.assertIn("09:00／21:00 日报照常", text)
        file = rs.fact_file("audits/missing.html", self.root, "relative/root")
        self.assertIn("Windows 路径不完整", file["check"])
        self.assertIsNone(file["command"])
        self.assertIsNone(rs.windows_path(r"C:partial\result.html"))
        drive = "d"
        mounted = "/".join(("", "mnt", drive, "work", "a.html"))
        self.assertEqual(rs.windows_path(mounted), r"D:\work\a.html")

    def test_each_item_expands_one_prioritized_result_and_counts_other_references(self):
        self.put("audits/owner.html", "owner result")
        self.put("audits/run.md", "executor result")
        self.put("audits/row.html", "row reference")
        stream = self.put("run.jsonl", json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": str(PureWindowsPath(WIN) / "audits" / "run.md")}}]}}))
        runs = [{"kind": "executor", "section": "组织线", "item": "组织：成果", "_at": NOW, "run_file": str(stream)}]
        backlog = ("## 组织线\n- [done 10-29；给 owner\x2010-29：看 audits/owner.html] "
                   "组织：成果 audits/row.html wt/drafts/check.py records\x2f2026-10-29-answer.json\n")
        text = self.render(backlog, runs)
        self.assertIn(r"C:\sandbox\incore\audits\owner.html", text)
        self.assertNotIn(r"C:\sandbox\incore\audits\run.md", text)
        self.assertNotIn("wt/drafts/check.py", text)
        self.assertNotIn("records\x2f2026-10-29-answer.json", text.split("成果／引用：")[-1])
        self.assertIn("另 2 个引用（未展开）", text)

    def test_done_ref_is_completed_and_globs_never_become_missing_files(self):
        run = {"kind": "review", "verdict": "pass", "section": "组织线", "item": "组织：done ref", "_at": NOW}
        text = self.render("## 组织线\n- [done-ref 10-29] 组织：done ref\n"
                           "- [blocked: 等你决定] 组织：通配 audits/result*.md\n", [run])
        self.assertIn("待行动：已完成；未见新的 owner 待行动卡", text)
        self.assertNotIn("未知，需核对当前行", text)
        self.assertIn("通配路径，不生成打开命令：audits/result*.md", text)
        self.assertNotIn("Start-Process 'C:\\sandbox\\incore\\audits\\result*.md'", text)
        self.assertNotIn("缺文件", text)

    def test_only_pending_owner_cards_expand_answered_and_seen_cards_are_counted(self):
        self.put("audits/answered.html", "answered")
        self.record("2026-10-30-answer.json", "audits/answered.html", "看过")
        backlog = ("## 组织线\n"
                   "- [done 10-29；给 owner\x2010-29：看 audits/answered.html] 组织：已答\n"
                   "- [done 10-29；owner 已看 10-29] 组织：已看\n"
                   "- [done 10-30；给 owner\x2010-30：看 audits/pending.html] 组织：待看\n")
        text = self.render(backlog)
        self.assertIn("成果卡（待 owner 动手 1；已答／已看 2；只读）", text)
        card_section = text.split("成果卡（", 1)[1].split("本线冲刺行", 1)[0]
        self.assertIn("组织：待看", card_section)
        self.assertNotIn("组织：已答", card_section)
        self.assertNotIn("组织：已看", card_section)

    def test_handled_markers_with_optional_dates_ignore_historical_owner_card(self):
        for marker in ("owner 已处理", "owner 已答", "owner已看"):
            for day in ("", " 10-29"):
                with self.subTest(marker=marker, day=day):
                    backlog = (f"## 组织线\n- [done 10-29；{marker}{day}；原状态："
                               "done 10-28；给 owner\x2010-28：看 audits/old.html] 组织：旧成果\n")
                    text = self.render(backlog)
                    self.assertIn("成果卡（待 owner 动手 0；已答／已看 1；只读）", text)

    def test_historical_tags_and_handled_markers_do_not_affect_current_card(self):
        backlog = ("## 组织线\n"
                   "- [done 10-29；已被新版取代；原状态：done 10-28；给 owner\x2010-28：看旧版] 组织：旧版\n"
                   "- [done 10-30；给 owner\x2010-30：看新版；原状态：done 10-29；owner 已答] 组织：新版\n")
        text = self.render(backlog)
        self.assertIn("成果卡（待 owner 动手 1；已答／已看 0；只读）", text)
        card_section = text.split("成果卡（", 1)[1].split("本线冲刺行", 1)[0]
        self.assertIn("组织：新版", card_section)
        self.assertNotIn("组织：旧版", card_section)

    def test_a_single_review_file_is_allowed_but_multiple_reviews_are_not_expanded(self):
        self.put("audits/only-review.md", "review")
        only = self.render("## 组织线\n- [blocked: 等你决定] 组织：只有 audits/only-review.md\n")
        self.assertIn(r"Start-Process 'C:\sandbox\incore\audits\only-review.md'", only)
        self.put("audits/second-review.md", "review")
        multiple = self.render("## 组织线\n- [blocked: 等你决定] 组织：多个 audits/only-review.md audits/second-review.md\n")
        self.assertNotIn("only-review.md（存在）", multiple)
        self.assertNotIn("second-review.md（存在）", multiple)
        self.assertIn("另 2 个引用（未展开）", multiple)
        self.assertIn("有引用但没有可选成果文件", multiple)
        self.assertNotIn("缺路径", multiple)

    def test_coordinator_and_line_fact_blocks_stay_inside_character_budgets(self):
        sections = ("组织线", "生活线")
        lines, runs = [], []
        for sec_index, section in enumerate(sections):
            lines.append("## " + section)
            for i in range(8):
                item = f"{section}：待决定 {i} " + " ".join(
                    (f"audits/{sec_index}-{i}-{j}.html" if j != 1 else f"audits/{sec_index}-{i}-{j}-review.md")
                    for j in range(4)) + f" wt/drafts/{sec_index}-{i}.py"
                lines.append(f"- [blocked: 等你决定] {item}")
            for i in range(8):
                item = f"{section}：给 owner 看 {i} " + f"audits/{sec_index}-card-{i}.html"
                lines.append(f"- [done 10-29；给 owner\x2010-29：看 audits/{sec_index}-card-{i}.html] {item}")
            runs.extend({"kind": "review", "verdict": "pass", "section": section,
                         "item": f"{section}：待决定 {i} " + " ".join(
                             (f"audits/{sec_index}-{i}-{j}.html" if j != 1 else f"audits/{sec_index}-{i}-{j}-review.md")
                             for j in range(4)) + f" wt/drafts/{sec_index}-{i}.py", "_at": NOW}
                        for i in range(8))
        backlog = "\n".join(lines) + "\n"
        day = NOW.astimezone().date()
        context = rs.daily_context(self.root, NOW)
        coordinator = rs.daily(runs, backlog, day, None, since=self.since, root=self.root, now=NOW,
                               context=context, windows_root=WIN)
        self.assertLessEqual(len(coordinator), 9000)
        for section in sections:
            line = rs.daily(runs, backlog, day, section, since=self.since, root=self.root, now=NOW,
                            context=context, windows_root=WIN)
            self.assertLessEqual(len(line), 6000, section)

    def test_runtime_cap_keeps_exact_limit_and_replaces_whole_items(self):
        lines = ["头" * 40, "项" * 80, "尾" * 20]
        spans = [(1, 2)]
        full = "\n".join(lines) + "\n\n"
        self.assertEqual(rs.cap_daily(lines, spans, len(full)), full)
        one_over = rs.cap_daily(lines, spans, len(full) - 1)
        self.assertLessEqual(len(one_over), len(full) - 1)
        self.assertIn("另 1 条（超出字数上限，未展开）", one_over)
        self.assertNotIn("项", one_over)

        many = ["头" * 100, "甲" * 150, "中" * 60, "乙" * 150, "尾" * 60]
        many_full = "\n".join(many) + "\n\n"
        trimmed = rs.cap_daily(many, [(1, 2), (3, 4)], len(many_full) - 180)
        self.assertLessEqual(len(trimmed), len(many_full) - 180)
        self.assertIn("另 2 条（超出字数上限，未展开）", trimmed)
        self.assertNotIn("甲", trimmed)
        self.assertNotIn("乙", trimmed)

    def test_daily_applies_runtime_character_cap(self):
        with mock.patch.object(rs, "LINE_FACT_MAX", 240):
            text = self.render("## 组织线\n- [blocked: 等你决定] 组织：待确认\n")
        self.assertLessEqual(len(text), 240)
        self.assertIn("超出字数上限，未展开", text)

    def test_input_reply_is_exact_scoped_and_future_replies_are_ignored(self):
        self.put("data/owner-replies.json", json.dumps({"records": [
            {"at": (NOW - timedelta(hours=1)).isoformat(), "section": "组织线", "head": "组织：选择", "status": "new"},
            {"at": (NOW + timedelta(hours=1)).isoformat(), "section": "组织线", "head": "组织：选择", "status": "done"}]}))
        text = self.render("## 组织线\n- [blocked: 等你决定] 组织：选择\n")
        self.assertIn("待行动：已回复；待执行者处理", text)
        self.assertIn("data/owner-replies.json:records[0]", text)
        self.assertNotIn("records[1]", text)

    def test_executor_stream_keeps_full_worktree_path_and_links_without_review(self):
        path = str(PureWindowsPath(WIN) / "audits" / "result.md")
        self.put("audits/result.md", "result")
        stream = self.put("run.jsonl", json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": path}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": r"D:\wt\branch\audits\result.md"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": r"D:\wt\branch\audits\only-read.md"}}]}}))
        runs = [{"kind": "executor", "section": "组织线", "item": "组织：运行", "_at": NOW, "run_file": str(stream)}]
        text = self.render("## 组织线\n- [review] 组织：运行\n", runs)
        self.assertIn("待审查员验收", text)
        self.assertIn(path, text)
        self.assertNotIn(r"D:\wt\branch\audits\result.md", text)
        self.assertIn("另 1 个引用（未展开）", text)
        self.assertIn("同名不同文件", text)
        self.assertNotIn("only-read.md", text)
        self.assertNotIn("没有可确认的新结果", text)
        runs[0].update(kind="review", verdict="pass")
        self.assertIn("没有可确认的新结果", self.render("## 组织线\n- [review] 组织：运行\n", runs))

    def test_executor_stream_finds_codex_path_write_but_not_read(self):
        stream = self.put("codex-run.jsonl", json.dumps({"type": "item.completed", "item": {
            "type": "command_execution",
            "command": "from pathlib import Path\n"
                       "source = Path('audits/cause.md').read_text(encoding='utf-8')\n"
                       "result = Path('audits/result.md')\n"
                       "result.write_text(source, encoding='utf-8')\n"}}) + "\n")
        self.assertEqual(rs.written_audits(stream, full_paths=True), ["audits/result.md"])

    def test_review_row_prefers_this_period_executor_result_over_cause_review(self):
        result = r"C:\sandbox\incore\audits\2026-11-02-merge-prep-review.md"
        cause = "audits/2026-11-02-org-batch-review-0805.md"
        self.put("audits/2026-11-02-merge-prep-review.md", "executor result")
        self.put(cause, "cause review")
        stream = self.put("run.jsonl", json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": result}}]}}))
        item = f"组织：merge-prep {cause}"
        run = {"kind": "executor", "section": "组织线", "item": item, "_at": NOW, "run_file": str(stream)}
        text = self.render(f"## 组织线\n- [review] {item}\n", [run])
        selected = next(line for line in text.splitlines() if "成果／引用：" in line)
        self.assertIn("2026-11-02-merge-prep-review.md", selected)
        self.assertNotIn("org-batch-review-0805.md", selected)

    def test_current_row_ambiguity_and_same_day_reply_order_are_not_invented(self):
        self.record("2026-10-30-a.json", "audits/a.html", "A")
        self.record("2026-10-30-b.json", "audits/a.html", "B")
        runs = [{"kind": "review", "verdict": "pass", "section": "组织线", "item": "组织：做 audits/a.html", "_at": NOW}]
        text = self.render("## 组织线\n- [todo] 组织：做 audits/a.html 一\n- [running] 组织：做 audits/a.html 二\n", runs)
        self.assertIn("待行动：未知，需核对当前行", text)
        self.assertIn("同日多份出处，日内先后未知", text)

    def test_future_records_do_not_mark_a_historical_card_seen(self):
        self.record("2026-10-30-future.json", "audits/a.html", "A", received_at=(NOW + timedelta(hours=1)).isoformat())
        run = {"kind": "review", "verdict": "pass", "section": "组织线", "item": "组织：结果", "_at": NOW}
        text = self.render("## 组织线\n- [done 10-29；给 owner\x2010-29：看 audits/a.html] 组织：结果\n", [run])
        self.assertNotIn("已答卡／", text)
        self.assertNotIn("future.json", text)

    def test_list_shaped_historical_record_does_not_break_the_fact_block(self):
        self.put("records\x2f2026-10-29-list.json", json.dumps([{"owner_verbatim": "先看", "context": "audits/a.html"}]))
        run = {"kind": "review", "verdict": "pass", "section": "组织线", "item": "组织：结果", "_at": NOW}
        text = self.render("## 组织线\n- [done 10-29；给 owner\x2010-29：看 audits/a.html] 组织：结果\n", [run])
        self.assertIn("已答卡／", text)
        self.assertIn("records\x2f2026-10-29-list.json", text)

    def test_latest_sent_report_keeps_its_review_file_and_current_action(self):
        self.put("audits/review.md", "review result")
        slot = NOW.astimezone().strftime("%H:%M")
        date = NOW.astimezone().date().isoformat()
        self.put("control/data/reports.json", json.dumps({date: {
            f"coordinator@{slot}": "sent", f"text:coordinator@{slot}": "audits/review.md"}}))
        text = self.render("## 组织线\n- [blocked: 等 owner 合] 组织：合并 audits/review.md\n")
        self.assertIn("本时段已报成果引用（1 个，不重复当作新成果；逐项见上方）", text)
        self.assertIn("待 owner 操作", text)
        self.assertIn("本时段已报（", text)
        self.assertIn(r"Start-Process 'C:\sandbox\incore\audits\review.md'", text)

    def test_already_cleared_card_still_shows_reply_without_requesting_owner_action(self):
        self.record("2026-10-29-reply.json", "audits/a.html", "用")
        run = {"kind": "review", "verdict": "pass", "section": "组织线", "item": "组织：成果", "_at": NOW}
        text = self.render("## 组织线\n- [done 12:03；owner 已看 10-29（records\x2f2026-10-29-reply.json）；原页 audits/a.html] 组织：成果\n", [run])
        self.assertIn("待行动：已完成；owner 已看", text)
        self.assertIn("records\x2f2026-10-29-reply.json:3", text)
        self.assertNotIn("待行动：待 owner", text)


if __name__ == "__main__":
    unittest.main()
