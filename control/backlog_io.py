"""Locked writes to projects/backlog.md (stdlib only). Every writer of backlog.md goes through here."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BACKLOG = Path(__file__).resolve().parent.parent / "projects" / "backlog.md"
RECORDS = BACKLOG.parent.parent / "records"
HISTORY_DIR = Path(__file__).resolve().parent / "data" / "backlog-history"  # a copy of the file before every write
HISTORY_KEEP = 50
HISTORY_DAILY_DAYS = 14  # besides the newest HISTORY_KEEP: the first copy of each UTC day (history/daily/), this many days
OWNER_HEADING = "## 你的指令（最优先）"
STALE_SECONDS = 60
TIMEOUT_SECONDS = 15
_OWNER_CARD = re.compile(r"给 owner (?P<day>\d{2}-\d{2})[：:]\s*(?P<text>[^\]]*)")
_OWNER_PATH = re.compile(r"(?:audits|wt)[\\/][^\s,，;；）)\]]+", re.I)
_RECORD_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


@contextmanager
def lock(path: Path = BACKLOG, timeout: float = TIMEOUT_SECONDS, stale: float = STALE_SECONDS):
    """Exclusive lockfile next to `path` (O_EXCL); a lock older than `stale` seconds is broken."""
    lock_path = path.with_name(path.name + ".lock")
    deadline = time.time() + timeout
    while True:
        try:
            os.close(os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError):  # Windows: a just-unlinked lock is "delete pending" → EACCES
            try:
                if time.time() - lock_path.stat().st_mtime > stale:
                    lock_path.unlink()
                    continue
            except OSError:
                continue
            if time.time() > deadline:
                raise TimeoutError(f"backlog lock busy: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _read(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read().split("\n")


def _snapshot(path: Path, *, required: bool = False) -> Path | None:
    """Copy the file about to be replaced into the history folder (UTC-stamped name), keep the newest HISTORY_KEEP, and
    the first copy of each UTC day in history/daily/ for HISTORY_DAILY_DAYS days (50 copies last hours, a bad overwrite
    found next morning would be gone). Names advance past the newest retained stamp even if the UTC clock stops or
    goes backwards; daily copies still use the actual UTC day. Best effort unless required=True (batch writes)."""
    history = HISTORY_DIR if path == BACKLOG else path.with_name("backlog-history")
    try:
        history.mkdir(parents=True, exist_ok=True)
        data, when = path.read_bytes(), datetime.now(timezone.utc)
        stamp = when
        for latest in sorted(history.glob("backlog-*.md"), reverse=True):
            try:
                previous = datetime.strptime(latest.name, "backlog-%Y%m%dT%H%M%S.%fZ.md").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            stamp = max(stamp, previous + timedelta(microseconds=1))
            break
        while True:
            try:
                snapshot = history / f"backlog-{stamp:%Y%m%dT%H%M%S.%fZ}.md"
                with snapshot.open("xb") as stream:
                    stream.write(data)
                break
            except FileExistsError:
                stamp += timedelta(microseconds=1)
        for old in sorted(history.glob("backlog-*.md"))[:-HISTORY_KEEP]:
            old.unlink()
        daily = history / "daily"
        daily.mkdir(exist_ok=True)
        try:
            with (daily / f"backlog-{when:%Y%m%d}.md").open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            pass
        cutoff = f"backlog-{when - timedelta(days=HISTORY_DAILY_DAYS):%Y%m%d}.md"
        for old in daily.glob("backlog-*.md"):
            if old.name < cutoff:
                old.unlink()
        return snapshot
    except OSError:
        if required:
            raise
        return None


def _write(path: Path, lines: list[str], *, require_snapshot: bool = False) -> Path | None:
    snapshot = None
    if path.exists():
        snapshot = _snapshot(path, required=require_snapshot)
    tmp = path.with_suffix(".md.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as stream:
            stream.write("\n".join(lines))
        for attempt in range(40):  # a reader (tick, dashboard, editor) may hold the target open for a moment
            try:
                os.replace(tmp, path)
                return snapshot
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.05)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def edit(fn, path: Path = BACKLOG, *, require_snapshot: bool = False) -> None:
    """Re-read the file under the lock and replace its lines with fn(lines) (a history copy is made first)."""
    with lock(path):
        _write(path, fn(_read(path)), require_snapshot=require_snapshot)


def insert_owner_item(line: str, path: Path = BACKLOG) -> None:
    """Append `line` at the end of the owner section, creating it right after the header block."""
    with lock(path):
        lines = _read(path)
        cr = "\r" if any(x.endswith("\r") for x in lines) else ""
        heads = [i for i, x in enumerate(lines) if x.startswith("## ")]
        start = next((i for i in heads if is_owner_heading(lines[i])), None)
        if start is None:
            at = heads[0] if heads else len(lines)
            lines[at:at] = [OWNER_HEADING + cr, line + cr, cr]
        else:
            end = next((i for i in heads if i > start), len(lines))
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1
            lines.insert(end, line + cr)
        _write(path, lines)


def is_owner_heading(heading: str) -> bool:
    return heading.rstrip("\r").strip() == OWNER_HEADING


def rewrite_line(old: str, new: str, path: Path = BACKLOG) -> bool:
    """Replace the exact line `old` with `new` in the freshly re-read backlog; False if it is gone."""
    with lock(path):
        lines = _read(path)
        for i, raw in enumerate(lines):
            if raw.rstrip("\r") == old:
                lines[i] = new + raw[len(raw.rstrip("\r")):]
                _write(path, lines)
                return True
    return False


_GENERIC_NAMES = {"readme", "index", "guide", "results", "result", "summary", "report", "overview"}
_NAME_CHARS = r"[\w.\-]"


def _ref_in_body(ref: str, body: str) -> bool:
    """The card's full relative path (slash direction ignored) occurs in the record on path boundaries; a generic file
    name (README, index, guide, results…) only counts with a directory below the audits/wt root."""
    parts = [x for x in re.split(r"[/\\]+", ref.casefold()) if x]
    if len(parts) < 2 or (Path(parts[-1]).stem in _GENERIC_NAMES and len(parts) < 3):
        return False
    pattern = rf"(?<!{_NAME_CHARS})" + "/".join(re.escape(x) for x in parts) + rf"(?!{_NAME_CHARS})"
    return re.search(pattern, re.sub(r"\\+", "/", body)) is not None


def cited_by(card: re.Match, records: list, today: date):
    """First record dated no earlier than the card (MM-DD, year taken from `today`) that cites one of its paths."""
    month, day = int(card["day"][:2]), int(card["day"][3:])
    try:
        card_day = date(today.year, month, day)
    except ValueError:
        return None
    if card_day > today:
        card_day = card_day.replace(year=today.year - 1)
    refs = [ref.rstrip("`'\"<>.,;:") for ref in _OWNER_PATH.findall(card["text"])]
    return next((r for r in records if r[0] >= card_day and any(_ref_in_body(ref, r[2]) for ref in refs)), None)


CLEAR_EVERY_S = 3600
_last_clear = [float("-inf")]


def clear_seen_owner_cards_hourly(at: float | None = None, **kwargs) -> list[tuple[int, str, str]]:
    """clear_seen_owner_cards, at most once per CLEAR_EVERY_S (the scheduler calls this every round)."""
    at = time.time() if at is None else at
    if at - _last_clear[0] < CLEAR_EVERY_S:
        return []
    _last_clear[0] = at
    archive = kwargs.pop("archive", None)
    if archive:
        archive()  # leave a failed/unfinished archive untouched for recovery
    path = kwargs.get("path", BACKLOG)
    if path.with_name("backlog-archive-pending.json").exists():
        raise ValueError("unfinished archive: recover its journal before clearing cards")
    changed = clear_seen_owner_cards(**kwargs)
    import backlog_history
    folder = path.parent / "backlog-archive"
    with lock(folder):
        for month in backlog_history.archive_paths(path):
            backlog_history.read_text(month)  # refuse links/oversized sources before writing
            changed.extend(clear_seen_owner_cards(**{**kwargs, "path": month}))
    return changed


def clear_seen_owner_cards(records_dir: Path = RECORDS, path: Path = BACKLOG,
                           now: datetime | None = None) -> list[tuple[int, str, str]]:
    """Clear done-row owner cards when a recent record mentions one of their cited audit/wt filenames."""
    today = (now or datetime.now().astimezone()).date()
    cutoff = today - timedelta(days=13)
    records = []
    for record in records_dir.glob("*.json"):
        match = _RECORD_DATE.match(record.name)
        try:
            day = date.fromisoformat(match[0]) if match else datetime.fromtimestamp(record.stat().st_mtime).date()
            if not cutoff <= day <= today:
                continue
            body = record.read_text(encoding="utf-8").casefold()
        except (OSError, UnicodeError, ValueError):
            continue
        records.append((day, record.name, body))
    records.sort()

    changed = []
    try:
        with lock(path):
            lines = _read(path)
            for i, raw in enumerate(lines):
                line = raw.rstrip("\r")
                if not re.match(r"^-\s+\[done(?:\s|\])", line):
                    continue
                left, bracket, right = line.partition("]")
                if not bracket:
                    continue

                def mark_seen(card: re.Match) -> str:
                    cited = cited_by(card, records, today)
                    if cited is None:
                        return card[0]
                    return f"owner 已看 {cited[0]:%m-%d}（records/{cited[1]}）"

                new_left = _OWNER_CARD.sub(mark_seen, left)
                if new_left != left:
                    new = new_left + bracket + right
                    lines[i] = new + raw[len(line):]
                    changed.append((i + 1, line, new))
            if changed:
                _write(path, lines, require_snapshot=True)
    except TimeoutError:
        return []
    return changed


TASK_RE = re.compile(r"^- \[(.+?)\] (.+)$")
STATE_RE = re.compile(r"(?:todo|review|running|done-ref|done|blocked|excluded|each tick)(?:[ :].*)?")


def line_sha256(line: str) -> str:
    """Fingerprint a UTF-8 row, excluding its line ending (never a prefix)."""
    return hashlib.sha256(line.rstrip("\r\n").encode("utf-8")).hexdigest()


def _one_line(value, name: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError(f"{name}: expected one nonempty line without a line ending")
    return value


def _state(value, name: str) -> str:
    value = _one_line(value, name)
    if "[" in value or "]" in value or not STATE_RE.fullmatch(value):
        raise ValueError(f"{name}: invalid task state (supply the text inside brackets)")
    return value


def _task(line: str):
    match = TASK_RE.fullmatch(_one_line(line, "task row"))
    if match is None or not STATE_RE.fullmatch(match[1]):
        raise ValueError("only task rows may be changed or inserted")
    return match


def _section_rows(lines: list[str], section: str) -> range:
    heads = [i for i, row in enumerate(lines) if row.startswith("## ")]
    found = [i for i in heads if lines[i][3:].strip() == section]
    if len(found) != 1:
        raise ValueError(f"section {section!r}: expected one heading, found {len(found)}")
    start = found[0]
    return range(start + 1, next((i for i in heads if i > start), len(lines)))


def _batch_plan(data: bytes, changes: list[dict], allow_cross_section: bool):
    if not isinstance(changes, list) or not changes:
        raise ValueError("changes must be a nonempty list")
    lines = data.decode("utf-8").split("\n")
    rows = [line.removesuffix("\r") for line in lines]
    sections, edits, used, outcomes = set(), [], set(), set()
    fields = {"section", "old_line", "old_sha256", "expected_state", "new_state", "append", "insert_after"}
    for change in changes:
        if not isinstance(change, dict) or change.keys() - fields:
            raise ValueError("change must be an object with only documented fields")
        section = _one_line(change.get("section"), "section")
        sections.add(section)
        if len(sections) > 1 and not allow_cross_section:
            raise ValueError("cross-section batch requires explicit allow_cross_section")
        old = change.get("old_line")
        fingerprint = change.get("old_sha256")
        if (old is None) == (fingerprint is None):
            raise ValueError("supply exactly one of old_line or old_sha256")
        if old is not None:
            _task(old)
        elif not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("old_sha256 must be a lowercase SHA-256 hex digest")
        expected = _state(change.get("expected_state"), "expected_state")
        state = _state(change.get("new_state"), "new_state")
        tail = change.get("append", "")
        if tail != "":
            _one_line(tail, "append")
        inserts = change.get("insert_after", [])
        if not isinstance(inserts, list):
            raise ValueError("insert_after must be a list of complete task rows")
        for row in inserts:
            _task(row)
        if len(set(inserts)) != len(inserts):
            raise ValueError("duplicate inserted row")
        matches = lambda row: row == old if old is not None else line_sha256(row) == fingerprint
        candidates = []
        for i in _section_rows(lines, section):
            row = rows[i]
            if matches(row):
                match = _task(row)
                if match[1] != expected:
                    raise ValueError(f"{section}: expected state does not match old row")
                candidates.append((i, f"- [{state}] {match[2]}{tail}"))
                continue
            # Reverse only the requested state and suffix. This also recognizes a
            # committed batch if the process died before saving its receipt.
            match = TASK_RE.fullmatch(row)
            if match and match[1] == state and (not tail or match[2].endswith(tail)):
                body = match[2][:-len(tail)] if tail else match[2]
                if matches(f"- [{expected}] {body}"):
                    candidates.append((i, row))
        if len(candidates) != 1:
            raise ValueError(f"{section}: expected one exact old/applied row, found {len(candidates)}")
        i, new = candidates[0]
        if i in used:
            raise ValueError("two changes select the same row")
        used.add(i)
        block = [new, *inserts]
        applied = rows[i:i + len(block)] == block
        neutral = state == expected and not tail and not inserts
        if not neutral:
            outcomes.add("applied" if applied else "pending")
        if not applied:
            if not matches(rows[i]):
                raise ValueError("applied row changed or its inserted rows moved/missing")
            if any(row in rows for row in inserts):
                raise ValueError("inserted row already exists outside the expected block")
        edits.append((i, block, section))
    if len(outcomes) > 1:
        raise ValueError("partially applied batch: refusing to write")
    if "pending" not in outcomes:
        return lines, "already_applied"
    for i, block, _ in sorted(edits, reverse=True):
        cr = "\r" if lines[i].endswith("\r") else ""
        if len(block) > 1 and i == len(lines) - 1:  # preserve an unterminated last row
            cr = "\r" if any(row.endswith("\r") for row in lines) else ""
            lines[i:i + 1] = [row + cr for row in block[:-1]] + [block[-1]]
        else:
            lines[i:i + 1] = [row + cr for row in block]
    # A proposed batch must still have unique anchors and insertion blocks.
    final = [line.removesuffix("\r") for line in lines]
    for _, block, section in edits:
        indices = [i for i in _section_rows(lines, section) if final[i] == block[0]]
        if len(indices) != 1 or final[indices[0]:indices[0] + len(block)] != block:
            raise ValueError("batch produces ambiguous or overlapping rows")
        if any(final.count(row) != 1 for row in block[1:]):
            raise ValueError("batch produces duplicate inserted rows")
    return lines, "preview"


def preview_batch(changes: list[dict], path: Path = BACKLOG, *, allow_cross_section: bool = False) -> dict:
    """Read-only diff. Each change supplies section, old_line OR old_sha256,
    expected_state, new_state, optional append and insert_after (complete rows).
    States exclude brackets; append is literal. No arbitrary text replacement.
    """
    before = path.read_bytes()
    lines, status = _batch_plan(before, changes, allow_cross_section)
    after = "\n".join(lines).encode("utf-8")
    diff = "\n".join(difflib.unified_diff(before.decode("utf-8").splitlines(), after.decode("utf-8").splitlines(),
                                          fromfile=str(path), tofile=str(path) + " (proposed)", lineterm=""))
    return {"status": status, "before_sha256": hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(), "diff": diff + "\n" if diff else ""}


def apply_batch(changes: list[dict], path: Path = BACKLOG, *, allow_cross_section: bool = False,
                expected_sha256: str | None = None) -> dict:
    """Re-read/validate the whole batch under lock, then replace once and read back.
    Pass preview_batch()['before_sha256'] to also reject changes since preview.
    An identical replay returns already_applied without writing backlog/history.
    """
    with lock(path):
        before = path.read_bytes()
        lines, status = _batch_plan(before, changes, allow_cross_section)
        if status != "already_applied" and expected_sha256 is not None and hashlib.sha256(before).hexdigest() != expected_sha256:
            raise ValueError("backlog changed since preview; preview again")
        after = "\n".join(lines).encode("utf-8")
        snapshot = _write(path, lines, require_snapshot=True) if status != "already_applied" else None
        readback = path.read_bytes()
        if readback != after:
            raise OSError("backlog read-back differs from the proposed batch")
        history = HISTORY_DIR if path == BACKLOG else path.with_name("backlog-history")
        history.mkdir(parents=True, exist_ok=True)
        batch_id = hashlib.sha256(json.dumps(changes, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        receipt_path = history / f"txn-{batch_id}.json"
        receipt = {"status": "already_applied" if status == "already_applied" else "applied",
                   "path": str(path.resolve()), "batch_sha256": batch_id,
                   "before_sha256": hashlib.sha256(before).hexdigest(),
                   "after_sha256": hashlib.sha256(after).hexdigest(),
                   "readback_sha256": hashlib.sha256(readback).hexdigest(),
                   "snapshot": str(snapshot) if snapshot else None, "receipt": str(receipt_path)}
        # Keep the original before/snapshot receipt on a replay. Idempotence does
        # not depend on this file, so a receipt failure cannot duplicate inserts.
        if status != "already_applied" or not receipt_path.exists():
            tmp = receipt_path.with_suffix(".json.tmp")
            try:
                tmp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                os.replace(tmp, receipt_path)
            except OSError as exc:
                raise OSError("batch is fully applied, but receipt failed; replay safely to recover") from exc
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
        return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Preview a structured backlog task batch; write only with --apply.")
    parser.add_argument("manifest", type=Path, help="UTF-8 JSON list of changes (see preview_batch docstring)")
    parser.add_argument("--path", type=Path, required=True, help="explicit backlog path")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-cross-section", action="store_true")
    args = parser.parse_args(argv)
    try:
        changes = json.loads(args.manifest.read_text(encoding="utf-8"))
        preview = preview_batch(changes, args.path, allow_cross_section=args.allow_cross_section)
        print(preview["diff"], end="", flush=True)
        if args.apply:
            receipt = apply_batch(changes, args.path, allow_cross_section=args.allow_cross_section,
                                  expected_sha256=preview["before_sha256"])
            print("已应用" if receipt["status"] == "already_applied" else "应用成功")
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
        else:
            print("已应用" if preview["status"] == "already_applied" else "仅预览；加 --apply 应用")
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"backlog batch: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
