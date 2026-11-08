"""Codex weekly-quota burn line (owner\x2010-26, records\x2f2026-10-26-owner-terminal-codex-window-reset-burn.json): the unused
part of a weekly window is lost at its reset, so the weekly used_percent should follow a straight line from 0% at the window's
start to TARGET_PERCENT at the target time (the next unexpired reset card's local expiry time, or the window's
resets_at if that comes first). `behind_points` > 0 means usage is below the line. Stdlib only; tick, review_stats and
shadow_review all read it. An unreadable weekly window is None, never 0."""
from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime

# ---- CONFIG ----
CODEX_RESET_CARDS = ("2026-11-30 15:00", "2026-12-01 17:33")  # local expiries; owner presses the cards himself (11-01)
TARGET_PERCENT = 95.0
BEHIND_POINTS = 10.0  # this far below the line: more Codex slots and a wider sol first review (tick.py)
SPEED_HOURS = 6  # the recent speed for the projection is measured over this many hours
WEEK_MINUTES = 10080
RESET_NET_DAILY_RATE = 14.3

# Reads session events only; prints [epoch seconds, weekly used_percent, resets_at] for every weekly rate_limits event in the
# last HOURS hours (newest 30 session files), never transcript text.
HISTORY_SCRIPT = '''import json, time
from datetime import datetime
from pathlib import Path
rows = []
files = sorted((Path.home()/".codex/sessions").glob("*/*/*/*.jsonl"), key=lambda p:p.stat().st_mtime, reverse=True)[:30]
for path in files:
    if time.time()-path.stat().st_mtime > HOURS*3600:
        continue
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size-3_000_000))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    for line in lines:
        if '"rate_limits"' not in line:
            continue
        try:
            e = json.loads(line)
            at = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp()
            limits = e["payload"]["rate_limits"]
            for k in ("primary", "secondary"):
                w = limits.get(k)
                if isinstance(w, dict) and w.get("window_minutes") == 10080 and at > time.time()-HOURS*3600:
                    rows.append([at, float(w["used_percent"]), w["resets_at"]])
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
rows.sort()
print(json.dumps(rows))
'''


def weekly_window(observation: dict, now: datetime) -> tuple[float, float] | None:
    """(used_percent, resets_at epoch) of the weekly window in a Codex rate_limits observation; None when it is not there, not
    finite, or its reset is already past (the same rule as shadow_review.weekly_used)."""
    try:
        for key in ("primary", "secondary"):
            w = observation["limits"].get(key)
            if isinstance(w, dict) and w.get("window_minutes") == WEEK_MINUTES:
                used, resets = float(w["used_percent"]), float(w["resets_at"])
                return (used, resets) if math.isfinite(used) and used >= 0 and resets > now.timestamp() else None
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        pass
    return None


def _local_time(timestamp: float) -> datetime:
    """Convert an instant using the local offset at that instant, including DST changes."""
    return datetime.fromtimestamp(timestamp).astimezone()


def _reset_card_time(expiry: str) -> datetime:
    """The card's actual local expiry/target time, including its minutes."""
    return datetime.fromisoformat(expiry).astimezone()


def target_time(now: datetime, resets_at: float) -> datetime:
    """The next unexpired card's local expiry, or the window's reset, whichever is earlier."""
    reset = _local_time(resets_at)
    for day in CODEX_RESET_CARDS:
        at = _reset_card_time(day)
        if at > now:
            return min(at, reset)
    return reset


def line_percent(now: datetime, resets_at: float, target: datetime) -> float:
    """0% at the window's start (resets_at minus a week) rising straight to TARGET_PERCENT at `target`, then flat."""
    start = _local_time(resets_at - WEEK_MINUTES * 60)
    span = (target - start).total_seconds()
    return TARGET_PERCENT * min(max((now - start).total_seconds() / span, 0.0), 1.0) if span > 0 else TARGET_PERCENT


def standing(observation: dict | None, now: datetime) -> dict | None:
    """used, resets_at, target, line and behind (points below the line; negative = ahead of it); None if unreadable."""
    window = weekly_window(observation, now) if observation else None
    if window is None:
        return None
    used, resets = window
    target = target_time(now, resets)
    line = line_percent(now, resets, target)
    elapsed_days = max(0.0, (now.timestamp() - (resets - WEEK_MINUTES * 60)) / 86400)
    return {"used": used, "resets_at": _local_time(resets), "target": target, "line": line,
            "behind": line - used, "net": used - RESET_NET_DAILY_RATE * elapsed_days}


def reset_note(observation: dict | None, now: datetime) -> str:
    # Owner 11-01: suggest the earliest-expiring card at >=95% with >=36 h until the natural reset.
    # In its final 24 h, suggest it whenever net gain >0; otherwise say to let it expire. Never press it automatically.
    s = standing(observation, now)
    cards = sorted(_reset_card_time(at) for at in CODEX_RESET_CARDS if _reset_card_time(at) > now)
    if s is None or not cards:
        return ""
    hours_left = (s["resets_at"].timestamp() - now.timestamp()) / 3600
    expiring = (cards[0].timestamp() - now.timestamp()) <= 24 * 3600
    prefix = f"Codex 周额度 {s['used']:g}%，离自然重置 {hours_left / 24:.1f} 天，"
    if expiring and s["net"] <= 0:
        return prefix + f"不按，让它过期（{cards[0]:%m-%d} 到期那张，净得约 {s['net']:.1f} 点）。"
    if (s["used"] >= TARGET_PERCENT and hours_left >= 36) or (expiring and s["net"] > 0):
        return prefix + f"建议现在按 {cards[0]:%m-%d} 到期那张，净得约 {s['net']:.1f} 点。"
    return ""


