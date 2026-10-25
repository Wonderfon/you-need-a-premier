"""Switching from the offline demo to real Claude workers, and running one real unit.

Order, each step explicit and the owner's own:
  1. `preview` shows what would be handed to a model: project, folder, the one task, the usage limits.
  2. `grant` writes a scoped consent into policy.json (live_enabled true, the one project, the one task). Only after the
     owner typed yes; only for a real (non-demo) project that has that task waiting; only after the offline rehearsal.
  3. `readiness` re-checks everything (sign-in, CLI options, every usage window) without starting a session. A failing
     check leaves the setup in `live_waiting_for_usage`; the demo result is never withdrawn. No probe task is run to
     obtain a usage reading: readings come from an interactive session's status data (see docs/real-workers.md).
  4. `run_unit` takes the consented row through one executor session and one reviewer session, then records what the unit
     consumed. If any check fails, it lists the reasons and starts nothing.
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from . import adapter, backlog, config, doctor, ledger, scheduler, usage, workflow
from . import setup as guided
from .config import Instance

DISPATCHABLE = scheduler.DISPATCHABLE


def real_projects(instance: Instance) -> list[dict]:
    return [{**p, "line": line["id"]} for line in instance.org["lines"] for p in line["projects"] if not p.get("demo")]


def waiting_rows(instance: Instance, project_id: str) -> list[backlog.Task]:
    return [t for t in backlog.read(instance.backlog_path).tasks if t.project == project_id and t.state in DISPATCHABLE]


def preview(instance: Instance, project_id: str, task_text: str) -> list[str]:
    folder = adapter.workdir(instance, project_id)
    lines = [f"Project:  {project_id}", f"Folder:   {folder}", f"Task:     {task_text}", "Usage limits (a session starts only below both):"]
    for w in instance.policy["subscription_windows"]:
        lines.append(f"  {w['name']}: stop at {100 - w['reserve_percent']:g}% used (your reserve is {w['reserve_percent']:g}%), "
                     f"or at {w['max_share_percent']:g}% used, whichever comes first")
    lines += ["Two separate sessions of the official CLI run on your subscription login: an executor (files in the folder only,",
              "no shell, no network) and a reviewer (read-only). Nothing is sent, deployed or bought. The consent covers this one task."]
    return lines


def _setup_phase(root: Path):
    try:
        return guided.load_state(root)
    except guided.SetupError:
        return None


def grant(root: Path, project_id: str, task_text: str, now: datetime) -> None:
    """Write the scoped consent. Raises ValueError with the reason when it must not be written."""
    instance = config.load_instance(root)
    state = _setup_phase(root)
    if state and state["phase"] not in ("demo_ready", "live_waiting_for_usage", "live_ready"):
        raise ValueError("run the offline rehearsal and rate it first; real workers are offered after it")
    if project_id not in {p["id"] for p in real_projects(instance)}:
        raise ValueError(f"'{project_id}' is not a real project of this instance (demo projects cannot be given to a model)")
    if not any(t.text == task_text for t in waiting_rows(instance, project_id)):
        raise ValueError(f"no waiting row '{task_text}' for project '{project_id}' on the board")
    policy = {**instance.policy, "live_enabled": True,
              "live_consent": {"granted_at": backlog.format_time(now), "projects": [project_id], "task_scope": task_text}}
    config.save_file(root, "policy.json", policy, overwrite=True)


def revoke(root: Path) -> None:
    instance = config.load_instance(root)
    config.save_file(root, "policy.json", {**instance.policy, "live_enabled": False, "live_consent": None}, overwrite=True)
    state = _setup_phase(root)
    if state and state["phase"] in ("live_waiting_for_usage", "live_ready"):
        guided.set_phase(root, "demo_ready")


def readiness(root: Path, claude: list[str], source: usage.UsageSource, now: datetime, environ, home: Path | None = None,
              run=subprocess.run) -> list[str]:
    """Reasons real work cannot start right now (empty: it can). Moves the setup phase to match; starts no session."""
    instance = config.load_instance(root)
    consent = instance.policy["live_consent"]
    reasons = list(doctor.cli_gaps(claude, run))
    try:
        adapter.guard(instance, consent["projects"][0] if consent else "", claude, source, now, environ, home, run)
    except adapter.Refused as exc:
        reasons += exc.reasons
    state = _setup_phase(root)
    if state and state["phase"] in ("demo_ready", "live_waiting_for_usage", "live_ready"):
        target = "live_waiting_for_usage" if reasons else "live_ready"
        if state["phase"] != target:
            guided.set_phase(root, target)  # the demo's own record (demo_run_id) is left as it was
    return reasons


def run_unit(root: Path, claude: list[str], environ, home: Path | None = None, run=subprocess.run,
             now: datetime | None = None, source: usage.UsageSource | None = None) -> list[str]:
    """One executor session and one reviewer session on the consented row. Returns the lines to show.
    Raises adapter.Refused (nothing started) or adapter.SessionFailed (the row is left where it was)."""
    moment = now or usage.now_utc()
    source = source or usage.JsonFileUsage(usage.usage_path(root))
    instance = config.load_instance(root)
    consent = instance.policy["live_consent"]
    if not instance.policy["live_enabled"] or consent is None:
        raise adapter.Refused(["live mode is off: no consent has been written (python -m orgcore enable-live ...)"])
    project, scope = consent["projects"][0], consent["task_scope"]
    reasons = readiness(root, claude, source, moment, environ, home, run)
    if reasons:
        raise adapter.Refused(reasons)
    row = next((t for t in backlog.read(instance.backlog_path).tasks if t.project == project and t.text == scope), None)
    if row is None:
        raise adapter.Refused([f"the consented task '{scope}' is not on the board"])
    executor = adapter.ClaudeExecutor(instance, claude, source, environ, home, run)
    reviewer = adapter.ClaudeReviewer(instance, claude, source, environ, home, run)
    out = []
    if row.state in DISPATCHABLE:
        result = scheduler.dispatch(instance, source, executor.actor, moment, [project], lambda t: t.text == scope)
        if isinstance(result, scheduler.Refusal):
            raise adapter.Refused(result.reasons)
        key, attempt = result.task.key, result.attempt
        out.append(f"{row.state} -> running: {executor.actor} started attempt {attempt}")
        with workflow.claim_lock(instance.root, key):
            notes = ""
            if attempt > 1:
                notes = (ledger.for_task(ledger.read(root), key, "reviewed") or [{"evidence": ""}])[-1]["evidence"]
            evidence = executor.work(key, attempt, moment, notes)
            workflow.submit(instance, key, executor.actor, evidence, moment)
        out.append(f"running -> review: {executor.actor} submitted {evidence}")
    elif row.state == "review":
        key = row.key
        last = ledger.for_task(ledger.read(root), key, "submitted")[-1]
        attempt, evidence = last["attempt"], last["evidence"]
        out.append("a submitted result was waiting for review")
    elif row.state == "running":
        raise adapter.Refused([f"the row is running; if its worker died, put it back with: python -m orgcore recover {root} --max-age-minutes 0"])
    else:
        raise adapter.Refused([f"the consented task is '{row.state}'; grant a new consent to run another task"])
    events = ledger.read(root)
    executor_actor = ledger.for_task(events, key, "dispatched")[-1]["actor"]
    verdict, notes = reviewer.judge(key, attempt, evidence, executor_actor, moment)
    workflow.review(instance, key, reviewer.actor, verdict, notes, moment)
    out.append(f"review -> {workflow.VERDICTS[verdict]}: {reviewer.actor} says {verdict}")
    reading = scheduler.settle(instance, source, key, moment)
    out.append("usage consumed: " + (f"{reading['delta']:g} points of {reading['window']}" if reading["delta"] is not None
                                     else f"not measured ({reading['reason']}); counted as a typical run"))
    return out
