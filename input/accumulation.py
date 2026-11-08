"""Counts only: no raw text, identifiers, filenames or paths leave this module."""
from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import asr_hours

CACHE_SECONDS = 300
QUOTE_KEYS = ("owner_verbatim", "owner_message_verbatim", "owner_text", "exact_text", "verbatim", "text", "original", "message")
CHANNELS = {"terminal": "终端", "discord": "Discord", "other": "其他"}
METRICS = [(f"quotes_{c}", f"{label}原话", "条") for c, label in CHANNELS.items()]
METRICS += [(f"chars_{c}", f"{label}原话字数", "字") for c, label in CHANNELS.items()]
METRICS += [
    ("discord_messages", "Discord 消息", "条"), ("discord_attachments", "Discord 已存附件", "个"),
    ("confirmed_count", "已确认录音", "条"), ("confirmed_hours", "已确认语音", "小时"),
    ("unconfirmed_count", "未确认录音", "条"), ("unconfirmed_hours", "未确认语音", "小时"),
    ("corrections", "手改转写版本", "版"), ("replies", "要你的 · 回复", "次"),
    ("rating_use", "评价 · 用", "次"), ("rating_no", "评价 · 没用", "次"),
    ("rating_rework", "评价 · 返工", "次"), ("materials", "已登记材料", "项／批"),
]


def _day(value):
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value))
    try:
        return date.fromisoformat(match[0]) if match else None
    except ValueError:
        return None


def _channel(record, filename):
    for value in (record.get("channel"), record.get("source")):
        source = str(value or "").lower()
        if "discord" in source:
            return "discord"
        if any(s in source for s in ("terminal", "终端", "chat", "conversation", "current user", "codex thread")):
            return "terminal"
    for channel in ("terminal", "discord"):
        if f"-owner-{channel}-" in filename.lower():
            return channel
    return "other"


def _quotes(record):
    # Historical aliases describe the same quote: take the first, not all aliases.
    for key in QUOTE_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        if isinstance(value, list):
            return [s.strip() for s in value if isinstance(s, str) and s.strip()]
    return []


def _json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _ro(path):
    db = sqlite3.connect(asr_hours.readonly_uri(path), uri=True, timeout=1)
    db.execute("PRAGMA query_only=ON")
    return db