def behind_points(observation: dict | None, now: datetime) -> float | None:
    s = standing(observation, now)
    return None if s is None else s["behind"]


def projection(history: list, now: datetime, resets_at: float, used: float, target: datetime) -> float | None:
    """Weekly used_percent expected at `target` if the last SPEED_HOURS keep their speed; None without two readings of this
    window at least half an hour apart. Capped at 100."""
    mine = [(at, u) for at, u, r in history or [] if r == resets_at and now.timestamp() - SPEED_HOURS * 3600 <= at <= now.timestamp()]
    if len(mine) < 2 or mine[-1][0] - mine[0][0] < 1800:
        return None
    speed = (mine[-1][1] - mine[0][1]) / ((mine[-1][0] - mine[0][0]) / 3600)  # percent points per hour
    return min(100.0, used + max(speed, 0.0) * max((target - now).total_seconds(), 0.0) / 3600)


def read_history(tk) -> list | None:
    """Weekly readings of the last SPEED_HOURS hours from the Codex session files (read-only probe); None if it fails."""
    script = f"HOURS = {SPEED_HOURS}\n" + HISTORY_SCRIPT
    try:
        out = subprocess.run([*tk.CODEX[:2], "python3", "-c", script], capture_output=True, text=True, encoding="utf-8",
                             timeout=30, creationflags=tk.CREATE_NO_WINDOW)
        return json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def section(tk, now: datetime, observation: dict | None = None, history: list | None = None) -> str:
    """The "Codex 燃烧" block of review-stats.md. `observation`/`history` are read from the Codex sessions unless given."""
    import shadow_review
    if observation is None:
        try:
            observation = shadow_review.observe_quota(tk, any_age=True)
        except Exception:  # noqa: BLE001 - an unreadable quota is reported, never fatal to the stats
            observation = None
    s = standing(observation, now)
    out = [f"## Codex 燃烧（周窗口；燃烧线 = 窗口起点 0% 直线升到目标点 {TARGET_PERCENT:g}%；落后 ≥ {BEHIND_POINTS:g} 点时调度加槽）\n"]
    out.append(f"用卡规则：周额度已用 ≥{TARGET_PERCENT:g}% 且离自然重置 ≥36 小时，提醒 owner 按最早到期的那张；"
               "任何一张卡到期前 24 小时还没按，净得 >0 就提醒按，≤0 就写「不按，让它过期」。owner 自己按卡。\n")
    if s is None:
        return "\n".join(out) + "读不到 Codex 周窗口读数（不当 0 算；调度器照 3 槽、原先审范围）。\n"
    history = read_history(tk) if history is None else history
    projected = projection(history, now, s["resets_at"].timestamp(), s["used"], s["target"])
    stamp = lambda at: at.strftime("%m-%d %H:%M")  # noqa: E731  (local time)
    gap = s["behind"]
    rows = [["当前周窗口已用", f"{s['used']:.0f}%"], ["窗口重置（resets_at）", stamp(s["resets_at"])],
            ["目标点", f"{stamp(s['target'])}，{TARGET_PERCENT:g}%（下一张未过期卡的到期时刻和 resets_at 取早者）"],
            ["燃烧线此刻", f"{s['line']:.1f}%"],
            ["相对燃烧线", f"落后 {gap:.1f} 点" if gap > 0 else f"超前 {-gap:.1f} 点"],
            [f"按最近 {SPEED_HOURS} 小时速度，到目标点预计用到",
             f"{projected:.0f}%" if projected is not None else f"样本不足（{SPEED_HOURS} 小时内同一窗口少于两个相隔半小时的读数）"],
            ["调度档位", f"落后 ≥ {BEHIND_POINTS:g} 点：并行 {tk.CODEX_BURN_SLOTS} 槽、sol 先审扩范围" if gap >= BEHIND_POINTS
             else f"并行 {tk.CODEX_PARALLEL_SLOTS} 槽、sol 先审原范围"]]
    cards = [f"{at} {s['net']:+.1f} 点" for at in CODEX_RESET_CARDS if _reset_card_time(at) > now]
    source = "[OpenAI 帮助页 20001507](https://help.openai.com/en/articles/20001507-paid-weekly-work-and-codex-rate-limit-resets)"
    rows.append([f"reset 卡净得（已用% − {RESET_NET_DAILY_RATE:g}% × 本窗口已过天数；{source}）",
                 "；".join(cards) if cards else "无未过期卡"])
    rows.append(["现在用卡建议", reset_note(observation, now) or "当前无需提醒按卡"])
    out.append("| 项 | 值 |\n|---|---|\n" + "\n".join(f"| {a} | {b} |" for a, b in rows) + "\n")
    return "\n".join(out)
