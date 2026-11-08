import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import timed_rows as tr


class TimedRows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / tr.STATE_FILE
        self.logs = []
        self.text = "## 组织线\n- [todo] 组织：到点恢复〔时段：10:50–13:00〕\n"

    def observe(self, when, text=None):
        text = self.text if text is None else text
        now = datetime.fromisoformat(when)
        tr.observe(text, self.path, now, lambda decision, reason, **kw: self.logs.append(
            {"decision": decision, "reason": reason, **kw}))
        return tr.missed(text, self.path, now)

    def test_daily_before_open_expired_are_distinct_and_deduplicated_after_restart(self):
        for clock in ("04:49", "04:49", "10:50", "12:59", "13:00", "15:00"):
            missed = self.observe("2026-11-02T" + clock)
        self.assertEqual([r["phase"] for r in self.logs], ["before", "open", "expired"])
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["message"], "到点行错过：原定 10:50、窗口已过、还没执行")
        self.assertEqual(missed[0]["window_date"], "2026-11-02")
        self.assertEqual(self.observe("2026-11-03T04:00")[0]["window_date"], "2026-11-02")
        self.assertEqual(self.observe("2026-11-03T13:00")[0]["window_date"], "2026-11-03")
        self.assertEqual([r["window_date"] for r in self.logs if r["phase"] == "expired"],
                         ["2026-11-02", "2026-11-03"])

    def test_one_off_explicit_date_does_not_roll_over_or_use_dates_in_prose(self):
        self.text = self.text.rstrip() + "〔窗口日期：2026-11-02〕 owner\x2010-01 已答\n"
        self.assertEqual(self.observe("2026-11-01T14:00"), [])
        self.observe("2026-11-02T10:50")
        self.observe("2026-11-02T13:00")
        self.assertEqual(self.observe("2026-11-04T14:00")[0]["window_date"], "2026-11-02")
        self.assertEqual([r["phase"] for r in self.logs], ["before", "open", "expired"])

    def test_overnight_new_row_does_not_inherit_yesterdays_miss(self):
        self.text = self.text.replace("10:50–13:00", "23:00–07:00")
        self.assertEqual(self.observe("2026-11-02T22:59"), [])
        self.observe("2026-11-02T23:00")
        self.observe("2026-11-03T06:59")
        self.assertEqual(self.observe("2026-11-03T07:00")[0]["window_date"], "2026-11-02")
        self.assertEqual([r["phase"] for r in self.logs if r["window_date"] == "2026-11-02"],
                         ["before", "open", "expired"])

    def test_one_off_overnight_and_equal_endpoints_use_start_date(self):
        for window in ("23:00–07:00", "23:00–23:00"):
            with self.subTest(window=window):
                text = self.text.replace("10:50–13:00", window).rstrip() + "〔窗口日期：2026-11-02〕\n"
                rows = tr.observations(text, {}, datetime(2026, 11, 3, 6, 59))
                self.assertEqual((rows[0]["phase"], rows[0]["window_date"]), ("open", "2026-11-02"))
                self.assertEqual(tr.observations(text, {}, datetime(2026, 11, 3, 23))[0]["phase"], "expired")

    def test_full_identity_not_prefix_and_current_status_controls_visibility_without_writes(self):
        prefix = "组织：" + "长" * 80
        self.text = "## 组织线\n" + "\n".join(f"- [todo] {prefix}{suffix}〔时段：10:50–13:00〕" for suffix in ("甲", "乙"))
        self.assertEqual(len(self.observe("2026-11-02T04:49")), 0)
        self.assertEqual(len(self.observe("2026-11-02T13:00")), 2)
        self.assertEqual(len({r["row_id"] for r in self.logs}), 2)
        before = self.path.read_bytes()
        for status in ("running 13:01", "review", "done 13:01", "blocked: 等总理"):
            self.assertEqual(tr.missed(self.text.replace("[todo]", f"[{status}]"), self.path,
                                       datetime(2026, 11, 2, 14)), [])
        self.assertEqual(self.path.read_bytes(), before)
        self.observe("2026-11-02T14:00", self.text.replace("[todo]", "[done 13:01]"))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {})

    def test_owner_and_malformed_rows_are_ignored_and_new_daily_row_has_no_old_debt(self):
        text = ("## 你的指令（最优先）\n- [todo] owner〔时段：10:50–13:00〕\n## 组织线\n"
                "- [todo] invalid〔时段：25:00–13:00〕\n- [todo] invalid〔时段：broken〕\n"
                "- [todo] invalid〔时段：10:50–13:00〕〔窗口日期：2026-02-30〕\n")
        self.assertEqual(self.observe("2026-11-03T14:00", text), [])
        self.assertEqual(self.observe("2026-11-03T04:00"), [])
        self.assertEqual({r["window_date"] for r in self.logs}, {"2026-11-03"})


if __name__ == "__main__":
    unittest.main()
