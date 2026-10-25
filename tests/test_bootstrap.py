import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bootstrap
from orgcore import config, doctor, feedback, statusline
from orgcore import setup as guided

from .support import ROOT
from .test_setup import ANSWERS

FAKE = [sys.executable, str(Path(__file__).with_name("fake_claude.py"))]
SECRET = "sk-test-secret-value"
BILLING = doctor.BILLING_ENV


class Session:
    """Runs bootstrap.main with scripted answers and a fake CLI; keeps everything it printed."""

    def __init__(self, case, **env):
        self.case = case
        self.log = case.tmp / "launches.jsonl"
        self.env = {k: v for k, v in os.environ.items() if k not in BILLING}
        self.env.update(FAKE_CLAUDE_LOG=str(self.log), FAKE_CLAUDE_SAVE=str(case.answers), PYTHONPATH=str(ROOT), **env)

    def run(self, inputs, claude=FAKE, instance=None, **kw):
        self.out, self.prompts = [], []
        replies = iter(inputs)

        def ask(prompt):
            self.prompts.append((prompt, len("\n".join(self.out))))
            try:
                return next(replies)
            except StopIteration:
                raise EOFError

        with mock.patch.dict(os.environ, self.env, clear=True):
            code = bootstrap.main(["--instance", str(instance or self.case.inst)], ask=ask, out=self.out.append,
                                  claude_cmd=claude, environ=self.env, home=self.case.home, **kw)
        self.text = "\n".join(self.out)
        return code

    def launches(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []


class BootstrapCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orgcore-boot-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.inst, self.home = self.tmp / "inst", self.tmp / "home"
        self.home.mkdir()
        self.answers = self.tmp / "answers.json"
        self.answers.write_text(json.dumps(ANSWERS), encoding="utf-8")


class Launch(BootstrapCase):
    def test_start_launches_the_official_cli_with_the_prompt_file_and_nothing_extra(self):
        session = Session(self, FAKE_CLAUDE_STEPS="2")
        self.assertEqual(session.run(["1", "yes"]), 0, session.text)
        [launch] = session.launches()
        argv = launch["argv"]
        self.assertEqual(argv[0], bootstrap.FIRST_MESSAGE)
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(self.inst.resolve()))
        self.assertEqual(argv[-2], "--allowedTools")  # last, so it cannot swallow a trailing argument
        self.assertIn("-m orgcore setup:*", argv[-1])
        self.assertNotIn("--bare", argv)
        status_line = json.loads(argv[argv.index("--settings") + 1])["statusLine"]
        self.assertEqual(status_line["type"], "command")
        self.assertEqual(status_line["command"], statusline.hook_command(sys.executable, self.inst.resolve()))
        self.assertFalse(launch["prompt_has_placeholder"])
        self.assertTrue(launch["prompt_names_instance"])
        self.assertEqual(Path(launch["cwd"]).resolve(), self.inst.resolve())
        self.assertIn(str(ROOT), launch["pythonpath"])

    def test_consent_must_be_a_yes_and_exit_starts_nothing(self):
        session = Session(self)
        self.assertEqual(session.run(["1", "y", "3"]), 0)
        self.assertEqual(session.launches(), [])
        self.assertFalse((self.inst / "state" / "setup.json").exists())
        self.assertIn("never be asked for a password", session.text)

    def test_second_start_continues_from_the_next_question_and_keeps_every_saved_answer(self):
        first = Session(self, FAKE_CLAUDE_STEPS="2")
        first.run(["1", "yes"])
        saved = {b: (self.inst / "state" / "answers" / f"{b}.json").read_bytes() for b in ("identity", "goal")}
        self.assertIn("Next question: lines", first.text)
        second = Session(self, FAKE_CLAUDE_STEPS="3")
        second.run(["1", "yes"])
        self.assertEqual(len(second.launches()), 2)
        self.assertIn("Next question: time", second.text)
        for block, data in saved.items():
            self.assertEqual((self.inst / "state" / "answers" / f"{block}.json").read_bytes(), data)
        self.assertEqual(guided.load_state(self.inst)["blocks"]["install"]["status"], "accepted")

    def test_a_cli_that_stops_early_leaves_a_checkpoint_not_a_failure_of_the_files(self):
        session = Session(self, FAKE_CLAUDE_STEPS="1", FAKE_CLAUDE_EXIT="1")
        self.assertEqual(session.run(["1", "yes"]), 1)
        self.assertEqual(guided.load_state(self.inst)["blocks"]["identity"]["status"], "accepted")

    def test_a_directory_with_someone_elses_organization_is_not_touched(self):
        self.inst.mkdir()
        (self.inst / "org.json").write_text("{}", encoding="utf-8")
        session = Session(self)
        self.assertEqual(session.run(["1", "yes"]), 1)
        self.assertIn("Choose another directory", session.text)
        self.assertEqual(session.launches(), [])


