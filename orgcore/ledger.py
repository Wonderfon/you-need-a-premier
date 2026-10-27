"""Append-only JSON-lines event files under state/ (runs.jsonl, feedback.jsonl)."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from .backlog import format_time
from .fsio import file_lock

RUNS = "runs.jsonl"
FEEDBACK = "feedback.jsonl"


def task_id(key: tuple[str, str, str]) -> str:
    return hashlib.sha1("\x1f".join(key).encode("utf-8")).hexdigest()[:10]


def path_of(root: Path, name: str = RUNS) -> Path:
    return root / "state" / name


def append(root: Path, name: str, now: datetime, **event) -> dict:
    """One fsynced line. A torn last line from an earlier crash was never acknowledged to anyone; it is cut off first."""
    path = path_of(root, name)
    record = {"ts": format_time(now), **event}
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        with path.open("ab+") as stream:
            stream.seek(0)
            data = stream.read()
            if data and not data.endswith(b"\n"):
                stream.truncate(data.rfind(b"\n") + 1)
            stream.seek(0, os.SEEK_END)
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
    return record


def read(root: Path, name: str = RUNS) -> list[dict]:
    """All events. An unreadable LAST line is ignored (crash while appending); an unreadable earlier line is an error."""
    path = path_of(root, name)
    if not path.exists():
        return []
    lines = path.read_bytes().decode("utf-8").split("\n")
    events = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            if all(not rest.strip() for rest in lines[number:]):
                break
            raise ValueError(f"{path} line {number}: not valid JSON") from None
    return events


def for_task(events: list[dict], key: tuple[str, str, str], kind: str | None = None) -> list[dict]:
    wanted = task_id(key)
    return [e for e in events if e.get("task") == wanted and (kind is None or e.get("event") == kind)]
