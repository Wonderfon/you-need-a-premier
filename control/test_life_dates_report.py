from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import life_dates
import tick


TABLES = """## 表 1：到期
| 项目 | 日期 | 下次核对 | 提前 30 天 | 提前 7 天 |
| --- | --- | --- | --- | --- |
| OPT | 2026-11-09 | | 2026-10-10 | 2026-11-02 |

## 表 2：倒排
| 步骤 | 日期窗口 | 下次核对 |
| --- | --- | --- |
"""
TODAY = date(2026, 11, 2)


class LifeDatesReportTests(unittest.TestCase):
    def setUp(self):
        cache = Path.home() / ".cache" / "codex-sandbox"
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="life-dates-report-test-", dir=cache)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.dates = root / "dates.md"
        self.record = root / "dates-reported.json"
        self.dates.write_text(TABLES, encoding="utf-8")
        self.record.write_text("[]\n", encoding="utf-8")
        for name, value in (("LIFE_DATES_FILE", self.dates), ("LIFE_DATES_REPORTED_FILE", self.record)):
            patcher = patch.object(tick, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_today_seven_day_reminder_is_in_prompt(self):
        prompt, keys = life_dates.prompt_block(self.dates, self.record, TODAY)

        self.assertIn("OPT 2026-11-09 到期／截止，只剩 7 天了。", prompt)
        self.assertIn("照 dates.md 第 2 条的两句模板写进日报。", prompt)
        self.assertEqual(len([line for line in prompt.splitlines() if line.startswith("- ")]), 1)
        self.assertEqual(len(keys), 2)  # today’s seven-day reminder and the missed 30-day reminder
        self.assertEqual({key["reminder_date"] for key in keys}, {"2026-10-10", "2026-11-02"})

    def test_table_two_reminder_date_is_derived_from_deadline(self):
        with self.dates.open("a", encoding="utf-8") as dates:
            dates.write("| 答辩公告 | 2026-11-09 | |\n")

        prompt, keys = life_dates.prompt_block(self.dates, self.record, TODAY)

        self.assertIn("[表 2 dates.md:9] 答辩公告 2026-11-09 到期／截止，只剩 7 天了。", prompt)
        self.assertEqual(sum(key["table"] == 2 for key in keys), 2)  # today’s seven-day and missed 30-day reminders

    def test_unreported_thirty_day_backfill_is_recorded_once(self):
        backfill_day = TODAY - timedelta(days=1)
        prompt, keys = life_dates.prompt_block(self.dates, self.record, backfill_day)
        self.assertIn("提前 30 天提醒日已过 22 天，现在还剩 8 天", prompt)

        with patch.object(tick, "LIFE_DATES_REPORTED_FILE", self.record):
            tick.record_life_date_reminders("life", "09:00", "sent", keys)

        prompt, keys = life_dates.prompt_block(self.dates, self.record, backfill_day)
        self.assertIn("今天没有需要带入的日期表提醒。", prompt)
        self.assertEqual(keys, [])

        prompt, keys = life_dates.prompt_block(self.dates, self.record, TODAY)
        self.assertIn("只剩 7 天了", prompt)
        self.assertEqual([key["reminder_date"] for key in keys], ["2026-11-02"])

    def test_missing_record_starts_empty_and_is_created_after_success(self):
        record = self.record.parent / "not-yet-created.json"
        self.assertEqual(life_dates.read_reported(record), set())
        prompt, keys = life_dates.prompt_block(self.dates, record, TODAY)
        self.assertIn("只剩 7 天了", prompt)
        self.assertEqual(len(keys), 2)
        self.assertFalse(record.exists())

        with patch.object(tick, "LIFE_DATES_REPORTED_FILE", record):
            tick.record_life_date_reminders("life", "09:00", "sent", keys)

        self.assertEqual(len(life_dates.read_reported(record)), 2)
        _, keys = life_dates.prompt_block(self.dates, record, TODAY)
        self.assertEqual(keys, [])

    def test_failed_send_does_not_record_reminders(self):
        _, keys = life_dates.prompt_block(self.dates, self.record, TODAY)

        with patch.object(tick, "LIFE_DATES_REPORTED_FILE", self.record):
            tick.record_life_date_reminders("life", "09:00", "failed", keys)

        self.assertEqual(life_dates.read_reported(self.record), set())
        _, still_due = life_dates.prompt_block(self.dates, self.record, TODAY)
        self.assertEqual(still_due, keys)

    def test_date_table_error_does_not_fail_daily_report(self):
        missing_dates = self.dates.parent / "missing-dates.md"
        captured = {}

        def run_resident(role, kind, prompt, *args, **kwargs):
            captured["prompt"] = prompt
            return 0, None, True, "生活日报正文", None

        with (patch.object(tick, "LIFE_DATES_FILE", missing_dates),
              patch.object(tick.review_stats, "day_facts", return_value=""),
              patch.object(tick, "report_health_block", return_value=""),
              patch.object(tick, "run_resident", side_effect=run_resident)):
            keys = []
            text, reason = tick.write_report("life", datetime(2026, 11, 2, 9), "09:00",
                                             machine_block="", date_reminder_keys=keys)

        self.assertEqual((text, reason), ("生活日报正文", ""))
        self.assertTrue(captured["prompt"].rstrip().endswith("日期表自检失败。"))
        self.assertEqual(keys, [])


if __name__ == "__main__":
    unittest.main()
