"""Read-only organization snapshot for the owner dashboard.

Sources: control/data/usage.jsonl (five-hour window observations), control/data/deputies.json
(resident deputy sessions), projects/backlog.md (each line's plan), discord/data/discord.sqlite3
(SQLite mode=ro) and the service pid files.
A page read never starts models or dispatch and never writes operational data. Text fields are
trimmed and scrubbed of secrets and full paths; session ids, raw replies and full inputs are not returned.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import sys as _sys
_sys.path.append(str(Path(__file__).resolve().parent.parent / "control"))
import instance_config as ic  # noqa: E402  (line titles and their roles; defaults = today's values)

STALE_SECONDS = 15 * 60
WINDOW_SECONDS = 5 * 3600
PAUSE_PCT = 80  # fallback only: the pacing target, tick.BASELINE_MAX (see _limits)
CEILING_PCT = 90  # fallback only: the hard ceiling, tick.HARD_CEILING
USAGE_TAIL_BYTES = 1024 * 1024
POINT_LIMIT = 500
HISTORY_LIMIT = 5
JSON_LIMIT = 2 * 1024 * 1024
BACKLOG_LIMIT = 32 * 1024 * 1024  # backlog.md grows daily and passed JSON_LIMIT at 2.75 MB
DB_TIMEOUT_SECONDS = 1.0
OPEN_LIMIT = 50
ANSWERED_LIMIT = 10
PID_START_TOLERANCE_SECONDS = 60
ROLES = tuple(r for r in ("coordinator", "work", "uvox", "life", "quant", "intelligence") if r in ic.get("lines"))  # the lines this instance has; work = Arbitros 线, uvox = Beacon Listener 线 (split of 10-27)
SERVICES = (("input", ("input", "data", "server-process.json")),
            ("backup", ("data", "backups", "watcher-process.json")),
            ("discord", ("discord", "data", "gateway-process.json")))
# Backlog sections as control/tick.py maps them to roles, coordinator first. The old 工作线 title stays an alias of `work`
# (listed first, so the new title is the one a role's card shows) until backlog.md is converted by control/line_split.py.
PLAN_SECTIONS = {}  # instance.json `lines` in order, the old 工作线 title just before the work line's own
for _role, _title in ic.get("lines").items():
    if _role == "work":
        PLAN_SECTIONS["工作线"] = "work"
    PLAN_SECTIONS[_title] = _role
OWNER_HEADING = "你的指令（最优先）"
PLAN_ITEM_LIMIT = 1200
_PLAN_LINE = re.compile(r"^- \[(.+?)\] (.+)$")
_PLAN_STATUS = re.compile(r"^(todo|running|review|blocked|done)(?![\w-])[\s:：]*(.*)$")
_PLAN_WEIGHT = re.compile(r"^权重[：:]\s*(\d+(?:\.\d+)?)\s*%\s*(.*)$")
_OWNER_TRAILER = re.compile(r"\s*〔msg:\S+ role:(\w+)〕")

_SECRET = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{20,}|xox[abprs]-[a-z0-9-]{10,}"
    r"|eyJ[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}(?:\.[a-z0-9_-]+)?|bearer\s+[a-z0-9._~+/=-]{8,}"
    r"|(?:api[_-]?key|token|secret|password|passwd)\s*[=:]\s*\S+)")
_PATH = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\|/(?:home|users|mnt|var|tmp|etc|opt|root|private)/)[^\s\"'<>|]*")


def _text(value: Any, limit: int = 120) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    text = _SECRET.sub("[已隐藏]", value)
    # Full filesystem paths collapse to their last component.
    text = _PATH.sub(lambda m: re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1] or "[路径]", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _time(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            text = value.strip()
            if not text or len(text) > 64:
                return None
            if re.fullmatch(r"\d{9,13}(?:\.\d+)?", text):
                value = float(text)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                # SQLite CURRENT_TIMESTAMP values carry no zone; they are UTC by convention.
                return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z") if moment else None


def _age(moment: datetime | None, now: datetime) -> int | None:
    return max(0, int((now - moment).total_seconds())) if moment else None


def _guard(build: Callable[[], Any], fallback: Any) -> Any:
    try:
        return build()
    except Exception:
        return fallback


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root.resolve())
    return resolved


def _read_text(root: Path, *parts: str, limit: int = JSON_LIMIT) -> tuple[str, str | None, datetime | None]:
    path = root.joinpath(*parts)
    try:
        if path.is_symlink():
            return "unreadable", None, None
        if not path.exists():
            return "missing", None, None
        resolved = _inside(path, root)
        stat = resolved.stat()
        if not resolved.is_file() or stat.st_size > limit:
            return "unreadable", None, None
        mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        with resolved.open("r", encoding="utf-8-sig") as stream:
            return "ok", stream.read(), mtime
    except (OSError, ValueError):
        return "unreadable", None, None


def _read_json(root: Path, *parts: str) -> tuple[str, Any, datetime | None]:
    state, text, mtime = _read_text(root, *parts)
    if state != "ok":
        return state, None, mtime
    try:
        return "ok", json.loads(text), mtime
    except ValueError:
        return "invalid", None, mtime


def _read_tail(root: Path, *parts: str) -> tuple[str, str]:
    path = root.joinpath(*parts)
    try:
        if path.is_symlink():
            return "unreadable", ""
        if not path.exists():
            return "missing", ""
        with _inside(path, root).open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - USAGE_TAIL_BYTES))
            data = stream.read()
    except (OSError, ValueError):
        return "unreadable", ""
    if size > USAGE_TAIL_BYTES:
        data = data.split(b"\n", 1)[-1]  # drop the partial first line
    return "ok", data.decode("utf-8", "replace")


def _usage(root: Path, now: datetime) -> dict[str, Any]:
    state, text = _read_tail(root, "control", "data", "usage.jsonl")
    pause, ceiling = _limits()
    out: dict[str, Any] = {"state": state, "pause_pct": pause, "ceiling_pct": ceiling,
                           "window_seconds": WINDOW_SECONDS, "latest": None, "window": None, "history": []}
    observations = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        at, used, resets = _time(item.get("observed_at")), _number(item.get("utilization")), _number(item.get("resets_at"))
        if at and used is not None and used >= 0 and resets:
            observations.append((at, round(used * 100, 1), int(resets), _text(item.get("source"), 40)))
    if state == "ok" and not observations:
        out["state"] = "empty"
    if not observations:
        return out
    observations.sort(key=lambda obs: obs[0])
    windows: dict[int, list[tuple[datetime, float, int, str | None]]] = {}
    for obs in observations:
        windows.setdefault(obs[2], []).append(obs)

    at, used, resets, source = latest = observations[-1]
    end = datetime.fromtimestamp(resets, timezone.utc)
    start = end - timedelta(seconds=WINDOW_SECONDS)
    elapsed = min(1.0, max(0.0, (at - start).total_seconds() / WINDOW_SECONDS))
    out["latest"] = {"observed_at_utc": _iso(at), "pct": used, "source": source, "age_seconds": _age(at, now),
                     "stale": _age(at, now) > STALE_SECONDS, "baseline_pct": round(pause * elapsed, 1)}
    out["window"] = {"start_utc": _iso(start), "end_utc": _iso(end), "ended": end <= now,
                     "points": [{"at_utc": _iso(obs[0]), "pct": obs[1], "source": obs[3]}
                                for obs in windows[latest[2]][-POINT_LIMIT:]]}
    out["history"] = [{"end_utc": _iso(datetime.fromtimestamp(key, timezone.utc)), "final_pct": group[-1][1],
                       "peak_pct": max(obs[1] for obs in group), "samples": len(group)}
                      for key, group in sorted(windows.items(), reverse=True)
                      if datetime.fromtimestamp(key, timezone.utc) <= now][:HISTORY_LIMIT]
    return out


def _origin(source: str | None) -> str | None:
    lowered = (source or "").lower()
    if not lowered:
        return None
    if lowered.startswith(("coordinator", "dispatch")):
        return "coordinator"
    return "input" if lowered.startswith("input") else "discord"


def _deputies(root: Path, now: datetime) -> dict[str, Any]:
    state, data, mtime = _read_json(root, "control", "data", "deputies.json")
    if state == "ok" and not isinstance(data, dict):
        state = "unknown_schema"
    roles = []
    for key in ROLES:
        entry = data.get(key) if state == "ok" else None
        if not isinstance(entry, dict):
            roles.append({"key": key, "enabled": False})
            continue
        current, last, runs = entry.get("current"), entry.get("last"), entry.get("runs")
        role: dict[str, Any] = {"key": key, "enabled": True, "status": _text(entry.get("status"), 20),
                                "runs": runs if isinstance(runs, int) and not isinstance(runs, bool) else None,
                                "current": None, "last": None}
        if isinstance(current, dict):
            started = _time(current.get("started_at"))
            role["current"] = {"preview": _text(current.get("text_preview"), 120),
                               "origin": _origin(_text(current.get("source"), 40)),
                               "started_utc": _iso(started), "elapsed_seconds": _age(started, now)}
        if isinstance(last, dict):
            ok = last.get("ok")
            role["last"] = {"finished_utc": _iso(_time(last.get("finished_at"))),
                            "ok": ok if isinstance(ok, bool) else None,
                            "elapsed_seconds": _number(last.get("elapsed_s")),
                            "summary": _text(last.get("summary"), 240)}
        roles.append(role)
    return {"state": state, "updated_utc": _iso(mtime), "roles": roles}


def _plans(root: Path) -> dict[str, Any]:
    """Each line's items from projects/backlog.md in file order, which is the order tick.py dispatches todos.
    Open owner instructions are listed under the role they were sent to, ahead of that line's own items."""
    state, text, mtime = _read_text(root, "projects", "backlog.md", limit=BACKLOG_LIMIT)
    plans = {role: {"key": role, "section": section, "weight_pct": None, "weight_note": None, "running": [],
                    "review": [], "todo": [], "blocked": [], "done_count": 0}
             for section, role in PLAN_SECTIONS.items()}
    current, since = None, 99
    for raw in (text or "").split("\n"):
        line = raw.rstrip("\r")
        if line.startswith("## "):
            heading = line[3:].strip()
            current, since = OWNER_HEADING if heading == OWNER_HEADING else PLAN_SECTIONS.get(heading), 0
            continue
        since += 1
        weight = _PLAN_WEIGHT.match(line)
        if weight and since <= 3 and current in plans:
            plans[current]["weight_pct"] = float(weight.group(1))
            plans[current]["weight_note"] = _text(weight.group(2).strip("（）() "), 200) or None
            continue
        item = _PLAN_LINE.match(line)
        status = _PLAN_STATUS.match(item.group(1).strip()) if item else None
        if not status or current is None:
            continue
        kind, detail, body, owner = status.group(1), status.group(2).strip(), item.group(2), current == OWNER_HEADING
        role = current
        if owner:
            trailer = _OWNER_TRAILER.search(body)
            role, body = (trailer.group(1) if trailer else None), _OWNER_TRAILER.sub("", body)
            if role not in plans or kind == "done":
                continue
        if kind == "done":
            continue
        entry: dict[str, Any] = {"text": _text(body, PLAN_ITEM_LIMIT), "owner": owner}
        if kind in ("running", "blocked"):
            entry["detail"] = _text(detail, 120) or None
        if kind == "todo" and not owner:
            try:
                entry["project"] = _tick().project_of(body)  # the executor project tick.py would give this todo
            except Exception:  # tick.py unreadable: no label, the plans still show
                pass
        plans[role][kind].append(entry)
    for row in _control("backlog_history").all_rows(Path(root) / "projects/backlog.md", text or ""):
        role = PLAN_SECTIONS.get(row["section"])
        if row["status"] == "done" and role in plans:
            plans[role]["done_count"] += 1
    return {"state": state, "updated_utc": _iso(mtime), "roles": list(plans.values()), "has_sprint": bool(re.search(r"^冲刺[：:]", text or "", re.M))}


