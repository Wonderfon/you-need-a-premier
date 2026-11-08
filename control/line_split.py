"""Work-line split, the one-off migration (owner\x2010-26/10-27; plan audits/2026-10-26-work-line-split-plan.md sections 4-6 and 8.3).

  python control\\line_split.py            dry run: say what would move, change nothing
  python control\\line_split.py apply      convert backlog.md and deputies.json (backup first), write control\\data\\line-split.json
  python control\\line_split.py rollback   undo it from line-split.json

Needs the dispatcher stopped: control\\data\\tick.PAUSE exists and no supervisor, owner, report, ask, work/uvox deputy or
work/uvox/coordinator-input lane lock is live; otherwise it refuses and changes nothing.

backlog.md: `## 工作线` becomes `## Arbitros 线` (weight 36 -> 24); open rows of Uvox, Beacon Listener, Field Observer and Kiki
(and the free-text notes that start with those names) move to a new `## Beacon Listener 线` (weight 12); open 输入台/语音 rows
move to the end of `## 组织线`. done / done-ref / excluded rows and every other row stay where they were, text untouched.
deputies.json: the work deputy's executors uvox, beaconlistener, fieldlistener and kiki move to a new role `uvox`, which starts as a
`fork_from` entry (tick.run_resident forks the work deputy's session on its first run, once); the active `input` executor
becomes the coordinator's (KEEP_INPUT_FROM), the other one is set aside under `migrated`, never dispatched."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backlog_io  # noqa: E402
import line_map  # noqa: E402
import tick  # noqa: E402

# ---- CONFIG ----
OLD, ARBITROS, LISTENER, ORG = line_map.LEGACY_SECTION, line_map.ARBITROS, line_map.LISTENER, line_map.ORG
SHARE = (24, 36)  # Arbitros' part of the old weight: owner\x2010-26 "拆线：照建议" (36 -> 24 + 12); any other old weight is split 2:1
LISTENER_PROJECTS = ("uvox", "beaconlistener", "fieldlistener", "kiki")  # executors that move to role uvox
# The two input executors: work's (created 10-08, 17 runs, last run 10-27) is the one the input rows have been going to; the
# coordinator's (2 runs, last 10-25) is older and almost empty. The plan (6.1) kept the coordinator's; its snapshot showed 10 runs
# against 2, and a session that has done the voice-input work since 10-08 is worth more than a clean one. "coordinator" restores the plan's choice.
KEEP_INPUT_FROM = "work"
MANIFEST = tick.DATA / "line-split.json"
# ----------------

_ROW = re.compile(r"^- \[(.+?)\] (.+)$")
_HEADER_TEXT = re.compile(r"^(权重|冲刺|空闲补行暂停到)[：:]")
_RECORDS = ("records\x2f2026-10-26-owner-discord-plane-answers.json、records\x2f2026-10-26-owner-discord-split-ok-postprocess-prompt.json")


class SplitError(RuntimeError):
    pass


def _head(line: str) -> str:
    return line.rstrip("\r")[3:].strip() if line.startswith("## ") else ""


def _section(lines: list[str], title: str) -> tuple[int, int] | None:
    heads = [i for i, x in enumerate(lines) if x.startswith("## ")]
    for n, i in enumerate(heads):
        if _head(lines[i]) == title:
            return i, heads[n + 1] if n + 1 < len(heads) else len(lines)
    return None


def _blocks(lines: list[str]) -> list[tuple[str, list[str]]]:
    """'row' = a `- [` line with the indented lines under it, 'blank', or 'text' (any other line), in order."""
    out, i = [], 0
    while i < len(lines):
        if lines[i].startswith("- ["):
            j = i + 1
            while j < len(lines) and lines[j][:1] in (" ", "\t") and lines[j].strip():
                j += 1
            out.append(("row", lines[i:j]))
            i = j
        else:
            out.append(("text" if lines[i].strip() else "blank", [lines[i]]))
            i += 1
    return out


def _destination(kind: str, first: str) -> str | None:
    """Section an open row (or a free-text note naming a moving project) goes to, None = stays."""
    plain = first.rstrip("\r")
    if kind == "row":
        m = _ROW.match(plain)
        if not m or re.split(r"[\s:：]", m.group(1))[0].startswith(("done", "excluded")):
            return None
        project = line_map.project_of_item(m.group(2))
        return LISTENER if project in LISTENER_PROJECTS else ORG if project == "input" else None
    if kind == "text" and not _HEADER_TEXT.match(plain):
        return LISTENER if line_map.project_of_item(plain) in LISTENER_PROJECTS else None
    return None


def _num(x: float) -> str:
    return f"{x:g}"


def _weights(line: str) -> tuple[float, float, float]:
    m = tick.WEIGHT_RE.match(line.rstrip("\r"))
    if not m:
        raise SplitError(f"no 权重 line at the top of the section: {line[:60]}")
    old = float(m.group(1))
    arbitros = round(old * SHARE[0] / SHARE[1], 2)
    return old, arbitros, round(old - arbitros, 2)


def convert_backlog(lines: list[str]) -> tuple[list[str], dict]:
    """(new lines, record) of backlog.md split as the module docstring says. Raises SplitError if `## 工作线` is missing, a
    `## Arbitros 线` already exists, or `## 组织线` is missing. `lines` are split on \\n and keep their \\r."""
    if _section(lines, ARBITROS) or _section(lines, LISTENER):
        raise SplitError("backlog.md already has an Arbitros 线 or Beacon Listener 线 section")
    old, org = _section(lines, OLD), _section(lines, ORG)
    if not old or not org:
        raise SplitError("backlog.md needs both ## 工作线 and ## 组织线")
    cr = "\r" if any(x.endswith("\r") for x in lines) else ""
    a, b = old
    kept: list[str] = []
    moved: dict[str, list[str]] = {LISTENER: [], ORG: []}
    moves = []
    for kind, block in _blocks(lines[a + 1:b]):
        dest = _destination(kind, block[0])
        if dest is None:
            kept += block
            continue
        moved[dest] += block
        if kind == "row":
            moves.append({"to": dest, "head": block[0].rstrip("\r")[:80], "sha1": hashlib.sha1(block[0].rstrip("\r").encode("utf-8")).hexdigest()[:12],
                          "project": line_map.project_of_item(_ROW.match(block[0].rstrip("\r")).group(2))})
    trailing = []
    while kept and not kept[-1].strip():
        trailing.insert(0, kept.pop())
    w = next((i for i, x in enumerate(kept[:4]) if tick.WEIGHT_RE.match(x.rstrip("\r"))), None)
    if w is None:
        raise SplitError("## 工作线 has no 权重 line in its first rows")
    before = kept[w]
    total, arbitros, listener = _weights(before)
    rest = before.rstrip("\r")[tick.WEIGHT_RE.match(before.rstrip("\r")).end():]
    note = (f"【owner\x2010-26 Discord「拆线：照建议」（records\x2f2026-10-26-owner-discord-split-ok-postprocess-prompt.json）：原工作线 {_num(total)}% "
            f"拆成本线 {_num(arbitros)}% 和 Beacon Listener 线 {_num(listener)}%；Kiki 和两个 Listener 网站归 Beacon Listener 线"
            f"（records\x2f2026-10-26-owner-discord-plane-answers.json）；总理 10-27 用 control/line_split.py 实施】")
    kept[w] = f"权重：{_num(arbitros)}%（{note}" + rest[1:] + cr if rest.startswith("（") else f"权重：{_num(arbitros)}%（{note}）" + rest + cr
    weight_listener = (f"权重：{_num(listener)}%（owner\x2010-26 Discord「Kiki归beacon listener线」「拆线：照建议」（{_RECORDS}）：原工作线 {_num(total)}% "
                       f"拆成 Arbitros 线 {_num(arbitros)}% 和本线 {_num(listener)}%；本线管 Uvox、Beacon Listener 官网、Field Observer 和 Kiki，"
                       f"各项目原有的暂停、优先级和 owner 已定的边界照旧；总理 10-27 用 control/line_split.py 实施）" + cr)
    new_section = [f"## {LISTENER}" + cr, weight_listener, *moved[LISTENER]]
    out = lines[:a] + [f"## {ARBITROS}" + cr] + kept + [cr] + new_section + (trailing or [cr]) + lines[b:]
    o = _section(out, ORG)
    end = o[1]
    while end > o[0] + 1 and not out[end - 1].strip():
        end -= 1
    out[end:end] = moved[ORG]
    record = {"weight_before": before.rstrip("\r"), "weight_after": kept[w].rstrip("\r"), "weights": [total, arbitros, listener],
              "moves": moves, "rows_moved": {s: sum(1 for m in moves if m["to"] == s) for s in (LISTENER, ORG)}}
    return out, record


def reverse_backlog(lines: list[str], record: dict) -> list[str]:
    """Undo convert_backlog: Beacon Listener 线 (everything in it except its 权重 line, rows added since too) goes to the end of
    the work section, the open rows that moved to 组织线 and are still there go back too, the headings and the weight (the sum) are restored."""
    ar, li = _section(lines, ARBITROS), _section(lines, LISTENER)
    if not ar or not li or not _section(lines, ORG):
        raise SplitError("backlog.md has no converted sections to undo")
    cr = "\r" if any(x.endswith("\r") for x in lines) else ""
    listener_rows = [x for x in lines[li[0] + 1:li[1]]]
    wl = next((i for i, x in enumerate(listener_rows[:4]) if tick.WEIGHT_RE.match(x.rstrip("\r"))), None)
    listener_weight = float(tick.WEIGHT_RE.match(listener_rows[wl].rstrip("\r")).group(1)) if wl is not None else 0.0
    back = [x for i, x in enumerate(listener_rows) if i != wl]
    while back and not back[-1].strip():
        back.pop()
    without = lines[:li[0]] + lines[li[1]:]
    org = _section(without, ORG)
    came_back = set(m["head"] for m in record.get("moves", []) if m["to"] == ORG)
    taken: list[str] = []
    kept_org = []
    for kind, block in _blocks(without[org[0] + 1:org[1]]):
        if kind == "row" and block[0].rstrip("\r")[:80] in came_back:
            taken += block
        else:
            kept_org += block
    without = without[:org[0] + 1] + kept_org + without[org[1]:]
    ar = _section(without, ARBITROS)
    body = without[ar[0] + 1:ar[1]]
    trailing = []
    while body and not body[-1].strip():
        trailing.insert(0, body.pop())
    w = next((i for i, x in enumerate(body[:4]) if tick.WEIGHT_RE.match(x.rstrip("\r"))), None)
    if w is not None:
        if body[w].rstrip("\r") == record.get("weight_after"):
            body[w] = record["weight_before"] + cr
        else:
            m = tick.WEIGHT_RE.match(body[w].rstrip("\r"))
            body[w] = f"权重：{_num(float(m.group(1)) + listener_weight)}%" + body[w].rstrip("\r")[m.end():] + cr
    return without[:ar[0]] + [f"## {OLD}" + cr] + body + back + taken + (trailing or [cr]) + without[ar[1]:]


def migrate_deputies(deputies: dict, now: str) -> tuple[dict, dict]:
    """(new deputies, record). Refuses (SplitError) if work has no session to fork, uvox exists already, or a unit that would
    move is not idle."""
    d = copy.deepcopy(deputies)
    work = d.get("work") if isinstance(d.get("work"), dict) else None
    if not work or not work.get("session_id"):
        raise SplitError("deputies.json has no work session to fork the Beacon Listener deputy from")
    if "uvox" in d:
        raise SplitError("deputies.json already has a uvox entry")
    if work.get("status") == "running" or work.get("current"):
        raise SplitError("the work deputy is running")
    coordinator = d.setdefault("coordinator", {"session_id": None, "created_at": None, "status": "idle", "current": None,
                                               "last": None, "runs": 0})
    work_ex, coord_ex = work.setdefault("executors", {}), coordinator.setdefault("executors", {})
    for who, ex in [*(("work", work_ex.get(p)) for p in (*LISTENER_PROJECTS, "input")), ("coordinator", coord_ex.get("input"))]:
        if isinstance(ex, dict) and (ex.get("current") or (ex.get("started_at") and not ex.get("finished_at"))):
            raise SplitError(f"{who} executor {ex.get('project')} has an unfinished run")
    moved = {p: work_ex.pop(p) for p in LISTENER_PROJECTS if p in work_ex}
    record = {"at": now, "source_session": work["session_id"], "source_context_tokens": work.get("context_tokens"),
              "moved_executors": {p: {"session_id": e.get("session_id"), "runs": e.get("runs")} for p, e in moved.items()},
              "input": {"moved": False, "kept_from": KEEP_INPUT_FROM, "work_session": (work_ex.get("input") or {}).get("session_id"),
                        "coordinator_session": (coord_ex.get("input") or {}).get("session_id")}}
    d["uvox"] = {"session_id": None, "created_at": None, "status": "idle", "current": None, "last": None, "runs": 0,
                 "fork_from": work["session_id"], "fork_state": "pending", "fork_tries": 0, "executors": moved,
                 "migrated_at": now}
    if isinstance(work_ex.get("input"), dict):  # only one of the two stays dispatchable; the other is kept under `migrated`
        record["input"]["moved"] = True
        if KEEP_INPUT_FROM == "work":
            aside = coord_ex.pop("input", None)
            coord_ex["input"] = work_ex.pop("input")
            if aside:
                coordinator.setdefault("migrated", {})["input_set_aside"] = {"executor": aside, "at": now, "reason": "10-27 split: work's input executor kept"}
        else:
            work.setdefault("migrated", {})["input_set_aside"] = {"executor": work_ex.pop("input"), "at": now,
                                                                   "reason": "10-27 split: coordinator's input executor kept"}
    return d, record


def reverse_deputies(deputies: dict, record: dict) -> dict:
    """Undo migrate_deputies: executors go back to work, the input executors to where they were. The uvox entry stays (a
    forked session id and its history are kept) but can no longer be forked from or dispatched."""
    d = copy.deepcopy(deputies)
    uvox, work, coordinator = d.get("uvox"), d.get("work"), d.get("coordinator")
    if not isinstance(uvox, dict) or not isinstance(work, dict):
        raise SplitError("deputies.json has no uvox entry to undo")
    for p in record.get("moved_executors", {}):
        if p in (uvox.get("executors") or {}):
            work.setdefault("executors", {})[p] = uvox["executors"].pop(p)
    inp = record.get("input", {})
    if inp.get("moved") and inp.get("kept_from") == "work" and isinstance(coordinator, dict):
        kept = (coordinator.get("executors") or {}).pop("input", None)
        aside = (coordinator.get("migrated") or {}).pop("input_set_aside", None)
        if kept:
            work.setdefault("executors", {})["input"] = kept
        if aside:
            coordinator.setdefault("executors", {})["input"] = aside["executor"]
        if not coordinator.get("migrated"):
            coordinator.pop("migrated", None)
    elif inp.get("moved"):
        aside = (work.get("migrated") or {}).pop("input_set_aside", None)
        if aside:
            work.setdefault("executors", {})["input"] = aside["executor"]
        if not work.get("migrated"):
            work.pop("migrated", None)
    uvox.update(fork_from=None, fork_state="rolled_back", rolled_back_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return d


def busy_units() -> list[str]:
    """Live locks that forbid a migration: dispatch must be paused and the units involved idle."""
    live = [name for name, path in (("supervisor", tick.LOCK_FILE), ("owner", tick.OWNER_LOCK), ("report", tick.REPORT_LOCK),
                                    ("ask", tick.ASK_LOCK), ("deputy work", tick.deputy_lock("work")), ("deputy uvox", tick.deputy_lock("uvox")))
            if tick.lock_live(path)]
    for p in tick.DATA.glob("lane-*.lock"):
        if (p.stem.split("-")[1] in ("work", "uvox") or p.stem == "lane-coordinator-input") and tick.lock_live(p):
            live.append(p.stem)
    return live


def _preconditions() -> None:
    if not tick.PAUSE_FILE.exists():
        raise SplitError(f"create {tick.PAUSE_FILE} first (the supervisor stops within about 30 s)")
    busy = busy_units()
    if busy:
        raise SplitError("still running: " + ", ".join(busy))


def plan() -> str:
    """What `apply` would do, from the live files; nothing is written."""
    lines = backlog_io._read(tick.BACKLOG)
    _, rec = convert_backlog(lines)
    _, drec = migrate_deputies(tick.load_deputies(), "dry-run")
    by = {}
    for m in rec["moves"]:
        by[(m["to"], m["project"])] = by.get((m["to"], m["project"]), 0) + 1
    rows = "\n".join(f"  -> {to}: {n} x {project}" for (to, project), n in sorted(by.items()))
    return (f"weights {rec['weights'][0]:g} -> Arbitros {rec['weights'][1]:g} + Beacon Listener {rec['weights'][2]:g}\nrows moved:\n{rows}\n"
            f"executors to uvox: {', '.join(drec['moved_executors']) or '-'}\ninput executor kept from {drec['input']['kept_from']} "
            f"(work {drec['input']['work_session']}, coordinator {drec['input']['coordinator_session']}); moved: {drec['input']['moved']}\n"
            f"fork source (work): {drec['source_session']}")


def apply() -> dict:
    _preconditions()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = tick.DATA / f"line-split-backup-{stamp}"
    with backlog_io.lock(tick.BACKLOG), backlog_io.lock(tick.DEPUTIES_FILE):
        lines = backlog_io._read(tick.BACKLOG)
        new_lines, brec = convert_backlog(lines)
        new_deputies, drec = migrate_deputies(tick.load_deputies(), now)
        backup.mkdir(parents=True)
        shutil.copy2(tick.BACKLOG, backup / "backlog.md")
        shutil.copy2(tick.DEPUTIES_FILE, backup / "deputies.json")
        try:
            backlog_io._write(tick.BACKLOG, new_lines)
            tick.save_deputies(new_deputies)
        except Exception:
            shutil.copy2(backup / "backlog.md", tick.BACKLOG)
            shutil.copy2(backup / "deputies.json", tick.DEPUTIES_FILE)
            raise
    record = {"at": now, "backup": str(backup), "backlog": brec, "deputies": drec}
    MANIFEST.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def rollback() -> None:
    _preconditions()
    record = json.loads(MANIFEST.read_text(encoding="utf-8"))
    with backlog_io.lock(tick.BACKLOG), backlog_io.lock(tick.DEPUTIES_FILE):
        new_lines = reverse_backlog(backlog_io._read(tick.BACKLOG), record["backlog"])
        new_deputies = reverse_deputies(tick.load_deputies(), record["deputies"])
        backlog_io._write(tick.BACKLOG, new_lines)
        tick.save_deputies(new_deputies)
    MANIFEST.rename(MANIFEST.with_name(f"line-split.rolled-back-{datetime.now():%Y%m%d-%H%M%S}.json"))


def main(argv: list[str]) -> int:
    try:
        if argv == []:
            print(plan())
        elif argv == ["apply"]:
            rec = apply()
            print(f"done; backup {rec['backup']}; rows moved {rec['backlog']['rows_moved']}; fork source {rec['deputies']['source_session']}")
        elif argv == ["rollback"]:
            rollback()
            print("rolled back")
        else:
            print(__doc__)
            return 2
    except SplitError as exc:
        print(f"refused: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
