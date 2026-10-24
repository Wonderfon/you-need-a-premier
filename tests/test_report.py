import json
import unittest
from datetime import datetime, timedelta, timezone, tzinfo

from orgcore import ledger, report
from orgcore.usage import FakeUsage

from .support import InstanceCase, run_cli


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


class NewYorkLike(tzinfo):
    """Fixed-offset stand-in for a zone with daylight saving in 2026 (works without a time-zone database):
    UTC-5, UTC-4 from 2026-03-08 07:00Z to 2026-11-01 06:00Z."""
    START, END = datetime(2026, 3, 8, 7), datetime(2026, 11, 1, 6)
    EST, EDT = timedelta(hours=-5), timedelta(hours=-4)

    def utcoffset(self, dt):
        wall = dt.replace(tzinfo=None)
        start_wall, end_wall = datetime(2026, 3, 8, 2), datetime(2026, 11, 1, 2)
        if wall < start_wall:
            return self.EST
        if wall < start_wall + timedelta(hours=1):  # the hour that does not exist: before-the-jump offset when fold=0
            return self.EDT if dt.fold else self.EST
        if wall < end_wall - timedelta(hours=1):
            return self.EDT
        if wall < end_wall:  # the hour that happens twice: first pass is still daylight time
            return self.EST if dt.fold else self.EDT
        return self.EST

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "NYL"

    def fromutc(self, dt):
        naive = dt.replace(tzinfo=None)
        if self.START <= naive < self.END:
            local = naive + self.EDT
            fold = 0
        else:
            local = naive + self.EST
            fold = 1 if self.END <= naive < self.END + timedelta(hours=1) else 0
        return local.replace(tzinfo=self, fold=fold)


TZ = NewYorkLike()


class ReportCase(InstanceCase):
    def setUp(self):
        super().setUp()
        self.set_reporting(timezone="America/New_York")  # only the label: these tests pass their own fixed-offset tzinfo

    def set_reporting(self, **changes):
        path = self.root / "reporting.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")

    def files(self):
        folder = self.root / "reports"
        return sorted(p.name for p in folder.glob("*.md")) if folder.exists() else []

    def run_at(self, moment, **kw):
        return report.run(self.instance(), moment, tz=TZ, source=FakeUsage.healthy(self.instance().policy, moment), **kw)


class Schedule(ReportCase):
    def test_on_time_report_is_written_once_and_a_rerun_writes_nothing(self):
        [path] = self.run_at(utc(2026, 1, 5, 14, 5))  # 09:05 local
        self.assertEqual(path.name, "2026-01-05-0900.md")
        text = path.read_text(encoding="utf-8")
        self.assertIn("# Report 2026-01-05 09:00 (America/New_York)", text)
        self.assertIn("- Key: 2026-01-05|09:00|local", text)
        self.assertIn("- Kind: scheduled", text)
        before = path.read_bytes()
        self.assertEqual(self.run_at(utc(2026, 1, 5, 14, 30)), [])
        self.assertEqual(self.run_at(utc(2026, 1, 5, 15, 0)), [])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.files(), ["2026-01-05-0900.md"])

    def test_nothing_is_written_before_a_slot_is_due_or_when_there_are_no_times(self):
        self.run_at(utc(2026, 1, 5, 14, 5))
        self.assertEqual(self.run_at(utc(2026, 1, 5, 18, 0)), [])  # 13:00 local: 21:00 not yet
        self.set_reporting(report_times=[])
        self.assertEqual(self.run_at(utc(2026, 1, 7, 2, 30)), [])
        self.assertEqual(self.files(), ["2026-01-05-0900.md"])

    def test_missed_slots_give_one_catch_up_and_never_a_second(self):
        self.run_at(utc(2026, 1, 5, 14, 5))  # 09:00 on the 5th
        self.run_at(utc(2026, 1, 6, 2, 5))   # 21:00 on the 5th
        # the machine was off for a day and a half; it wakes at 11:30 local on the 7th
        [path] = self.run_at(utc(2026, 1, 7, 16, 30))
        self.assertEqual(path.name, "2026-01-07-0900.md")
        text = path.read_text(encoding="utf-8")
        self.assertIn("- Kind: catch-up (due 09:00, written 2026-01-07 11:30)", text)
        self.assertIn("Earlier missed slots covered by this report: 2026-01-06 21:00", text)
        self.assertEqual(self.run_at(utc(2026, 1, 7, 17, 0)), [])
        self.assertEqual(self.run_at(utc(2026, 1, 7, 20, 0)), [])
        self.assertEqual(self.files(), ["2026-01-05-0900.md", "2026-01-05-2100.md", "2026-01-07-0900.md"])
        skipped = [e["slot"] for e in ledger.read(self.root, report.REPORTS) if e["event"] == "skipped"]
        # the first run on a fresh instance also skipped the evening before it (inside the 24-hour lookback)
        self.assertEqual(skipped, ["2026-01-04|21:00|local", "2026-01-06|21:00|local"])
        # the next real slot is an ordinary scheduled report again
        [later] = self.run_at(utc(2026, 1, 8, 2, 10))
        self.assertIn("- Kind: scheduled", later.read_text(encoding="utf-8"))

    def test_a_crash_between_recording_skipped_slots_and_writing_the_file_leaves_one_catch_up(self):
        self.run_at(utc(2026, 1, 5, 14, 5))
        slot = "2026-01-05|21:00|local"
        ledger.append(self.root, report.REPORTS, utc(2026, 1, 6, 4, 0), event="skipped", slot=slot,
                      covered_by="2026-01-06|09:00|local")
        [path] = self.run_at(utc(2026, 1, 6, 16, 0))
        self.assertEqual(path.name, "2026-01-06-0900.md")
        self.assertIn("covered by this report: 2026-01-05 21:00", path.read_text(encoding="utf-8"))
        self.assertEqual(self.run_at(utc(2026, 1, 6, 16, 5)), [])

    def test_quiet_hours_are_marked_and_the_window_wraps_midnight(self):
        quiet = {"start": "23:00", "end": "08:00"}
        reporting = {"quiet_hours": quiet}
        self.assertTrue(all(report.in_quiet_hours(reporting, t) for t in ("23:00", "23:59", "00:00", "07:59")))
        self.assertFalse(any(report.in_quiet_hours(reporting, t) for t in ("08:00", "09:00", "22:59")))
        self.assertTrue(report.in_quiet_hours({"quiet_hours": {"start": "01:00", "end": "05:00"}}, "03:00"))
        self.assertFalse(report.in_quiet_hours({"quiet_hours": {"start": "01:00", "end": "05:00"}}, "05:00"))
        self.assertFalse(report.in_quiet_hours({"quiet_hours": None}, "03:00"))
        self.set_reporting(report_times=["07:30"])
        [path] = self.run_at(utc(2026, 1, 5, 12, 35))
        self.assertIn("- Quiet hours: this slot is inside 23:00-08:00", path.read_text(encoding="utf-8"))


