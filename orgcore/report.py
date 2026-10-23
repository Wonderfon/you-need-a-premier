"""Local reports: reports/<date>-<HHMM>.md, written at the times in reporting.json.

A slot is one (local date, local time, channel) triple. It gets at most one report, ever:
- a re-run finds the file and writes nothing;
- when slots were missed (the machine was off), at most ONE catch-up report is written, for the latest missed slot; the
  earlier missed slots are recorded as skipped in state/reports.jsonl and named inside that report;
- a local time that happens twice when clocks go back is one slot (the first occurrence); one that does not exist when
  clocks go forward is due when the clock jumps past it.

Quiet hours only mark a report; writing a file is silent, nothing is pushed anywhere in v0.1.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path

from . import backlog, feedback, ledger, usage
from .config import Instance
from .fsio import atomic_write, file_lock

CHANNEL = "local"
GRACE = timedelta(minutes=60)  # later than this after the slot time, a report is labelled a catch-up
LOOKBACK = timedelta(hours=24)  # slots older than this are forgotten, not caught up
REPORTS = "reports.jsonl"


class ReportError(ValueError):
    pass


def zone(name: str) -> tzinfo:
    if name == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        raise ReportError(f"time zone '{name}' is not available on this system "
                          f"(on Windows install the 'tzdata' package, or use \"UTC\")") from None


class Slot:
    def __init__(self, day: date, hhmm: str, due: datetime):
        self.day, self.hhmm, self.due = day, hhmm, due

    @property
    def key(self) -> str:
        return f"{self.day.isoformat()}|{self.hhmm}|{CHANNEL}"

    @property
    def filename(self) -> str:
        return f"{self.day.isoformat()}-{self.hhmm.replace(':', '')}.md"

    def label(self) -> str:
        return f"{self.day.isoformat()} {self.hhmm}"


def slots_between(reporting: dict, tz: tzinfo, start: datetime, end: datetime) -> list[Slot]:
    """Slots due in (start, end], oldest first. `start` and `end` are aware datetimes."""
    first = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    out, day = [], first
    while day <= last:
        for hhmm in reporting["report_times"]:
            hour, minute = map(int, hhmm.split(":"))
            due = datetime.combine(day, time(hour, minute), tzinfo=tz).replace(fold=0).astimezone(timezone.utc)
            if start < due <= end:
                out.append(Slot(day, hhmm, due))
        day += timedelta(days=1)
    return sorted(out, key=lambda s: s.due)


def in_quiet_hours(reporting: dict, hhmm: str) -> bool:
    quiet = reporting.get("quiet_hours")
    if not quiet:
        return False
    start, end = quiet["start"], quiet["end"]  # "HH:MM" strings compare correctly
    return start <= hhmm < end if start <= end else hhmm >= start or hhmm < end


def run(instance: Instance, now: datetime | None = None, source: usage.UsageSource | None = None,
        tz: tzinfo | None = None) -> list[Path]:
    """Write the report that is due, if any. Returns the files written (zero or one)."""
    now = now or usage.now_utc()
    reporting = instance.reporting
    if not reporting["report_times"]:
        return []
    tz = tz or zone(reporting["timezone"])
    root = instance.root
    with file_lock(root / "state" / "report"):
        events = ledger.read(root, REPORTS)
        skipped = {e["slot"]: e for e in events if e.get("event") == "skipped"}
        pending = [s for s in slots_between(reporting, tz, now - LOOKBACK, now)
                   if not (root / "reports" / s.filename).exists() and s.key not in skipped]
        if not pending:
            return []
        target, earlier = pending[-1], pending[:-1]
        for slot in earlier:  # recorded before the file is written, so a crash in between cannot produce a second catch-up
            ledger.append(root, REPORTS, now, event="skipped", slot=slot.key, covered_by=target.key)
        covered = sorted({e["slot"] for e in ledger.read(root, REPORTS)
                          if e.get("event") == "skipped" and e.get("covered_by") == target.key})
        text = render(instance, target, now, tz, covered, source)
        path = root / "reports" / target.filename
        atomic_write(path, text.encode("utf-8"))
        ledger.append(root, REPORTS, now, event="report", slot=target.key,
                      kind="catch-up" if now - target.due > GRACE else "scheduled")
        return [path]


def _local(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def render(instance: Instance, slot: Slot, now: datetime, tz: tzinfo, covered: list[str],
           source: usage.UsageSource | None = None) -> str:
    reporting = instance.reporting
    board = backlog.read(instance.backlog_path)
    late = now - slot.due > GRACE
    out = [f"# Report {slot.label()} ({reporting['timezone']})", "",
           f"- Key: {slot.key}",
           f"- Kind: catch-up (due {slot.hhmm}, written {_local(now, tz)})" if late else "- Kind: scheduled"]
    if in_quiet_hours(reporting, slot.hhmm):
        quiet = reporting["quiet_hours"]
        out.append(f"- Quiet hours: this slot is inside {quiet['start']}-{quiet['end']}; the report is only written to a file")
    if covered:
        out.append("- Earlier missed slots covered by this report: " + ", ".join(k.split("|")[0] + " " + k.split("|")[1] for k in covered))

    out += ["", "## Board", "", "| Line | todo | running | review | rework | blocked | done |", "|---|---|---|---|---|---|---|"]
    for line in instance.org["lines"]:
        mine = [t for t in board.tasks if t.section == line["id"]]
        counts = [sum(1 for t in mine if t.state == s) for s in ("todo", "running", "review", "rework", "blocked", "done")]
        out.append(f"| {line['id']} | " + " | ".join(map(str, counts)) + " |")
    for state, heading in (("review", "Waiting for review"), ("rework", "Sent back for rework"), ("blocked", "Blocked")):
        rows = [t for t in board.tasks if t.state == state]
        if rows:
            out += ["", f"{heading}:"]
            out += [f"- {t.project}: {t.text[:100]}" + (f" ({t.detail})" if state == "blocked" and t.detail else "") for t in rows[:10]]
            if len(rows) > 10:
                out.append(f"- ... and {len(rows) - 10} more")

    previous = slots_between(reporting, tz, slot.due - timedelta(days=3), slot.due - timedelta(seconds=1))
    since = previous[-1].due if previous else slot.due - timedelta(hours=24)
    events = [e for e in ledger.read(instance.root)
              if since < backlog.parse_time(e["ts"]) <= now]
    count = lambda kind: sum(1 for e in events if e.get("event") == kind)  # noqa: E731
    verdicts = [e.get("verdict") for e in events if e.get("event") == "reviewed"]
    out += ["", f"## Activity since {_local(since, tz)}", "",
            f"- Dispatched: {count('dispatched')}",
            f"- Submitted for review: {count('submitted')}",
            f"- Reviewed: {len(verdicts)} (pass {verdicts.count('pass')}, rework {verdicts.count('rework')}, "
            f"blocked {verdicts.count('blocked')})"]

    out += ["", "## Owner feedback", ""]
    out += [f"- {line.strip()}" if not line.startswith("  ") else f"  {line.strip()}"
            for line in feedback.describe(feedback.summarize(instance, board))]

    out += ["", "## Usage", ""]
    source = source or usage.JsonFileUsage(instance.root / "state" / "usage.json")
    try:
        readings = source.observe()
    except usage.UsageUnavailable as exc:
        out.append(f"- No reading: {str(exc).replace(str(instance.root), '<instance>')} (unknown is never treated as zero)")
        readings = None
    if readings is not None:
        for window in instance.policy["subscription_windows"]:
            mine = [r for r in readings if r.window == window["name"]]
            if not mine:
                out.append(f"- {window['name']}: no reading (unknown is never treated as zero)")
                continue
            r = max(mine, key=lambda x: x.observed_at)
            out.append(f"- {window['name']}: {r.used_percent:g}% used, observed {_local(r.observed_at, tz)} by "
                       f"{r.source}{' (synthetic)' if r.synthetic else ''}, resets {_local(r.resets_at, tz)}")
        gate = usage.evaluate(instance.policy, readings, now)
        out.append("- Dispatch gate: open" if gate.allowed else "- Dispatch gate: closed")
        out += [f"  - {reason}" for reason in gate.reasons]
    return "\n".join(out) + "\n"
