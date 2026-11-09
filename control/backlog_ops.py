"""Small one-off backlog edits. See backlog_ops_template.py for the CONFIG format.

All row changes/validation/backup run in backlog_io.edit's existing lock. The
log is appended after a successful edit, under that same lock path (a second
acquisition); backlog and log are not a crash-atomic two-file transaction.
"""
from __future__ import annotations

from pathlib import Path

import backlog_io


def apply(section: str, *, states=(), replacements=(), additions=(), duplicate_keys=(),
          log: str, path: Path = backlog_io.BACKLOG, log_path: Path | None = None) -> Path:
    """Apply one section's edits or raise ValueError without writing it.

    states: (unique row prefix, new text INSIDE the status brackets).
    replacements: (unique row prefix, literal old text, literal new text).
    additions: complete task rows, inserted after the section's last task.
    duplicate_keys: optional stable substrings, e.g. a task title, checked
    throughout the backlog as well as against other additions.
    Returns the required pre-edit backup in backlog-history/. A replay is an
    error if its anchors/old text disappeared or an added task already exists.
    """
    import tick  # reuse the scheduler's parser without starting the scheduler

    path = Path(path)
    log_path = Path(log_path) if log_path is not None else path.parent.parent / "audits" / "coordinator-log.md"
    if log_path.resolve() == path.resolve():
        raise ValueError("log_path must differ from backlog path")
    log = backlog_io._one_line(log.removesuffix("\n"), "log")
    backup = None

    def change(rows):
        nonlocal backup
        scope = backlog_io._section_rows(rows, section)

        def find(prefix):
            backlog_io._one_line(prefix, "prefix")
            hits = [i for i in scope if rows[i].startswith(prefix)]
            if len(hits) != 1:
                raise ValueError(f"{section}: prefix {prefix!r}: expected one row, found {len(hits)}")
            backlog_io._task(rows[hits[0]].removesuffix("\r"))
            return hits[0]

        for prefix, state in states:
            i = find(prefix)
            state = backlog_io._state(state, "new state")
            match = backlog_io._task(rows[i].removesuffix("\r"))
            rows[i] = f"- [{state}] {match[2]}" + ("\r" if rows[i].endswith("\r") else "")
        for prefix, old, new in replacements:
            i = find(prefix)
            backlog_io._one_line(old, "old text")
            if not isinstance(new, str) or "\r" in new or "\n" in new:
                raise ValueError("new text must be a single-line string")
            if rows[i].count(old) != 1:
                raise ValueError(f"{prefix!r}: expected old text exactly once")
            rows[i] = rows[i].replace(old, new, 1)
            backlog_io._task(rows[i].removesuffix("\r"))

        added = list(additions)
        # Compare bodies so a prior copy with a different status also counts.
        bodies = [backlog_io._task(row)[2] for row in added]
        keys = [backlog_io._one_line(key, "duplicate key") for key in duplicate_keys]
        if len(set(bodies)) != len(bodies) or any(body in row for body in bodies for row in rows):
            raise ValueError("added task already exists or is repeated in additions")
        if any(any(key in row for row in rows) or sum(key in row for row in added) != 1 for key in keys):
            raise ValueError("duplicate key must occur in exactly one addition and nowhere in backlog")
        if added:
            at = max((i for i in scope if rows[i].startswith("- [")),
                     default=max((i for i in scope if rows[i].strip()), default=scope.start - 1)) + 1
            cr = "\r" if any(row.endswith("\r") for row in rows) else ""
            block = [row + cr for row in added]
            # Match the existing one-offs byte for byte, including their final
            # CR marker when the file has no terminal LF.
            rows[at:at] = block

        tick.parse_backlog("\n".join(rows))
        # edit's normal history is best effort; this copy must succeed first.
        backup = backlog_io._snapshot(path, required=True)
        return rows

    backlog_io.edit(change, path)
    with backlog_io.lock(path):
        with log_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(log + "\n")
    return backup