class Subscription(BootstrapCase):
    def refused(self, session, expect):
        self.assertEqual(session.run(["1", "yes"]), 1, session.text)
        self.assertEqual(session.launches(), [])
        self.assertIn(expect, session.text)
        for private in ("someone@example.invalid", "Example Org", SECRET, "subscriptionType", "firstParty"):
            self.assertNotIn(private, session.text)

    def test_a_key_in_the_environment_is_refused_without_printing_it(self):
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK"):
            with self.subTest(name):
                self.refused(Session(self, **{name: SECRET}), f"{name} is set in the environment")

    def test_not_signed_in_and_api_sign_in_are_refused(self):
        self.refused(Session(self, FAKE_CLAUDE_AUTH=json.dumps({"loggedIn": False})), "not signed in")
        api = {"loggedIn": True, "authMethod": "api_key", "email": "someone@example.invalid"}
        self.refused(Session(self, FAKE_CLAUDE_AUTH=json.dumps(api)), "not a subscription login")

    def test_unreadable_status_is_refused(self):
        self.refused(Session(self, FAKE_CLAUDE_AUTH="not json"), "could not read the CLI sign-in status")

    def test_an_api_key_helper_in_user_settings_is_refused(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(
            json.dumps({"apiKeyHelper": f"echo {SECRET}", "env": {"ANTHROPIC_API_KEY": SECRET}}), encoding="utf-8")
        session = Session(self)
        self.refused(session, "settings.json sets apiKeyHelper")
        self.assertIn("settings.json sets ANTHROPIC_API_KEY in env", session.text)

    def test_oauth_token_login_counts_as_a_subscription(self):
        auth = {"loggedIn": True, "authMethod": "oauth_token"}
        session = Session(self, FAKE_CLAUDE_AUTH=json.dumps(auth))
        self.assertEqual(session.run(["1", "yes"]), 0, session.text)
        self.assertEqual(json.loads(guided.show(self.inst, "install"))["install"], {"auth_method": "oauth_token"})

    def test_no_cli_installed_prints_install_help_and_starts_nothing(self):
        session = Session(self)
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(session.run(["1", "yes"], claude=None), 1)
        self.assertIn("Claude Code CLI: not found", session.text)
        self.assertIn("claude.ai/install", session.text)
        self.assertEqual(session.launches(), [])

    def test_doctor_reads_only_login_state_and_method(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(stdout=json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "free"}), returncode=0)

        auth = doctor.subscription(["claude"], {}, run=run)
        self.assertEqual((auth.ok, auth.method), (True, "claude.ai"))  # not decided by the plan name
        self.assertEqual(calls, [["claude", "auth", "status", "--json"]])
        self.assertTrue(doctor.python_ok((3, 11, 0)))
        self.assertFalse(doctor.python_ok((3, 10, 9)))


class Rehearsal(BootstrapCase):
    def test_results_are_shown_before_the_rating_is_asked_for(self):
        session = Session(self)
        self.assertEqual(session.run(["2", "1"]), 0, session.text)
        question, printed = next((p, n) for p, n in session.prompts if p.lstrip().startswith("Was this useful"))
        shown_before = "\n".join(session.out)[:printed]
        self.assertIn("DEMO-", shown_before)
        self.assertIn("The fake worker's finished work", shown_before)
        self.assertIn("Summary:", shown_before)
        instance = config.load_instance(self.inst / "rehearsal")
        self.assertEqual([e["rating"] for e in feedback.summarize(instance).latest.values()], ["useful"])
        self.assertEqual(session.launches(), [])  # no CLI is needed or started

    def test_enter_records_nothing_and_a_later_rate_command_does(self):
        session = Session(self)
        self.assertEqual(session.run(["2", ""]), 0)
        instance = config.load_instance(self.inst / "rehearsal")
        self.assertEqual(feedback.summarize(instance).latest, {})
        self.assertIn("No verdict recorded", session.text)
        again = Session(self)
        self.assertEqual(again.run(["2", "3"]), 0)  # re-entrant: continues the same row, adds no second one
        self.assertEqual([e["rating"] for e in feedback.summarize(instance).latest.values()], ["rework"])
        self.assertEqual(Session(self).run(["2"]), 0)

    def test_rehearsal_works_without_the_cli_and_marks_a_started_setup_ready(self):
        guided.start(self.inst)
        session = Session(self)
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(session.run(["2", "1", "1"], claude=None), 0, session.text)  # 1 = keep the offline demo
        state = guided.load_state(self.inst)
        self.assertEqual((state["phase"], state["demo_run_id"]), ("demo_ready", "offline-rehearsal"))

    def test_skipping_the_rating_leaves_the_phase_at_demo_running(self):
        guided.start(self.inst)
        Session(self).run(["2", ""])
        self.assertEqual(guided.load_state(self.inst)["phase"], "demo_running")


if __name__ == "__main__":
    unittest.main()
