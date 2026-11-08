"""Before/after archive checks through each historical reader, on independent files."""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import backlog_archive as a
import backlog_history as h
import backlog_io as b
import local_secretary as secretary
import review_stats as rs
import tick

spec = importlib.util.spec_from_file_location("archive_input", Path(__file__).resolve().parents[1] / "input/__init__.py")
input_package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = input_package
spec.loader.exec_module(input_package)
from archive_input import dashboard, pending, review, server

NOW = datetime(2026, 11, 2, 17, tzinfo=timezone.utc)
TODAY = date(2026, 11, 2)
COMMAND = "`Start-Process 'C:\\demo\\sample.html'`"
FIXTURE = (
    "# Tasks\r\n## 你的指令（最优先）\r\n"
    "- [done 10-20] 组织：原话今天收到（10-20 记完成）〔owner Discord 2026-11-02 12:00；coordinator〕\r\n"
    "## 组织线\r\n份额：22%\r\n"
    "- [done 10-20：审查通过，等合并；给 owner\x2010-20：看，audits/sample.html] 组织：heliostat（旧成果） " + COMMAND + "\r\n"
    "- [done 10-26：审查通过；给 owner\x2010-26：看，audits/boundary.html] 组织：边界（第八天） " + COMMAND + "\r\n"
    "- [done 10-01：总理答 已核对] 组织：guard 〔主目录 HEAD：claude/test-history〕\r\n"
    "- [done 10-27：审查通过] 组织：保留（最新） " + COMMAND + "\r\n"
    "- [blocked: 等你决定] 组织：选项 A 保留 B 删除\r\n"
    "- [todo] 组织：下一项\r\n"
    "## 工作线\r\n份额：24%\r\n- [done 10-19] FOS：旧工作线（保持映射）\r\n"
    "## 生活线\r\n份额：24%\r\n- [done 10-20] 写作：sparse（稀疏旧成果一）\r\n"
    "- [done 10-21] 写作：sparse（稀疏旧成果二）\r\n"
)


def payload(items):
    return [{k: v for k, v in item.items() if k not in ("line", "source", "n")} for item in items]