class DaylightSaving(ReportCase):
    def test_a_time_that_is_skipped_when_clocks_go_forward_is_still_reported_once(self):
        self.set_reporting(report_times=["02:30"])  # 2026-03-08 02:30 does not exist locally
        self.run_at(utc(2026, 3, 7, 7, 35))  # the day before: 02:30 local exists
        self.assertEqual(self.run_at(utc(2026, 3, 8, 7, 20)), [])  # not yet due
        [path] = self.run_at(utc(2026, 3, 8, 7, 35))
        self.assertEqual(path.name, "2026-03-08-0230.md")
        self.assertEqual(self.run_at(utc(2026, 3, 8, 9, 0)), [])
        self.assertEqual([f for f in self.files() if f.startswith("2026-03-08")], ["2026-03-08-0230.md"])

    def test_a_time_that_happens_twice_when_clocks_go_back_is_reported_once(self):
        self.set_reporting(report_times=["01:30"])  # 2026-11-01 01:30 happens at 05:30Z and again at 06:30Z
        self.run_at(utc(2026, 10, 31, 5, 31))
        [first] = self.run_at(utc(2026, 11, 1, 5, 35))
        self.assertEqual(first.name, "2026-11-01-0130.md")
        self.assertEqual(self.run_at(utc(2026, 11, 1, 6, 35)), [])  # the second 01:30 is not a new slot
        self.assertEqual(self.run_at(utc(2026, 11, 1, 7, 5)), [])
        self.assertEqual([f for f in self.files() if f.startswith("2026-11-01")], ["2026-11-01-0130.md"])

    def test_a_fixed_daily_time_keeps_its_local_hour_across_the_change(self):
        self.set_reporting(report_times=["09:00"])
        before = report.slots_between(self.instance().reporting, TZ, utc(2026, 3, 7, 0), utc(2026, 3, 10, 0))
        self.assertEqual([(s.label(), s.due.hour) for s in before],
                         [("2026-03-07 09:00", 14), ("2026-03-08 09:00", 13), ("2026-03-09 09:00", 13)])


class Content(ReportCase):
    def test_report_summarises_board_feedback_activity_and_usage(self):
        text = self.run_at(utc(2026, 1, 5, 14, 5))[0].read_text(encoding="utf-8")
        self.assertIn("| research | 2 | 0 | 0 | 0 | 0 | 1 |", text)
        self.assertIn("Blocked:\n- demo-bikes: Estimate the repair cost (waiting for the sample parts list)", text)
        self.assertIn("Finished rows without a rating: 1", text)
        self.assertIn("Dispatch gate: open", text)
        self.assertIn("synthetic", text)

    def test_missing_usage_is_unknown_not_zero(self):
        path = report.run(self.instance(), utc(2026, 1, 5, 14, 5), tz=TZ)[0]
        text = path.read_text(encoding="utf-8")
        self.assertIn("No reading: usage file not found: <instance>", text)
        self.assertNotIn(str(self.root), text)
        self.assertIn("unknown is never treated as zero", text)

    def test_activity_counts_ledger_events_since_the_previous_slot(self):
        for event in ("dispatched", "submitted"):
            ledger.append(self.root, ledger.RUNS, utc(2026, 1, 5, 12, 0), event=event, task="x", actor="a", attempt=1)
        ledger.append(self.root, ledger.RUNS, utc(2026, 1, 5, 1, 0), event="dispatched", task="y", actor="a", attempt=1)  # before the previous slot
        ledger.append(self.root, ledger.RUNS, utc(2026, 1, 5, 13, 0), event="reviewed", task="x", actor="r", attempt=1, verdict="pass")
        text = self.run_at(utc(2026, 1, 5, 14, 5))[0].read_text(encoding="utf-8")
        self.assertIn("- Dispatched: 1\n- Submitted for review: 1", text)
        self.assertIn("- Reviewed: 1 (pass 1, rework 0, blocked 0)", text)


class Basics(ReportCase):
    def test_unknown_time_zone_is_a_clear_error(self):
        with self.assertRaises(report.ReportError):
            report.zone("Nowhere/Land")
        self.assertIs(report.zone("UTC"), timezone.utc)

    def test_command_writes_once_then_reports_nothing_due(self):
        self.set_reporting(timezone="UTC")
        first = run_cli("report", self.root)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("Wrote", first.stdout)
        again = run_cli("report", self.root)
        self.assertEqual(again.stdout.strip(), "No report is due.")
        self.assertEqual(len(self.files()), 1)


if __name__ == "__main__":
    unittest.main()
