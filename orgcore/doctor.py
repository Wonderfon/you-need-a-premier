"""Read-only checks of this machine before guided setup. Nothing here installs software, signs in, spends or sends.

The subscription check reads `claude auth status --json` and keeps two facts from it: whether a login exists and which
method it uses. Nothing else from that output (account, organization, plan) is stored or printed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MIN_PYTHON = (3, 11)
SUBSCRIPTION_METHODS = ("claude.ai", "oauth_token")
# Any of these makes the CLI bill an API account or a cloud provider instead of using the subscription login.
BILLING_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
               "CLAUDE_CODE_USE_FOUNDRY")
STATUS_TIMEOUT_SECONDS = 30

INSTALL_HELP = {
    "windows": ["Recommended (native installer), in PowerShell:  irm https://claude.ai/install.ps1 | iex",
                "Alternative:  winget install Anthropic.ClaudeCode"],
    "other": ["Recommended (native installer):  curl -fsSL https://claude.ai/install.sh | bash"],
}


@dataclass
class Auth:
    ok: bool
    method: str | None = None
    problems: list[str] = field(default_factory=list)


def python_ok(version=None) -> bool:
    return tuple((version or sys.version_info)[:2]) >= MIN_PYTHON


def find_claude(explicit: list[str] | None = None) -> list[str] | None:
    """The command that starts the official CLI, or None. `explicit` is a full command list (used by tests)."""
    if explicit:
        return list(explicit)
    found = shutil.which("claude")
    return [found] if found else None


def install_help(platform: str | None = None) -> list[str]:
    return INSTALL_HELP["windows" if (platform or sys.platform).startswith("win") else "other"]


def _settings_files(home: Path | None, cwd: Path | None) -> list[Path]:
    paths = []
    if home:
        paths.append(home / ".claude" / "settings.json")
    if cwd:
        paths += [cwd / ".claude" / "settings.json", cwd / ".claude" / "settings.local.json"]
    return paths


def settings_problems(home: Path | None, cwd: Path | None) -> list[str]:
    """Names only: settings files that point the CLI at an API key. File contents are never returned."""
    problems = []
    for path in _settings_files(home, cwd):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("apiKeyHelper"):
            problems.append(f"{path.name} sets apiKeyHelper")
        env = data.get("env")
        if isinstance(env, dict):
            problems += [f"{path.name} sets {name} in env" for name in BILLING_ENV if name in env]
    return problems


def subscription(claude_cmd: list[str], environ=None, home: Path | None = None, cwd: Path | None = None,
                 run=subprocess.run) -> Auth:
    """Pass only if the CLI is signed in with a subscription login and nothing redirects billing elsewhere."""
    environ = os.environ if environ is None else environ
    problems = [f"{name} is set in the environment" for name in BILLING_ENV if environ.get(name)]
    problems += settings_problems(home, cwd)
    try:
        done = run([*claude_cmd, "auth", "status", "--json"], capture_output=True, text=True,
                   timeout=STATUS_TIMEOUT_SECONDS)
        data = json.loads(done.stdout) if done.stdout.strip() else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return Auth(False, None, problems + ["could not read the CLI sign-in status (run `claude auth status`)"])
    method = data.get("authMethod") if isinstance(data, dict) else None
    if not (isinstance(data, dict) and data.get("loggedIn") is True):
        problems.append("the CLI is not signed in (run `claude auth login` and choose the subscription option)")
    elif method not in SUBSCRIPTION_METHODS:
        problems.append("the CLI is signed in with a method that is not a subscription login")
    return Auth(not problems, method if not problems else None, problems)


def version(claude_cmd: list[str], run=subprocess.run) -> str | None:
    try:
        done = run([*claude_cmd, "--version"], capture_output=True, text=True, timeout=STATUS_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = done.stdout.strip().splitlines()
    return first[0][:60] if done.returncode == 0 and first else None