def build_snapshot(root: Path, now: datetime | None = None):
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    today = now.astimezone().date()
    events = {key: [] for key, _, _ in METRICS}
    unavailable, warnings = set(), set()

    def add(key, when, amount=1):
        events[key].append((_day(when), amount))

    def quote(record, filename):
        channel = _channel(record, filename)
        when = next((record[k] for k in ("date", "time", "time_local", "received_local", "recorded_at", "at")
                     if _day(record.get(k))), filename)
        for text in _quotes(record):
            add(f"quotes_{channel}", when)
            add(f"chars_{channel}", when, len(re.sub(r"\s", "", text)))

    record_keys = [key for key in events if key.startswith(("quotes_", "chars_"))]
    records = root / "records"
    if not records.is_dir():
        unavailable.update(record_keys)
    else:
        linked = set()
        for path in sorted(records.glob("*.json")):
            try:
                record = _json(path)
                for row in record if isinstance(record, list) else [record]:
                    if isinstance(row, dict):
                        quote(row, path.name)
                        if _quotes(row) and isinstance(row.get("source_record_key"), str):
                            linked.add(row["source_record_key"])
            except (OSError, ValueError):
                warnings.add("部分原话存档无法读取")
        archive = records / "current-chat" / "messages.jsonl"
        if archive.is_file():
            try:
                with archive.open(encoding="utf-8-sig") as stream:
                    for line in stream:
                        try:
                            row = json.loads(line)
                            if row.get("record_key") in linked:
                                continue
                            native = row["source_jsonl_record"]
                            native = json.loads(native) if isinstance(native, str) else native
                            payload = native["payload"]
                            if payload.get("role") != "user":
                                continue
                            content = payload.get("content", [])
                            text = content if isinstance(content, str) else "\n".join(
                                p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
                            quote({"owner_verbatim": text, "channel": "terminal",
                                   "date": row.get("source_timestamp")}, "")
                        except (ValueError, KeyError, TypeError, AttributeError):
                            warnings.add("部分旧会话存档无法读取")
            except OSError:
                warnings.add("旧会话存档无法读取")

    try:
        # Gateway is_allowed filters owner identity before inserting inbound; no config/secrets read.
        with closing(_ro(root / "discord/data/discord.sqlite3")) as db:
            for message_id, when in db.execute("SELECT message_id, received_at FROM inbound"):
                add("discord_messages", when)
                directory = root / "discord/data/attachments" / message_id
                if re.fullmatch(r"\d+", message_id) and directory.is_dir():
                    add("discord_attachments", when, sum(p.is_file() and not p.is_symlink() for p in directory.iterdir()))
    except (OSError, ValueError, sqlite3.Error):
        unavailable.update(("discord_messages", "discord_attachments"))

    try:
        db_path = root / "input/data/input.sqlite3"
        for row in asr_hours.read_recordings(db_path):
            dur = row["dur"]
            known = isinstance(dur, (int, float)) and not isinstance(dur, bool) and math.isfinite(dur) and dur > 0
            state = "confirmed" if row["confirmed"] and known else "unconfirmed"
            add(f"{state}_count", row["created"])
            add(f"{state}_hours", row["created"], dur / 3600 if known else 0)
        with closing(_ro(db_path)) as db:
            for when, in db.execute("SELECT created_at FROM revisions WHERE kind='correction' AND source='user_correction'"):
                add("corrections", when)
    except (OSError, ValueError, sqlite3.Error):
        unavailable.update(("confirmed_count", "confirmed_hours", "unconfirmed_count", "unconfirmed_hours", "corrections"))

    for filename, keys in (("owner-replies.json", ["replies"]),
                           ("owner-ratings.json", ["rating_use", "rating_no", "rating_rework"])):
        try:
            data = _json(root / "data" / filename)
            rows = data["records"]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("invalid rows")
            for row in rows:
                if filename == "owner-replies.json":
                    if row.get("status") != "withdrawn":
                        add("replies", row.get("at"))
                else:
                    key = {"用": "rating_use", "没用": "rating_no", "返工": "rating_rework"}.get(row.get("verdict"))
                    if key:
                        add(key, row.get("at"))
        except (OSError, ValueError, KeyError, TypeError):
            unavailable.update(keys)

    try:
        text = (root / "projects/materials.md").read_text(encoding="utf-8-sig")
        seen = set()
        for line in text.splitlines():
            match = re.match(r"\|\s*(M\d+)\s*\|\s*([^|]+)\|", line)
            if match and match[1] not in seen:
                seen.add(match[1])
                when = re.search(r"(?<!\d)(\d{2}-\d{2})(?!\d)", match[2])
                add("materials", f"2026-{when[1]}" if when else None)
    except OSError:
        unavailable.add("materials")

    dates = [d for rows in events.values() for d, _ in rows if d and d <= today]
    start = min(dates, default=today)
    start -= timedelta(days=start.weekday())
    end = today - timedelta(days=today.weekday())
    weeks = []
    while start <= end:
        weeks.append(start)
        start += timedelta(days=7)
    metrics = []
    for key, label, unit in METRICS:
        buckets = defaultdict(float)
        undated = 0
        for day, amount in events[key]:
            if day is None or day > today:
                undated += amount
            else:
                buckets[day - timedelta(days=day.weekday())] += amount
        running, series = 0, []
        for week in weeks:
            running += buckets[week]
            series.append(round(running, 6))
        metrics.append({"key": key, "label": label, "unit": unit,
                        "total": None if key in unavailable else round(running + undated, 6),
                        "undated": round(undated, 6), "weekly": [] if key in unavailable else series})
    return {"generated_at": now.isoformat(), "cache_seconds": CACHE_SECONDS,
            "weeks": [d.isoformat() for d in weeks], "metrics": metrics, "warnings": sorted(warnings)}


class SnapshotCache:
    """One scan per app per five minutes; concurrent requests share the worker."""
    def __init__(self, root):
        self.root, self.task, self.updated = root, None, 0

    async def get(self):
        if self.task is None or self.task.done() and (self.task.exception() or time.monotonic() - self.updated >= CACHE_SECONDS):
            self.updated = time.monotonic()
            self.task = asyncio.create_task(asyncio.to_thread(build_snapshot, self.root))
        return await asyncio.shield(self.task)
