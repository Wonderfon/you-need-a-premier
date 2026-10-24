import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from orgcore import adapter, doctor, usage
from orgcore.usage import FakeUsage, Observation

from .support import ROOT, InstanceCase

NOW = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)
KEY = ("research", "demo-notes", "write the notes")
FAKE = [sys.executable, str(Path(__file__).with_name("fake_claude.py"))]
CLEAN_ENV = {k: v for k, v in os.environ.items() if k not in doctor.BILLING_ENV}


class Stub:
    """A replacement for subprocess.run that plays the CLI: answers `auth status`, records worker launches."""

    def __init__(self, reply="Summary: done\nEVIDENCE: out.txt", write="out.txt", auth=None, code=0):
        self.launches, self.reply, self.write, self.code = [], reply, write, code
        self.auth = auth or {"loggedIn": True, "authMethod": "claude.ai", "email": "someone@example.invalid"}

    def __call__(self, cmd, **kw):
        if cmd[-3:] == ["auth", "status", "--json"]:
            return mock.Mock(stdout=json.dumps(self.auth), returncode=0)
        self.launches.append({"cmd": cmd, **kw})
        if self.write:
            (Path(kw["cwd"]) / self.write).write_text("Summary: x\nSources: none\n", encoding="utf-8")
        return mock.Mock(stdout=json.dumps({"result": self.reply}), returncode=self.code)


class AdapterCase(InstanceCase):
    def setUp(self):
        super().setUp()
        self.edit_policy(live_enabled=True, live_consent={"granted_at": "2026-01-05T11:00:00Z", "projects": ["demo-notes"],
                                                          "task_scope": "one task"})
        self.stub = Stub()
        self.home = self.tmp / "home"
        self.home.mkdir()

    def source(self, used=10):
        return FakeUsage([Observation(w["name"], used, NOW, NOW + timedelta(hours=3), "official", False)
                          for w in self.instance().policy["subscription_windows"]])

    def worker(self, kind, **kw):
        return kind(self.instance(), ["claude"], kw.pop("source", self.source()), kw.pop("environ", CLEAN_ENV), self.home, self.stub)

    def refused(self, call):
        with self.assertRaises(adapter.Refused) as caught:
            call()
        self.assertEqual(self.stub.launches, [], "a refused session must not start the CLI")
        return "\n".join(caught.exception.reasons)


class Guard(AdapterCase):
    def test_live_mode_off_or_project_not_in_the_consent_starts_nothing(self):
        self.edit_policy(live_enabled=False, live_consent=None)
        self.assertIn("live mode is off", self.refused(lambda: self.worker(adapter.ClaudeExecutor).work(KEY, 1, NOW)))
        self.edit_policy(live_enabled=True, live_consent={"granted_at": "2026-01-05T11:00:00Z", "projects": ["demo-garden"],
                                                          "task_scope": "x"})
        self.assertIn("does not name project 'demo-notes'", self.refused(lambda: self.worker(adapter.ClaudeExecutor).work(KEY, 1, NOW)))

    def test_billing_variables_and_api_sign_in_are_refused_without_printing_values(self):
        env = {**CLEAN_ENV, "ANTHROPIC_API_KEY": "sk-test-secret-value"}
        found = self.refused(lambda: self.worker(adapter.ClaudeExecutor, environ=env).work(KEY, 1, NOW))
        self.assertIn("ANTHROPIC_API_KEY is set", found)
        self.assertNotIn("sk-test-secret-value", found)
        self.stub.auth = {"loggedIn": True, "authMethod": "api_key"}
        self.assertIn("not a subscription login", self.refused(lambda: self.worker(adapter.ClaudeExecutor).work(KEY, 1, NOW)))

    def test_unknown_stale_synthetic_or_high_usage_starts_nothing(self):
        for source, expect in [(FakeUsage([]), "unknown"),
                               (self.source(95), "reserve boundary"),
                               (FakeUsage([Observation(w["name"], 5, NOW - timedelta(hours=9), NOW + timedelta(hours=1), "x")
                                           for w in self.instance().policy["subscription_windows"]]), "stale"),
                               (FakeUsage([Observation(w["name"], 5, NOW, NOW + timedelta(hours=1), "x", True)
                                           for w in self.instance().policy["subscription_windows"]]), "synthetic")]:
            with self.subTest(expect=expect):
                self.assertIn(expect, self.refused(lambda: self.worker(adapter.ClaudeExecutor, source=source).work(KEY, 1, NOW)))
        with mock.patch.object(FakeUsage, "observe", side_effect=usage.UsageUnavailable("file missing")):
            self.assertIn("unknown is never treated as zero",
                          self.refused(lambda: self.worker(adapter.ClaudeExecutor).work(KEY, 1, NOW)))


