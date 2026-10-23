"""One pass over an instance that reports every problem together: configuration, task board, and how they disagree.

Weights: the task board's `Weight:` rows are the authority (the board is the ledger and is edited while running).
`org.json` carries the initial value and must agree with the board.
"""
from __future__ import annotations

from pathlib import Path

from . import backlog, config, intent

BOARD = "projects/backlog.md"


def weight_problems(org: dict, board: backlog.Backlog) -> list[str]:
    problems = []
    for line in org["lines"]:
        seen = board.weights.get(line["id"], "missing")
        if seen == "missing":
            problems.append(f"{BOARD}: line '{line['id']}' has no section (org.json defines it)")
        elif seen is None:
            problems.append(f"{BOARD}: line '{line['id']}' has no Weight row (org.json says {line['weight']:g}%)")
        elif abs(seen - line["weight"]) > 1e-9:
            problems.append(f"weight mismatch for line '{line['id']}': org.json {line['weight']:g}, {BOARD} {seen:g}; "
                            f"the board is authoritative, update org.json to match")
    return problems


def duplicate_tasks(board: backlog.Backlog) -> list[str]:
    seen, problems = {}, []
    for task in board.tasks:
        if task.key in seen:
            problems.append(f"{BOARD} line {task.index + 1}: duplicate of line {seen[task.key] + 1} "
                            f"(same line, project and text); task rows must be unique")
        seen.setdefault(task.key, task.index)
    return problems


def check_instance(root: str | Path | None) -> tuple[list[str], config.Instance | None]:
    errors: list[str] = []
    instance = None
    try:
        instance = config.load_instance(root)
    except config.ConfigError as exc:
        errors.extend(exc.errors)
    board = None
    if instance is not None:
        path = instance.backlog_path
    else:
        try:
            path = config.resolve_instance(root) / BOARD
        except config.ConfigError:
            return errors, None
    try:
        board = backlog.read(path)
    except FileNotFoundError:
        errors.append(f"{BOARD}: file not found at {path}")
    except backlog.ParseError as exc:
        errors.extend(f"{BOARD} line {number}: {message}" for number, message in exc.errors)
    if board is not None:
        errors.extend(duplicate_tasks(board))
        if instance is not None:
            try:
                board.check_against(instance.line_projects())
            except backlog.ParseError as exc:
                errors.extend(f"{BOARD} line {number}: {message}" if number else f"{BOARD}: {message}"
                              for number, message in exc.errors)
            errors.extend(weight_problems(instance.org, board))
            errors.extend(intent.problems(instance, board))
    return errors, instance
