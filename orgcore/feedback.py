"""Summary of the owner's ratings (state/feedback.jsonl) against the task board.

Three views: the latest rating of every rated row, finished rows nobody has rated yet, and rows whose latest rating
is "rework". A later rating replaces an earlier one; the file itself is never rewritten.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import backlog, ledger
from .config import Instance


@dataclass
class Summary:
    latest: dict[str, dict] = field(default_factory=dict)  # task id -> latest feedback event
    unrated: list[backlog.Task] = field(default_factory=list)
    rework: list[str] = field(default_factory=list)  # task ids
    names: dict[str, str] = field(default_factory=dict)  # task id -> "section/project: text"

    def counts(self) -> dict[str, int]:
        out = {rating: 0 for rating in ("useful", "not_useful", "rework")}
        for event in self.latest.values():
            out[event["rating"]] = out.get(event["rating"], 0) + 1
        return out


def summarize(instance: Instance, board: backlog.Backlog | None = None) -> Summary:
    board = board or backlog.read(instance.backlog_path)
    summary = Summary()
    for event in ledger.read(instance.root, ledger.FEEDBACK):
        if event.get("event") == "feedback" and "task" in event and "rating" in event:
            summary.latest[event["task"]] = event  # file order is time order; the last one wins
    for task in board.tasks:
        ident = ledger.task_id(task.key)
        summary.names[ident] = f"{task.section}/{task.project}: {task.text}"
        if task.state == "done" and ident not in summary.latest:
            summary.unrated.append(task)
    summary.rework = [ident for ident, event in summary.latest.items() if event["rating"] == "rework"]
    return summary


def describe(summary: Summary, name_limit: int = 80) -> list[str]:
    def label(ident: str) -> str:
        return (summary.names.get(ident) or f"{ident} (no longer on the board)")[:name_limit]

    counts = summary.counts()
    lines = [f"Rated rows: {len(summary.latest)} (useful {counts['useful']}, not useful {counts['not_useful']}, "
             f"rework {counts['rework']})"]
    lines.append(f"Finished rows without a rating: {len(summary.unrated)}")
    lines += [f"  - {task.section}/{task.project}: {task.text[:name_limit]}" for task in summary.unrated]
    lines.append(f"Rows the owner rated 'rework': {len(summary.rework)}")
    lines += [f"  - {label(ident)}" for ident in summary.rework]
    return lines
