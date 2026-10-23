"""Subscription usage: a pluggable observation source and the dispatch gate built on it.

A reading always carries its source, observation time, window name and reset time. Missing, unreadable, outdated or
synthetic-in-live readings stop new dispatch and say why; unknown is never treated as zero.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .backlog import format_time, parse_time


class UsageUnavailable(Exception):
    """The source could not produce readings at all (missing file, bad content, adapter error)."""


@dataclass(frozen=True)
class Observation:
    window: str
    used_percent: float  # share of the whole window already consumed on the account, by anyone
    observed_at: datetime
    resets_at: datetime
    source: str
    synthetic: bool = False

    def __post_init__(self):
        if not 0 <= self.used_percent <= 100:
            raise UsageUnavailable(f"window '{self.window}': used_percent {self.used_percent} is outside 0-100")
        if not self.source:
            raise UsageUnavailable(f"window '{self.window}': reading has no source")


class UsageSource(Protocol):
    def observe(self) -> list[Observation]: ...


class FakeUsage:
    """Fixed readings; the built-in source for rehearsals and tests."""

    def __init__(self, readings: list[Observation]):
        self.readings = list(readings)

    def observe(self) -> list[Observation]:
        return list(self.readings)

    @classmethod
    def healthy(cls, policy: dict, now: datetime, used_percent: float = 10.0) -> "FakeUsage":
        """One synthetic, fresh, low reading per configured window."""
        return cls([Observation(w["name"], used_percent, now, now + timedelta(hours=3), "synthetic-rehearsal", True)
                    for w in policy["subscription_windows"]])


class JsonFileUsage:
    """Readings from a JSON file (state/usage.json): {"observations": [{window, used_percent, observed_at, resets_at,
    source, synthetic?}]}. Times are UTC like 2026-01-05T10:00:00Z."""

    def __init__(self, path: Path):
        self.path = path

    def observe(self) -> list[Observation]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [Observation(o["window"], float(o["used_percent"]), parse_time(o["observed_at"]),
                                parse_time(o["resets_at"]), o["source"], bool(o.get("synthetic", False)))
                    for o in raw["observations"]]
        except FileNotFoundError:
            raise UsageUnavailable(f"usage file not found: {self.path}") from None
        except (ValueError, KeyError, TypeError) as exc:
            raise UsageUnavailable(f"usage file unreadable ({type(exc).__name__}: {exc})") from None


@dataclass
class Gate:
    allowed: bool
    reasons: list[str]


def evaluate(policy: dict, readings: list[Observation], now: datetime) -> Gate:
    """Check every configured window; any failing window blocks new dispatch. Reasons name the window and the cause."""
    live = policy["live_enabled"]
    reasons = []
    for window in policy["subscription_windows"]:
        name = window["name"]
        mine = [r for r in readings if r.window == name]
        if not mine:
            reasons.append(f"{name}: unknown, no observation (unknown is never treated as zero)")
            continue
        reading = max(mine, key=lambda r: r.observed_at)
        age = (now - reading.observed_at).total_seconds() / 60
        if reading.synthetic and live:
            reasons.append(f"{name}: synthetic reading from '{reading.source}' is not accepted in live mode")
        elif age < 0:
            reasons.append(f"{name}: reading from '{reading.source}' is dated in the future ({format_time(reading.observed_at)})")
        elif age > window["max_observation_age_minutes"]:
            reasons.append(f"{name}: stale, observed {age:.0f} min ago by '{reading.source}' "
                           f"(limit {window['max_observation_age_minutes']} min)")
        elif now >= reading.resets_at:
            reasons.append(f"{name}: reading predates the window reset at {format_time(reading.resets_at)}")
        elif reading.used_percent >= 100 - window["reserve_percent"]:
            reasons.append(f"{name}: used {reading.used_percent:g}% reaches the reserve boundary "
                           f"({100 - window['reserve_percent']:g}%)")
        elif reading.used_percent >= window["max_share_percent"]:
            reasons.append(f"{name}: used {reading.used_percent:g}% reaches the maximum share ({window['max_share_percent']:g}%)")
        elif "length_hours" in window:
            length = timedelta(hours=window["length_hours"])
            elapsed = min(max((now - (reading.resets_at - length)) / length, 0.0), 1.0)
            pace = window["max_share_percent"] * elapsed + window["pacing_slack_percent"]
            if reading.used_percent > pace:
                reasons.append(f"{name}: used {reading.used_percent:g}% is ahead of the pace line ({pace:.1f}%)")
    return Gate(not reasons, reasons)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
