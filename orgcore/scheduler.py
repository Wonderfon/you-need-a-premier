"""One dispatch decision at a time.

Rule: among lines that have a todo or rework row, pick the one with the lowest (spend + typical) / weight, where spend is
the account usage (percentage points of the tracking window) the line's runs consumed in the last 7 days and weight is
the board's Weight row. `typical` is what one run is assumed to cost (mean of the measured runs, else 1 point); a run
that was never measured is charged `typical`, never zero. Ties go to the line listed first. Inside the line a row sent back for rework is taken first, otherwise the
first todo row in board order.
Only one row is dispatched per call, and only if the configured concurrency, the usage gate and the weight
consistency check all allow it. Everything runs under the board lock, so two schedulers never claim the same row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Callable, Iterable

from . import backlog, ledger, usage
from .check import weight_problems
from .config import Instance

SPEND_DAYS = 7
UNMEASURED_COST = 1.0  # points of the tracking window assumed per run until a run has been measured
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


def _settled(events: list[dict]) -> dict[tuple[str, int], float | None]:
    return {(e["task"], e["attempt"]): e["delta"] for e in events if e.get("event") == "usage_settled"}


def typical_cost(events: list[dict], now: datetime, days: int = SPEND_DAYS) -> float:
    """What one run is assumed to cost when its consumption was not measured: the mean of the measured runs in the
    window, or UNMEASURED_COST points when nothing has been measured yet. Never zero."""
    cutoff = now - timedelta(days=days)
    measured = [e["delta"] for e in events if e.get("event") == "usage_settled" and e["delta"] is not None
                and backlog.parse_time(e["ts"]) > cutoff]
    return sum(measured) / len(measured) if measured and sum(measured) > 0 else UNMEASURED_COST


def recent_spend(events: list[dict], now: datetime, days: int = SPEND_DAYS) -> dict[str, float]:
    """Per line, the account usage (percentage points of the tracking window) consumed by its runs in the last `days`
    days. A run whose consumption was not measured counts as `typical_cost`; it never counts as free."""
    cutoff = now - timedelta(days=days)
    settled, estimate = _settled(events), typical_cost(events, now, days)
    spend: dict[str, float] = {}
    for event in events:
        if event.get("event") == "dispatched" and backlog.parse_time(event["ts"]) > cutoff:
            delta = settled.get((event["task"], event["attempt"]))
            spend[event["line"]] = spend.get(event["line"], 0.0) + (estimate if delta is None else delta)
    return spend


def settle(instance: Instance, source: usage.UsageSource, key: tuple[str, str, str], now: datetime | None = None) -> dict:
    """After a run: record how many points of the tracking window it consumed (reading now minus reading at dispatch).
    Recorded as unmeasured (delta null, with the reason) when either reading is missing or the window reset in between."""
    moment = now or usage.now_utc()
    events = ledger.read(instance.root)
    start = ledger.for_task(events, key, "dispatched")[-1]
    window = usage.tracking_window(instance.policy)["name"]
    delta, reason = None, "no reading at dispatch"
    if start.get("used_before") is not None:
        try:
            later = [r for r in source.observe() if usage.canonical(r.window) == usage.canonical(window)]
        except usage.UsageUnavailable as exc:
            later, reason = [], f"usage source unavailable: {exc}"
        else:
            reason = "no reading after the run"
        if later:
            after = max(later, key=lambda r: r.observed_at)
            if format_reset(after.resets_at) != start["resets_at"]:
                reason = "the window reset during the run"
            elif after.used_percent < start["used_before"]:
                reason = "the reading went down"
            else:
                delta, reason = after.used_percent - start["used_before"], "measured"
    return ledger.append(instance.root, ledger.RUNS, moment, event="usage_settled", task=ledger.task_id(key), line=start["line"],
                         attempt=start["attempt"], window=window, delta=delta, reason=reason)


def format_reset(moment: datetime) -> str:
    return backlog.format_time(moment)


def choose(board: backlog.Backlog, spend: dict[str, float], projects: Iterable[str] | None = None,
           estimate: float = UNMEASURED_COST, accept: Callable[[backlog.Task], bool] | None = None) -> backlog.Task | None:
    wanted = set(projects) if projects is not None else None
    best, best_score = None, None
    for line, weight in board.weights.items():
        if weight is None:
            continue
        rows = [t for t in board.tasks if t.section == line and t.state in DISPATCHABLE
                and (wanted is None or t.project in wanted) and (accept is None or accept(t))]
        row = next((t for t in rows if t.state == "rework"), rows[0] if rows else None)  # sent-back work goes first
        if row is None:
            continue
        score = Fraction(spend.get(line, 0) + estimate) / Fraction(weight)
        if best_score is None or score < best_score:
            best, best_score = row, score
    return best


def dispatch(instance: Instance, source: usage.UsageSource, actor: str, now: datetime | None = None,
             projects: Iterable[str] | None = None, accept: Callable[[backlog.Task], bool] | None = None) -> Dispatch | Refusal:
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
        task = choose(board, recent_spend(events, moment), projects, typical_cost(events, moment), accept)
        if task is None:
            return Refusal("no_work", ["no todo or rework row to dispatch"])
        try:
            readings = source.observe()
            gate = usage.evaluate(instance.policy, readings, moment)
        except usage.UsageUnavailable as exc:
            return Refusal("usage", [f"usage source unavailable: {exc}"])
        if not gate.allowed:
            return Refusal("usage", gate.reasons)
        attempt = len(ledger.for_task(events, task.key, "dispatched")) + 1
        # Ledger first: a crash between the two writes over-counts spend by one, it never loses a dispatch.
        track = usage.canonical(usage.tracking_window(instance.policy)["name"])
        start = max((r for r in readings if usage.canonical(r.window) == track), key=lambda r: r.observed_at)
        ledger.append(instance.root, ledger.RUNS, moment, event="dispatched", task=ledger.task_id(task.key),
                      line=task.section, project=task.project, text=task.text, actor=actor, attempt=attempt,
                      used_before=start.used_percent, resets_at=format_reset(start.resets_at))
        board.set_state(task, "running", backlog.format_time(moment))
        return Dispatch(task, actor, attempt)

    return backlog.update(instance.backlog_path, change)
