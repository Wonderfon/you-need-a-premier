"""Entry point for first-time setup. Standard library only.

    python bootstrap.py [--instance DIR]

It checks this machine, asks for your consent, and then starts the official Claude Code CLI with the coordinator's
setup prompt so that the interview happens in the CLI, on your own subscription login. This script never asks for a
credential, never calls a model itself, and never changes anything outside the instance directory you name.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from orgcore import backlog, check, config, doctor, feedback, live, rehearsal, statusline, usage  # noqa: E402
from orgcore import setup as guided  # noqa: E402

PROMPT = HERE / "prompts" / "setup-coordinator.md"
DEMO = HERE / "examples" / "demo"
FIRST_MESSAGE = "Begin the guided setup. Read the setup state at the end of your instructions, then continue with the next question."
CONSENT = """\
Before we start, this is what will happen:
  - The official Claude Code command-line tool opens and the coordinator interviews you, one question at a time.
  - It uses your own Claude subscription sign-in. The conversation counts against your subscription usage.
  - Your answers are saved as files in the instance directory you chose. Nothing is sent anywhere else.
  - You will never be asked for a password, API key or token. Do not paste one.
  - No paid API is used, nothing is bought, and nobody is contacted.
  - The first time Claude Code opens in a new folder, it asks “Is this a project you created or one you trust?”; “No, exit” is selected by default, so press the down arrow and Enter to choose “Yes, I trust this folder.” If you exit, run this script again to continue.
