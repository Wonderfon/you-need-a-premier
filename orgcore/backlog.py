"""Text task board: lossless parser, locked read-modify-write, atomic save, recovery of abandoned `running` rows.

Format (see docs/backlog-format.md):

    ## Line: research
    Weight: 60%
    - [todo] demo-notes: Summarize the reading list
    - [running 2026-01-05T10:00:00Z] demo-notes: Draft the outline
    - [blocked missing input file] demo-garden: Plan the beds
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .fsio import atomic_write, file_lock

STATES = ("todo", "running", "review", "rework", "done", "blocked")
TRANSITIONS = {
    "todo": {"running", "blocked"},
    "running": {"review", "blocked", "todo"},  # running -> todo is crash recovery
    "review": {"done", "rework", "blocked"},
    "rework": {"running", "blocked"},
    "blocked": {"todo"},
    "done": set(),
}
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_SLUG = r"[A-Za-z0-9][A-Za-z0-9_-]*"
TASK_RE = re.compile(rf"^- \[(?P<state>[a-z]+)(?: (?P<detail>[^\]]*))?\] (?P<project>{_SLUG}): (?P<text>\S.*)$")
SECTION_RE = re.compile(rf"^## Line: (?P<name>{_SLUG})\s*$")
WEIGHT_RE = re.compile(r"^Weight: (?P<value>\d+(?:\.\d+)?)%\s*$")


class ParseError(ValueError):
    """Every problem found, as (line number, message); nothing is skipped silently."""

    def __init__(self, errors: list[tuple[int, str]]):
        self.errors = errors
        super().__init__("\n".join(f"line {number}: {message}" for number, message in errors))


@dataclass
class Task:
    index: int  # position in Backlog.lines
    section: str
    project: str
    state: str
    detail: str
    text: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.section, self.project, self.text)


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=timezone.utc)


class Backlog:
    """Raw lines are the source of truth; `serialize()` of an untouched board equals the input byte for byte."""

    def __init__(self, text: str):
        self.lines = text.split("\n")
        self.weights: dict[str, float] = {}
        self.tasks: list[Task] = []
        errors: list[tuple[int, str]] = []
        section = None
        for index, raw in enumerate(self.lines):
            line = raw.rstrip("\r")
            number = index + 1
            if line.startswith("## "):
                match = SECTION_RE.match(line)
                if not match:
                    errors.append((number, "section heading must be '## Line: <name>'"))
                    section = None
                elif match["name"] in self.weights:
                    errors.append((number, f"duplicate line section '{match['name']}'"))
                    section = None
                else:
                    section = match["name"]
                    self.weights.setdefault(section, None)  # type: ignore[arg-type]
            elif line.startswith("Weight:"):
                match = WEIGHT_RE.match(line)
                if section is None:
                    errors.append((number, "weight row outside a line section"))
                elif not match or float(match["value"]) <= 0:
                    errors.append((number, "weight row must be 'Weight: <positive number>%'"))
                elif self.weights[section] is not None:
                    errors.append((number, f"second weight row in section '{section}'"))
                else:
                    self.weights[section] = float(match["value"])
            elif line.startswith("- ["):
                match = TASK_RE.match(line)
                if not match:
                    errors.append((number, "malformed task row; expected '- [state] project: text'"))
                elif match["state"] not in STATES:
                    errors.append((number, f"unknown state '{match['state']}'; expected one of {', '.join(STATES)}"))
                elif section is None:
                    errors.append((number, "task row outside a line section"))
                elif match["state"] == "running" and not _is_time(match["detail"]):
                    errors.append((number, "running row needs a UTC time like 2026-01-05T10:00:00Z"))
                else:
                    self.tasks.append(Task(index, section, match["project"], match["state"], match["detail"] or "", match["text"]))
        if errors:
            raise ParseError(errors)

    def serialize(self) -> str:
        return "\n".join(self.lines)

    def set_state(self, task: Task, state: str, detail: str = "") -> None:
        """Rewrite one row in place. Everything else, including this row's line ending, stays byte-identical."""
        if state not in TRANSITIONS.get(task.state, ()):
            raise ValueError(f"transition {task.state} -> {state} is not allowed")
        if "]" in detail or "\n" in detail:
            raise ValueError("detail must not contain ']' or a newline")
        if state == "running" and not _is_time(detail):
            raise ValueError("a running row needs a UTC time as its detail")
        raw = self.lines[task.index]
        eol = raw[len(raw.rstrip("\r")):]
        label = f"{state} {detail}" if detail else state
        self.lines[task.index] = f"- [{label}] {task.project}: {task.text}{eol}"
        task.state, task.detail = state, detail

    def find(self, key: tuple[str, str, str], state: str) -> Task:
        for task in self.tasks:
            if task.key == key and task.state == state:
                return task
        raise KeyError(f"no {state} task matching {key}")

    def add(self, section: str, project: str, text: str) -> Task:
        if section not in self.weights:
            raise KeyError(f"unknown line section '{section}'")
        if not re.fullmatch(_SLUG, project) or not text.strip() or "\n" in text:
            raise ValueError("project must be a slug and text a single non-empty line")
        last = max([t.index for t in self.tasks if t.section == section] or [self._section_start(section)])
        eol = "\r" if self.lines[last].endswith("\r") else ""
        self.lines.insert(last + 1, f"- [todo] {project}: {text.strip()}{eol}")
        for task in self.tasks:
            if task.index > last:
                task.index += 1
        task = Task(last + 1, section, project, "todo", "", text.strip())
        self.tasks.append(task)
        self.tasks.sort(key=lambda t: t.index)
        return task

    def _section_start(self, section: str) -> int:
        for index, raw in enumerate(self.lines):
            match = SECTION_RE.match(raw.rstrip("\r"))
            if match and match["name"] == section:
                following = self.lines[index + 1].rstrip("\r") if index + 1 < len(self.lines) else ""
                return index + 1 if WEIGHT_RE.match(following) else index
        raise KeyError(section)

    def check_against(self, line_projects: dict[str, set[str]]) -> None:
        """Every section and project must exist in the configuration (`{line id: {project ids}}`)."""
        errors = []
        for name in self.weights:
            if name not in line_projects:
                errors.append((0, f"section '{name}' is not a configured line"))
        for task in self.tasks:
            if task.section in line_projects and task.project not in line_projects[task.section]:
                errors.append((task.index + 1, f"project '{task.project}' is not in line '{task.section}'"))
        if errors:
            raise ParseError(errors)


