"""Lossless backlog monthly archive. Explicit ROOT is required for manual runs.

Usage: python control/backlog_archive.py ROOT [--today YYYY-MM-DD]
The supervisor calls daily() at its existing hourly housekeeping point.
"""
from __future__ import annotations

from pathlib import Path

CONFIG = {
    "keep_days": 7,  # calendar dates including today
    "archive_dir": Path("projects/backlog-archive"),
    "cache_root": Path.home() / ".cache" / "codex-sandbox",
}

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import date, datetime, timedelta

import backlog_io

STATE = re.compile(r"^- \[([^\]]+)\]")
DONE = re.compile(r"^done(?:[\s:：]|$)")
DONE_DATE = re.compile(r"^done[\s:：]+(?:(\d{4})-)?(\d{1,2})-(\d{1,2})(?=$|[\s:：;；,，(（])")


def completed_on(state: str, today: date) -> date | None:
    """Only a date immediately following done counts; never borrow a card/body date.

    Yearless dates use today's year. Future dates stay put (no guessed previous year).
    """
    match = DONE_DATE.match(state)
    if not match:
        return None
    try:
        result = date(int(match[1] or today.year), int(match[2]), int(match[3]))
    except ValueError:
        return None
    return result if result <= today else None


def rows(data: bytes):
    section = "文件头"
    for raw in data.splitlines(keepends=True):
        text = raw.decode("utf-8").rstrip("\r\n")
        if text.startswith("## "):
            section = text[3:].strip()
        match = STATE.match(text)
        yield section, match[1] if match else None, raw


def stats(data: bytes) -> dict:
    sections = {}
    for section, state, raw in rows(data):
        counts = sections.setdefault(section, {"bytes": 0, "lines": 0, "done": 0})
        counts["bytes"] += len(raw)
        counts["lines"] += 1
        counts["done"] += bool(state and DONE.match(state))
    return {"bytes": len(data), "lines": sum(s["lines"] for s in sections.values()), "sections": sections}


