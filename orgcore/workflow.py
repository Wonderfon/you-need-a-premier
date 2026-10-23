"""Roles around a dispatched row: the executor can only hand work to review; only a different identity can judge it.

Enforcement lives in this deterministic layer (identity checks against the run ledger), not in prompts. The board is a
plain text file, so anything that can write the file directly can bypass it; agents should be given these functions,
not file write access to the board.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from . import backlog, ledger
from .config import Instance
from .fsio import file_lock, held_elsewhere
from .usage import now_utc

VERDICTS = {"pass": "done", "rework": "rework", "blocked": "blocked"}
RATINGS = ("useful", "not_useful", "rework")


class RoleError(PermissionError):
    pass


def claim_path(root: Path, key: tuple[str, str, str]) -> Path:
    return root / "state" / "claims" / ledger.task_id(key)


def claim_lock(root: Path, key: tuple[str, str, str], timeout: float = 15.0):
    """Held by whoever is working on a running row. The OS drops it if that process dies, which is how recovery
    tells a live long-running worker from an abandoned row."""
    return file_lock(claim_path(root, key), timeout)


def worker_alive(root: Path):
    return lambda task: held_elsewhere(claim_path(root, task.key))


def _last_dispatch(root: Path, key) -> dict:
    events = ledger.for_task(ledger.read(root), key, "dispatched")
    if not events:
        raise RoleError(f"task {ledger.task_id(key)} has no dispatch record")
    return events[-1]


def _short(text: str, limit: int = 100) -> str:
    return re.sub(r"[\]\r\n]+", " ", text).strip()[:limit]


def submit(instance: Instance, key, actor: str, evidence: str, now: datetime | None = None) -> backlog.Task:
    """Executor hands a running row to review. The only state change an executor can make."""
    if not evidence.strip():
        raise ValueError("submit needs evidence (what was produced and where)")
    moment = now or now_utc()

    def change(board: backlog.Backlog):
        task = board.find(key, "running")
        last = _last_dispatch(instance.root, key)
        if last["actor"] != actor:
            raise RoleError(f"'{actor}' does not hold this row; it was dispatched to '{last['actor']}'")
        ledger.append(instance.root, ledger.RUNS, moment, event="submitted", task=ledger.task_id(key), actor=actor,
                      attempt=last["attempt"], evidence=evidence)
        board.set_state(task, "review")
        return task

    return backlog.update(instance.backlog_path, change)


def review(instance: Instance, key, reviewer: str, verdict: str, evidence: str, now: datetime | None = None) -> backlog.Task:
    """Independent verdict: pass -> done, rework -> rework, blocked -> blocked. Needs evidence; the reviewer must not
    be any identity that executed an attempt of this row."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    if not evidence.strip():
        raise ValueError("a verdict needs evidence")
    moment = now or now_utc()

    def change(board: backlog.Backlog):
        task = board.find(key, "review")
        events = ledger.read(instance.root)
        executors = {e["actor"] for e in ledger.for_task(events, key, "dispatched")}
        if reviewer in executors:
            raise RoleError(f"'{reviewer}' executed this row and cannot review it")
        last = ledger.for_task(events, key, "dispatched")[-1]
        ledger.append(instance.root, ledger.RUNS, moment, event="reviewed", task=ledger.task_id(key), actor=reviewer,
                      attempt=last["attempt"], verdict=verdict, evidence=evidence)
        board.set_state(task, VERDICTS[verdict], "" if verdict == "pass" else _short(evidence))
        return task

    return backlog.update(instance.backlog_path, change)


def record_feedback(instance: Instance, key, rating: str, note: str = "", now: datetime | None = None) -> dict:
    """The owner's verdict on a finished row: useful, not_useful or rework. Recorded; it does not change the board."""
    if rating not in RATINGS:
        raise ValueError(f"rating must be one of {', '.join(RATINGS)}")
    if not any(t.key == key and t.state == "done" for t in backlog.read(instance.backlog_path).tasks):
        raise KeyError(f"no done task matching {key}; feedback is only taken for finished rows")
    return ledger.append(instance.root, ledger.FEEDBACK, now or now_utc(), event="feedback", task=ledger.task_id(key),
                         rating=rating, note=note)
