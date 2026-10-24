"""Real workers: one session of the official Claude Code CLI per role and attempt, on the owner's own subscription login.

What this module decides, so that no prompt has to be trusted with it:
  * the launch arguments of each role (prompt file, working directory, allowed and denied tools, a fresh session id)
  * whether a session may start at all: live mode switched on, consent that names the project, a subscription login
    with nothing redirecting billing, and an open usage gate. Every refusal says why; none of them calls a model.
  * the executor never gets a shell or the network; the reviewer cannot edit; neither gets the billing variables

The CLI is started through `run`, so tests give it a fake CLI. Nothing here is used by the offline rehearsal.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from . import doctor, ledger, usage
from .config import Instance

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
NAMESPACE = uuid.UUID("3b1f6a52-8f43-4f5e-9d54-6f0f3a1d7c10")
SESSION_TIMEOUT_SECONDS = 1800
ROLES = {
    "executor": {"actor": "claude-executor-1", "allowed": ("Read", "Edit", "Write", "Glob", "Grep"),
                 "denied": ("Bash", "WebFetch", "WebSearch")},
    "reviewer": {"actor": "claude-reviewer-1", "allowed": ("Read", "Glob", "Grep"),
                 "denied": ("Bash", "Edit", "Write", "WebFetch", "WebSearch", "NotebookEdit")},
}


class Refused(Exception):
    """A session was not started. `reasons` are shown to the owner as they are."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class SessionFailed(Exception):
    pass


def prompt_file(instance: Instance, role: str) -> Path:
    """The instance's own copy wins (owners edit it); the packaged template is the fallback."""
    own = instance.root / "prompts" / f"{role}.md"
    return own if own.is_file() else PROMPTS / f"{role}.md"


def workdir(instance: Instance, project_id: str) -> Path:
    for line in instance.org["lines"]:
        for project in line["projects"]:
            if project["id"] == project_id and project.get("path"):
                path = Path(project["path"])
                return path if path.is_absolute() else (instance.root / path).resolve()
    return instance.root / "work" / project_id


def session_id(instance: Instance, key: tuple[str, str, str], role: str, attempt: int) -> str:
    """Stable per (instance, row, role, attempt) and different for the two roles: sessions are never shared."""
    return str(uuid.uuid5(NAMESPACE, f"{instance.org['instance_id']}|{ledger.task_id(key)}|{role}|{attempt}"))


def command(claude: list[str], role: str, message: str, prompt: Path, folder: Path, sid: str) -> list[str]:
    spec = ROLES[role]
    return [*claude, "-p", message, "--output-format", "json", "--append-system-prompt-file", str(prompt),
            "--add-dir", str(folder), "--permission-mode", "dontAsk", "--session-id", sid,
            "--disallowedTools", ",".join(spec["denied"]), "--allowedTools", ",".join(spec["allowed"])]


def guard(instance: Instance, project_id: str, claude: list[str], source: usage.UsageSource, now: datetime,
          environ, home: Path | None = None, run=subprocess.run) -> None:
    """Raise Refused unless real work on `project_id` is allowed right now."""
    policy, reasons = instance.policy, []
    if not policy["live_enabled"]:
        reasons.append("live mode is off (policy.json live_enabled is false)")
    elif project_id not in policy["live_consent"]["projects"]:
        reasons.append(f"the live consent does not name project '{project_id}'")
    auth = doctor.subscription(claude, environ, home=home, cwd=instance.root, run=run)
    reasons += auth.problems
    try:
        reasons += usage.evaluate(policy, source.observe(), now).reasons
    except usage.UsageUnavailable as exc:
        reasons.append(f"usage source unavailable: {exc} (unknown is never treated as zero)")
    if reasons:
        raise Refused(reasons)


def _clean_env(environ) -> dict:
    return {k: v for k, v in environ.items() if k not in doctor.BILLING_ENV}


def run_session(cmd: list[str], folder: Path, environ, run=subprocess.run) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    try:
        done = run(cmd, cwd=str(folder), env=_clean_env(environ), capture_output=True, text=True,
                   timeout=SESSION_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SessionFailed(f"the CLI session did not finish ({type(exc).__name__})") from None
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        reply = None
    if done.returncode != 0 or not isinstance(reply, dict) or not isinstance(reply.get("result"), str):
        raise SessionFailed(f"the CLI session failed (exit code {done.returncode}) or returned no result")
    return reply["result"]


def _last(text: str, label: str) -> str | None:
    found = [line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith(label + ":")]
    return found[-1] if found else None


def _message(project: str, task: str, role_line: str, notes: str) -> str:
    return f"Project: {project}\nTask: {task}\n{role_line}" + (f"\n\nNotes:\n{notes}" if notes else "")


class _Worker:
    role = ""

    def __init__(self, instance: Instance, claude: list[str], source: usage.UsageSource, environ, home: Path | None = None,
                 run=subprocess.run):
        self.instance, self.claude, self.source, self.environ, self.home, self.run = instance, claude, source, environ, home, run
        self.actor = ROLES[self.role]["actor"]

    def _start(self, key, attempt, message, now) -> tuple[str, Path]:
        project = key[1]
        guard(self.instance, project, self.claude, self.source, now, self.environ, self.home, self.run)
        folder = workdir(self.instance, project)
        cmd = command(self.claude, self.role, message, prompt_file(self.instance, self.role), folder,
                      session_id(self.instance, key, self.role, attempt))
        return run_session(cmd, folder, self.environ, self.run), folder


class ClaudeExecutor(_Worker):
    role = "executor"

    def work(self, key: tuple[str, str, str], attempt: int, now: datetime, notes: str = "") -> str:
        """Run the session; returns the evidence path the executor named, which must be a file inside its working directory."""
        message = _message(key[1], key[2], "This is attempt %d." % attempt, notes)
        text, folder = self._start(key, attempt, message, now)
        named = _last(text, "EVIDENCE")
        target = (folder / named).resolve() if named else None
        if target is None or folder.resolve() not in target.parents or not target.is_file():
            raise SessionFailed("the executor did not name an evidence file inside its working directory")
        return str(target)


class ClaudeReviewer(_Worker):
    role = "reviewer"

    def judge(self, key: tuple[str, str, str], attempt: int, evidence: str, executor_actor: str, now: datetime) -> tuple[str, str]:
        """Run the session; returns (verdict, the reviewer's notes). A reviewer that is the executor is refused."""
        if executor_actor == self.actor:
            raise Refused(["the reviewer must not be the executor"])
        message = _message(key[1], key[2], f"Evidence: {evidence}", "")
        text, _ = self._start(key, attempt, message, now)
        verdict = (text.strip().splitlines() or [""])[-1].strip()
        if verdict not in ("VERDICT: pass", "VERDICT: rework", "VERDICT: blocked"):
            raise SessionFailed("the reviewer's last line was not a VERDICT line")
        return verdict.split(": ", 1)[1], text.strip()