def _safe(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    linked = any(p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 0x400
                 for p in (path, *path.parents) if p.exists())
    if not resolved.is_relative_to(root) or linked:
        raise ValueError(f"refusing path outside copy root or symlink: {path}")
    if resolved.is_file() and resolved.stat().st_nlink != 1:
        raise ValueError(f"refusing hard-linked file: {path}")
    return resolved


def _atomic_write(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        for attempt in range(40):  # same Windows reader tolerance as backlog_io._write
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.05)
        if path.read_bytes() != data:
            raise OSError(f"read-back differs: {path}")
    finally:
        tmp.unlink(missing_ok=True)


def _merge_archive(existing: bytes, month: str, moved: dict) -> bytes:
    """One heading per section, with every selected occurrence appended verbatim."""
    header = f"# Backlog archive {month}\n\n".encode("utf-8")
    if existing and not existing.startswith(header):
        raise ValueError("unrecognized archive header; refusing to overwrite")
    sections = {}
    section = None
    for raw in existing[len(header):].splitlines(keepends=True):
        if raw.startswith(b"## "):
            section = raw.decode("utf-8")[3:].strip()
            if section in sections:
                raise ValueError("duplicate archive section")
            sections[section] = []
        elif section is None or not (match := STATE.match(raw.decode("utf-8"))) or not DONE.match(match[1]):
            raise ValueError("unrecognized archive row; refusing to overwrite")
        else:
            sections[section].append(raw)
    for section, selected in moved.items():
        sections.setdefault(section, []).extend(selected)
    chunks = [header]
    for section, archived in sections.items():
        chunks.append(f"## {section}\n".encode("utf-8"))
        chunks.extend(archived)
    if any(not chunk.endswith(b"\n") for chunk in chunks[:-1]):
        raise ValueError("cannot append after an unterminated archive row; no rows removed")
    return b"".join(chunks)


def archive(copy_root: Path, *, today: date | None = None, keep_days: int = CONFIG["keep_days"]) -> dict:
    root = copy_root.absolute()
    _safe(root, root.resolve())
    root = root.resolve()
    if keep_days < 1:
        raise ValueError("keep_days must be positive")
    today = today or date.today()
    cutoff = today - timedelta(days=keep_days - 1)
    path = _safe(root / "projects" / "backlog.md", root)
    archive_dir = _safe(root / CONFIG["archive_dir"], root)
    journal = _safe(path.with_name("backlog-archive-pending.json"), root)
    # Global order: backlog -> archive directory -> individual month. Clearing
    # cards takes the directory then a month, never backlog while holding either.
    with backlog_io.lock(path), backlog_io.lock(archive_dir):
        before = path.read_bytes()
        fingerprint = lambda data: hashlib.sha256(data).hexdigest()
        pending = json.loads(journal.read_text(encoding="utf-8")) if journal.exists() else None
        if pending and fingerprint(before) == pending["after"]:
            # Previous run committed the backlog, but died before removing its receipt.
            for name, hashes in pending["archives"].items():
                target = _safe(archive_dir / name, root)
                if fingerprint(target.read_bytes()) != hashes["after"]:
                    raise ValueError("committed archive differs from receipt; keep backup for recovery")
            journal.unlink()
            pending = None
        if pending and fingerprint(before) != pending["before"]:
            raise ValueError("backlog changed during an unfinished archive; restore its backup before retrying")
        kept, moved = [], {}
        for section, state, raw in rows(before):
            day = completed_on(state, today) if state else None
            if day is not None and day < cutoff:
                moved.setdefault(f"{day:%Y-%m}", {}).setdefault(section, []).append(raw)
            else:
                kept.append(raw)
        after = b"".join(kept)
        result = {"retain_from": cutoff.isoformat(), "before": stats(before), "after": stats(after),
                  "moved_rows": sum(len(rs) for ss in moved.values() for rs in ss.values()),
                  "moved_bytes": len(before) - len(after), "backup": None, "archives": []}
        if not moved:
            if pending:
                raise ValueError("unfinished archive options differ; retry with the original options")
            return result
        # Snapshot and parse the in-memory result BEFORE changing any monthly file.
        source_history = backlog_io.HISTORY_DIR if path == backlog_io.BACKLOG else path.with_name("backlog-history")
        _safe(source_history, root)
        _safe(source_history / "daily", root)
        result["backup"] = str(backlog_io._snapshot(path, required=True))
        if Path(result["backup"]).read_bytes() != before:
            raise OSError("backup differs from locked source")
        validate(before, after, today)
        plans = []
        for month, groups in sorted(moved.items()):
            target = _safe(archive_dir / f"{month}.md", root)
            old = target.read_bytes() if target.exists() else b""
            previous = pending["archives"].get(target.name) if pending else None
            if pending and (previous is None or fingerprint(old) not in previous.values()):
                raise ValueError("archive changed during an unfinished move; refusing to overwrite")
            data = old if previous and fingerprint(old) == previous["after"] else _merge_archive(old, month, groups)
            if previous:
                hashes = previous
            else:
                backup = None
                if target.exists():
                    _safe(archive_dir / "backlog-history", root)
                    _safe(archive_dir / "backlog-history/daily", root)
                    backup = str(backlog_io._snapshot(target, required=True))
                    if Path(backup).read_bytes() != old:
                        raise OSError("archive backup differs from locked source")
                hashes = {"before": fingerprint(old), "after": fingerprint(data), "backup": backup}
            if fingerprint(data) != hashes["after"]:
                raise ValueError("unfinished archive plan differs; refusing to remove rows")
            plans.append((target, data, hashes))
        plan = {"before": fingerprint(before), "after": fingerprint(after),
                "archives": {p.name: hashes for p, _, hashes in plans}}
        if pending and plan != pending:
            raise ValueError("unfinished archive options differ; retry with the original options")
        # Receipt distinguishes a retry from a NEW occurrence of an identical source row.
        _atomic_write(journal, (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        archive_dir.mkdir(parents=True, exist_ok=True)
        for target, data, _ in plans:
            with backlog_io.lock(target):
                _atomic_write(target, data)
            result["archives"].append({"path": str(target), "bytes": len(data), "backup": plan["archives"][target.name].get("backup")})
        _atomic_write(path, after)  # only after ALL monthly archives have been read back
        journal.unlink()
        return result


def validate(before: bytes, after: bytes, today: date) -> None:
    """A parse exception or a changed active plan leaves the on-disk source intact."""
    import tick
    when = datetime.combine(today, datetime.min.time()).replace(hour=4)
    if tick.parse_backlog(before.decode("utf-8-sig"), when) != tick.parse_backlog(after.decode("utf-8-sig"), when):
        raise ValueError("archiving changed the parsed active plan; backup retained, nothing committed")


def daily(path: Path, now: datetime | None = None) -> dict | None:
    """First hourly opportunity at/after local 04:00; durable across restarts.

    Failed attempts retain their backup/journal for manual recovery; no hourly retries.
    """
    now = now or datetime.now().astimezone()
    local = now.astimezone() if now.tzinfo else now
    if local.hour < 4:
        return None
    root = Path(path).parent.parent
    state = _safe(Path(path).with_name("backlog-archive-state.json"), root.resolve())
    with backlog_io.lock(state):
        saved = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
        day = local.date().isoformat()
        if max(saved.get("last_attempt") or "", saved.get("last_success") or "") >= day:
            return None
        attempt = {"last_attempt": day, "last_success": saved.get("last_success") or "", "status": "attempted"}
        _atomic_write(state, (json.dumps(attempt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        result = archive(root, today=local.date())
        _atomic_write(state, (json.dumps({**attempt, "last_success": day, "status": "committed", "result": result},
                                       ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("copy_root", type=Path)
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    parser.add_argument("--keep-days", type=int, default=CONFIG["keep_days"])
    args = parser.parse_args()
    try:
        result = archive(args.copy_root, today=args.today, keep_days=args.keep_days)
    except (ValueError, OSError, TimeoutError) as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
