"""Read Claude's built-in /usage without sending a model prompt.

Only sanitized quota JSON leaves the worker. Terminal contents stay in memory.
The caller must still enforce missing-window, freshness and budget policies.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SOURCE = "claude_code_builtin_usage"
ROOT = Path(__file__).resolve().parents[1]
CLAUDE = Path.home() / ".local" / "bin" / "claude.exe"
TIMEOUT_SECONDS = 60


class UsageParseError(ValueError):
    """A controlled error code, never terminal or authentication text."""


def normal_prompt_ready(text: str) -> bool:
    """Match the native 2.1.283 prompt verified on this machine, not a menu."""
    normalized = text.replace("’", "'")
    return bool(
        re.search(r"safe[ -]mode", normalized, re.I)
        and "don't ask on" in normalized.lower()
        and re.search(r"─{20,}", normalized)
        and re.search(r'^\s*>\s*(?:Try "[^"\n]*"|Try “[^”\n]*”)?\s*$', normalized, re.M)
    )


def usage_refresh_in_progress(text: str) -> bool:
    # The native Usage view says only `Refreshing…` while showing cached bars.
    return bool(re.search(r"\b(?:refreshing|loading)\b", text, re.I))


def blocked(reason: str) -> dict:
    return {
        "status": "blocked", "reason": reason, "source": SOURCE,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "complete_visible_snapshot": False, "windows": [],
        "model_request_sent": False,
    }


def parse_reset(value: str, observed: datetime, weekly: bool) -> dict:
    match = re.fullmatch(
        r"(?:(?P<day>Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\s+|"
        r"(?P<date>[A-Z][a-z]{2}\s+\d{1,2}(?:,?\s+\d{4})?),?\s+)?"
        r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)"
        r"\s*\((?P<zone>[A-Za-z_+\-/]+)\)", value, re.I,
    )
    if not match:
        raise UsageParseError("reset_format_unknown")
    try:
        zone = ZoneInfo(match["zone"])
    except ZoneInfoNotFoundError as exc:
        raise UsageParseError("reset_timezone_unknown") from exc
    local = observed.astimezone(zone)
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    if not 1 <= hour <= 12 or not 0 <= minute < 60:
        raise UsageParseError("reset_time_invalid")
    hour = hour % 12 + (12 if match["ampm"].lower() == "pm" else 0)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if match["date"]:
        parts = match["date"].replace(",", "").split()
        try:
            target = target.replace(
                month=datetime.strptime(parts[0].title(), "%b").month,
                day=int(parts[1]), year=int(parts[2]) if len(parts) == 3 else local.year,
            )
        except ValueError as exc:
            raise UsageParseError("reset_date_invalid") from exc
        if len(parts) == 2 and target < local:
            target = target.replace(year=target.year + 1)
    elif match["day"]:
        day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(match["day"][:3].lower())
        target += timedelta(days=(day - local.weekday()) % 7)
        if target <= local:
            target += timedelta(days=7)
    elif weekly:
        raise UsageParseError("weekly_reset_date_missing")
    elif target <= local:
        target += timedelta(days=1)
    # Reject nonexistent or ambiguous DST wall times, and implausible inference.
    if target.replace(fold=0).utcoffset() != target.replace(fold=1).utcoffset():
        raise UsageParseError("reset_dst_ambiguous")
    delta = target.astimezone(timezone.utc) - observed
    maximum = timedelta(days=7) if weekly else timedelta(hours=5)
    if not timedelta(0) < delta <= maximum:
        raise UsageParseError("reset_outside_window")
    return {
        "resets_at": int(target.timestamp()),
        "resets_at_utc": target.astimezone(timezone.utc).isoformat(),
        "reset_timezone": match["zone"],
        "reset_precision": "minute" if match["minute"] else "hour",
    }


def parse_usage_screen(text: str, observed: datetime) -> dict:
    if observed.tzinfo is None:
        raise UsageParseError("observation_timezone_missing")
    normalized = text.replace("’", "'")
    if usage_refresh_in_progress(normalized):
        raise UsageParseError("usage_refresh_in_progress")
    if re.search(r"last.known|couldn't fetch|unable to (?:fetch|load)|failed to (?:fetch|load)|rate.limit", normalized, re.I):
        raise UsageParseError("usage_not_fresh")
    boundary = re.search(r"What's contributing", normalized, re.I)
    if not boundary:
        raise UsageParseError("complete_usage_boundary_missing")
    region = normalized[:boundary.start()]
    lines = [line.strip(" │┃╭╮╰╯─\t") for line in region.splitlines()]
    headers = [(i, line) for i, line in enumerate(lines) if re.fullmatch(r"Current (?:session|week)(?:\s*\([^()]+\))?", line, re.I)]
    if not headers:
        raise UsageParseError("usage_windows_missing")
    windows = []
    consumed_percent = 0
    for index, (start, label) in enumerate(headers):
        end = headers[index + 1][0] if index + 1 < len(headers) else len(lines)
        body = "\n".join(lines[start + 1:end])
        percents = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%\s*used\b", body, re.I)
        resets = re.findall(r"^Resets\s+(.+?)\s*$", body, re.I | re.M)
        if len(percents) != 1 or len(resets) != 1:
            raise UsageParseError("window_fields_missing_or_ambiguous")
        percent = float(percents[0])
        if not 0 <= percent <= 100:
            raise UsageParseError("percent_invalid")
        weekly = "week" in label.lower()
        name = "seven_day" if weekly else "five_hour"
        qualifier = re.search(r"\(([^()]+)\)", label)
        if qualifier and qualifier[1].lower() != "all models":
            name += "_" + re.sub(r"[^a-z0-9]+", "_", qualifier[1].lower()).strip("_")
        if any(window["name"] == name for window in windows):
            raise UsageParseError("duplicate_window")
        windows.append({"name": name, "label": label, "used_percent": percent, **parse_reset(resets[0], observed, weekly)})
        consumed_percent += 1
    # All usage-like numbers above the local-contribution boundary must belong to
    # supported windows. New UI formats fail closed rather than drop a constraint.
    if len(re.findall(r"\d+(?:\.\d+)?\s*%", region)) != consumed_percent:
        raise UsageParseError("unrecognized_usage_metric")
    if len(re.findall(r"^\s*Resets\b", region, re.M | re.I)) != len(windows):
        raise UsageParseError("unrecognized_reset_metric")
    return {
        "status": "ok", "source": SOURCE,
        "observed_at_utc": observed.astimezone(timezone.utc).isoformat(),
        "complete_visible_snapshot": True, "windows": windows,
        "unreported_windows": [name for name in ("five_hour", "seven_day") if not any(w["name"] == name for w in windows)],
        "notes": ["Only visibly reported plan windows are represented; local contribution statistics are excluded.",
                  "A missing window is unknown, never zero; the caller decides which windows are required."],
        "model_request_sent": False,
    }


def read_usage() -> dict:
    # Imports stay here so parser tests work without a live PTY or Claude process.
    import pyte
    from winpty import Backend, PTY

    if os.name != "nt" or not CLAUDE.is_file():
        return blocked("native_claude_unavailable")
    env = dict(os.environ)
    if any(key.upper().startswith("ANTHROPIC_") or key.upper().startswith("CLAUDE_CODE_USE_")
           or key.upper() in {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR"}
           for key in env):
        return blocked("provider_or_auth_override_present")
    env["CLAUDE_CODE_MAX_RETRIES"] = "0"
    args = ["--safe-mode", "--restricted", "--strict-mcp-config", "--tools", "",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--settings", '{"disableAllHooks":true,"disableWorkflows":true,"disableRemoteControl":true}']
    screen = pyte.Screen(180, 110)
    stream = pyte.Stream(screen)
    pty = None
    command_sent = False
    refresh_seen = False
    last_change = time.monotonic()
    last_screen = ""
    deadline = time.monotonic() + 50
    try:
        pty = PTY(180, 110, backend=Backend.ConPTY)
        if not pty.spawn(str(CLAUDE), cmdline=" " + subprocess.list2cmdline(args), cwd=str(ROOT),
                         env="\0".join(f"{key}={value}" for key, value in env.items()) + "\0"):
            return blocked("claude_spawn_failed")
        # pyte explicitly provides this callback for terminal device/status
        # responses; these CSI bytes are not interactive command input.
        screen.write_process_input = pty.write
        while time.monotonic() < deadline:
            chunk = pty.read(blocking=False)
            if chunk:
                stream.feed(chunk)
            current = "\n".join(screen.display)
            refreshing = usage_refresh_in_progress(current)
            if command_sent and (refreshing or usage_refresh_in_progress(chunk)):
                # A fast redraw can start and finish in one PTY read. The raw
                # chunk is inspected only for this status marker, never saved.
                refresh_seen = True
            if current != last_screen:
                last_screen, last_change = current, time.monotonic()
            if re.search(r"trust this|trust the files|safety check|sign in|log in|login required|permission required|do you want to proceed|enter.*(?:code|token)|select.*login|Select with numbers", current, re.I):
                return blocked("unexpected_interactive_prompt")
            stable = time.monotonic() - last_change >= 0.6
            if not command_sent:
                if stable and normal_prompt_ready(current):
                    pty.write("/usage\r")
                    command_sent = True
            elif stable and refresh_seen and not refreshing and "What's contributing" in current.replace("’", "'"):
                result = parse_usage_screen(current, datetime.now(timezone.utc))
                result["freshness_evidence"] = "refresh_indicator_observed_then_cleared"
                return result
            if not pty.isalive():
                return blocked("claude_exited_before_snapshot")
            time.sleep(0.05)
        reason = "normal_prompt_not_detected"
        if command_sent:
            reason = "usage_refresh_timeout" if refresh_seen else "usage_refresh_not_observed"
        result = blocked(reason)
        result["ui_diagnostics"] = {
            "output_received": bool(last_screen.strip()),
            "claude_code_heading": "Claude Code" in last_screen,
            "prompt_glyph_present": "❯" in last_screen,
            "shortcut_footer": "? for shortcuts" in last_screen,
            "usage_heading": "Current session" in last_screen,
            "contribution_boundary": "contributing" in last_screen.lower(),
            "usage_command_sent": command_sent,
            "screen_reader_banner": "[Screen Reader Mode: on via flag]" in last_screen,
            "screen_reader_empty_prompt": bool(re.search(r"^\s*you:\s*$", last_screen, re.M)),
            "native_prompt_ready": normal_prompt_ready(last_screen),
            "refresh_indicator_seen": refresh_seen,
        }
        return result
    except UsageParseError as exc:
        result = blocked(str(exc))
        # Preserve the reason for rejecting cached bars without logging terminal
        # contents, account identifiers, URLs or authentication material.
        result["freshness_diagnostics"] = {
            "last_known_notice": bool(re.search(r"last.known", last_screen, re.I)),
            "fetch_failure_notice": bool(re.search(r"couldn't fetch|unable to (?:fetch|load)|failed to (?:fetch|load)", last_screen, re.I)),
            "rate_limit_notice": bool(re.search(r"rate.limit", last_screen, re.I)),
            "visible_http_error_codes": sorted(set(re.findall(r"\b(?:401|403|429|50[0234])\b", last_screen))),
            "refresh_started_and_finished": refresh_seen and not usage_refresh_in_progress(last_screen),
        }
        result["visible_quota_lines"] = [line.strip() for line in last_screen.splitlines()
                                         if re.fullmatch(r"\s*(?:Current (?:session|week)(?:\s*\([^()]+\))?|Resets \d[^\n]*|[█░▒▓━─ ]*\d+(?:\.\d+)?% used)\s*", line)]
        return result
    except Exception:
        return blocked("terminal_probe_failed")
    finally:
        if pty is not None:
            try:
                if pty.isalive():
                    os.kill(pty.pid, signal.SIGTERM)
            except (OSError, RuntimeError):
                pass


def main() -> int:
    if sys.argv[1:] == ["--worker"]:
        result = read_usage()
    elif sys.argv[1:]:
        result = blocked("unsupported_argument")
    else:
        # Separate process gives the caller a real wall-clock bound even if a
        # native terminal operation hangs. Kill only this worker and its child.
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            output, _ = process.communicate(timeout=54)
            result = json.loads(output)
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=4, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
            except (subprocess.TimeoutExpired, OSError):
                process.kill()
            result = blocked("hard_timeout")
        except (ValueError, OSError):
            result = blocked("probe_worker_failed")
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
