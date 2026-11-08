import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_health  # noqa: E402
import tick  # noqa: E402

NOW = datetime(2026, 10, 31, 21, 0)
GB = 1024 ** 3


class DailyHealth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "backups"
        self.root.mkdir()
        self.dates = Path(self.tmp.name) / "life-dates.md"
        self.dates.write_text(
            "## 表 1：测试\n| 项目 | 日期 | 提前 30 天 | 提前 7 天 | 下次核对 |\n| --- | --- | --- | --- | --- |\n"
            "## 表 2：测试\n| 步骤 | 日期窗口 | 下次核对 |\n| --- | --- | --- |\n",
            encoding="utf-8",
        )
        for name, value in (("LIFE_DATES_FILE", self.dates),
                            ("LIFE_DATES_REPORTED_FILE", Path(self.tmp.name) / "dates-reported.json")):
            patcher = patch.object(tick, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.drive = self.root / "drive"
        self.drive.mkdir()
        latest = "2026-10-31-20261101T000000000000Z-test.zip"
        package = self.drive / latest
        package.write_bytes(b"")
        (self.root / "status.json").write_text(json.dumps({
            "stage": "local_snapshot_verified",
            "daily_archive_hour": 20,
            "last_staged_package": {"package_name": latest, "package_bytes": 5 * 1024 ** 2,
                                     "package_path": str(package)},
            "arbitros": {"ok_at": NOW.astimezone().isoformat()},
        }), encoding="utf-8")
        (self.root / "zip-chain-test.json").write_text(json.dumps({
            "package_name": latest, "package_bytes": 5 * 1024 ** 2,
            "package_path": str(package), "prior_package_name": "2026-10-30-previous.zip",
            "successful_archive_cutoff_date": "2026-10-31",
        }), encoding="utf-8")
        self.watch = Path(self.tmp.name) / "machine-health.jsonl"
        patcher = patch.object(daily_health, "MACHINE_HEALTH", self.watch)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_watch(cpu=5.0)
        for offset in range(7):
            day = NOW.date() - timedelta(days=offset)
            (self.drive / f"{day.isoformat()}-20261101T000000000000Z-test.zip").write_bytes(b"")

    def write_watch(self, cpu: float, count: int = 6) -> None:
        rows = []
        for n in range(count):  # 10-minute rows ending 20:50 on NOW's day
            at = (NOW - timedelta(minutes=10 * (count - n))).astimezone().isoformat(timespec="seconds")
            rows.append({"at": at, "interval_s": 600, "top": [{"name": "iGoSwServer.exe", "pid": 10888, "cpu": cpu}],
                         "dwm": 8.0, "gpu": {"util": 3.0}, "idle_min": 1.0, "model_procs": []})
        self.watch.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def line(self, gpu="55, 4096, 16384, 30\n"):
        with (patch.object(daily_health, "system_readings", return_value={
                   "memory_free": 16 * GB, "c_free": 200 * GB, "uptime": 3 * 24 * 3600}),
              patch.object(daily_health, "input_status", return_value=200),
              patch.object(daily_health.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=gpu))):
            return daily_health.report_line(NOW, self.root)

    def test_normal_synthetic_sources_make_one_complete_line(self):
        line = self.line()
        self.assertEqual(line.count("\n"), 0)
        self.assertTrue(line.startswith("机器和备份：正常（"))
        for value in ("55°C", "4.0GB", "内存余", "C盘余", "开机", "10-31", "近7天日包齐", "Arbitros", "8765=200"):
            self.assertIn(value, line)

    def test_sampler_summary_joins_the_line_and_sustained_anomalies_make_it_abnormal(self):
        line = self.line()
        self.assertIn("近 12 小时取样 6 次，无持续异常", line)
        self.write_watch(cpu=98.0)
        line = self.line()
        self.assertTrue(line.startswith("机器和备份：异常：近 12 小时持续异常：iGoSwServer.exe（pid 10888）"), line)
        self.watch.unlink()
        self.assertIn("没有机器取样记录", self.line())

    def test_a_missing_daily_zip_is_the_first_reported_problem(self):
        (self.drive / "2026-10-29-20261101T000000000000Z-test.zip").unlink()
        line = self.line()
        self.assertTrue(line.startswith("机器和备份：异常：日包缺 10-29"), line)

    def test_gpu_memory_over_seven_gb_is_reported_first(self):
        line = self.line("61, 8193, 12288, 72\n")
        self.assertTrue(line.startswith("机器和备份：异常：GPU显存当前 8.0GB（超7GB）"), line)

    def test_report_prompts_for_coordinator_and_each_deputy_include_the_line(self):
        prompts = {}

        def run(role, _kind, prompt, *_args, **_kwargs):
            prompts[role] = prompt
            return 0, None, True, "报告", 0

        with (patch.object(daily_health, "report_line", return_value="机器和备份：异常：输入台8765不通"),
              patch.object(tick.review_stats, "day_facts", return_value="【今日数据】\n"),
              patch.object(tick, "codex_report_note", return_value=""),
              patch.object(tick.shadow_review, "disabled_note", return_value=""),
              patch.object(tick, "run_resident", side_effect=run)):
            for role in tick.ROLE_NAMES:
                tick.write_report(role, NOW, "09:00")

        self.assertEqual(set(prompts), set(tick.ROLE_NAMES))
        for prompt in prompts.values():
            self.assertIn("【机器和备份】\n机器和备份：异常：输入台8765不通", prompt)
            self.assertIn("有异常时先写异常项", prompt)


if __name__ == "__main__":
    unittest.main()
