"""One dispatch decision at a time.

Rule: among lines that have a todo or rework row, pick the one with the lowest (spend + 1) / weight, where spend is
the number of dispatches the line received in the last `spend_days` (from state/runs.jsonl) and weight is the board's
Weight row. Ties go to the line listed first. Inside the line a row sent back for rework is taken first, otherwise the
first todo row in board order.
Only one row is dispatched per call, and only if the configured concurrency, the usage gate and the weight
consistency check all allow it. Everything runs under the board lock, so two schedulers never claim the same row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Iterable

from . import backlog, ledger, usage
from .check import weight_problems
from .config import Instance

SPEND_DAYS = 7
DISPATCHABLE = ("todo", "rework")


@dataclass
class Dispatch:
    task: backlog.Task
    actor: str
    attempt: int


@dataclass
class Refusal:
    code: str  # inconsistent | busy | no_work | usage
    reasons: list[str] = field(default_factory=list)


def recent_spend(events: list[dict], now: datetime, days: int = SPEND_DAYS) -> dict[str, int]:
    cutoff = now - timedelta(days=days)
    spend: dict[str, int] = {}
    for event in events:
        if event.get("event") == "dispatched" and backlog.parse_time(event["ts"]) > cutoff:
            spend[event["line"]] = spend.get(event["line"], 0) + 1
    return spend


def choose(board: backlog.Backlog, spend: dict[str, int], projects: Iterable[str] | None = None) -> backlog.Task | None:
    wanted = set(projects) if projects is not None else None
    best, best_score = None, None
    for line, weight in board.weights.items():
        if weight is None:
            continue
        rows = [t for t in board.tasks if t.section == line and t.state in DISPATCHABLE
                and (wanted is None or t.project in wanted)]
        row = next((t for t in rows if t.state == "rework"), rows[0] if rows else None)  # sent-back work goes first
        if row is None:
            continue
        score = Fraction(spend.get(line, 0) + 1) / Fraction(weight)
        if best_score is None or score < best_score:
            best, best_score = row, score
    return best


def dispatch(instance: Instance, source: usage.UsageSource, actor: str, now: datetime | None = None,
             projects: Iterable[str] | None = None) -> Dispatch | Refusal:
    moment = now or usage.now_utc()

    def change(board: backlog.Backlog):
        problems = weight_problems(instance.org, board)
        try:
            board.check_against(instance.line_projects())
        except backlog.ParseError as exc:
            problems += [message for _, message in exc.errors]
        if problems:
            return Refusal("inconsistent", problems)
        running = sum(1 for t in board.tasks if t.state == "running")
        if running >= instance.policy["max_concurrency"]:
            return Refusal("busy", [f"{running} row(s) running; max_concurrency is {instance.policy['max_concurrency']}"])
        events = ledger.read(instance.root)
        task = choose(board, recent_spend(events, moment), projects)
        if task is None:
            return Refusal("no_work", ["no todo or rework row to dispatch"])
        try:
            gate = usage.evaluate(instance.policy, source.observe(), moment)
        except usage.UsageUnavailable as exc:
            return Refusal("usage", [f"usage source unavailable: {exc}"])
        if not gate.allowed:
            return Refusal("usage", gate.reasons)
        attempt = len(ledger.for_task(events, task.key, "dispatched")) + 1
        # Ledger first: a crash between the two writes over-counts spend by one, it never loses a dispatch.
        ledger.append(instance.root, ledger.RUNS, moment, event="dispatched", task=ledger.task_id(task.key),
                      line=task.section, project=task.project, text=task.text, actor=actor, attempt=attempt)
        board.set_state(task, "running", backlog.format_time(moment))
        return Dispatch(task, actor, attempt)

    return backlog.update(instance.backlog_path, change)
