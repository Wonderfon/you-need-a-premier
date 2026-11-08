import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from claude_usage import UsageParseError, normal_prompt_ready, parse_usage_screen, read_usage


NOW = datetime(2026, 9, 28, 8, 51, tzinfo=timezone.utc)
SESSION = """Usage
Current session
████ 3% used
Resets 8:59am (America/New_York)
What's contributing
Unrelated local contribution 93% used
Resets never
"""


class UsageTests(unittest.TestCase):
    def test_native_prompt_requires_all_verified_markers(self):
        prompt = 'Safe mode\n' + '─' * 80 + '\n>\u00a0Try "edit <filepath> to..."\n⏵⏵ don\'t ask on'
        self.assertTrue(normal_prompt_ready(prompt))
        self.assertFalse(normal_prompt_ready(prompt.replace('Safe mode', '')))
        self.assertFalse(normal_prompt_ready(prompt.replace("don't ask on", '')))
        self.assertFalse(normal_prompt_ready(prompt.replace('>\u00a0Try "edit <filepath> to..."', '> 1. Yes')))

    def test_only_visible_session_and_ignore_local_activity(self):
        result = parse_usage_screen(SESSION, NOW)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["complete_visible_snapshot"])
        self.assertEqual(result["windows"][0]["used_percent"], 3)
        self.assertEqual(result["windows"][0]["resets_at"], int(datetime(2026, 9, 28, 12, 59, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(result["unreported_windows"], ["seven_day"])

    def test_every_visible_week_and_model_window_is_kept(self):
        screen = SESSION.replace("What's contributing", """Current week (all models)
26% used
Resets Fri 8:59am (America/New_York)
Current week (Sonnet only)
0% used
Resets Fri 8:59am (America/New_York)
What's contributing""")
        result = parse_usage_screen(screen, NOW)
        self.assertEqual([w["used_percent"] for w in result["windows"]], [3, 26, 0])
        self.assertEqual(len({w["name"] for w in result["windows"]}), 3)
        self.assertEqual(result["unreported_windows"], [])

    def test_full_weekday_and_explicit_date(self):
        for reset in ("Friday 8:59am", "Oct 2, 8:59am", "Oct 2, 2026 8:59am"):
            with self.subTest(reset=reset):
                result = parse_usage_screen(SESSION.replace("Current session", "Current week").replace("8:59am", reset), NOW)
                self.assertEqual(result["windows"][0]["resets_at"], int(datetime(2026, 10, 2, 12, 59, tzinfo=timezone.utc).timestamp()))

    def test_missing_completion_marker_blocks(self):
        with self.assertRaisesRegex(UsageParseError, "complete_usage_boundary_missing"):
            parse_usage_screen(SESSION.split("What's")[0], NOW)

    def test_incomplete_second_window_blocks_whole_snapshot(self):
        with self.assertRaisesRegex(UsageParseError, "window_fields_missing_or_ambiguous"):
            parse_usage_screen(SESSION.replace("What's", "Current week\n17% used\nWhat's"), NOW)

    def test_duplicate_or_unrecognized_usage_blocks(self):
        for screen in (SESSION.replace("What's", "Current session\n4% used\nResets 8:59am (America/New_York)\nWhat's"),
                       SESSION.replace("What's", "Monthly\n4% used\nWhat's")):
            with self.subTest(screen=screen), self.assertRaises(UsageParseError):
                parse_usage_screen(screen, NOW)

    def test_stale_quota_blocks(self):
        with self.assertRaisesRegex(UsageParseError, "usage_not_fresh"):
            parse_usage_screen("Showing last-known usage\n" + SESSION, NOW)

    def test_plain_refreshing_and_loading_never_parse_cached_bars(self):
        for marker in ("Refreshing…", "Refreshing...", "Loading…", "Loading", "loading usage"):
            with self.subTest(marker=marker), self.assertRaisesRegex(UsageParseError, "usage_refresh_in_progress"):
                parse_usage_screen(marker + "\n" + SESSION, NOW)

    def test_probe_waits_past_cached_snapshot_for_refresh_completion(self):
        import claude_usage

        clock = [0.0]
        startup = 'Safe mode\r\n' + '─' * 80 + '\r\n>\u00a0Try "edit <filepath> to..."\r\n⏵⏵ don\'t ask on'
        cached = "Refreshing…\n" + SESSION.replace("8:59am", "9am")

        class FakePTY:
            pid = 123456
            sent = False
            reads_after_command = 0

            def spawn(self, *_args, **_kwargs):
                return True

            def read(self, blocking=False):
                if not self.sent:
                    return "\x1b[2J\x1b[H" + startup
                self.reads_after_command += 1
                if self.reads_after_command == 1:
                    return "\x1b[2J\x1b[H" + cached.replace("\n", "\r\n")
                if self.reads_after_command == 25:
                    return "\x1b[2J\x1b[H" + SESSION.replace("\n", "\r\n")
                return ""

            def write(self, data):
                if data == "/usage\r":
                    self.sent = True

            def isalive(self):
                return True

        terminal = FakePTY()

        class FixedDatetime:
            @staticmethod
            def now(_tz):
                return NOW

        with patch("winpty.PTY", return_value=terminal), patch.dict("os.environ", {}, clear=True), \
                patch.object(claude_usage.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(claude_usage.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                patch.object(claude_usage, "datetime", FixedDatetime), patch.object(claude_usage.os, "kill"):
            result = read_usage()
        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(terminal.reads_after_command, 25)
        self.assertEqual(result["windows"][0]["reset_precision"], "minute")
        self.assertEqual(result["windows"][0]["resets_at"], int(datetime(2026, 9, 28, 12, 59, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(result["freshness_evidence"], "refresh_indicator_observed_then_cleared")

    def test_missing_reset_or_percent_blocks(self):
        for screen in (SESSION.replace("Resets 8:59am (America/New_York)", ""),
                       SESSION.replace("3% used", ""), SESSION.replace("3% used", "103% used")):
            with self.subTest(screen=screen), self.assertRaises(UsageParseError):
                parse_usage_screen(screen, NOW)

    def test_weekly_time_without_date_is_ambiguous(self):
        with self.assertRaisesRegex(UsageParseError, "weekly_reset_date_missing"):
            parse_usage_screen(SESSION.replace("Current session", "Current week"), NOW)

    def test_reset_outside_five_hour_window_blocks(self):
        with self.assertRaisesRegex(UsageParseError, "reset_outside_window"):
            parse_usage_screen(SESSION.replace("8:59am", "11:59am"), NOW)

    def test_timezone_required(self):
        with self.assertRaises(UsageParseError):
            parse_usage_screen(SESSION.replace("(America/New_York)", ""), NOW)

    def test_dst_ambiguity_blocks(self):
        when = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
        with self.assertRaisesRegex(UsageParseError, "reset_dst_ambiguous"):
            parse_usage_screen(SESSION.replace("8:59am", "1:30am"), when)


if __name__ == "__main__":
    unittest.main()