class Launch(AdapterCase):
    def test_executor_gets_files_only_in_its_own_directory_and_no_shell_or_network(self):
        evidence = self.worker(adapter.ClaudeExecutor).work(KEY, 1, NOW)
        [launch] = self.stub.launches
        cmd = launch["cmd"]
        folder = self.root / "work" / "demo-notes"
        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertIn("Project: demo-notes\nTask: write the notes", cmd[2])
        self.assertEqual(cmd[cmd.index("--append-system-prompt-file") + 1], str(ROOT / "prompts" / "executor.md"))
        self.assertEqual(cmd[cmd.index("--add-dir") + 1], str(folder))
        self.assertEqual(launch["cwd"], str(folder))
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Read,Edit,Write,Glob,Grep")
        self.assertIn("Bash", cmd[cmd.index("--disallowedTools") + 1].split(","))
        self.assertIn("WebFetch", cmd[cmd.index("--disallowedTools") + 1].split(","))
        self.assertEqual(Path(evidence), (folder / "out.txt").resolve())

    def test_reviewer_is_read_only_with_its_own_session(self):
        reviewer = self.worker(adapter.ClaudeReviewer)
        self.stub.reply = "Looks fine.\nVERDICT: pass"
        verdict, notes = reviewer.judge(KEY, 1, "out.txt", "claude-executor-1", NOW)
        self.assertEqual((verdict, notes), ("pass", "Looks fine.\nVERDICT: pass"))
        cmd = self.stub.launches[-1]["cmd"]
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Read,Glob,Grep")
        denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
        self.assertTrue({"Edit", "Write", "Bash"} <= set(denied))
        self.assertEqual(cmd[cmd.index("--append-system-prompt-file") + 1], str(ROOT / "prompts" / "reviewer.md"))

    def test_sessions_are_independent_stable_and_different_per_role_and_attempt(self):
        inst = self.instance()
        ids = {adapter.session_id(inst, KEY, role, n) for role in ("executor", "reviewer") for n in (1, 2)}
        self.assertEqual(len(ids), 4)
        self.assertEqual(adapter.session_id(inst, KEY, "executor", 1), adapter.session_id(inst, KEY, "executor", 1))

    def test_a_reviewer_that_is_the_executor_is_refused(self):
        found = self.refused(lambda: self.worker(adapter.ClaudeReviewer).judge(KEY, 1, "out.txt", "claude-reviewer-1", NOW))
        self.assertIn("must not be the executor", found)

    def test_the_instances_own_prompt_copy_wins_over_the_packaged_one(self):
        own = self.root / "prompts"
        own.mkdir()
        (own / "executor.md").write_text("my edited copy", encoding="utf-8")
        self.assertEqual(adapter.prompt_file(self.instance(), "executor"), own / "executor.md")
        self.assertEqual(adapter.prompt_file(self.instance(), "reviewer"), ROOT / "prompts" / "reviewer.md")

    def test_project_path_is_used_as_the_working_directory(self):
        target = self.tmp / "elsewhere"
        self.edit_org(lambda org: org["lines"][0]["projects"][0].update(path=str(target)))
        self.worker(adapter.ClaudeExecutor).work(KEY, 1, NOW)
        self.assertEqual(self.stub.launches[0]["cwd"], str(target))


class Results(AdapterCase):
    def test_evidence_must_exist_inside_the_working_directory(self):
        for reply, write in [("Summary: x\nEVIDENCE: ../../org.json", "out.txt"), ("Summary: x", "out.txt"),
                             ("Summary: x\nEVIDENCE: missing.txt", None)]:
            with self.subTest(reply=reply):
                self.stub.reply, self.stub.write = reply, write
                with self.assertRaises(adapter.SessionFailed):
                    self.worker(adapter.ClaudeExecutor).work(KEY, 1, NOW)

    def test_a_reviewer_reply_without_a_verdict_line_is_not_a_verdict(self):
        self.stub.reply = "VERDICT: pass\nand some more words"
        with self.assertRaises(adapter.SessionFailed):
            self.worker(adapter.ClaudeReviewer).judge(KEY, 1, "out.txt", "claude-executor-1", NOW)

    def test_a_failing_cli_is_reported_without_its_output(self):
        self.stub.code = 3
        with self.assertRaises(adapter.SessionFailed) as caught:
            self.worker(adapter.ClaudeExecutor).work(KEY, 1, NOW)
        self.assertIn("exit code 3", str(caught.exception))

    def test_the_cli_never_gets_billing_variables(self):
        env = {**CLEAN_ENV, "ANTHROPIC_API_KEY": "x", "ANTHROPIC_AUTH_TOKEN": "y", "KEEP": "1"}
        cleaned = adapter._clean_env(env)
        self.assertEqual((cleaned.get("KEEP"), "ANTHROPIC_API_KEY" in cleaned, "ANTHROPIC_AUTH_TOKEN" in cleaned), ("1", False, False))


class RealProcess(AdapterCase):
    """The same launch through a real child process (the fake CLI), so argument passing and exit codes are real."""

    def test_launch_through_a_real_process(self):
        log = self.tmp / "log.jsonl"
        env = {**CLEAN_ENV, "FAKE_CLAUDE_LOG": str(log), "FAKE_CLAUDE_WRITE": "out.txt", "FAKE_CLAUDE_RESULT": "Summary: x\nEVIDENCE: out.txt"}
        folder = self.root / "work" / "demo-notes"
        sid = adapter.session_id(self.instance(), KEY, "executor", 1)
        cmd = adapter.command(FAKE, "executor", "hello", ROOT / "prompts" / "executor.md", folder, sid)
        text = adapter.run_session(cmd, folder, env)
        self.assertEqual(text, "Summary: x\nEVIDENCE: out.txt")
        record = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(record["argv"][:2], ["-p", "hello"])
        self.assertEqual(record["argv"][record["argv"].index("--session-id") + 1], sid)
        self.assertEqual(Path(record["cwd"]).resolve(), folder.resolve())
        self.assertTrue((folder / "out.txt").exists())


class Templates(unittest.TestCase):
    def read(self, name):
        return (ROOT / "prompts" / name).read_text(encoding="utf-8")

    def test_role_templates_state_the_machine_readable_last_lines_and_the_limits(self):
        executor, reviewer = self.read("executor.md"), self.read("reviewer.md")
        self.assertIn("EVIDENCE: <path", executor)
        for verdict in ("VERDICT: pass", "VERDICT: rework", "VERDICT: blocked"):
            self.assertIn(verdict, reviewer)
        self.assertIn("no shell, no network", executor)
        self.assertIn("read-only", reviewer)
        for text in (executor, reviewer):
            self.assertNotIn("{{", text)
            self.assertTrue(text.isascii())


if __name__ == "__main__":
    unittest.main()
