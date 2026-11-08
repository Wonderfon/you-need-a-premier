"""Stuck sprint rows (tick.stuck_rows / stuck_alarm / report_stuck_block). TickCase points tick's DATA, BACKLOG, BACKLOG_HISTORY and
RUNS_FILE at a temp dir, so nothing here reads or writes the production backlog or control/data."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import tick
from test_tick import TickCase

DOTS = "blocked: dotz T-1 阻塞：缺全文"
CLAUDE_TODO = "量化：跑第二次〔执行者：Claude〕说明"


def backlog(*rows, sprint="量化", cr="\n"):
    return cr.join(["# h", "## 量化线", f"冲刺：{sprint}，版本目标：x", *rows, "", "## 组织线", "- [todo] 组织：别的", ""])


class Stuck(TickCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 11, 3, 14, 0).astimezone()  # local wall 14:00

    def put(self, text):
        (self.d / "backlog.md").write_bytes(text.encode("utf-8"))

    def snap(self, hours_ago, text):
        when = (self.now - timedelta(hours=hours_ago)).astimezone(timezone.utc)
        (self.d / "backlog-history").mkdir(exist_ok=True)
        (self.d / "backlog-history" / f"backlog-{when:%Y%m%dT%H%M%S.%fZ}.md").write_bytes(text.encode("utf-8"))

    def rows(self, text, now=None):
        self.put(text)
        return tick.stuck_rows(text, now or self.now)

    def org_alarms(self):
        return [l for l in self.backlog().split("\n") if l.startswith("- [blocked: 等总理] 组织：卡住报警")]

    def test_four_kinds_each_reported_with_hours(self):
        for status, item, kind in ((DOTS, "量化：扩充语料", "dotz 阻塞"),
                                   ("blocked: 返回重派：缺脚本", "量化：补脚本", "返回重派"),
                                   ("blocked: Codex 沙箱不让写日志", "量化：开训", "沙箱/权限挡住"),
                                   ("todo", CLAUDE_TODO, "Claude 执行者 todo 没派出去")):
            with self.subTest(kind=kind):
                text = backlog(f"- [{status}] {item}")
                self.snap(7, text)
                got = self.rows(text)
                self.assertEqual([(r["kind"], r["project"], round(r["hours"])) for r in got], [(kind, "quant", 7)])

    def test_under_six_hours_and_unknown_duration_are_not_reported(self):
        text = backlog(f"- [{DOTS}] 量化：扩充语料")
        self.snap(5, text)
        self.assertEqual(self.rows(text), [])
        self.assertEqual(self.rows(backlog("- [blocked: dotz T-2 阻塞：别的] 量化：另一行")), [])  # in no copy: unknown, not "long"

    def test_ordinary_rows_and_waiting_for_a_person_are_not_stuck(self):
        text = backlog("- [todo] 量化：普通 todo", "- [blocked: 等你决定：权限] 量化：等人", "- [blocked: 等总理] 量化：问", "- [review] 量化：审")
        self.snap(30, text)
        self.assertEqual(self.rows(text), [])

    def test_a_copy_without_the_row_restarts_the_clock(self):
        text = backlog(f"- [{DOTS}] 量化：扩充语料")
        self.snap(10, text)
        self.snap(8, backlog("- [todo] 量化：扩充语料"))
        self.snap(7, text)
        self.assertEqual([round(r["hours"]) for r in self.rows(text)], [7])

    def test_a_claude_todo_run_since_the_copy_resets_the_clock(self):
        text = backlog(f"- [todo] {CLAUDE_TODO}")
        self.snap(10, text)
        (self.d / "runs.jsonl").write_text(json.dumps(
            {"at": (self.now - timedelta(hours=2)).astimezone(timezone.utc).isoformat(), "kind": "executor", "role": "quant",
             "item": "量化：上一件", "project": "quant"}) + "\n", encoding="utf-8")
        self.assertEqual(self.rows(text), [])

    def test_a_row_of_a_non_sprint_project_is_not_reported(self):
        text = backlog(f"- [{DOTS}] 写作模型：别的项目", f"- [blocked: dotz T-3 阻塞：x] 量化：这行才算")
        self.snap(9, text)
        self.assertEqual([r["item"] for r in self.rows(text)], ["量化：这行才算"])

    def test_row_written_once_per_24h_in_the_org_section_and_the_ask_lane_takes_it(self):
        text = backlog(f"- [{DOTS}] 量化：扩充语料", cr="\r\n")
        self.snap(7, text)
        self.put(text)
        tick.stuck_alarm({}, 1000.0, self.now)
        tick.stuck_alarm({}, 2000.0, self.now + timedelta(hours=1))  # a new supervisor process: state comes from the file
        raw = (self.d / "backlog.md").read_bytes().decode("utf-8")  # backlog() would fold CRLF
        alarm = [l for l in raw.split("\n") if l.startswith("- [blocked: 等总理] 组织：卡住报警")]
        self.assertEqual(len(alarm), 1)
        self.assertTrue(alarm[0].endswith("请总理处理\r"))  # CRLF kept
        self.assertIn("已 7 小时", alarm[0])
        self.assertIn("quant…量化：扩充语料…卡在 blocked: dotz T-1 阻塞：缺全文", alarm[0])
        self.assertLess(raw.index("## 组织线"), raw.index("卡住报警"))
        self.assertEqual(raw.count("\r\n"), raw.count("\n"))
        asks = tick.ask_pending(raw, 0.0)
        self.assertEqual([s for s, _ in asks], ["组织线"])
        self.assertIn("卡住报警", asks[0][1])
        tick.stuck_alarm({}, 3000.0, self.now + timedelta(hours=25))  # still stuck a day later: written again
        self.assertEqual(len(self.org_alarms()), 2)

    def test_lf_file_stays_lf(self):
        text = backlog(f"- [{DOTS}] 量化：扩充语料")
        self.snap(7, text)
        self.put(text)
        tick.stuck_alarm({}, 1000.0, self.now)
        self.assertNotIn(b"\r", (self.d / "backlog.md").read_bytes())

    def test_quiet_hours_only_note_and_08_00_writes(self):
        text = backlog(f"- [{DOTS}] 量化：扩充语料")
        self.snap(30, text)
        self.put(text)
        for hh, mm, expect in ((23, 0, 0), (3, 0, 0), (7, 59, 0), (8, 0, 1)):
            now = datetime(2026, 11, 3 if hh > 12 else 4, hh, mm).astimezone()
            tick.stuck_alarm({}, 1000.0 * (hh * 60 + mm), now)
            self.assertEqual(len(self.org_alarms()), expect, (hh, mm))
        self.assertEqual(sum(1 for x in self.logs() if x["decision"] == "stuck_noted"), 1)  # noted once, while quiet
        self.assertEqual(sum(1 for x in self.logs() if x["decision"] == "stuck_alarm"), 1)

    def test_report_block_lists_all_for_the_coordinator_and_own_line_for_a_deputy(self):
        text = backlog(f"- [{DOTS}] 量化：扩充语料")
        self.snap(7, text)
        self.put(text)
        block = tick.report_stuck_block("coordinator", self.now)
        self.assertTrue(block.startswith("【卡住的冲刺行】\n- quant｜量化：扩充语料｜blocked: dotz T-1 阻塞：缺全文（dotz 阻塞）｜已 7 小时"))
        self.assertIn("quant", tick.report_stuck_block("quant", self.now))
        self.assertEqual(tick.report_stuck_block("life", self.now), "")
        self.put(backlog("- [todo] 量化：没事"))
        self.assertEqual(tick.report_stuck_block("coordinator", self.now), "")

    def test_supervise_step_runs_it_and_survives_a_failure(self):
        with mock.patch.object(tick, "stuck_alarm", side_effect=OSError("disk")) as alarm, \
                mock.patch.object(tick.dots_review, "step"), \
                mock.patch.object(tick, "fresh_usage", return_value=None), \
                mock.patch.object(tick, "latest_usage", return_value=None):
            tick.supervise_step({"sample": None, "spawned": {}, "aux": {}}, 500.0)
        alarm.assert_called_once()
        self.assertIn("stuck_alarm_failed", [x["decision"] for x in self.logs()])


if __name__ == "__main__":
    unittest.main()
