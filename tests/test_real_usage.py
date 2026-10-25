import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from orgcore import __main__ as cli
from orgcore import backlog, ledger, scheduler, statusline, usage
from orgcore.usage import JsonFileUsage, Observation

from .support import InstanceCase, board

NOW = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)
EPOCH = int((NOW + timedelta(hours=3)).timestamp())
PAYLOAD = {"version": "9.9.9", "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": EPOCH},
                                              "seven_day": {"used_percentage": 41, "resets_at": EPOCH + 86400}}}


def payload(**windows):
    return json.dumps({"version": "9.9.9", "rate_limits": windows})


class Parse(unittest.TestCase):
    def test_both_windows_become_non_synthetic_readings_with_source_and_version(self):
        found, notes = statusline.parse(json.dumps(PAYLOAD), NOW)
        self.assertEqual([(o.window, o.used_percent, o.synthetic) for o in found],
                         [("five_hour", 23.5, False), ("seven_day", 41.0, False)])
        self.assertEqual(found[0].source, "claude-code-statusline 9.9.9")
        self.assertEqual(found[0].resets_at, NOW + timedelta(hours=3))
        self.assertEqual(notes, [])

    def test_whatever_is_missing_or_wrong_stays_unknown_never_zero(self):
        for text, note in [("not json", "not valid JSON"), ("{}", "no rate_limits"), ('{"rate_limits": []}', "no rate_limits"),
                           (payload(five_hour={"used_percentage": 101, "resets_at": EPOCH}), "outside 0-100"),
                           (payload(five_hour={"used_percentage": True, "resets_at": EPOCH}), "outside 0-100"),
                           (payload(five_hour={"used_percentage": 5}), "resets_at missing"),
                           (payload(five_hour={"used_percentage": 5, "resets_at": int(NOW.timestamp())}), "predates the reset")]:
            with self.subTest(text=text[:40]):
                found, notes = statusline.parse(text, NOW)
                self.assertEqual([o for o in found if o.window == "five_hour"], [])
                self.assertTrue(any(note in n for n in notes), notes)

    def test_one_window_can_be_missing_while_the_other_is_read(self):
        found, notes = statusline.parse(payload(seven_day={"used_percentage": 3, "resets_at": EPOCH}), NOW)
        self.assertEqual([o.window for o in found], ["seven_day"])
        self.assertEqual(notes, ["five_hour: not reported"])


REAL = json.loads((Path(__file__).with_name("fixtures") / "statusline-real-2.1.310.json").read_text(encoding="utf-8"))


class RealShape(InstanceCase):
    """A status payload captured from a real interactive session (CLI 2.1.310, enterprise plan), identity fields removed."""
    BEFORE_RESET = datetime.fromtimestamp(REAL["rate_limits"]["five_hour"]["resets_at"] - 3600, timezone.utc)

    def test_the_fields_the_parser_reads_are_where_the_real_cli_puts_them(self):
        found, notes = statusline.parse(json.dumps(REAL), self.BEFORE_RESET)
        [reading] = found
        self.assertEqual((reading.window, reading.used_percent, reading.synthetic), ("five_hour", 71.0, False))
        self.assertEqual(reading.resets_at, datetime.fromtimestamp(REAL["rate_limits"]["five_hour"]["resets_at"], timezone.utc))
        self.assertEqual(reading.source, "claude-code-statusline 2.1.310")
        self.assertEqual(notes, ["seven_day: not reported"])  # this plan reports one window only

    def test_a_policy_that_lists_a_window_the_plan_never_reports_keeps_the_gate_closed(self):
        statusline.capture(self.root, json.dumps(REAL), self.BEFORE_RESET)
        readings = JsonFileUsage(usage.usage_path(self.root)).observe()
        gate = usage.evaluate(self.instance().policy, readings, self.BEFORE_RESET)
        self.assertFalse(gate.allowed)
        self.assertTrue(any("seven_day" in r for r in gate.reasons), gate.reasons)
        self.edit_policy(subscription_windows=[{**self.instance().policy["subscription_windows"][0], "reserve_percent": 10,
                                                "max_share_percent": 90}])
        self.assertTrue(usage.evaluate(self.instance().policy, readings, self.BEFORE_RESET).allowed)

    def test_the_fixture_holds_no_identity(self):
        text = json.dumps(REAL)
        for part in ("/Users/", "C:\\\\", "@"):
            self.assertNotIn(part, text)


