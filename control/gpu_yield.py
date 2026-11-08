"""Can the local 35B model start or keep running right now?  (owner\x2010-21 option A: yield to the input station.)

Run: python control/gpu_yield.py        (prints the decision; exit code 0 = allowed, 1 = not allowed)
Import: from gpu_yield import can_run;  d = can_run();  if not d.allowed: wait / pause (d.reason says why)

Rules, in order (the first that fails decides):
  1. VRAM: nvidia-smi free memory must stay above MIN_FREE_MIB (the input station's ASR worker needs it). Unreadable = low.
  2. Input station: read-only look at input.sqlite3. It counts as "in use" when
       - any session is still queued / transcribing / collecting and was touched within STUCK_HOURS, or
       - the newest session update, audio chunk, revision or verification is younger than IDLE_MINUTES.
     Idle for IDLE_MINUTES or more -> allowed, at any hour.
  3. Input state unreadable (file missing, locked, odd timestamps): fall back to the old rule, the 23:00-07:00 US Eastern window.
Poll at most every POLL_SECONDS; one query on a 9 MB file is a few milliseconds. Nothing here writes to the input station.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

# ---- CONFIG ----
IDLE_MINUTES = 20            # input station must have been quiet this long before 35B may start / continue
STUCK_HOURS = 2              # a session left "transcribing" longer than this is a crash leftover, not activity (ASR limit is 30 min)
POLL_SECONDS = 30            # how often a long-running caller should ask again
MIN_FREE_MIB = 1536          # keep this much VRAM free for the ASR worker (same value as bench/adapters.py:34)
WINDOW_START_HOUR, WINDOW_END_HOUR = 23, 7   # US Eastern, wraps midnight; used only when the input station cannot be read
INPUT_DB = Path(ic.V["workspace"] + r"\incore\input\data\input.sqlite3")
BUSY_STATUSES = ("collecting", "queued", "transcribing")
# ----------------


@dataclass
class Decision:
    allowed: bool
    reason: str
    basis: str            # "vram" | "input" | "night-window"

    def __bool__(self):
        return self.allowed


def _nth_sunday(year: int, month: int, n: int) -> datetime:
    d = datetime(year, month, 1, tzinfo=timezone.utc)
    return d + timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))


def eastern_hour(now_utc: datetime) -> int:
    """US Eastern hour of day (DST 2nd Sunday of March 07:00 UTC to 1st Sunday of November 06:00 UTC; same rule as the FA bench, no tzdata)."""
    start = _nth_sunday(now_utc.year, 3, 2).replace(hour=7)
    end = _nth_sunday(now_utc.year, 11, 1).replace(hour=6)
    return (now_utc + timedelta(hours=-4 if start <= now_utc < end else -5)).hour


def in_night_window(now_utc: datetime) -> bool:
    h = eastern_hour(now_utc)
    return h >= WINDOW_START_HOUR or h < WINDOW_END_HOUR


def vram_free_mib() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=10, check=True).stdout
    return int(out.split()[0])


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)


def input_last_activity(db_path: Path = INPUT_DB, now: datetime | None = None) -> tuple[datetime | None, int]:
    """(newest activity time or None if the station has never been used, number of live unfinished sessions). Raises on any read problem."""
    now = now or datetime.now(timezone.utc)
    if not Path(db_path).is_file():
        raise FileNotFoundError(str(db_path))
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        db.execute("PRAGMA query_only=1")
        newest = [row[0] for row in (
            db.execute("SELECT MAX(updated_at) FROM sessions").fetchone(),
            db.execute("SELECT MAX(stored_at) FROM chunks").fetchone(),
            db.execute("SELECT MAX(created_at) FROM revisions").fetchone(),
            db.execute("SELECT MAX(verified_at) FROM verifications").fetchone()) if row[0]]
        marks = ",".join("?" * len(BUSY_STATUSES))
        unfinished = [_parse(r[0]) for r in db.execute(f"SELECT updated_at FROM sessions WHERE status IN ({marks})", BUSY_STATUSES)]
    finally:
        db.close()
    live = sum(1 for t in unfinished if now - t < timedelta(hours=STUCK_HOURS))
    return (max(_parse(s) for s in newest) if newest else None), live


def can_run(now: datetime | None = None, db_path: Path = INPUT_DB, free_mib=vram_free_mib, idle_minutes: float | None = None) -> Decision:
    now = now or datetime.now(timezone.utc)
    idle = IDLE_MINUTES if idle_minutes is None else idle_minutes
    try:
        free = free_mib()
    except Exception as exc:  # unknown VRAM is treated like low VRAM
        return Decision(False, f"VRAM unreadable ({type(exc).__name__}); treated as low", "vram")
    if free < MIN_FREE_MIB:
        return Decision(False, f"free VRAM {free} MiB is below the {MIN_FREE_MIB} MiB kept for ASR", "vram")
    try:
        last, live = input_last_activity(db_path, now)
    except Exception as exc:
        if in_night_window(now):
            return Decision(True, f"input station unreadable ({type(exc).__name__}); inside the night window", "night-window")
        return Decision(False, f"input station unreadable ({type(exc).__name__}); outside the night window", "night-window")
    if live:
        return Decision(False, f"{live} input session(s) still collecting / queued / transcribing", "input")
    if last is not None and now - last < timedelta(minutes=idle):
        quiet = int((now - last).total_seconds() // 60)
        return Decision(False, f"input station active {quiet} min ago (needs {idle:g} min quiet)", "input")
    since = "never used" if last is None else f"quiet for {int((now - last).total_seconds() // 60)} min"
    return Decision(True, f"input station {since}; free VRAM {free} MiB", "input")


if __name__ == "__main__":
    decision = can_run()
    print(("ALLOW " if decision.allowed else "DENY  ") + f"[{decision.basis}] {decision.reason}")
    sys.exit(0 if decision.allowed else 1)
