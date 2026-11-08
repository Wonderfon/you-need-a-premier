import json
import sys
from datetime import datetime, timezone
from unittest import mock

import backfill_ledger as bf
import review_packet as rp
import review_stats as rs
import tick
from test_tick import TickCase


class MonthlyTicks(TickCase):
    def stream(self, path, text="完成 done"):
        path.parent.mkdir(parents=True, exist_ok=True)
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "C:/w/incore/audits/monthly.md"}}]}},
            {"type": "result", "is_error": False, "result": text, "total_cost_usd": 0.5,
             "usage": {"input_tokens": 3, "output_tokens": 5}}]
        path.write_text("broken\n[]\n" + "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")

    def test_utc_month_boundary_and_real_writer_creates_parents(self):
        for stamp, month in (("2026-10-31T23:59:00+00:00", "2026-10"),
                             ("2026-11-01T00:00:00+00:00", "2026-11")):
            with self.subTest(stamp=stamp), mock.patch.object(tick, "datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime.fromisoformat(stamp)
                path = tick.tick_path("unchanged-report-life.jsonl")
                clock.now.assert_called_once_with(timezone.utc)
                self.assertEqual(path, tick.TICKS_DIR / month / "unchanged-report-life.jsonl")
                self.assertFalse(path.parent.exists())
                argv = [sys.executable, "-c", 'print(\'{"type":"result","result":"done"}\')']
                with mock.patch.object(tick, "build_argv", return_value=argv), \
                        mock.patch.object(tick, "WORKSPACE", self.d), \
                        mock.patch.object(tick, "CREATE_NO_WINDOW", 0):
                    self.assertEqual(tick.run_claude("review", "offline", path), 0)
                self.assertEqual(tick.final_result(path), (True, "done", False))

    def test_session_retry_across_month_uses_new_month_and_original_filename_stamp(self):
        current = datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)
        paths = []

        def run(_kind, _prompt, path, *_args, **_kwargs):
            nonlocal current
            paths.append(path)
            if len(paths) == 1:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"type": "stderr", "message": "No conversation found for session ID: old"}),
                                encoding="utf-8")
                current = datetime(2026, 11, 1, tzinfo=timezone.utc)
                return 1
            self.stream(path)
            return 0

        with mock.patch.object(tick, "datetime", wraps=datetime) as clock, \
                mock.patch.object(tick, "run_claude", side_effect=run):
            clock.now.side_effect = lambda *_args: current
            code, path, sid, resumed = tick.session_run("review", "offline", "report-life", "old", True, None)
        self.assertEqual((code, resumed), (0, False))
        self.assertNotEqual(sid, "old")
        self.assertEqual(paths[0], tick.TICKS_DIR / "2026-10" / "20261031-235900-report-life.jsonl")
        self.assertEqual(path, tick.TICKS_DIR / "2026-11" / "20261031-235900-report-life-retry.jsonl")
        self.assertTrue(all(p.is_file() for p in paths))

    def test_compact_lessons_and_codex_also_use_monthly_paths(self):
        paths = []

        def run(_kind, _prompt, path, *_args, **_kwargs):
            paths.append(path)
            self.stream(path)
            return 0

        def codex(_prompt, path, *_args, **_kwargs):
            paths.append(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"type": "turn.completed", "usage": {}}), encoding="utf-8")
            return 0

        with mock.patch.object(tick, "datetime", wraps=datetime) as clock, \
                mock.patch.object(tick, "run_claude", side_effect=run), \
                mock.patch.object(tick, "codex_exec", side_effect=codex), \
                mock.patch.object(tick, "executor_launch", return_value={}), \
                mock.patch.object(tick, "record_run", return_value={"context_tokens": 100}), \
                mock.patch.object(tick, "update_deputy"):
            clock.now.return_value = datetime(2026, 11, 1, tzinfo=timezone.utc)
            tick.maybe_compact("life", {"context_tokens": tick.COMPACT_TOKENS + 1, "session_id": "old", "section": "生活线"},
                               "sonnet", "compact-life")
            tick.retire_executor("life", "生活线", "writing", {"session_id": "old"})
            tick.run_codex("life", "生活线", "写作模型：offline", "writing", ("gpt-6.1-sol", ("high",), 1, False))
        self.assertEqual(len(paths), 3)
        self.assertEqual({p.parent for p in paths}, {tick.TICKS_DIR / "2026-11"})
        self.assertTrue(all(p.is_file() for p in paths))

    def test_direct_readers_and_daily_report_accept_both_layouts(self):
        paths = [tick.TICKS_DIR / "20261031-235900-executor.jsonl",
                 tick.TICKS_DIR / "2026-11" / "20261031-200000-executor.jsonl"]
        now = datetime.now(timezone.utc)
        rows = []
        for i, path in enumerate(paths):
            with self.subTest(path=path):
                self.stream(path)
                self.assertEqual(tick.final_result(path), (True, "完成 done", True))
                self.assertEqual(tick.result_stats(path)["cost_usd"], 0.5)
                self.assertEqual(rs.written_audits(str(path)), ["monthly.md"])
                self.assertTrue(rp.run_wrote(path))
                rows.append({"_at": now, "kind": "executor", "role": "life", "section": "生活线",
                             "item": f"写作模型：{i}", "run_file": str(path), "cost_usd": 0.5})
        report = rs.daily(rows, "", now.astimezone().date(), "生活线")
        self.assertIn("monthly.md", report)
        self.assertIn("今天执行者写的结果文件 audits/（1；", report)
        self.assertIn("| 生活线 | 2 | 0 | 0 | $1.00 |", report)
        tick.RUNS_FILE.write_text("\n".join(json.dumps({"at": now.isoformat(), **{k: v for k, v in r.items() if k != "_at"}})
                                              for r in rows), encoding="utf-8")
        with mock.patch.multiple(rs, DATA=self.d, BACKLOG=tick.BACKLOG):
            replay = rs.day_facts(now.astimezone().date(), "生活线", now=now)
        self.assertIn("monthly.md", replay)
        self.assertIn("今天执行者写的结果文件 audits/（1；", replay)
        self.assertIn("| 生活线 | 2 | 0 | 0 | $1.00 |", replay)

    def test_backfill_finds_flat_and_monthly_reports_and_absolute_log_paths_once(self):
        paths = [tick.TICKS_DIR / "20261031-235900-report-life.jsonl",
                 tick.TICKS_DIR / "2026-11" / "20261101-000000-report-life.jsonl",
                 tick.TICKS_DIR / "2026-11" / "20261101-000000-report-life-retry.jsonl",
                 tick.TICKS_DIR / "20261031-235900-executor.jsonl",
                 tick.TICKS_DIR / "2026-11" / "20261101-000000-executor.jsonl"]
        for path in paths:
            self.stream(path)
        before = {p.relative_to(tick.TICKS_DIR): p.read_bytes() for p in paths}
        entries = [{"at": "2026-11-01T00:00:00+00:00", "decision": "ran", "kind": "executor",
                    "section": "生活线", "run_file": str(p), "exit_code": 0} for p in paths[3:]]
        tick.LOG_FILE.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
        with mock.patch.multiple(bf, TICKS_DIR=tick.TICKS_DIR, LOG_FILE=tick.LOG_FILE, RUNS_FILE=tick.RUNS_FILE):
            self.assertEqual(bf.backfill(), 5)
            self.assertEqual(bf.backfill(), 0)
        rows = [json.loads(line) for line in tick.RUNS_FILE.read_text(encoding="utf-8").splitlines()]
        self.assertEqual({r["run_file"] for r in rows}, {str(p) for p in paths})
        self.assertEqual(sum(r["cost_usd"] for r in rows), 2.5)
        self.assertEqual(before, {p.relative_to(tick.TICKS_DIR): p.read_bytes() for p in tick.TICKS_DIR.rglob("*.jsonl")})