class HookCommand(unittest.TestCase):
    def test_a_path_without_spaces_is_written_unquoted_with_forward_slashes(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(statusline.hook_command("C:\\py\\python.exe", Path("C:/inst")),
                             "C:/py/python.exe -m orgcore usage-capture C:/inst")

    def test_a_path_with_a_space_is_quoted_and_on_windows_starts_with_the_call_operator(self):
        with mock.patch.object(statusline, "_short", side_effect=lambda p: p.replace("\\", "/")):
            with mock.patch.object(sys, "platform", "win32"):
                self.assertEqual(statusline.hook_command("C:\\Program Files\\py\\python.exe", Path("C:/inst")),
                                 '& "C:/Program Files/py/python.exe" -m orgcore usage-capture C:/inst')
            with mock.patch.object(sys, "platform", "linux"):
                self.assertTrue(statusline.hook_command("/opt/my py/python", Path("/x")).startswith('"/opt/my py/python" '))


class Capture(InstanceCase):
    def test_capture_writes_a_file_the_gate_reads_and_merges_windows(self):
        line = statusline.capture(self.root, json.dumps(PAYLOAD), NOW)
        self.assertEqual(line, "usage five 23.5% seven 41%")
        statusline.capture(self.root, payload(five_hour={"used_percentage": 30, "resets_at": EPOCH}), NOW + timedelta(minutes=5))
        readings = JsonFileUsage(usage.usage_path(self.root)).observe()
        self.assertEqual({(r.window, r.used_percent) for r in readings}, {("five_hour", 30.0), ("seven_day", 41.0)})
        gate = usage.evaluate(self.instance().policy, readings, NOW + timedelta(minutes=6))
        self.assertTrue(gate.allowed, gate.reasons)  # policy window names five_hour / seven_day match the readings

    def test_nothing_usable_writes_no_file_and_the_gate_stays_closed(self):
        self.assertTrue(statusline.capture(self.root, "{}", NOW).startswith("usage unknown"))
        self.assertFalse(usage.usage_path(self.root).exists())
        with self.assertRaises(usage.UsageUnavailable):
            JsonFileUsage(usage.usage_path(self.root)).observe()

    def test_a_damaged_usage_file_is_replaced_by_fresh_readings(self):
        usage.usage_path(self.root).parent.mkdir(parents=True, exist_ok=True)
        usage.usage_path(self.root).write_text("{broken", encoding="utf-8")
        statusline.capture(self.root, json.dumps(PAYLOAD), NOW)
        self.assertEqual(len(JsonFileUsage(usage.usage_path(self.root)).observe()), 2)

    def test_readings_go_stale_and_close_the_gate_by_themselves(self):
        statusline.capture(self.root, json.dumps(PAYLOAD), NOW)
        readings = JsonFileUsage(usage.usage_path(self.root)).observe()
        gate = usage.evaluate(self.instance().policy, readings, NOW + timedelta(minutes=40))
        self.assertFalse(gate.allowed)
        self.assertIn("five_hour: stale", gate.reasons[0])

    def test_named_windows_match_official_ones_by_alias(self):
        self.assertEqual([usage.canonical(n) for n in ("5-hour", "Weekly", "7 day", "five_hour", "monthly")],
                         ["five_hour", "seven_day", "seven_day", "five_hour", "monthly"])

    def test_hook_command_reads_stdin_and_never_fails_the_session(self):
        import sys
        real = sys.stdin
        stamp = int((usage.now_utc() + timedelta(hours=3)).timestamp())  # the hook uses the real clock
        fresh = payload(five_hour={"used_percentage": 5, "resets_at": stamp}, seven_day={"used_percentage": 6, "resets_at": stamp})
        try:
            for text in (fresh, "garbage"):
                sys.stdin = io.StringIO(text)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cli.main(["usage-capture", str(self.root)]), 0)
                self.assertTrue(out.getvalue().startswith("usage"))
        finally:
            sys.stdin = real
        self.assertEqual(len(JsonFileUsage(usage.usage_path(self.root)).observe()), 2)