_tick_module: Any = None
_tick_mtime: float | None = None


def _tick() -> Any:
    """control/tick.py, for its PROJECTS table and project_of routing. Importing it only defines constants and
    functions (it also puts control/ on sys.path and imports backlog_io, kpi, usage_sample); nothing runs or is written.
    Reloaded when the file changes, so a merged tick.py (a new project) shows without restarting the server."""
    global _tick_module, _tick_mtime
    path = Path(__file__).resolve().parent.parent / "control" / "tick.py"
    mtime = path.stat().st_mtime
    if _tick_module is None or mtime != _tick_mtime:
        import importlib.util
        spec = importlib.util.spec_from_file_location("incubator_tick", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _tick_module, _tick_mtime = module, mtime
    return _tick_module


def _control(name: str) -> Any:
    """A stdlib-only module from control/ (loaded by path, like tick.py), for the deployment registry helpers."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"incubator_{name}", Path(__file__).resolve().parent.parent / "control" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _limits() -> tuple[float, float]:
    """(pacing target, hard ceiling) of the usage chart, read from tick.py so it cannot drift from the scheduler;
    the constants above if tick.py cannot be loaded."""
    try:
        tick = _tick()
        return tick.BASELINE_MAX, tick.HARD_CEILING
    except Exception:
        return PAUSE_PCT, CEILING_PCT


def _lane_live(root: Path, name: str) -> bool:
    """control/data/lane-<name>.lock holds the PID of the live run; a dead or reused pid does not count."""
    state, text, _ = _read_text(root, "control", "data", f"lane-{name}.lock")
    if state != "ok" or os.name != "nt":
        return False
    try:
        pid = int(text.strip())
        return pid > 0 and _process_created(pid) is not None
    except (ValueError, OSError):
        return False


EXECUTOR_STALE_SECONDS = 2 * 3600 + 600  # tick.py's executor timeout is 7200 s; older unfinished entries are crashed runs
AGENT_ORDER = ("running", "review", "todo", "blocked", "idle", "unstarted")
RUN_KINDS = {"review": "验收", "plan": "规划", "coordinate": "跨线复盘", "report": "写日报", "terminal": "终端会话"}


def _agents(root: Path, now: datetime, plans: dict[str, Any]) -> dict[str, Any]:
    """Each line's resident project sessions ("agents") and what they are doing. Status precedence:
    running (lane lock with a live pid, or an unfinished run) > unstarted (no session yet) > review (a [review] row
    of the line routes here: stopped, waiting for the deputy) > todo > blocked (only blocked rows) > idle."""
    tick = _tick()
    state, data, _ = _read_json(root, "control", "data", "deputies.json")
    data = data if state == "ok" and isinstance(data, dict) else {}
    kstate, kpi, _ = _read_json(root, "control", "data", "kpi.json")
    sessions = {(s.get("role"), s.get("project"), s.get("session")): s
                for s in (kpi.get("executor_sessions") or [] if kstate == "ok" and isinstance(kpi, dict) else [])
                if isinstance(s, dict)}
    by_role = {p["key"]: p for p in plans.get("roles", [])}
    talk = f"pwsh -File {tick.WORKSPACE / 'incore' / 'control' / 'talk.ps1'}"  # the live script, whichever tree serves
    roles = []
    for key in ROLES:
        entry = data.get(key) if isinstance(data.get(key), dict) else {}
        rows: dict[str, dict[str, int]] = {}
        for kind in ("review", "todo", "blocked"):
            for item in (by_role.get(key) or {}).get(kind, []):
                if not item.get("owner"):  # open owner instructions belong to the deputy, not a project
                    count = rows.setdefault(tick.project_of(item.get("text") or ""), {})
                    count[kind] = count.get(kind, 0) + 1
        executors = entry.get("executors") if isinstance(entry.get("executors"), dict) else {}
        current = entry.get("current") if isinstance(entry.get("current"), dict) else None
        deputy_run = _lane_live(root, key) or entry.get("status") == "running"
        kind = (current or {}).get("kind")
        agents = []
        for project in [p for p in (*tick.PROJECTS, "general") if p in rows or p in executors]:
            ex = executors.get(project) if isinstance(executors.get(project), dict) else {}
            counts = rows.get(project, {})
            sid, started, finished = ex.get("session_id"), _time(ex.get("started_at")), _time(ex.get("finished_at"))
            unfinished = bool(started and not finished and (_age(started, now) or 0) <= EXECUTOR_STALE_SECONDS)
            live = _lane_live(root, f"{key}-{project}")
            if live or unfinished:
                status = "running"
            elif not sid:
                status = "unstarted"
            else:
                status = next(s for s, on in (("review", counts.get("review")), ("todo", counts.get("todo")),
                                              ("blocked", counts.get("blocked")), ("idle", True)) if on)
            kpi_row = sessions.get((key, project, str(sid)[:8])) if sid else None
            cwd = tick.PROJECTS[project][0] if project in tick.PROJECTS else tick.WORKSPACE
            try:
                folder = str(cwd.relative_to(tick.WORKSPACE)) if cwd != tick.WORKSPACE else "（工作区根目录）"
            except ValueError:
                folder = cwd.name
            task = _text(ex.get("last_item") or ex.get("item_preview"), 200)
            agents.append({
                "project": project, "status": status, "folder": folder, "session": str(sid)[:8] if sid else None,
                "runs": ex.get("runs") if isinstance(ex.get("runs"), int) else None,
                "context_tokens": ex.get("context_tokens") if isinstance(ex.get("context_tokens"), int) else None,
                "terminal": bool(live and (ex.get("current") or {}).get("kind") == "terminal"),
                "command": f"{talk} {key} {project}" if sid else None,
                "task": task, "started_utc": _iso(started), "finished_utc": _iso(finished),
                "exit_code": ex.get("exit_code") if isinstance(ex.get("exit_code"), int) else None,
                "review": counts.get("review", 0), "todo": counts.get("todo", 0), "blocked": counts.get("blocked", 0),
                "first_pass_rate": _number((kpi_row or {}).get("first_pass_rate")),
                "reviewed": (kpi_row or {}).get("reviewed_n") if isinstance((kpi_row or {}).get("reviewed_n"), int) else None,
            })
        agents.sort(key=lambda a: AGENT_ORDER.index(a["status"]))
        roles.append({"key": key, "running": bool(deputy_run), "kind": RUN_KINDS.get(kind, "处理你的指令" if current else None)
                      if deputy_run else None, "preview": _text((current or {}).get("text_preview"), 100) if deputy_run else None,
                      "command": f"{talk} {key}" if entry.get("session_id") else None, "agents": agents,
                      "counts": {s: sum(1 for a in agents if a["status"] == s) for s in AGENT_ORDER}})
    return {"state": "ok" if state == "ok" else state, "roles": roles}


def _request(row: sqlite3.Row, now: datetime) -> dict[str, Any]:
    retry = _time(row["next_attempt"])
    status = _text(row["status"], 40)
    return {
        "status": status,
        "queued_only": bool(row["queued_only"]),
        "role": _text(row["role"], 40),
        "source": _text(row["source"], 40),
        "origin": _origin(row["source"]),
        "preview": _text(row["exact_text"], 100),
        "received_utc": _iso(_time(row["received_at"])),
        "updated_utc": _iso(_time(row["updated_at"])),
        "next_attempt_utc": _iso(retry) if retry and retry > now and status != "answered" else None,
        "detail": _text(row["detail"], 160) if status != "answered" else None,
    }


def _queue(root: Path, now: datetime) -> dict[str, Any]:
    out: dict[str, Any] = {"state": "missing", "counts": {}, "open": [], "answered": []}
    path = root / "discord" / "data" / "discord.sqlite3"
    try:
        if not path.exists() and not path.is_symlink():
            return out
        if path.is_symlink() or not path.is_file():
            return {**out, "state": "unreadable"}
        # mode=ro never creates the file; a missing DB stays missing.
        conn = sqlite3.connect(f"{_inside(path, root).as_uri()}?mode=ro", uri=True, timeout=DB_TIMEOUT_SECONDS)
    except (OSError, ValueError, sqlite3.Error):
        return {**out, "state": "unreadable"}
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        select = ("SELECT r.status, r.queued_only, r.updated_at, r.next_attempt, r.detail, i.source, i.role,"
                  " i.received_at, i.exact_text FROM direct_requests r LEFT JOIN inbound i USING(message_id)")
        out["open"] = [_request(row, now) for row in conn.execute(
            f"{select} WHERE r.status != 'answered' ORDER BY r.updated_at DESC LIMIT ?", (OPEN_LIMIT,))]
        out["answered"] = [_request(row, now) for row in conn.execute(
            f"{select} WHERE r.status = 'answered' ORDER BY r.updated_at DESC LIMIT ?", (ANSWERED_LIMIT,))]
        out["counts"] = {str(status): n for status, n in conn.execute(
            "SELECT status, count(*) FROM direct_requests GROUP BY status")}
        out["state"] = "ok"
    except sqlite3.Error:
        out["state"] = "error"
    finally:
        conn.close()
    return out


def _process_created(pid: int) -> datetime | None:
    """Creation time of a live Windows process, None when it is not running; OSError when unknown."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such process
            return None
        raise OSError("process not queryable")
    try:
        code, times = wintypes.DWORD(), [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or \
                not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise OSError("process not queryable")
        if code.value != 259:  # STILL_ACTIVE
            return None
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
    finally:
        kernel.CloseHandle(handle)


def _service(root: Path, key: str, parts: tuple[str, ...]) -> dict[str, Any]:
    state, data, _ = _read_json(root, *parts)
    data = data if isinstance(data, dict) else {}
    pid = data.get("ProcessId", data.get("pid"))
    pid = pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None
    started = _time(data.get("StartedAtUtc"))
    out = {"key": key, "state": "missing" if state == "missing" else "unknown", "pid": pid, "started_utc": _iso(started)}
    if pid is None or os.name != "nt":
        return out
    try:
        created = _process_created(pid)
    except OSError:
        return out
    # A reused pid belongs to a process created at a different time than the recorded start.
    alive = created is not None and (started is None or
                                     abs((created - started).total_seconds()) <= PID_START_TOLERANCE_SECONDS)
    out["state"] = "running" if alive else "stopped"
    return out


URL_TIMEOUT_SECONDS = 3
URL_CACHE_SECONDS = 300
URL_WAIT_SECONDS = 1.0  # a page read waits at most this long for fresh checks; slower ones show "checking" until the next poll
_url_cache: dict[str, tuple[float, str]] = {}
_url_pending: dict[str, Any] = {}
_url_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="url-check")


def _fetch_status(url: str) -> str:
    """online | password (401) | down. Only the status line is looked at."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "incore-dashboard"}),
                                    timeout=URL_TIMEOUT_SECONDS):
            return "online"
    except urllib.error.HTTPError as err:
        return "password" if err.code == 401 else "down"
    except Exception:
        return "down"


def _url_states(urls: list[str]) -> dict[str, str]:
    """Cached result per url (5 min); stale or unknown urls are re-checked in the background."""
    now = time.monotonic()
    fresh = []
    for url in urls:
        if url in _url_pending and not _url_pending[url].done():
            continue
        hit = _url_cache.get(url)
        if hit is None or now - hit[0] > URL_CACHE_SECONDS:
            def done(fut: Any, url: str = url) -> None:
                _url_cache[url] = (time.monotonic(), fut.result())
            _url_pending[url] = fut = _url_pool.submit(_fetch_status, url)
            fut.add_done_callback(done)
            fresh.append(fut)
    wait(fresh, timeout=URL_WAIT_SECONDS)
    return {url: _url_cache[url][1] if url in _url_cache else "checking" for url in urls}


def _deploy_gap(entry: dict[str, Any], root: Path) -> dict[str, Any]:
    """Commits on the deployed branch since the deployed commit; 'unknown' when repo, branch or commit is not found."""
    ctl = _control("deployments")
    repo = ctl.repo_path(entry, root)
    if repo is None:
        return {"state": "norepo"}
    commit, branch = entry.get("commit"), entry.get("branch")
    count = ctl.git(repo, "rev-list", "--count", f"{commit}..{branch}") if commit and branch else None
    head = ctl.git(repo, "log", "-1", "--format=%h%x09%cI%x09%s", branch) if count is not None else None
    if head is None:
        return {"state": "unknown"}
    short, committed, title = (head.split("\t", 2) + ["", ""])[:3]
    return {"state": "ok", "behind": int(count), "head": short, "head_at_utc": _iso(_time(committed)),
            "head_title": _text(title, 100)}


def _deployments(root: Path) -> dict[str, Any]:
    state, data, _ = _read_json(root, "projects", "deployments.json")
    if state != "ok":
        return {"state": state, "products": []}
    entries = [e for e in data.get("products", []) if isinstance(e, dict) and isinstance(e.get("url"), str)] \
        if isinstance(data, dict) else []
    states = _url_states([e["url"] for e in entries])
    products = []
    for e in entries:
        stamp = e.get("deployed_at")
        products.append({
            "name": _text(e.get("name")), "url": e["url"], "access": "password" if e.get("access") == "password" else "public",
            "online": states[e["url"]], "branch": _text(e.get("branch")), "commit": _text(e.get("commit"), 12),
            "deployed_at": _iso(_time(stamp)) if isinstance(stamp, str) and "T" in stamp else _text(stamp),
            "gap": _guard(lambda: _deploy_gap(e, root), {"state": "unknown"}),
        })
    return {"state": "ok" if products else "empty", "products": products}


_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _supervisor(root: Path) -> dict[str, Any]:
    """Startup version and how many master merge commits have landed since it started."""
    state, record, _ = _read_json(root, "control", "data", "supervisor-state.json")
    empty = {"state": state, "started_at": None, "loaded_commit": None, "current_commit": None,
             "behind_merges": None}
    if state != "ok":
        return empty
    if not isinstance(record, dict):
        return {**empty, "state": "unknown_schema"}
    loaded = record.get("master_commit")
    loaded = loaded.strip() if isinstance(loaded, str) and _COMMIT.fullmatch(loaded.strip()) else None
    started = _time(record.get("started_at"))
    try:
        git = _control("review_packet").git
        current = git(root, "rev-parse", "master")
        current = current.strip() if isinstance(current, str) and _COMMIT.fullmatch(current.strip()) else None
        merges = git(root, "rev-list", "--merges", "--count", f"{loaded}..master") if loaded and current else None
        merges = merges.strip() if isinstance(merges, str) else ""
        behind = int(merges) if merges.isdigit() else None
    except Exception:
        current, behind = None, None
    return {"state": "ok" if current and behind is not None else "unknown",
            "started_at": _iso(started), "loaded_commit": loaded, "current_commit": current,
            "behind_merges": behind}


def _machine(root: Path, now: datetime) -> dict:
    """Sustained machine anomalies from the 10-minute sampler (control/machine_watch.py); the org board shows one line."""
    return _control("machine_watch").current(root / "data" / "machine-health.jsonl", now)


def _project_delivery(root: Path, now: datetime) -> dict:
    """Cached git inventory: dashboard polling never scans or mutates project repositories."""
    state, data, _ = _read_json(root, "control", "data", "project-delivery.json")
    if state != "ok":
        return {"state": state, "projects": []}
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("projects"), list):
        return {"state": "invalid", "projects": []}
    at = _time(data.get("generated_at_utc"))
    if at is None or at > now + timedelta(minutes=5):
        return {"state": "invalid", "projects": []}
    projects = []
    for p in data["projects"]:
        if not isinstance(p, dict):
            return {"state": "invalid", "projects": []}
        days, count = _number(p.get("days_since_delivery")), _number(p.get("approved_unmerged"))
        projects.append({"key": _text(p.get("key")), "name": _text(p.get("name")),
                         "last_delivery": _text(p.get("last_delivery")), "delivery_source": _text(p.get("delivery_source")),
                         "days_since_delivery": days, "approved_unmerged": count,
                         "delivery_state": _text(p.get("delivery_state")), "age_exceeded": days is not None and days > 7,
                         "pause_new_drafts": p.get("pause_new_drafts") is True,
                         "issues": [_text(s) for s in p.get("issues", []) if isinstance(s, str)]})
    return {"state": "ok", "generated_at_utc": _iso(at), "stale": (now - at).total_seconds() > 9 * 3600,
            "projects": projects}


def _timed_rows(root: Path, now: datetime) -> dict:
    state, text, _ = _read_text(root, "projects", "backlog.md", limit=BACKLOG_LIMIT)
    module = _tick().timed_rows
    missed = module.missed(text or "", root / "control" / "data" / module.STATE_FILE, now)
    return {"state": state, "missed": [{"message": r["message"], "section": _text(r["section"]),
                                        "item": _text(r["row"][9:]),
                                        "window_date": r["window_date"]} for r in missed]}


def build_snapshot(root: Path, now: datetime | None = None) -> dict[str, Any]:
    """Read-only view of organization state under the incore root."""
    root = Path(root)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    plans = _guard(lambda: _plans(root), {"state": "error", "roles": []})
    return {
        "schema_version": 2,
        "generated_at_utc": _iso(now),
        "stale_after_seconds": STALE_SECONDS,
        "usage": _guard(lambda: _usage(root, now), {"state": "error", "latest": None, "window": None, "history": [],
                                                    "pause_pct": PAUSE_PCT, "ceiling_pct": CEILING_PCT}),
        "deputies": _guard(lambda: _deputies(root, now),
                           {"state": "error", "roles": [{"key": key, "enabled": False} for key in ROLES]}),
        "plans": plans,
        "timed_rows": _guard(lambda: _timed_rows(root, now), {"state": "error", "missed": []}),
        "project_delivery": _guard(lambda: _project_delivery(root, now), {"state": "error", "projects": []}),
        "machine": _guard(lambda: _machine(root, now), {"state": "error", "anomalies": []}),
        "agents": _guard(lambda: _agents(root, now, plans), {"state": "error", "roles": []}),
        "queue": _guard(lambda: _queue(root, now), {"state": "error", "counts": {}, "open": [], "answered": []}),
        "deployments": _guard(lambda: _deployments(root), {"state": "error", "products": []}),
        # no supervisor record and no tick-log: nothing registered the scheduler on this instance
        "scheduler_registered": _guard(lambda: any(root.joinpath("control", "data", n).exists()
                                                   for n in ("supervisor-state.json", "tick-log.jsonl")), True),
        "supervisor": _guard(lambda: _supervisor(root), {"state": "error", "started_at": None,
                                                            "loaded_commit": None, "current_commit": None,
                                                            "behind_merges": None}),
        "services": [_guard(lambda: _service(root, key, parts), {"key": key, "state": "unknown"})
                     for key, parts in SERVICES],
    }
