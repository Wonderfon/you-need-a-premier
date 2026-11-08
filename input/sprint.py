"""「冲刺一页状态」页的数据（owner\x2011-03 终端「我每天都问进度」）。只读，每次现读，不另存一份。

来源：projects/backlog.md（行、状态、各线小节开头的「冲刺：」行）、control/data/runs.jsonl（执行和审查的账本）、
control/data/backlog-history（任务表每次写入前的副本，推「进入当前状态多久了」用）。路径一律从 `root` 往下拿，
不读 control/tick.py 里写死的生产路径常量，所以测试和隔离端口只要把 root 指到临时目录就碰不到主目录。

分线、冲刺项目、行属于哪个项目照 control/tick.py 的口径：SECTION_ROLES、SPRINT_RE（小节前 4 行内）、project_of、
LINE_RE，以及 backlog_io.is_owner_heading。不直接调 tick.parse_backlog：它会把「格式不对的行」写进生产 control/data，
页面读一次就写文件，违背只读。「真卡住」复用 tick.stuck_rows，显式传入本页解析的冲刺项目和 root 下的数据路径，
跳过会写诊断的解析器；与原有「等了 6 小时以上」（API 的 stuck）分别计数。

一行的「现在这个状态多久了」按可信度依次推：
  1. 账本：待审 = 该行最近一次执行结束；待做 = 最近一次审查判返工；卡住 = 最近一次审查判 blocked。
  2. 进行中 = 行首状态括号里写的 `MM-DD HH:Mx` 时间。
  3. 任务表副本：从新到旧找第一份这一行不是当前状态的副本，那次写入就是进入时间；一直找到最老的副本都还是，只能说「至少」。
  都推不出来就写「不知道」，不当 0。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import dashboard as d
from . import pending, review

STUCK_HOURS = 6
HEAD_CHARS = 60
GOAL_CHARS = 200
KEY_CHARS = 80  # a row's identity across the ledger and the history copies: its text from the start, 返工 prefix off
RUNS_LIMIT = 256 * 1024 * 1024
SNAPSHOT_LIMIT = 64 * 1024 * 1024
SNAPSHOT_KEEP = 90
STATES = ("running", "review", "todo", "blocked")
LABELS = {"running": "进行中", "review": "等审查", "todo": "待做", "blocked": "卡住／等别的"}
_STATUS = re.compile(r"^(todo|running|review|blocked|done)(?![\w-])")
_STAMP = re.compile(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d)([\dx])")
_DONE_DAY = re.compile(r"^done\s+(\d{1,2})-(\d{1,2})")
_GOAL = re.compile(r"版本目标[：:]\s*(.*)$", re.S)
_LEAD_NOTE = re.compile(r"^（[^（）]*(?:（[^（）]*）[^（）]*)*）")
_FILE = re.compile(r"audits[\\/][\w.\-/\\]+?\.(?:md|html|json|txt|csv|png|jpe?g|webm|mp4|pdf|svg)(?![\w])", re.I)
_SNAP_NAME = re.compile(r"backlog-(\d{8}T\d{6})\.(\d{6})Z\.md")
_SNAP_ROW = re.compile(r"^- \[(todo|running|review|blocked)(?![\w-]).*?\] (?:（返工）)?\s*([^\r\n]{1,%d})" % KEY_CHARS, re.M)


def _key(body: str) -> str:
    return body.removeprefix("（返工）").lstrip()[:KEY_CHARS]


# ---- the backlog ----

def _sections(text: str, tick: Any) -> dict[str, dict[str, Any]]:
    """section title -> {role, sprint (project key or None), sprint_text, rows}. Line sections only, owner section left out."""
    out: dict[str, dict[str, Any]] = {}
    cur, since = None, 99
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        if line.startswith("## "):
            title = line[3:].strip()
            owner = tick.backlog_io.is_owner_heading(line)
            cur = None if owner or title not in tick.SECTION_ROLES else out.setdefault(
                title, {"role": tick.SECTION_ROLES[title], "sprint": None, "sprint_text": None, "rows": []})
            since = 0
            continue
        since += 1
        if cur is None:
            continue
        sprint = tick.SPRINT_RE.match(line)
        if sprint and since <= 4:
            project = tick.project_of(sprint.group(1))
            cur["sprint"], cur["sprint_text"] = (None if project == "general" else project), sprint.group(1).strip()
            continue
        match = tick.LINE_RE.match(line)
        status = _STATUS.match(match.group(1).strip()) if match else None
        if status:
            cur["rows"].append({"n": n, "status": status.group(1), "raw": match.group(1).strip(), "body": match.group(2)})
    return out


def _goal(sprint_text: str) -> dict[str, Any]:
    """Project and version goal of a 冲刺： line. A leading （where the goal came from …） note is left out of the short
    text when something is left after it; the full line stays available."""
    found = _GOAL.search(sprint_text)
    goal = (found.group(1) if found else sprint_text).strip()
    plain = _LEAD_NOTE.sub("", goal, count=1).strip()
    shown = plain if len(plain) >= 20 else goal
    return {"project": (sprint_text[:found.start()] if found else "").strip(" ，,"), "short": shown[:GOAL_CHARS],
            "cut": len(shown) > GOAL_CHARS, "full": d._text(sprint_text, 4000)}


# ---- the ledger ----

_LEDGER: dict[str, dict[str, Any]] = {}


def _ledger(root: Path) -> dict[str, dict[str, Any]] | None:
    """key -> latest executor / review runs of that row, from runs.jsonl; only the bytes added since the last read are parsed.
    None when the file is missing, unreadable or too big (every age then says 不知道 unless another source has it)."""
    path = root / "control" / "data" / "runs.jsonl"
    try:
        size = path.stat().st_size
        if path.is_symlink() or size > RUNS_LIMIT:
            return None
        state = _LEDGER.get(str(path))
        if state is None or size < state["offset"]:
            state = _LEDGER[str(path)] = {"offset": 0, "rows": {}}
        with path.open("rb") as stream:
            stream.seek(state["offset"])
            chunk = stream.read(size - state["offset"])
    except OSError:
        return None
    end = chunk.rfind(b"\n") + 1  # a half-written last line waits for the next read
    state["offset"] += end
    rows = state["rows"]
    for line in chunk[:end].decode("utf-8", "replace").splitlines():
        try:
            run = json.loads(line)
        except ValueError:
            continue
        kind, item, at = (run.get("kind"), run.get("item"), d._time(run.get("at"))) if isinstance(run, dict) else (None, None, None)
        if kind not in ("executor", "review", "shadow_review") or not isinstance(item, str) or not at:
            continue
        slot = rows.setdefault(_key(item), {})
        if kind == "executor":
            if "exec" not in slot or at >= slot["exec"]["at"]:
                slot["exec"] = {"at": at, "ok": run.get("ok") is not False and run.get("exit_code") in (0, None), "code": run.get("exit_code")}
            continue
        verdict = run.get("verdict") if isinstance(run.get("verdict"), str) else None
        if "review" not in slot or at >= slot["review"]["at"]:
            slot["review"] = {"at": at, "verdict": verdict, "file": run.get("audit_file") if isinstance(run.get("audit_file"), str) else None}
        if kind == "review" and verdict in ("rework", "blocked") and at >= slot.get(verdict, at):
            slot[verdict] = at
    return rows


# ---- the history copies ----

_SNAP_MAPS: dict[str, tuple[tuple[int, int], dict[str, frozenset[str]]]] = {}


def _snapshots(root: Path) -> list[tuple[datetime, Path, bool]]:
    """(time of the write the copy was taken before, path, from the contiguous recent set) newest first. The first copy of each
    day (daily/) reaches back further, with gaps; a daily copy that duplicates a recent one is dropped."""
    folder = root / "control" / "data" / "backlog-history"
    recent: list[tuple[datetime, Path, bool]] = []
    daily: list[tuple[datetime, Path, bool]] = []
    try:
        for path in folder.glob("backlog-*.md"):
            found = _SNAP_NAME.fullmatch(path.name)
            if found:
                when = datetime.strptime(found[1], "%Y%m%dT%H%M%S").replace(microsecond=int(found[2]), tzinfo=timezone.utc)
                recent.append((when, path, True))
        for path in (folder / "daily").glob("backlog-????????.md"):
            daily.append((datetime.fromtimestamp(path.stat().st_mtime, timezone.utc), path, False))
    except OSError:
        pass
    recent.sort(reverse=True)
    recent = recent[:SNAPSHOT_KEEP]
    daily = [x for x in daily if all(abs((x[0] - r[0]).total_seconds()) > 5 for r in recent)]
    return sorted(recent + daily, reverse=True)


def _snapshot_map(path: Path) -> dict[str, frozenset[str]] | None:
    """key -> statuses the row has in that copy (not-done rows only), cached per file."""
    try:
        stat = path.stat()
        if stat.st_size > SNAPSHOT_LIMIT:
            return None
        sig = (stat.st_size, stat.st_mtime_ns)
        hit = _SNAP_MAPS.get(str(path))
        if hit and hit[0] == sig:
            return hit[1]
        found: dict[str, set[str]] = {}
        for status, key in _SNAP_ROW.findall(path.read_text(encoding="utf-8", errors="replace")):
            found.setdefault(key, set()).add(status)
    except OSError:
        return None
    if len(_SNAP_MAPS) > 400:
        _SNAP_MAPS.clear()
    _SNAP_MAPS[str(path)] = (sig, {k: frozenset(v) for k, v in found.items()})
    return _SNAP_MAPS[str(path)][1]


def _from_snapshots(wanted: set[tuple[str, str]], root: Path) -> dict[tuple[str, str], tuple[datetime, bool]]:
    """{(key, status): (since, exact)}. The newest copy in which the row is not in that status ends the search: the write that
    followed it made the status current. Copies are taken before every write, so that is exact when the next newer copy is the
    live file or sits right beside it in the recent set; across a gap (daily copies) only the newer copy's time is safe, and an
    `exact` of False means 「至少这么久」. A row that is in that status in every copy has been there at least since the oldest."""
    out: dict[tuple[str, str], tuple[datetime, bool]] = {}
    if not wanted:
        return out
    snaps = _snapshots(root)
    open_: set[tuple[str, str]] = set(wanted)
    for i, (when, path, recent) in enumerate(snaps):
        found = _snapshot_map(path)
        if found is None:
            continue
        for item in [k for k in open_ if k[1] not in found.get(k[0], ())]:
            open_.discard(item)
            newer = snaps[i - 1] if i else None
            out[item] = (when, True) if newer is None or (recent and newer[2]) else (newer[0], False)
        if not open_:
            break
    if open_ and snaps:
        for item in open_:
            out[item] = (snaps[-1][0], False)
    return out


# ---- one row ----

def _duration(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {rest} 分钟" if rest else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


def _local(when: datetime | None) -> str | None:
    return when.astimezone().strftime("%m-%d %H:%M") if when else None


def _stamp(raw: str, now: datetime) -> datetime | None:
    """`running 11-03 18:4x …`: the time written in the status bracket, local; an x in the minutes counts as 0."""
    found = _STAMP.search(raw)
    if not found:
        return None
    month, day, hour, ten, one = int(found[1]), int(found[2]), int(found[3]), int(found[4]), found[5]
    local_now = now.astimezone()
    for year in (local_now.year, local_now.year - 1):
        try:
            when = datetime(year, month, day, hour, ten * 10 + (0 if one == "x" else int(one))).astimezone()
        except ValueError:
            return None
        if when <= local_now + timedelta(days=1):  # the minute digit x counts as 0, which can land a few minutes ahead
            return min(when, local_now).astimezone(timezone.utc)
    return None


def _files(root: Path, *texts: str, first: str | None = None) -> list[dict[str, Any]]:
    """audits/ files named in the texts (a file the ledger names first), with a link when the input page can serve it."""
    names = ([first] if first else []) + [m.group(0) for t in texts for m in _FILE.finditer(t)]
    out, seen = [], set()
    for name in names:
        rel = name.replace("\\", "/")
        rel = rel.split("audits/", 1)[1] if rel.lower().startswith("audits/") else rel
        if not rel or rel in seen:
            continue
        seen.add(rel)
        out.append({"name": "audits/" + rel, "href": f"/review/a/{review._u(rel)}" if review.audit_path(root, rel) else None})
    return out[:3]


def _row(root: Path, tick: Any, sprint: str, item: dict[str, Any], ledger: dict | None, now: datetime) -> dict[str, Any]:
    status, raw, body = item["status"], item["raw"], item["body"]
    slot = (ledger or {}).get(_key(body), {})
    since, exact, source = None, True, None
    if status == "review" and slot.get("exec") and slot["exec"]["ok"]:
        since, source = slot["exec"]["at"], "账本：最近一次执行结束"
    elif status == "todo" and slot.get("rework"):
        since, source = slot["rework"], "账本：最近一次审查判返工"
    elif status == "blocked" and slot.get("blocked"):
        since, source = slot["blocked"], "账本：最近一次审查判卡住"
    elif status == "running":
        since = _stamp(raw, now)
        source = "行里写的时间" if since else None
        exact = bool(since) and _STAMP.search(raw)[5] != "x"
    plain = body.removeprefix("（返工）").lstrip()  # the project prefix is the block title already; 返工 stays visible
    text = ("（返工）" if body.startswith("（返工）") else "") + (re.sub(r"^[^：:]{1,20}[：:]\s*", "", plain) if plain.startswith(tuple(tick.PROJECTS[sprint][1])) else plain)
    owner = bool(status == "blocked" and pending._WAIT_OWNER.match(raw))
    review_run, exec_run = slot.get("review"), slot.get("exec")
    return {
        "line": item["n"], "status": status, "status_label": LABELS[status], "owner": owner,
        "head": d._text(text, HEAD_CHARS), "detail": d._text(re.sub(r"^\w+[:：]?\s*", "", raw), 120) or None,
        "full": d._text(body, 800), "key": _key(body),
        "_since": since, "_exact": exact, "_source": source,
        "last_exec": None if not exec_run else {"at": _local(exec_run["at"]), "ok": exec_run["ok"], "code": exec_run["code"]},
        "last_review": None if not review_run else {"at": _local(review_run["at"]), "verdict": review_run["verdict"]},
        "files": _files(root, raw, first=(review_run or {}).get("file")),
        "ledger_known": ledger is not None, "ledger_has_row": bool(slot),
    }


def _age(row: dict[str, Any], now: datetime) -> None:
    since, exact = row.pop("_since"), row.pop("_exact")
    source = row.pop("_source")
    if since is None or since > now + timedelta(minutes=5):
        row.update(since=None, age_seconds=None, age_text="不知道", age_exact=False, age_source=None, stuck=False)
        return
    delta = max(timedelta(0), now - since)
    row.update(since=_local(since), age_seconds=int(delta.total_seconds()), age_exact=exact, age_source=source,
               age_text=("" if exact else "至少 ") + _duration(delta))
    row["stuck"] = delta >= timedelta(hours=STUCK_HOURS) and (row["status"] != "blocked" or row["owner"])


def _done(root: Path, rows: list[dict[str, Any]], tick: Any, sprint: str, today: date) -> list[dict[str, Any]]:
    found = []
    for item in rows:
        if item["status"] != "done" or tick.project_of(item["body"]) != sprint:
            continue
        stamp = _DONE_DAY.match(item["raw"])
        when = None
        if stamp:
            try:
                when = date(today.year, int(stamp[1]), int(stamp[2]))
                when = when if when <= today else when.replace(year=today.year - 1)
            except ValueError:
                when = None
        found.append((when or date.min, item["n"], when, item))
    found.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [{"line": item["n"], "head": d._text(item["body"], HEAD_CHARS + 20), "day": when.strftime("%m-%d") if when else None,
             "detail": d._text(re.sub(r"^done\s*", "", item["raw"]), 120) or None,
             "files": _files(root, item["raw"], item["body"])}
            for _, _, when, item in found[:3]]


# ---- the page ----

def snapshot(root: Path, now: datetime | None = None) -> dict[str, Any]:
    root = Path(root)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tick = d._tick()
    state, text, _ = d._read_text(root, "projects", "backlog.md", limit=d.BACKLOG_LIMIT)
    out: dict[str, Any] = {"state": state, "generated_at_utc": d._iso(now), "stuck_hours": STUCK_HOURS,
                           "lines": [], "owner": [], "stuck_total": 0, "alarm_total": 0, "unknown_total": 0}
    if state != "ok":
        return out
    sections = _sections(text, tick)
    alarms = {(r["section"], r["status"], r["item"]): r for r in tick.stuck_rows(
        text, now, sprints={s: sec["sprint"] for s, sec in sections.items() if sec["sprint"]},
        history=root / "control" / "data" / "backlog-history", runs_file=root / "control" / "data" / "runs.jsonl")}
    ledger = _ledger(root)
    out["ledger"] = "ok" if ledger is not None else "unreadable"
    order = [t for role in tick.LINE_SECTIONS for t, s in sections.items() if s["role"] == role]
    order += [t for t in sections if t not in order]
    lines, wanted = [], set()
    for title in order:
        sec = sections[title]
        entry: dict[str, Any] = {"section": title, "role": sec["role"], "sprint": sec["sprint"],
                                 "goal": _goal(sec["sprint_text"]) if sec["sprint_text"] else None, "groups": {s: [] for s in STATES},
                                 "done": [], "owner": [], "next": None, "stuck": 0, "alarm_rows": [], "unknown": 0}
        if sec["sprint"]:
            for item in sec["rows"]:
                if item["status"] in STATES and tick.project_of(item["body"]) == sec["sprint"]:
                    row = _row(root, tick, sec["sprint"], item, ledger, now)
                    alarm = alarms.get((title, item["raw"], item["body"]))
                    row["alarm"] = {"kind": alarm["kind"], "age_text": "至少 " + _duration(timedelta(hours=alarm["hours"]))} if alarm else None
                    if alarm:
                        entry["alarm_rows"].append(row)
                    (entry["owner"] if row["owner"] else entry["groups"][row["status"]]).append(row)
            entry["done"] = _done(root, sec["rows"], tick, sec["sprint"], now.astimezone().date())
            wanted |= {(r["key"], r["status"]) for r in entry["owner"] + [r for g in entry["groups"].values() for r in g] if r["_since"] is None}
        lines.append(entry)
    snaps = _from_snapshots(wanted, root)
    for entry in lines:
        everything = entry["owner"] + [r for s in STATES for r in entry["groups"][s]]
        for row in everything:
            if row["_since"] is None and (row["key"], row["status"]) in snaps:
                row["_since"], row["_exact"] = snaps[(row["key"], row["status"])]
                row["_source"] = "任务表副本" if row["_exact"] else "任务表副本（只留到更早的那份）"
            _age(row, now)
            row.pop("key")
            if row["stuck"]:
                entry["stuck"] += 1
            elif row["age_seconds"] is None and (row["status"] != "blocked" or row["owner"]):
                entry["unknown"] += 1
        first = next((r for s in ("review", "todo") for r in entry["groups"][s]), None)
        running = entry["groups"]["running"]
        entry["next"] = {"head": first["head"], "status_label": first["status_label"], "line": first["line"],
                         "age_text": first["age_text"]} if first else None
        entry["working"] = {"head": running[0]["head"], "age_text": running[0]["age_text"]} if running else None
        entry["counts"] = {s: len(entry["groups"][s]) for s in STATES}
        out["owner"] += [dict(r, section=entry["section"]) for r in entry["owner"]]
        out["stuck_total"] += entry["stuck"]
        out["alarm_total"] += len(entry["alarm_rows"])
        out["unknown_total"] += entry["unknown"]
    out["lines"] = lines
    return out
