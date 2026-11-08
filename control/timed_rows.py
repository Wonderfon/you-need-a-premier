"""Read-only window observations; never selects tasks or changes backlog/weights.

Existing HH:MM windows repeat daily. Optional 〔窗口日期：YYYY-MM-DD〕 pins the
observation to one start date (dispatch keeps its existing daily-time rules).
Cross-midnight windows belong to their start date. A new daily row never inherits
misses before its first observed occurrence; the latest missed occurrence stays
visible until that exact row leaves todo. Dates in prose are never guessed.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import backlog_io

STATE_FILE = "timed-rows.json"
WINDOW = re.compile(r"〔时段：([^〕]*)〕")
CLOCKS = re.compile(r"(\d{2}):(\d{2})–(\d{2}):(\d{2})")
DATE = re.compile(r"〔窗口日期：([^〕]*)〕")


def identity(section: str, row: str) -> str:
    return hashlib.sha256((section + "\n" + row).encode("utf-8")).hexdigest()


def load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def clocks(window: str) -> tuple[int, int] | None:
    match = CLOCKS.fullmatch(window)
    if not match:
        return None
    sh, sm, eh, em = map(int, match.groups())
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        return None
    return sh * 60 + sm, eh * 60 + em


def span(day: str, start: int, end: int) -> tuple[datetime, datetime]:
    midnight = datetime.strptime(day, "%Y-%m-%d")
    return (midnight + timedelta(minutes=start),
            midnight + timedelta(days=end <= start, minutes=end))


def observations(text: str, state: dict, now: datetime) -> list[dict]:
    now = now.astimezone().replace(tzinfo=None) if now.tzinfo else now
    section, skip, out = "", False, []
    for row in text.splitlines():
        if row.startswith("## "):
            section, skip = row[3:].strip(), backlog_io.is_owner_heading(row)
        if skip or not row.startswith("- [todo] ") or not (tag := WINDOW.search(row)):
            continue
        times = clocks(tag[1])
        if times is None:
            continue  # malformed windows remain the parser's responsibility
        start, end = times
        key = identity(section, row)
        saved = state.get(key, {})
        saved = saved if isinstance(saved, dict) else {}
        day = now.date()
        if end <= start and now.hour * 60 + now.minute < end:
            day -= timedelta(days=1)  # currently inside yesterday's overnight window
        date_tag = DATE.search(row)
        try:
            first = datetime.strptime(saved.get("first_date", day.isoformat()), "%Y-%m-%d").date()
            if date_tag:
                day = datetime.strptime(date_tag[1], "%Y-%m-%d").date()
            opened, closed = span(day.isoformat(), start, end)
        except (ValueError, TypeError):
            continue
        candidates = [(day, opened, closed)]
        if not date_tag and now < closed:
            # Only a previously observed daily row can have a miss from yesterday.
            previous = day - timedelta(days=1)
            if previous >= first:
                candidates.insert(0, (previous, *span(previous.isoformat(), start, end)))
        for occurrence, opened, closed in candidates:
            phase = "before" if now < opened else "open" if now < closed else "expired"
            if phase == "expired" and not date_tag and occurrence < first:
                continue
            label = {"before": "窗口未到", "open": "窗口已开", "expired": "窗口已过"}[phase]
            out.append({"id": key, "section": section, "row": row, "first_date": first.isoformat(),
                        "window": tag[1], "window_date": occurrence.isoformat(), "phase": phase,
                        "message": f"{'到点行错过' if phase == 'expired' else '到点行待执行'}：原定 {opened:%H:%M}、{label}、还没执行"})
    return out


def missed(text: str, path: Path, now: datetime) -> list[dict]:
    return [r for r in observations(text, load(path), now) if r["phase"] == "expired"]


def observe(text: str, path: Path, now: datetime, log) -> None:
    """Supervisor-only writer. Deduplicate each full row + start date + phase.

    Readers recheck the live backlog, so completed/removed rows disappear even
    before the next supervisor loop. Only observation state and tick log are written.
    """
    old, updated = load(path), {}
    for row in observations(text, old, now):
        key, phase, day = row["id"], row["phase"], row["window_date"]
        saved = old.get(key, {})
        saved = saved if isinstance(saved, dict) else {}
        current = updated.setdefault(key, {"first_date": row["first_date"],
                                           "seen": dict(saved.get("seen", {}))})
        if current["seen"].get(phase) != day:
            log("timed_row_" + phase, row["message"], row=row["row"], section=row["section"],
                row_id=key, window=row["window"], window_date=day, phase=phase)
            current["seen"][phase] = day
    if old != updated:
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_suffix(".tmp")
        staging.write_text(json.dumps(updated, ensure_ascii=False), encoding="utf-8")
        staging.replace(path)
