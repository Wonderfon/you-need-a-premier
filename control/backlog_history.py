"""Read-only task rows with real provenance; scheduling and writes use backlog.md only."""
from pathlib import Path
import re

LIMIT = 32 * 1024 * 1024
MONTH = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])\.md\Z")
TASK = re.compile(r"^- \[(.+?)\] (.+)$")


def archive_paths(backlog: Path, since=None):
    folder = Path(backlog).parent / "backlog-archive"
    if folder.is_symlink() or folder.resolve() != folder.absolute():
        raise ValueError("linked archive directory refused")
    return sorted(p for p in folder.glob("*.md") if MONTH.fullmatch(p.name)
                  and (since is None or p.stem >= since.strftime("%Y-%m")))


def read_text(path: Path) -> str:
    if path.is_symlink() or path.stat().st_nlink > 1 or path.stat().st_size > LIMIT:
        raise ValueError(f"unsafe or oversized backlog source: {path}")
    return path.read_text(encoding="utf-8-sig")


def text_rows(text: str, source: Path):
    section = "?"
    for number, raw in enumerate(text.splitlines(), 1):
        if raw.startswith("## "):
            section = raw[3:].strip()
        if match := TASK.match(raw):
            state, item = match.groups()
            yield {"section": section, "state": state, "item": item,
                   "status": re.split(r"[\s:：]", state, maxsplit=1)[0],
                   "line": number, "source": Path(source).as_posix(), "raw": raw}


def archived_rows(backlog: Path, since=None):
    # Fail visibly on corrupt/unreadable history rather than silently lower counts.
    # During an interrupted move, read the old monthly snapshot until the source
    # commit. This also preserves legitimate identical rows from earlier batches.
    import json
    import hashlib
    journal = Path(backlog).with_name("backlog-archive-pending.json")
    try:
        pending = json.loads(journal.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pending = None
    uncommitted = pending and hashlib.sha256(Path(backlog).read_bytes()).hexdigest() == pending["before"]
    for path in archive_paths(backlog, since):
        old = pending["archives"].get(path.name) if uncommitted else None
        if old:
            text = ""
            if old.get("backup"):
                backup = Path(old["backup"])
                if (backup.parent != path.parent / "backlog-history"
                        or not re.fullmatch(r"backlog-\d{8}T\d{6}\.\d+Z\.md", backup.name)):
                    raise ValueError("invalid archive backup location")
                text = read_text(backup)
                if hashlib.sha256(backup.read_bytes()).hexdigest() != old["before"]:
                    raise ValueError("archive backup differs from receipt")
        else:
            text = read_text(path)
        for row in text_rows(text, path):
            if row["status"] != "done":
                raise ValueError(f"non-done row in archive: {path}:{row['line']}")
            yield row


def all_rows(backlog: Path, text: str | None = None, since=None):
    backlog = Path(backlog)
    if text is None:
        try:
            text = read_text(backlog)
        except FileNotFoundError:
            text = ""
    yield from text_rows(text, backlog)
    yield from archived_rows(backlog, since)