You can stop at any time and run this script again; it continues from the next unanswered question."""


def render_prompt(root: Path) -> Path:
    """Fill the instance path and the setup command into the coordinator prompt; the copy lives in the instance."""
    command = f'"{sys.executable}" -m orgcore'
    text = PROMPT.read_text(encoding="utf-8").replace("{{INSTANCE}}", str(root)).replace("{{ORGCORE}}", command)
    target = root / "state" / "setup-coordinator.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    (root / "state" / "inbox").mkdir(exist_ok=True)
    if guided.load_state(root):
        text += "\n## Setup state when this session started\n\n" + "\n".join(guided.describe(root)) + "\n"
    target.write_text(text, encoding="utf-8")
    return target


def launch_command(claude: list[str], prompt: Path, root: Path) -> list[str]:
    """The session gets two tools, Read and Write, and no shell. Windows cannot express "Write, but only here" as an allow
    rule, so a PreToolUse hook allows exactly the answer files in state/inbox/ and a PostToolUse hook saves them."""
    hook = lambda *extra: [{"matcher": "Write", "hooks": [{"type": "command", "command":  # noqa: E731
                                                       statusline.hook_command(sys.executable, root, "setup_hook.py") + "".join(f" {e}" for e in extra)}]}]
    # the status line is how an interactive session hands its usage windows to state/usage.json
    settings = {"statusLine": {"type": "command", "command": statusline.hook_command(sys.executable, root)},
                "hooks": {"PreToolUse": hook("guard"), "PostToolUse": hook()}}
    # the first message comes before the options: --tools takes a list and would swallow a trailing argument
    return [*claude, FIRST_MESSAGE, "--append-system-prompt-file", str(prompt), "--add-dir", str(root),
            "--settings", json.dumps(settings), "--tools", "Read,Write"]

def local_checks(out, claude: list[str] | None, run) -> None:
    out(f"  Python {sys.version_info.major}.{sys.version_info.minor}: " + ("ok" if doctor.python_ok() else
        f"too old, version {doctor.MIN_PYTHON[0]}.{doctor.MIN_PYTHON[1]} or newer is needed"))
    if claude:
        out(f"  Claude Code CLI: found ({doctor.version(claude, run) or 'version unknown'})")
    else:
        out("  Claude Code CLI: not found. Install it first:")
        for line in doctor.install_help():
            out(f"      {line}")


def offline_rehearsal(root: Path, ask, out, claude: list[str] | None = None, environ=None, run=subprocess.run,
                      home: Path | None = None) -> int:
    copy = root / "rehearsal"
    if not copy.exists():
        shutil.copytree(DEMO, copy)
    errors, instance = check.check_instance(copy)
    if errors:
        out("The rehearsal copy is not valid:\n" + "\n".join(errors))
        return 1
    state = guided.load_state(root)
    if state and state["phase"] in ("collecting", "validated"):  # a later phase is never moved backwards
        guided.set_phase(root, "demo_running", "offline-rehearsal")
    out("Offline rehearsal: fake workers take one pretend task through the whole workflow. No model, nothing sent.\n")
    rehearsal.rehearse(instance, None, emit=out)
    for path in sorted((copy / "demo").glob("*.txt")):
        out(f"\nThe fake worker's finished work ({path.name}):\n" + path.read_text(encoding="utf-8").rstrip())
    if not rehearsal.awaiting(instance):
        out("\nThis rehearsal already has your verdict.")
        return live_offer(root, ask, out, claude, environ, run, home) if state and state["phase"] == "demo_ready" else 0
    choice = ask("\nWas this useful?  1 = useful   2 = not useful   3 = needs rework   Enter = decide later: ").strip()
    rating = {"1": "useful", "2": "not_useful", "3": "rework"}.get(choice)
    if not rating:
        out("No verdict recorded. Run the rehearsal again, or: python -m orgcore rate " + str(copy) + " useful|not-useful|rework")
        return 0
    rehearsal.rate(instance, rating, emit=out)
    out(feedback.describe(feedback.summarize(instance))[0])
    if state:
        guided.set_phase(root, "demo_ready", "offline-rehearsal")
        return live_offer(root, ask, out, claude, environ, run, home)
    return 0


def live_offer(root: Path, ask, out, claude: list[str] | None, environ, run, home: Path | None = None) -> int:
    """After the demo has been rated: keep the offline demo (the default) or hand one task to real workers."""
    answer = ask("\nKeep using the offline demo, or enable real Claude workers?\n"
                 "  1 = keep the offline demo   2 = enable real workers: ").strip()
    if answer != "2":
        out("Staying with the offline demo. Nothing real is enabled (live_enabled stays false).")
        return 0
    if not (root / "org.json").exists():
        out("Finish the guided setup first (menu 1): real workers need your organization, projects and a task on the board.")
        return 0
    instance = config.load_instance(root)
    projects = live.real_projects(instance)
    if not projects:
        out("This organization has no real projects (only demo ones). Add one in the guided setup first.")
        return 0
    out("\nProjects real workers could take:")
    for number, project in enumerate(projects, 1):
        out(f"  {number}  {project['id']}  ({project['name']})")
    picked = ask("Project number (Enter = cancel): ").strip()
    if not picked.isdigit() or not 1 <= int(picked) <= len(projects):
        out("Cancelled; nothing was written.")
        return 0
    project = projects[int(picked) - 1]
    rows = live.waiting_rows(instance, project["id"])
    if rows:
        out(f"\nWaiting tasks of {project['id']}:")
        for number, row in enumerate(rows, 1):
            out(f"  {number}  {row.text}")
    choice = ask("Task number, or type a new task line (Enter = cancel): ").strip()
    if not choice:
        out("Cancelled; nothing was written.")
        return 0
    new_row = not (choice.isdigit() and 1 <= int(choice) <= len(rows))
    task = choice if new_row else rows[int(choice) - 1].text
    out("\nThis is what would be handed to the model:\n" + "\n".join("  " + line for line in live.preview(instance, project["id"], task)))
    if ask("\nType yes to write the consent for this one task: ").strip().lower() != "yes":
        out("No consent was written.")
        return 0
    if new_row:
        backlog.add_task(instance.backlog_path, project["line"], project["id"], task)
    try:
        live.grant(root, project["id"], task, usage.now_utc())
    except ValueError as exc:
        out(f"Not written: {exc}")
        return 1
    reasons = live.readiness(root, claude or [], usage.JsonFileUsage(usage.usage_path(root)), usage.now_utc(), environ,
                             home or Path.home(), run) if claude else ["the Claude Code CLI was not found"]
    out("\nConsent written (policy.json: live_enabled true, scoped to this project and task).")
    if reasons:
        out("Setup phase: live_waiting_for_usage. Real workers cannot start yet (the offline demo result stays valid):")
        for reason in reasons:
            out(f"  - {reason}")
        out("Headless sessions give no usage readings. Keep one interactive Claude Code session open (docs/real-workers.md),\n"
            f"then check with: python -m orgcore usage {root}")
    else:
        out("Everything needed is in place.")
    out(f"Start one real unit when you are ready: python -m orgcore run {root}")
    return 0


def start_setup(root: Path, claude: list[str] | None, out, environ, run, call, home: Path | None = None) -> int:
    if (root / "org.json").exists() and not (root / "state" / "setup.json").exists():
        out(f"{root} already holds an organization that was not created by this setup. Choose another directory.")
        return 1
    if not claude:
        out("The Claude Code CLI is needed for the interview. Install it, then run this script again:")
        for line in doctor.install_help():
            out(f"  {line}")
        return 1
    auth = doctor.subscription(claude, environ, home=home or Path.home(), cwd=root if root.exists() else None, run=run)
    if not auth.ok:
        out("The CLI is not ready to use your subscription:")
        for problem in auth.problems:
            out(f"  - {problem}")
        out("Fix this and run the script again. This script never asks for credentials.")
        return 1
    guided.start(root)
    try:
        guided.save(root, "install", {"auth_method": auth.method})
    except guided.SetupError:  # already saved earlier: keep it, or replace it only if the sign-in method changed
        saved = guided.show(root, "install")
        if auth.method not in saved:
            guided.save(root, "install", {"auth_method": auth.method}, change=True)
    prompt = render_prompt(root)
    env = dict(environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(HERE), env.get("PYTHONPATH", "")]))
    out("Starting the coordinator in the Claude Code CLI...\n")
    code = call(launch_command(claude, prompt, root), cwd=str(root), env=env)
    out("\n" + "\n".join(guided.describe(root)))
    return code or 0


def main(argv: list[str] | None = None, ask=input, out=print, claude_cmd: list[str] | None = None,
         environ=None, run=subprocess.run, call=subprocess.call, home: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bootstrap.py", description="First-time setup of the organization.")
    parser.add_argument("--instance", help="directory for your organization's files (created if missing)")
    args = parser.parse_args(argv)
    environ = dict(os.environ if environ is None else environ)
    out("Organization setup\n")
    claude = doctor.find_claude(claude_cmd)
    local_checks(out, claude, run)
    if not doctor.python_ok():
        return 1
    try:
        chosen = args.instance or ask("\nDirectory for your organization's files (a new or an existing setup folder): ").strip()
        if not chosen:
            out("No directory given.")
            return 1
        root = Path(chosen).expanduser().resolve()
        while True:
            out("\n  1  Start or resume guided setup\n  2  Run offline rehearsal\n  3  Exit")
            choice = ask("Choose 1, 2 or 3: ").strip()
            if choice == "1":
                out("\n" + CONSENT)
                if ask("\nType yes to continue: ").strip().lower() != "yes":
                    out("Nothing was started.")
                    continue
                return start_setup(root, claude, out, environ, run, call, home)
            if choice == "2":
                return offline_rehearsal(root, ask, out, claude, environ, run, home)
            if choice == "3":
                return 0
            out("Please type 1, 2 or 3.")
    except EOFError:
        out("\nNo input; nothing was started.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