class ArchiveReaders(unittest.TestCase):
    def test_production_package_import_without_top_level_control_or_input(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            repo = Path(__file__).resolve().parents[1]
            for name in ("input", "control"):
                (workspace / "incore" / name).mkdir(parents=True)
            for folder_name in ("input", "input/asr_bench"):
                target = workspace / "incore" / folder_name
                target.mkdir(parents=True, exist_ok=True)
                for source in (repo / folder_name).glob("*.py"):
                    shutil.copyfile(source, target / source.name)
            for name in ("backlog_history.py", "instance_config.py"):  # input/*.py import instance_config from ../control
                shutil.copyfile(repo / "control" / name, workspace / "incore/control" / name)
            code = (
                "from datetime import date; from pathlib import Path; import sys; import incore.input.server as server; "
                "from incore.input import dashboard, pending, review; "
                "assert 'control' not in sys.modules and 'backlog_history' not in sys.modules; "
                "root = Path('incore'); "
                "assert dashboard._plans(root)['state'] == 'missing'; "
                "assert pending.recent_results(root) == []; assert review.trials(root, date.today()) == []; "
                "print('production package import and three history readers: OK')"
            )
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code], cwd=workspace,
                                    env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "projects/backlog.md"
        self.path.parent.mkdir()
        self.path.write_bytes(FIXTURE.encode("utf-8"))
        (self.root / "records").mkdir()
        (self.root / "audits").mkdir()
        for obj, key, value in ((tick, "BACKLOG", self.path), (rs, "BACKLOG", self.path),
                                (tick, "note_malformed", lambda *args: None),
                                (tick, "note_window_skipped", lambda *args: None)):
            patcher = patch.object(obj, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def archive(self):
        return a.archive(self.root, today=TODAY)

    def test_dashboard_accumulated_counts_and_live_queues(self):
        before = dashboard._plans(self.root)["roles"]
        self.archive()
        self.assertEqual(dashboard._plans(self.root)["roles"], before)
        self.assertEqual(next(r for r in before if r["key"] == "work")["done_count"], 1)
        self.assertEqual(pending.extract_pending(self.root, NOW)["total"], 1)

    @patch.object(pending, "RESULT_DAYS", 14)  # exercise history even with master's shorter display window
    def test_results_ids_sort_commands_and_rating_entry_survive(self):
        before = pending.recent_results(self.root, NOW)
        self.archive()
        after = pending.recent_results(self.root, NOW)
        self.assertEqual(payload(after), payload(before))
        archived = next(it for it in after if "边界" in it["title"])
        self.assertTrue(archived["source"].endswith("backlog-archive/2026-10.md"))
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz)
        with patch.object(pending, "datetime", Clock):
            saved = pending.save_rating(self.root, self.root / "data", archived["id"], "用", "保留")
        self.assertEqual(saved["id"], archived["id"])
        snap = server._pending_snapshot(self.root, self.root / "data", "test", self.root / "wt")
        self.assertIn("links", snap["results"][0])

    def test_trials_inclusive_eighth_date_and_real_source(self):
        before = review.trials(self.root, TODAY)
        self.archive()
        after = review.trials(self.root, TODAY)
        self.assertEqual(payload(after), payload(before))
        boundary = next(row for row in after if row["day"] == date(2026, 10, 26))
        self.assertEqual(Path(boundary["source"]).name, "2026-10.md")
        self.assertIn("边界", Path(boundary["source"]).read_text(encoding="utf-8").splitlines()[boundary["line"] - 1])

    @patch.object(pending, "RESULT_DAYS", 14)
    def test_nested_bracket_receipts_and_same_day_cards_keep_all_results(self):
        text = ("## 组织线\n- [done 10-26：审查通过，检查[x]无误；给 owner\x2010-26：看] 组织：旧卡 " + COMMAND
                + "\n- [done 16:56；给 owner\x2010-26：看] 组织：未记完成日卡 " + COMMAND
                + "\n## 生活线\n- [done 16:57；给 owner\x2010-26：看] 写作：另一线卡 " + COMMAND + "\n")
        self.path.write_text(text, encoding="utf-8")
        before = review.trials(self.root, TODAY)
        self.assertEqual(len(before), 3)
        self.assertEqual(len(pending.recent_results(self.root, NOW)), 1)
        self.archive()
        self.assertEqual(payload(review.trials(self.root, TODAY)), payload(before))
        self.assertEqual(len(pending.recent_results(self.root, NOW)), 1)

    def test_hourly_hook_archives_once_and_uses_one_lock_order(self):
        calls = []
        with patch.object(b, "_last_clear", [float("-inf")]):
            for hour, at in ((3, 0), (4, 3600), (4, 3610), (5, 7200)):
                def run(hour=hour):
                    result = a.daily(self.path, datetime(2026, 11, 2, hour))
                    if result is not None:
                        calls.append(result)
                b.clear_seen_owner_cards_hourly(at, path=self.path, records_dir=self.root / "records", now=NOW, archive=run)
        self.assertEqual(len(calls), 1)
        self.assertGreater(calls[0]["moved_rows"], 0)
        self.assertFalse(list(self.root.rglob("*.lock")))

    def test_snapshot_old_merge_count_and_active_rows(self):
        before = rs.review_snapshot(FIXTURE, [], tick, NOW, branches=[])
        before_diff = rs.diff_section(before, None)
        self.archive()
        after = rs.review_snapshot(self.path.read_text(encoding="utf-8"), [], tick, NOW, branches=[])
        self.assertEqual(payload([r for r in before["rows"] if r["status"] != "done"]),
                         payload([r for r in after["rows"] if r["status"] != "done"]))
        self.assertEqual(len(before["rows"]), len(after["rows"]) + len(after["history_done"]))
        self.assertIn("旧等合并引用：1 行", before_diff)
        self.assertIn("旧等合并引用：1 行", rs.diff_section(after, None))
        self.assertEqual(rs.branch_facts(after["rows"], tick), [])

    def test_daily_old_item_and_unexpired_owner_cards(self):
        context = {"reports": {}, "records": {"rows": [], "gaps": []}, "card_records": [], "replies": []}
        runs = [{"_at": NOW, "kind": "review", "role": "coordinator", "section": "组织线",
                 "item": "组织：heliostat（旧成果） " + COMMAND, "verdict": "pass"}]
        before = rs.daily(runs, FIXTURE, TODAY, root=self.root, now=NOW, context=context)
        self.archive()
        after = rs.daily(runs, self.path.read_text(encoding="utf-8"), TODAY, root=self.root, now=NOW, context=context)
        clean = lambda text: re.sub(r"（(?:backlog|[^（）\n]*backlog-archive/\d{4}-\d{2}\.md):\d+）", "", text)
        self.assertEqual(clean(before), clean(after))
        self.assertIn("成果卡（待 owner 动手 2；已答／已看 0", after)
        self.assertIn("backlog-archive", after)

    def test_hourly_clears_historic_cards_with_backup_and_crlf(self):
        self.archive()
        month = self.path.parent / "backlog-archive/2026-10.md"
        before = month.read_bytes()
        (self.root / "records\x2f2026-11-02-answer.json").write_text('{"owner_text":"audits/sample.html"}', encoding="utf-8")
        with patch.object(b, "_last_clear", [float("-inf")]):
            changed = b.clear_seen_owner_cards_hourly(10000, path=self.path, records_dir=self.root / "records", now=NOW)
        self.assertEqual(len(changed), 1)
        self.assertIn("owner 已看 11-02", month.read_text(encoding="utf-8"))
        self.assertIn(b"\r\n", month.read_bytes())
        self.assertIn(before, [p.read_bytes() for p in month.parent.glob("backlog-history/backlog-*.md")])
        self.assertNotIn(b"heliostat", self.path.read_bytes())

    def test_secretary_history_lookup_hash_bounds_and_sparse_recent_two(self):
        before = secretary.make_packet(self.root, "heliostat")
        overview = secretary.make_packet(self.root, "各线进展")
        self.archive()
        after = secretary.make_packet(self.root, "heliostat")
        src = next(s for s in after["sources"] if s["path"].startswith("projects/backlog-archive/"))
        self.assertEqual([it["text"] for it in src["lines"]], [it["text"] for it in before["sources"][0]["lines"]])
        self.assertEqual(src["sha256"], hashlib.sha256((self.root / src["path"]).read_bytes()).hexdigest())
        sparse = lambda p: payload(next(r for r in p["host_overview"] if r["lane"] == "生活线")["recent_done"])
        self.assertEqual(sparse(overview), sparse(secretary.make_packet(self.root, "各线进展")))
        for bad in ("projects/backlog-archive/2026-13.md", "projects/backlog-archive/other.md",
                    "projects/backlog-archive/../backlog.md"):
            with self.assertRaises(secretary.Rejected):
                secretary.source_path(self.root, bad)

    def test_notice_location_and_guard_history_marker(self):
        notice = {"section": "组织线", "item": "组织：guard 〔主目录 HEAD：claude/test-history〕",
                  "line_no": 8, "project": "组织", "question": "guard", "answer": "已核对"}
        self.archive()
        rendered = tick.format_ask_notice(notice)
        self.assertIn("projects/backlog-archive/2026-10.md 第", rendered)
        before = self.path.read_bytes()
        with patch.object(tick.review_packet, "git", return_value="claude/test-history"), patch.object(tick, "log") as log:
            tick.main_worktree_guard()
        self.assertEqual(self.path.read_bytes(), before)
        log.assert_not_called()

    def test_owner_message_time_filter_after_completion_was_archived(self):
        # Use the scheduler's actual source/time grammar, including an old completion date.
        line = '- [done 10-20] 组织：新的原话【Discord·coordinator】2026-11-02T16:30:00+00:00'
        text = "## 你的指令（最优先）\n" + line + "\n## 组织线\n"
        self.path.write_text(text, encoding="utf-8")
        with patch.object(tick, "terminal_owner_words", return_value=[]):
            before = tick.owner_words_block(text, "work", NOW)
            self.assertIn("新的原话", before)
            self.archive()
            after = tick.owner_words_block(self.path.read_text(encoding="utf-8"), "work", NOW)
        self.assertEqual(before, after)
        with patch.object(tick, "terminal_owner_words", return_value=[]):
            block = tick.owner_words_block("## 你的指令（最优先）\n- 【Discord·coordinator】2026-11-02T16:35:00Z 原始非任务原话\n", "work", NOW)
        self.assertIn("原始非任务原话", block)

    def test_daily_after_four_once_even_after_restart_then_next_day(self):
        self.assertIsNone(a.daily(self.path, datetime(2026, 11, 2, 3, 59)))
        result = a.daily(self.path, datetime(2026, 11, 2, 4))
        self.assertGreater(result["moved_rows"], 0)
        before = self.path.read_bytes()
        with patch.object(a, "archive", side_effect=AssertionError("must not rerun")):
            self.assertIsNone(a.daily(self.path, datetime(2026, 11, 2, 5)))
            self.assertIsNone(a.daily(self.path, datetime(2026, 11, 2, 23, 59)))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(a.daily(self.path, datetime(2026, 11, 3, 4))["moved_rows"], 1)

    def test_parse_failure_leaves_source_and_archives_exactly_unchanged(self):
        self.archive()
        before = self.path.read_bytes()
        month = self.path.parent / "backlog-archive/2026-10.md"
        old_month = month.read_bytes()
        with patch.object(tick, "parse_backlog", side_effect=ValueError("invalid parse")):
            with self.assertRaisesRegex(ValueError, "invalid parse"):
                a.archive(self.root, today=TODAY + timedelta(days=1))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(month.read_bytes(), old_month)
        self.assertIn(before, [p.read_bytes() for p in self.path.parent.glob("backlog-history/backlog-*.md")])

    def test_failed_daily_attempt_is_not_retried_or_cleared_while_pending(self):
        before = self.path.read_bytes()
        with patch.object(a, "archive", side_effect=OSError("disk full")) as run:
            with self.assertRaises(OSError):
                a.daily(self.path, datetime(2026, 11, 2, 4))
            self.assertIsNone(a.daily(self.path, datetime(2026, 11, 2, 5)))
            self.assertEqual(run.call_count, 1)
        self.assertEqual(self.path.read_bytes(), before)
        journal = self.path.with_name("backlog-archive-pending.json")
        journal.write_text("{}", encoding="utf-8")
        with patch.object(b, "_last_clear", [float("-inf")]), patch.object(b, "clear_seen_owner_cards") as clear:
            with self.assertRaisesRegex(ValueError, "unfinished archive"):
                b.clear_seen_owner_cards_hourly(0, path=self.path, archive=lambda: None)
        clear.assert_not_called()

    def test_interrupted_second_batch_readers_do_not_double_count_old_duplicates(self):
        self.archive()
        line = "- [done 10-21] 写作：sparse（稀疏旧成果二）\r\n"
        self.path.write_bytes(self.path.read_bytes() + line.encode("utf-8"))
        before = list(h.all_rows(self.path))
        real_write = a._atomic_write
        def fail(path, data):
            if path == self.path:
                raise OSError("interrupt before commit")
            real_write(path, data)
        with patch.object(a, "_atomic_write", side_effect=fail), self.assertRaises(OSError):
            self.archive()
        self.assertEqual(payload(list(h.all_rows(self.path))), payload(before))
        self.archive()
        self.assertEqual(len(list(h.all_rows(self.path))), len(before))


if __name__ == "__main__":
    unittest.main()
