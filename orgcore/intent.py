"""Owner intent files: one Markdown page per project at projects/intent/<project>.md.

Each entry keeps the owner's own words, the date, and where the words came from, so a task row can point back to the
sentence it serves:

    - [I-001] 2026-01-05 | source: chat-2026-01-05 | "Keep the garden plan under one page."
    - [I-002] 2026-01-09 | source: chat-2026-01-09 | "Add a watering schedule." | superseded-by: I-003

A task row cites intent with `intent: I-001` (several: `intent: I-001, I-004`) anywhere in its text. A superseded entry
stays in the file so the history is readable, but a row may not cite it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import backlog
from .config import SLUG, Instance
from .fsio import atomic_write, file_lock

ENTRY_RE = re.compile(
    r'^- \[(?P<id>I-\d{3,})\] (?P<date>\d{4}-\d\d-\d\d) \| source: (?P<source>[^|]+?) \| "(?P<quote>.+?)"'
    r'(?: \| superseded-by: (?P<by>I-\d{3,}))?$')
REF_RE = re.compile(r"\bintent:\s*(I-\d{3,}(?:\s*,\s*I-\d{3,})*)")


class IntentError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Intent:
    id: str
    date: str
    source: str
    quote: str
    superseded_by: str | None = None


def path_of(root: Path, project: str) -> Path:
    if not re.fullmatch(SLUG, project):
        raise IntentError([f"'{project}' is not a valid project id"])
    return root / "projects" / "intent" / f"{project}.md"


def parse(text: str, name: str = "intent file") -> list[Intent]:
    """Every problem is reported together with its line number; a bad file is never half-read."""
    entries, errors, seen = [], [], set()
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        if not line.startswith("- ["):
            continue  # headings and prose are allowed around the entries
        match = ENTRY_RE.match(line)
        if not match:
            errors.append(f'{name} line {number}: malformed entry; expected '
                          f'- [I-001] YYYY-MM-DD | source: <id> | "<owner words>"')
            continue
        try:
            date.fromisoformat(match["date"])
        except ValueError:
            errors.append(f"{name} line {number}: '{match['date']}' is not a real date")
            continue
        if match["id"] in seen:
            errors.append(f"{name} line {number}: duplicate id {match['id']}")
        seen.add(match["id"])
        entries.append(Intent(match["id"], match["date"], match["source"].strip(), match["quote"], match["by"]))
    for entry in entries:
        if entry.superseded_by and entry.superseded_by not in seen:
            errors.append(f"{name}: {entry.id} is superseded by {entry.superseded_by}, which does not exist")
    if errors:
        raise IntentError(errors)
    return entries


def read(root: Path, project: str) -> list[Intent]:
    path = path_of(root, project)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return parse(text, f"projects/intent/{project}.md")


def add(root: Path, project: str, quote: str, source: str, day: str, supersedes: str | None = None) -> Intent:
    """Append one entry with the next free id. The file is parsed first: a damaged file is reported, never overwritten."""
    if not quote.strip() or '"' in quote or "\n" in quote:
        raise IntentError(['the owner\'s words must be non-empty, on one line, without double quotes (use single quotes)'])
    if not source.strip() or "|" in source or "\n" in source:
        raise IntentError(["source must be non-empty, on one line, without '|'"])
    date.fromisoformat(day)
    path = path_of(root, project)
    with file_lock(path):
        existing = read(root, project)
        if supersedes and supersedes not in {e.id for e in existing}:
            raise IntentError([f"cannot supersede {supersedes}: no such entry in {project}"])
        new_id = f"I-{max([int(e.id[2:]) for e in existing], default=0) + 1:03d}"
        text = path.read_text(encoding="utf-8") if path.exists() else f"# Intent: {project}\n\n"
        if supersedes:
            lines = text.split("\n")
            for i, raw in enumerate(lines):
                if raw.startswith(f"- [{supersedes}]") and "| superseded-by:" not in raw:
                    lines[i] = f"{raw.rstrip()} | superseded-by: {new_id}"
            text = "\n".join(lines)
        if not text.endswith("\n"):
            text += "\n"
        text += f'- [{new_id}] {day} | source: {source.strip()} | "{quote.strip()}"\n'
        atomic_write(path, text.encode("utf-8"))
    return Intent(new_id, day, source.strip(), quote.strip())


def references(text: str) -> list[str]:
    return [i for group in REF_RE.findall(text) for i in re.findall(r"I-\d{3,}", group)]


def problems(instance: Instance, board: backlog.Backlog) -> list[str]:
    """For `check`: unreadable intent files, and task rows citing an intent that is missing or superseded."""
    out, loaded = [], {}
    folder = instance.root / "projects" / "intent"
    if folder.is_dir():
        for path in sorted(folder.glob("*.md")):
            try:
                loaded[path.stem] = parse(path.read_text(encoding="utf-8"), f"projects/intent/{path.name}")
            except IntentError as exc:
                out.extend(exc.errors)
                loaded[path.stem] = None
    for task in board.tasks:
        cited = references(task.text)
        if not cited:
            continue
        entries = loaded.get(task.project)
        where = f"projects/backlog.md line {task.index + 1}"
        if task.project not in loaded:
            out.append(f"{where}: cites {', '.join(cited)} but projects/intent/{task.project}.md does not exist")
        elif entries is None:
            continue  # the file's own errors are already listed
        else:
            known = {e.id: e for e in entries}
            for ident in cited:
                if ident not in known:
                    out.append(f"{where}: cites {ident}, which is not in projects/intent/{task.project}.md")
                elif known[ident].superseded_by:
                    out.append(f"{where}: cites {ident}, superseded by {known[ident].superseded_by}")
    return out