def _is_time(value: str | None) -> bool:
    try:
        parse_time(value or "")
        return True
    except ValueError:
        return False


def read(path: Path) -> Backlog:
    """Lock-free read: writers replace the file atomically, so a reader sees a whole old or new file."""
    return Backlog(path.read_bytes().decode("utf-8"))


def update(path: Path, change: Callable[[Backlog], object]):
    """Locked read-modify-write; the file is rewritten only if `change` altered it. Returns what `change` returns."""
    with file_lock(path):
        board = read(path)
        before = board.serialize()
        result = change(board)
        after = board.serialize()
        if after != before:
            atomic_write(path, after.encode("utf-8"))
        return result


def add_task(path: Path, section: str, project: str, text: str) -> Task:
    return update(path, lambda board: board.add(section, project, text))


def claim(path: Path, now: datetime | None = None, section: str | None = None) -> Task | None:
    """Turn the first todo row (file order, optionally one section) into `running <now>`; None when nothing is todo."""
    stamp = format_time(now or datetime.now(timezone.utc))

    def change(board: Backlog):
        for task in board.tasks:
            if task.state == "todo" and (section is None or task.section == section):
                board.set_state(task, "running", stamp)
                return task
        return None

    return update(path, change)


def transition(path: Path, key: tuple[str, str, str], current: str, new: str, detail: str = "") -> Task:
    def change(board: Backlog):
        task = board.find(key, current)
        board.set_state(task, new, detail)
        return task

    return update(path, change)


def recover_running(path: Path, max_age: timedelta, now: datetime | None = None,
                    alive: Callable[[Task], bool] | None = None) -> list[Task]:
    """Put `running` rows older than `max_age` back to todo (their worker died); returns the rows it reset.
    A row whose worker is still alive (`alive(task)` true, e.g. it holds the claim lock) is left alone however old."""
    moment = now or datetime.now(timezone.utc)

    def change(board: Backlog):
        reset = []
        for task in board.tasks:
            if task.state == "running" and moment - parse_time(task.detail) > max_age and not (alive and alive(task)):
                reset.append(Task(task.index, task.section, task.project, "running", task.detail, task.text))
                board.set_state(task, "todo")
        return reset

    return update(path, change)
