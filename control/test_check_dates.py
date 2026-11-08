"""Synthetic tables only. Temporary files stay under the user's cache."""

from datetime import date
from pathlib import Path
import tempfile
import unittest

from life_dates import build_report, read_rows


TABLE1 = """## 表 1：到期
| 下次核对 | 提前 7 天 | 项目 | 出处 | 日期 | 提前 30 天 |
| --- | --- | --- | --- | --- | --- |
"""
TABLE2 = """\n## 表 2：倒排
| 备注 | 下次核对 | 日期窗口 | 步骤 |
| --- | --- | --- | --- |
"""
TODAY = date(2026, 11, 2)


class CheckDatesTests(unittest.TestCase):
    def setUp(self):
        cache = Path.home() / ".cache" / "codex-sandbox"
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="life-dates-test-", dir=cache)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "dates.md"

    def report(self, first="", second="", today=TODAY):
        self.path.write_text(TABLE1 + first + TABLE2 + second, encoding="utf-8")
        return build_report(self.path, today)

    def test_reordered_columns_and_escaped_pipe_keep_source_line(self):
        self.report("| | 2026-11-02 | OPT | A \\| B | 2026-11-09 | 2026-10-10 |\n")
        row = read_rows(self.path)[0]
        self.assertEqual((row.table, row.line, row.cells["出处"]), (1, 4, "A | B"))
        self.assertIn("[表 1 dates.md:4] OPT｜日期 2026-11-09｜剩 7 天",
                      build_report(self.path, TODAY))

    def test_month_blank_and_text_windows_are_counted_without_guessing(self):
        report = self.report("| | | 空日期 | |   | |\n", """| | | 2027-04（只有月份） | 答辩 |
| | | 答辩日到 2027-05-02 之间 | 修改 |
""")
        self.assertIn("日期列跳过 3 行：空白 1、只有月份 1、文字窗口 1、无效日期 0", report)
        self.assertIn("1. 窗口内日期（0 条）", report)
        self.assertIn("2. 30 天／7 天提醒候选（0 条）", report)

    def test_date_with_time_and_table2_derived_reminders(self):
        report = self.report(second="| | | 2026-11-09 17:00 | 签字页 |\n")
        self.assertIn("[表 2 dates.md:8] 签字页｜日期 2026-11-09｜剩 7 天", report)
        self.assertIn("提前 7 天 2026-11-02（今天）", report)
        self.assertIn("提前 30 天 2026-10-10（已过 23 天）", report)

    def test_reminder_today_and_overdue_before_deadline(self):
        report = self.report("| | 2026-11-02 | OPT | | 2026-11-09 | 2026-10-10 |\n")
        self.assertIn("2. 30 天／7 天提醒候选（2 条）", report)
        self.assertIn("提前 7 天 2026-11-02（今天）｜日期 2026-11-09｜剩 7 天", report)
        self.assertIn("提前 30 天 2026-10-10（已过 23 天）", report)
        self.assertIn("表 1 提醒日校验：0 处不一致", report)

    def test_next_check_today_overdue_and_first_date_only(self):
        report = self.report("| 2026-11-01（后改 2026-12-02） | | 未填到期日 | | | |\n",
                             """| | 2026-11-02（例行） | 2027-05-05 | 授予 |
| | 2026-12-02（上次 2026-10-01） | 2027-05-01 | 典礼 |
""")
        self.assertIn("3. 到期的下次核对（2 条）", report)
        self.assertIn("下次核对 2026-11-01（已过 1 天）", report)
        self.assertIn("下次核对 2026-11-02（今天）", report)
        self.assertNotIn("下次核对 2026-12-02", report)

    def test_both_reminder_columns_mismatch_and_missing_reminder(self):
        report = self.report("""| | 2026-11-03 | 错日 | | 2026-11-09 | 2026-10-11 |
| | | 漏日 | | 2026-11-09 | 2026-10-10 |
""")
        self.assertIn("表 1 提醒日校验：3 处不一致", report)
        self.assertIn("提前 30 天 实填 2026-10-11｜应为 2026-10-10", report)
        self.assertIn("提前 7 天 实填 2026-11-03｜应为 2026-11-02", report)
        self.assertIn("提前 7 天 实填 空白｜应为 2026-11-02", report)

    def test_inclusive_window_and_no_reminders_for_expired_deadlines(self):
        report = self.report(second="""| | | 2026-11-02 | 今天 |
| | | 2026-12-02 | 窗口尾 |
| | | 2026-12-03 | 窗口外 |
| | | 2026-11-01 | 昨天 |
""")
        self.assertIn("1. 窗口内日期（2 条）", report)
        window = report.split("2. 30 天／7 天")[0]
        self.assertIn("今天｜日期 2026-11-02｜剩 0 天", window)
        self.assertIn("窗口尾｜日期 2026-12-02｜剩 30 天", window)
        self.assertNotIn("窗口外", report)
        self.assertNotIn("昨天", report)
        self.assertIn("2. 30 天／7 天提醒候选（1 条）", report)

    def test_invalid_date_is_counted_and_input_bytes_are_unchanged(self):
        report = self.report(second="| | | 2026-02-30 | 无效 |\n")
        before = self.path.read_bytes()
        self.assertIn("无效日期 1", report)
        self.assertEqual(build_report(self.path, TODAY), report)
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_headers_or_ragged_rows_fail_visibly(self):
        self.path.write_text(TABLE1.replace("日期", "Date") + TABLE2, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "缺少表头：日期"):
            read_rows(self.path)
        self.path.write_text(TABLE1 + "| 太短 |\n" + TABLE2, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "dates.md:4 列数"):
            read_rows(self.path)
        self.path.write_text(TABLE1, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "同时找到表 1 和表 2"):
            read_rows(self.path)


if __name__ == "__main__":
    unittest.main()
