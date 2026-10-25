import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from orgcore import __main__ as cli
from orgcore import adapter, backlog, config, doctor, ledger, live, statusline, usage
from orgcore import setup as guided

from .support import ROOT, InstanceCase
from .test_bootstrap import BootstrapCase, Session
from .test_setup import ANSWERS

FAKE = [sys.executable, str(Path(__file__).with_name("fake_claude.py"))]
TASK = "Summarize the three sample articles in notes.txt"
SECRET = "sk-test-secret-value"


def payload(five, seven, now):
    stamp = int((now + timedelta(hours=3)).timestamp())
    return json.dumps({"version": "9.9.9", "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": stamp},
                                                           "seven_day": {"used_percentage": seven, "resets_at": stamp + 86400}}})


class LiveCase(InstanceCase):
    """The demo instance with demo-notes turned into a real project, a rated offline demo, and a fake CLI as a real child process."""

    def setUp(self):
        super().setUp()
        self.edit_org(lambda org: [p.pop("demo", None) for line in org["lines"] for p in line["projects"] if p["id"] == "demo-notes"])
        guided.set_phase(self.root, "demo_ready", "offline-rehearsal")
        self.log = self.tmp / "launches.jsonl"
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in doctor.BILLING_ENV}
        self.env.update(FAKE_CLAUDE_LOG=str(self.log), FAKE_CLAUDE_ROLES="1", PYTHONPATH=str(ROOT))

    def consent(self, project="demo-notes", task=TASK):
        self.edit_policy(live_enabled=True, live_consent={"granted_at": "2026-01-05T11:00:00Z", "projects": [project],
                                                          "task_scope": task})

    def reading(self, five=10, seven=10, age_minutes=0):
        now = usage.now_utc()
        statusline.capture(self.root, payload(five, seven, now), now - timedelta(minutes=age_minutes))

    def launches(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def run_unit(self, **env):
        environ = {**self.env, **env}
        with mock.patch.dict(os.environ, environ, clear=True):  # the status and help probes inherit the process environment
            return live.run_unit(self.root, FAKE, environ, self.home)

    def state_of(self, text=TASK):
        return next(t.state for t in backlog.read(self.root / "projects" / "backlog.md").tasks if t.text == text)

    def assert_refused(self, expect, **env):
        board = (self.root / "projects" / "backlog.md").read_text(encoding="utf-8")
        with self.assertRaises(adapter.Refused) as caught:
            self.run_unit(**env)
        reasons = "\n".join(caught.exception.reasons)
        self.assertIn(expect, reasons)
        self.assertEqual(self.launches(), [], "no worker session may start")
        self.assertEqual((self.root / "projects" / "backlog.md").read_text(encoding="utf-8"), board)
        return reasons


class DoesNotStart(LiveCase):
    def test_consent_not_written(self):
        self.reading()
        self.assert_refused("no consent has been written")

    def test_consent_written_for_a_different_project(self):
        self.consent(project="demo-garden")
        self.reading()
        self.assert_refused("is not on the board")

    def test_stale_reading(self):
        self.consent()
        self.reading(age_minutes=120)
        self.assertIn("stale", self.assert_refused("stale"))
        state = guided.load_state(self.root)
        self.assertEqual((state["phase"], state["demo_run_id"]), ("live_waiting_for_usage", "offline-rehearsal"))

    def test_no_reading_at_all_waits_for_usage_and_keeps_the_demo_result(self):
        self.consent()
        self.assert_refused("usage source unavailable")
        self.assertEqual(guided.load_state(self.root)["phase"], "live_waiting_for_usage")
        self.assertEqual(guided.load_state(self.root)["demo_run_id"], "offline-rehearsal")

    def test_api_key_in_the_environment(self):
        self.consent()
        self.reading()
        reasons = self.assert_refused("ANTHROPIC_API_KEY is set", ANTHROPIC_API_KEY=SECRET)
        self.assertNotIn(SECRET, reasons)

    def test_api_key_sign_in(self):
        self.consent()
        self.reading()
        self.assert_refused("not a subscription login", FAKE_CLAUDE_AUTH=json.dumps({"loggedIn": True, "authMethod": "api_key"}))

    def test_cli_that_lacks_a_needed_option(self):
        self.consent()
        self.reading()
        self.assert_refused("does not list --session-id", FAKE_CLAUDE_FLAGS="--add-dir --allowedTools")

    def test_a_high_window_blocks(self):
        self.consent()
        self.reading(five=95)
        self.assert_refused("reserve boundary")


class Runs(LiveCase):
    def test_everything_in_place_runs_two_separate_sessions_and_settles_usage(self):
        self.consent()
        self.reading()
        lines = self.run_unit()
        first, second = self.launches()
        self.assertEqual((first["role"], second["role"]), ("executor", "reviewer"))
        self.assertNotEqual(first["prompt"], second["prompt"])
        self.assertNotEqual(first["session"], second["session"])
        self.assertEqual(self.state_of(), "done")
        events = ledger.read(self.root)
        self.assertEqual([e["actor"] for e in events if e["event"] in ("dispatched", "reviewed")],
                         ["claude-executor-1", "claude-reviewer-1"])
        settled = [e for e in events if e["event"] == "usage_settled"]
        self.assertEqual((len(settled), settled[0]["reason"]), (1, "measured"))
        self.assertEqual(guided.load_state(self.root)["phase"], "live_ready")
        self.assertIn("usage consumed", "\n".join(lines))

    def test_the_other_demo_rows_are_not_touched(self):
        self.consent()
        self.reading()
        self.run_unit()
        states = {t.text: t.state for t in backlog.read(self.root / "projects" / "backlog.md").tasks}
        self.assertEqual(states["List five plants for a shaded bed"], "todo")
        self.assertEqual(states["Draft a checklist for a chain replacement"], "todo")

    def test_a_rework_verdict_leaves_the_row_for_another_attempt(self):
        self.consent()
        self.reading()
        self.run_unit(FAKE_CLAUDE_VERDICT="rework")
        self.assertEqual(self.state_of(), "rework")
        self.run_unit()
        self.assertEqual(self.state_of(), "done")
        self.assertEqual([x["role"] for x in self.launches()], ["executor", "reviewer", "executor", "reviewer"])

    def test_a_failed_executor_session_leaves_the_row_running_for_recover(self):
        self.consent()
        self.reading()
        with self.assertRaises(adapter.SessionFailed):
            self.run_unit(FAKE_CLAUDE_EXIT="1")
        self.assertEqual(self.state_of(), "running")
        with self.assertRaises(adapter.Refused) as caught:
            self.run_unit()
        self.assertIn("recover", "\n".join(caught.exception.reasons))

    def test_a_row_waiting_for_review_only_gets_the_reviewer(self):
        self.consent()
        self.reading()
        with mock.patch.object(adapter.ClaudeReviewer, "judge", side_effect=adapter.SessionFailed("down")):
            with self.assertRaises(adapter.SessionFailed):
                self.run_unit()
        self.assertEqual(self.state_of(), "review")
        self.run_unit()
        self.assertEqual(self.state_of(), "done")
        self.assertEqual([x["role"] for x in self.launches()], ["executor", "reviewer"])


class Grant(LiveCase):
    def test_grant_writes_a_scoped_consent(self):
        live.grant(self.root, "demo-notes", TASK, datetime(2026, 1, 5, 12, tzinfo=timezone.utc))
        policy = self.instance().policy
        self.assertEqual((policy["live_enabled"], policy["live_consent"]),
                         (True, {"granted_at": "2026-01-05T12:00:00Z", "projects": ["demo-notes"], "task_scope": TASK}))

    def test_demo_projects_unknown_rows_and_an_unfinished_setup_are_refused(self):
        now = usage.now_utc()
        for project, task, expect in [("demo-garden", "List five plants for a shaded bed", "not a real project"),
                                      ("demo-notes", "something not on the board", "no waiting row")]:
            with self.subTest(project=project), self.assertRaisesRegex(ValueError, expect):
                live.grant(self.root, project, task, now)
        guided.set_phase(self.root, "demo_running")
        with self.assertRaisesRegex(ValueError, "offline rehearsal"):
            live.grant(self.root, "demo-notes", TASK, now)
        self.assertFalse(self.instance().policy["live_enabled"])

    def test_revoke_returns_to_demo_ready(self):
        live.grant(self.root, "demo-notes", TASK, usage.now_utc())
        guided.set_phase(self.root, "live_waiting_for_usage")
        live.revoke(self.root)
        self.assertEqual((self.instance().policy["live_enabled"], guided.load_state(self.root)["phase"]), (False, "demo_ready"))

    def test_preview_names_project_folder_task_and_each_window(self):
        text = "\n".join(live.preview(self.instance(), "demo-notes", TASK))
        for part in ("Project:  demo-notes", "work", TASK, "five_hour", "seven_day", "reserve is 30%"):
            self.assertIn(part, text)


class Cli(LiveCase):
    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(doctor, "find_claude", return_value=FAKE), mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(Path, "home", return_value=self.home), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(map(str, args)))
        return code, out.getvalue(), err.getvalue()

    def test_without_yes_only_the_preview_is_shown(self):
        code, out, _ = self.call("enable-live", self.root, "--project", "demo-notes", "--task", TASK)
        self.assertEqual(code, 0)
        self.assertIn(TASK, out)
        self.assertIn("reserve", out)
        self.assertIn("Nothing was written", out)
        self.assertFalse(self.instance().policy["live_enabled"])

    def test_a_task_that_is_not_on_the_board_is_refused_before_any_preview(self):
        code, out, err = self.call("enable-live", self.root, "--project", "demo-notes", "--task", "No such task", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("no waiting row 'No such task'", err)
        self.assertFalse(self.instance().policy["live_enabled"])

    def test_hook_settings_writes_a_file_whose_command_is_the_hook_command(self):
        code, out, _ = self.call("hook-settings", self.root)
        target = self.root / "state" / "statusline-settings.json"
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["statusLine"],
                         {"type": "command", "command": statusline.hook_command(sys.executable, self.root)})
        self.assertIn(f"claude --settings {target}", out)

    def test_yes_writes_the_consent_and_without_a_reading_says_what_is_missing(self):
        code, out, _ = self.call("enable-live", self.root, "--project", "demo-notes", "--task", TASK, "--yes")
        self.assertEqual(code, 0)
        self.assertTrue(self.instance().policy["live_enabled"])
        self.assertIn("usage source unavailable", out)
        self.assertIn("Headless sessions give no usage readings", out)
        self.assertEqual(guided.load_state(self.root)["phase"], "live_waiting_for_usage")

    def test_enable_run_disable_end_to_end(self):
        self.reading()
        code, out, _ = self.call("enable-live", self.root, "--project", "demo-notes", "--task", TASK, "--yes")
        self.assertIn("python -m orgcore run", out)
        self.assertEqual(guided.load_state(self.root)["phase"], "live_ready")
        code, out, _ = self.call("run", self.root)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state_of(), "done")
        self.assertEqual(len(self.launches()), 2)
        code, out, _ = self.call("disable-live", self.root)
        self.assertEqual((code, self.instance().policy["live_enabled"]), (0, False))

    def test_run_that_may_not_start_prints_every_reason_and_exits_1(self):
        code, _, err = self.call("run", self.root)
        self.assertEqual(code, 1)
        self.assertIn("Not started", err)
        self.assertIn("live mode is off", err)
        self.assertEqual(self.launches(), [])

    def test_a_real_process_shows_the_new_commands(self):
        done = subprocess.run([sys.executable, "-m", "orgcore", "enable-live", str(self.root), "--project", "demo-notes",
                               "--task", TASK], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Nothing was written", done.stdout)


class CliHelp(unittest.TestCase):
    def test_cli_gaps_names_each_missing_option(self):
        run = lambda cmd, **kw: mock.Mock(stdout="  --add-dir  --output-format", returncode=0)  # noqa: E731
        gaps = "\n".join(doctor.cli_gaps(["claude"], run))
        self.assertIn("--session-id", gaps)
        self.assertNotIn("--add-dir", gaps)
        self.assertEqual(doctor.cli_gaps(["claude"], lambda cmd, **kw: mock.Mock(stdout="", returncode=1)),
                         ["could not read the CLI help (run `claude --help`)"])
        self.assertEqual(doctor.cli_gaps(FAKE), [])

    def test_a_flag_the_real_help_hides_is_judged_by_its_listed_sibling(self):
        # the real CLI's --help lists --append-system-prompt but not --append-system-prompt-file (its docs say help is partial)
        help_text = " ".join(f for f in doctor.REQUIRED_FLAGS if f != "--append-system-prompt-file") + " --append-system-prompt"
        self.assertEqual(doctor.cli_gaps(["claude"], lambda cmd, **kw: mock.Mock(stdout=help_text, returncode=0)), [])
        gone = help_text.replace(" --append-system-prompt", "")
        self.assertIn("--append-system-prompt;", "\n".join(doctor.cli_gaps(["claude"], lambda cmd, **kw: mock.Mock(stdout=gone, returncode=0))) + ";")


class Offer(BootstrapCase):
    """The question after the offline demo, on a composed instance (menu 2, rating 1)."""

    def setUp(self):
        super().setUp()
        guided.start(self.inst)
        for block, answer in ANSWERS:
            guided.save(self.inst, block, answer)
        guided.compose(self.inst)
        self.session = Session(self)

    def ask(self, *answers):
        code = self.session.run(["2", "1", *answers])
        self.assertEqual(code, 0, self.session.text)
        return config.load_instance(self.inst)

    def test_keeping_the_demo_leaves_live_off(self):
        instance = self.ask("1")
        self.assertIn("Keep using the offline demo, or enable real Claude workers?", [p for p, _ in self.session.prompts][-1])
        self.assertFalse(instance.policy["live_enabled"])
        self.assertEqual(guided.load_state(self.inst)["phase"], "demo_ready")
        self.assertEqual(self.session.launches(), [])

    def test_real_workers_show_the_scope_first_and_without_yes_write_nothing(self):
        instance = self.ask("2", "1", "Sort the seed packets by date", "no")
        asked = [p for p, _ in self.session.prompts]
        self.assertTrue(asked[-1].startswith("\nType yes"), asked[-1])
        self.assertIn("Folder:", self.session.text)
        self.assertIn("Sort the seed packets by date", self.session.text)
        self.assertIn("reserve", self.session.text)
        self.assertFalse(instance.policy["live_enabled"])
        self.assertIsNone(instance.policy["live_consent"])
        self.assertNotIn("Sort the seed packets", (self.inst / "projects" / "backlog.md").read_text(encoding="utf-8"))

    def test_yes_writes_a_scoped_consent_and_waits_for_a_usage_reading(self):
        instance = self.ask("2", "1", "Sort the seed packets by date", "yes")
        self.assertTrue(instance.policy["live_enabled"])
        consent = instance.policy["live_consent"]
        self.assertEqual((consent["projects"], consent["task_scope"]), (["garden"], "Sort the seed packets by date"))
        self.assertIn("Setup phase: live_waiting_for_usage", self.session.text)
        self.assertIn("Headless sessions give no usage readings", self.session.text)
        state = guided.load_state(self.inst)
        self.assertEqual((state["phase"], state["demo_run_id"]), ("live_waiting_for_usage", "offline-rehearsal"))
        self.assertEqual(self.session.launches(), [])
        self.assertIn("garden", (self.inst / "projects" / "backlog.md").read_text(encoding="utf-8"))

    def test_a_fresh_reading_makes_the_same_setup_ready_and_the_rehearsal_does_not_undo_it(self):
        self.ask("2", "1", "Sort the seed packets by date", "yes")
        now = usage.now_utc()
        statusline.capture(self.inst, payload(10, 10, now), now)
        claude = FAKE
        with mock.patch.dict(os.environ, self.session.env, clear=True):
            reasons = live.readiness(self.inst, claude, usage.JsonFileUsage(usage.usage_path(self.inst)), now, os.environ, self.home)
        self.assertEqual(reasons, [])
        self.assertEqual(guided.load_state(self.inst)["phase"], "live_ready")
        Session(self).run(["2", ""])  # running the offline rehearsal again
        self.assertEqual(guided.load_state(self.inst)["phase"], "live_ready")

    def test_compose_still_works_after_the_consent_was_written(self):
        self.ask("2", "1", "Sort the seed packets by date", "yes")
        self.assertEqual(guided.compose(self.inst), [])

    def test_demo_projects_cannot_be_picked(self):
        self.assertEqual(live.real_projects(config.load_instance(self.inst))[0]["id"], "garden")
        self.assertTrue(all(not p.get("demo") for p in live.real_projects(config.load_instance(self.inst))))


if __name__ == "__main__":
    unittest.main()
