"""Usage readings from the official CLI's own status data. No endpoint is polled and no model is called.

An interactive Claude Code session runs the configured `statusLine` command and hands it JSON on standard input. When the
account reports them, that JSON holds `rate_limits.five_hour` and `rate_limits.seven_day`, each with `used_percentage`
(0-100) and `resets_at` (Unix seconds). `capture` turns them into readings in state/usage.json:

  * source is "claude-code-statusline" plus the CLI version when it is in the data; the reading is never synthetic
  * a window that is absent, malformed, out of range or already reset is not written: it stays unknown, never zero
  * a window that is absent from this call keeps its older reading, which goes stale by itself

Headless (`-p`) sessions draw no status line, so they produce no readings; keep an interactive session open (the setup
session does this) or the gate stays closed because the readings go stale.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .backlog import format_time
from .fsio import atomic_write, file_lock
from .usage import Observation, UsageUnavailable, usage_path

SOURCE = "claude-code-statusline"
WINDOWS = ("five_hour", "seven_day")


def _short(path: str) -> str:
    """Forward slashes; on Windows the 8.3 short form when the path holds a space (that form needs no quoting)."""
    if " " in path and sys.platform.startswith("win"):
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024):
            path = buf.value
    return path.replace("\\", "/")


def hook_command(python: str, root: Path) -> str:
    """The status-line command for `statusLine`. The CLI runs it through the machine's shell, and some Windows setups use
    PowerShell, where a quoted program path at the start is a syntax error. So the paths are written with forward slashes
    and without quotes when they hold no space (this is also how the CLI's own examples write them); a path that still
    holds a space is quoted, with a leading `& ` on Windows (PowerShell syntax; a cmd or bash shell needs a space-free path)."""
    parts = [_short(python), "-m", "orgcore", "usage-capture", _short(str(root))]
    if not any(" " in p for p in parts):
        return " ".join(parts)
    quoted = " ".join(f'"{p}"' if " " in p else p for p in parts)
    return "& " + quoted if sys.platform.startswith("win") else quoted


def parse(text: str, now: datetime) -> tuple[list[Observation], list[str]]:
    """Readings found in one status payload, and a note for every window that could not be used."""
    try:
        data = json.loads(text)
    except ValueError:
        return [], ["the status data is not valid JSON"]
    limits = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(limits, dict):
        return [], ["no rate_limits in the status data (not reported for this account or session yet)"]
    version = data.get("version")
    source = f"{SOURCE} {version}" if isinstance(version, str) and version else SOURCE
    found, notes = [], []
    for name in WINDOWS:
        entry = limits.get(name)
        if not isinstance(entry, dict):
            notes.append(f"{name}: not reported")
            continue
        used, resets = entry.get("used_percentage"), entry.get("resets_at")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
            notes.append(f"{name}: used_percentage missing or outside 0-100")
        elif isinstance(resets, bool) or not isinstance(resets, (int, float)):
            notes.append(f"{name}: resets_at missing")
        elif datetime.fromtimestamp(resets, timezone.utc) <= now:
            notes.append(f"{name}: reading predates the reset")
        else:
            found.append(Observation(name, float(used), now, datetime.fromtimestamp(resets, timezone.utc).replace(microsecond=0),
                                     source))
    return found, notes


def capture(root: Path, text: str, now: datetime) -> str:
    """Merge the readings in `text` into state/usage.json; returns the one line the status bar shows."""
    found, notes = parse(text, now)
    path = usage_path(root)
    if found:
        with file_lock(path):
            try:
                old = [o for o in _read(path) if o.window not in {f.window for f in found}]
            except UsageUnavailable:
                old = []  # an unreadable file is replaced by the fresh readings; it was not trusted before either
            rows = [*old, *found]
            payload = {"observations": [{"window": o.window, "used_percent": o.used_percent, "observed_at": format_time(o.observed_at),
                                         "resets_at": format_time(o.resets_at), "source": o.source, "synthetic": o.synthetic}
                                        for o in rows]}
            atomic_write(path, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
        return "usage " + " ".join(f"{o.window.split('_')[0]} {o.used_percent:g}%" for o in found)
    return "usage unknown: " + notes[0]


def _read(path: Path) -> list[Observation]:
    from .usage import JsonFileUsage
    return JsonFileUsage(path).observe() if path.exists() else []