class UsageCommand(InstanceCase):
    def run_usage(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["usage", str(self.root)]), 0)
        return out.getvalue()

    def test_without_readings_the_gate_is_closed_and_says_unknown_is_not_zero(self):
        text = self.run_usage()
        self.assertIn("No usage readings", text)
        self.assertIn("Dispatch gate: closed (unknown is never treated as zero)", text)

    def test_with_a_fresh_reading_the_gate_opens_or_names_the_window_that_blocks(self):
        now = usage.now_utc()
        stamp = int((now + timedelta(hours=3)).timestamp())
        statusline.capture(self.root, payload(five_hour={"used_percentage": 10, "resets_at": stamp},
                                              seven_day={"used_percentage": 10, "resets_at": stamp}), now)
        self.assertIn("Dispatch gate: open", self.run_usage())
        statusline.capture(self.root, payload(seven_day={"used_percentage": 90, "resets_at": stamp}), now)
        text = self.run_usage()
        self.assertIn("Dispatch gate: closed", text)
        self.assertIn("seven_day: used 90% reaches the reserve boundary", text)


class SpendByUsage(InstanceCase):
    """Line choice follows the share of account usage each line's runs consumed, not a count of dispatches."""

    def setUp(self):
        super().setUp()
        self.write_board(board(("research", 60, [f"- [todo] demo-notes: r{i}" for i in range(6)]),
                               ("build", 40, [f"- [todo] demo-bikes: b{i}" for i in range(6)])))
        self.used = 10.0

    def source(self, used):
        return usage.FakeUsage([Observation(w["name"], used, NOW, NOW + timedelta(hours=3), "official", False)
                                for w in self.instance().policy["subscription_windows"]])

    def run_one(self, cost):
        """Dispatch, finish, and report that the run consumed `cost` points."""
        result = scheduler.dispatch(self.instance(), self.source(self.used), "e", NOW)
        for current, new in [("running", "review"), ("review", "done")]:
            backlog.transition(self.instance().backlog_path, result.task.key, current, new)
        self.used += cost
        scheduler.settle(self.instance(), self.source(self.used), result.task.key, NOW)
        return result.task.section

    def test_a_line_whose_runs_were_expensive_is_picked_less_often(self):
        picked = [self.run_one(cost) for cost in (10, 1, 1, 1, 1, 1)]
        # research ran first and cost 10 points; build then catches up cheaply and is picked until its share reaches its weight
        self.assertEqual(picked[:2], ["research", "build"])
        self.assertEqual(picked[2:], ["build"] * 4)

    def test_equal_costs_reproduce_the_old_count_based_order(self):
        picked = [self.run_one(1) for _ in range(8)]
        self.assertEqual(picked, ["research", "build", "research", "research", "build", "research", "build", "research"])

    def test_settle_records_unknown_when_the_window_reset_or_there_is_no_reading(self):
        result = scheduler.dispatch(self.instance(), self.source(10), "e", NOW)
        reset = usage.FakeUsage([Observation(w["name"], 2, NOW, NOW + timedelta(hours=9), "official", False)
                                 for w in self.instance().policy["subscription_windows"]])
        event = scheduler.settle(self.instance(), reset, result.task.key, NOW)
        self.assertEqual((event["delta"], event["reason"]), (None, "the window reset during the run"))
        event = scheduler.settle(self.instance(), usage.FakeUsage([]), result.task.key, NOW)
        self.assertEqual((event["delta"], event["reason"]), (None, "no reading after the run"))

    def test_an_unmeasured_run_is_charged_the_typical_cost_never_zero(self):
        self.run_one(4)  # measured: 4 points
        result = scheduler.dispatch(self.instance(), self.source(self.used), "e", NOW)  # second row stays unsettled
        spend = scheduler.recent_spend(ledger.read(self.root), NOW)
        self.assertEqual(spend, {"research": 4.0, "build": 4.0})  # the unsettled build run counts as the mean, 4
        self.assertEqual(result.task.section, "build")

    def test_dispatch_records_the_reading_it_started_from(self):
        scheduler.dispatch(self.instance(), self.source(12.5), "e", NOW)
        [event] = [e for e in ledger.read(self.root) if e["event"] == "dispatched"]
        self.assertEqual(event["used_before"], 12.5)


if __name__ == "__main__":
    unittest.main()
