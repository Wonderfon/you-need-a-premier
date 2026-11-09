"""Numbers for the coordinator's cross-line review, computed instead of re-derived by the model each time (owner\x2010-05,
程序化降本 2/4): per-line and per-engine runs, cost and first-pass rate, every Codex run with its verdict, what the
scheduler decided per line, the fair-share table (as tick.choose_lane scores it), every project with an open row and
the hours since its last executor run, the last 24 h of `malformed` backlog rows, and blocked / review rows
older than STUCK_H. Reads runs.jsonl, tick-log.jsonl, usage.jsonl and backlog.md; writes control/data/review-stats.md.

Run directly (`python review_stats.py`) to print the table. Set AS_OF to replay an earlier moment: rows after it are
ignored, so a number the coordinator once counted by hand can be checked."""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

# ---- CONFIG ----
WINDOW_H = 72  # the tables cover runs that ended in this many hours before AS_OF
STUCK_H = 24  # a [blocked] or [review] row older than this is listed
STUCK_MAX = 15  # longest first; the rest is only counted
AS_OF = None  # e.g. "2026-10-05T10:00:00+00:00"; None = now
DATA = Path(ic.V["workspace"] + r"\incore\control\data")
BACKLOG = Path(ic.V["workspace"] + r"\incore\projects\backlog.md")
OWNER_DATA = BACKLOG.parent.parent / "data"  # owner-todo-ids.json: the card numbers the 要你的 page shows
OUT = DATA / "review-stats.md"
SEEN = DATA / "review-stats-seen.json"  # first time each blocked/review row was seen here: backlog.md has no timestamps
# ----------------

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_burn  # noqa: E402  (the 「Codex 燃烧」 section)
import records_index  # noqa: E402  (literal owner-word candidates, never approval decisions)
import review_packet  # noqa: E402  (read-only git helpers and branch/path patterns)
import backlog_history
import backlog_io  # noqa: E402  (reuse the owner-card evidence rule without clearing anything)
import timed_rows  # noqa: E402  (same read-only missed-window view as the dashboard)
import pause_projects  # noqa: E402  (read owner-pause summaries; never change dispatch)
import kpi  # noqa: E402  (review_join: the executor run -> verdict of its next review)
import line_map  # noqa: E402  (a row of the old 工作线 is read as the line it belongs to now)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "input"))
import pending  # noqa: E402  (the one count of 等你决定 rows)

VERDICTS = ("pass", "rework", "blocked")
LINE_RE = re.compile(r"^- \[(.+?)\] (.+)$")
WAIT_OWNER_RE = re.compile(r"blocked[:：]\s*等\s*(你|owner)", re.I)  # only these [blocked] rows want something from the owner
DECISIONS = ("spawn", "codex_spawn", "ran", "skipped", "capped", "hard_ceiling", "deputy_busy")


def load_jsonl(path: Path, key: str, as_of: datetime) -> list[dict]:
    """Rows with a parsed `_at` (from `key`) not after as_of, in time order."""
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for number, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        at = kpi._when(row.get(key)) if isinstance(row, dict) else None
        if at and at <= as_of:
            rows.append({**row, "_at": at, "_source": f"{path}:{number}"})
    return sorted(rows, key=lambda r: r["_at"])


def engine(row: dict) -> str:
    return f"codex {row.get('model')}" if row.get("engine") == "codex" else f"claude {row.get('model')}"


def line_of(row: dict) -> str:
    return row.get("section") or row.get("role") or "?"


def tally(rows: list[dict], joined: dict[int, str], all_rows: list[dict]) -> dict:
    """Executor runs among `rows` (a subset of all_rows) with their verdicts."""
    idx = {id(r): i for i, r in enumerate(all_rows)}
    ex = [r for r in rows if r.get("kind") == "executor"]
    seen = [joined.get(idx[id(r)]) for r in ex]
    counts = {v: seen.count(v) for v in VERDICTS}
    reviewed = sum(counts.values())
    return {"n": len(ex), **counts, "unreviewed": len(ex) - reviewed,
            "rate": f"{counts['pass'] / reviewed:.0%}" if reviewed else "-"}


def cost(rows: list[dict]) -> float:
    return sum(r.get("cost_usd") or 0 for r in rows)


def table(head: list[str], body: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in body]
    return "\n".join(out) + "\n"


def stamp(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%m-%d %H:%MZ")


def open_rows(text: str) -> list[tuple[str, str, str]]:
    """(section, status word, item) for every `- [status] item` row of backlog.md."""
    out, section = [], "?"
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        m = LINE_RE.match(line)
        if m:
            out.append((section, re.split(r"[\s:：]", m.group(1), maxsplit=1)[0], m.group(2)))
    return out


def row_key(status: str, item: str) -> str:
    return f"{status}|{item[:160]}"


def idle_section(runs: list[dict], tick_log: list[dict], usage: list[dict], secs: dict, backlog: str, tk,
                 now: datetime, window_h: float) -> str:
    """Per line (owner\x2010-15 "不要停"): hours of the window with no run of the line in progress (a run spans
    [end - elapsed_s, end]; any overlap with the hour counts as running), how many of those hours the 5-hour window's usage
    was below the pacing line (the latest usage sample at the hour's end against tk.baseline), the line's open todo / review
    rows and [running] rows now, the idle stretch since its last run when it has none of them, and how often the cross-line
    starvation rule started it (tick-log `cross_starved`). backlog.md keeps no history, so an hour with an empty queue
    cannot be told from one waiting on a gate: both count as idle."""
    ends = [now - timedelta(hours=k) for k in range(int(window_h) - 1, -1, -1)]
    samples = [u for u in usage if isinstance(u.get("utilization"), (int, float)) and isinstance(u.get("resets_at"), (int, float))]

    def under_pace(end: datetime) -> bool:
        s = next((u for u in reversed(samples) if u["_at"] <= end), None)
        return s is not None and s["utilization"] * 100 < tk.baseline(end.timestamp(), s["resets_at"])

    paced = {end: under_pace(end) for end in ends}
    running_now = {s for s, status, _ in open_rows(backlog) if status == "running"}
    body = []
    for section, sec in secs.items():
        if not tk.SECTION_ROLES.get(section) or sec["weight"] <= 0:
            continue
        mine = [r for r in runs if line_of(r) == section and r.get("kind") in tk.COST_KINDS]
        spans = [(r["_at"] - timedelta(seconds=r.get("elapsed_s") or 0), r["_at"]) for r in mine]
        idle = [end for end in ends if not any(a < end and b > end - timedelta(hours=1) for a, b in spans)]
        todo, review = len(sec["todo"]), len(sec.get("review_rows", []))
        stretch = "有活或在跑"
        if not (todo or review or section in running_now):
            last = max((b for _, b in spans), default=None)
            stretch = f"{(now - last).total_seconds() / 3600:.1f} 小时" if last else f"≥{window_h:g} 小时"
        crossed = sum(1 for e in tick_log if e["_at"] > now - timedelta(hours=window_h) and e.get("cross_starved") == section
                      and e.get("decision") in ("spawn", "codex_spawn"))
        body.append([section, todo, review, "是" if section in running_now else "否", len(idle),
                     sum(1 for end in idle if paced[end]), stretch, crossed])
    return (f"## 空转（最近 {window_h:g} 小时，按整点算；没在跑 = 这一小时里没有该线的运行在进行，"
            "不区分「队列空」和「有活但被闸门挡住」，backlog 没有历史）\n\n"
            + table(["线", "现在 todo", "现在待审", "现在在跑", "没在跑的小时", "其中用量低于节奏线", "没活时已空转", "跨线防饿派出次数"], body))


def render(runs: list[dict], tick_log: list[dict], usage: list[dict], backlog: str, tk,
           now: datetime, window_h: float = None, seen: dict | None = None) -> str:
    """`tk`: the tick module (passed in, not imported: tick imports this module); its cost_7d, parse_backlog, COST_FLOOR,
    SECTION_ROLES, last_executor_runs and starve_key give the same numbers choose_lane and next_unit use."""
    window_h = WINDOW_H if window_h is None else window_h
    since = now - timedelta(hours=window_h)
    joined = kpi.review_join(runs)
    win = [r for r in runs if r["_at"] > since]
    head = ["次数", "花费", "pass", "rework", "blocked", "未审", "一次通过率"]
    out = [f"# 复盘统计（{stamp(now)} 之前 {window_h:g} 小时；脚本生成，账本里的 cost_usd 求和，Codex 走订阅记 0）\n",
           f"一次通过率 = pass / 已有审查结论的执行次数；未审 = 这次执行之后还没有对同一行的审查。执行次数只数 kind=executor。\n"]

    out.append("## 各线\n")
    body = []
    for sec in sorted({line_of(r) for r in win}):
        rows = [r for r in win if line_of(r) == sec]
        t = tally(rows, joined, runs)
        reviews = [r for r in rows if r.get("kind") == "review"]
        body.append([sec, len(rows), f"${cost(rows):.2f}", t["n"], len(reviews), f"${cost(reviews):.2f}",
                     t["pass"], t["rework"], t["blocked"], t["unreviewed"], t["rate"]])
    out.append(table(["线", "全部次数", "花费", "执行", "审查", "审查花费", "pass", "rework", "blocked", "未审", "一次通过率"], body))

    secs = tk.parse_backlog(backlog)
    spent = tk.cost_7d(runs, now)
    shares = sorted(((spent.get(s, 0.0) + tk.COST_FLOOR) / sec["weight"], s, sec) for s, sec in secs.items()
                    if tk.SECTION_ROLES.get(s) and sec["weight"] > 0)
    out.append(f"## 份额表（{tk.RUN_DAYS} 天，只算 {'/'.join(tk.COST_KINDS)}；分数 =（花费 + {tk.COST_FLOOR:g}）/ 权重，最低的先派）\n")
    out.append(table(["线", "权重", "花费", "分数"], [[s, f"{sec['weight']:g}%", f"${spent.get(s, 0.0):.2f}", f"{score:.3f}"]
                                                for score, s, sec in shares]))
    out.append("## 各引擎和模型\n")
    body = []
    for eng in sorted({engine(r) for r in win}):
        rows = [r for r in win if engine(r) == eng]
        t = tally(rows, joined, runs)
        body.append([eng, len(rows), f"${cost(rows):.2f}", t["n"], t["pass"], t["rework"], t["blocked"], t["unreviewed"], t["rate"]])
    out.append(table(["引擎 模型", "全部次数", "花费", "执行", "pass", "rework", "blocked", "未审", "一次通过率"], body))

    out.append("## 花费按类型\n")
    kinds = sorted({r.get("kind") for r in win}, key=lambda k: -cost([r for r in win if r.get("kind") == k]))
    out.append(table(["类型", "次数", "花费", "单次"], [
        [k, len(g), f"${cost(g):.2f}", f"${cost(g) / len(g):.2f}"] for k in kinds
        for g in [[r for r in win if r.get("kind") == k]]]))

    codex = [r for r in win if r.get("engine") == "codex" and r.get("kind") not in ("shadow_review", "review")]
    out.append(f"## Codex 每次执行（窗口内 {len(codex)} 次）\n")
    idx = {id(r): i for i, r in enumerate(runs)}
    out.append(table(["时间", "模型", "线", "项目", "任务", "审查结论"], [
        [stamp(r["_at"]), r.get("model"), line_of(r), r.get("project"), (r.get("item") or "")[:36].replace("|", "/"),
         joined.get(idx[id(r)]) or "未审"] for r in codex]))

    sections = {r.get("role"): line_of(r) for r in runs if r.get("role")}
    out.append("## 调度决定（tick-log）\n")
    per: dict[str, dict] = {}
    for e in (x for x in tick_log if x["_at"] > since and x.get("decision") in DECISIONS):
        role = e.get("role") or (e.get("lane") or "?").split(":")[0]
        d = per.setdefault(sections.get(role, "全局" if role == "?" else role), dict.fromkeys(DECISIONS, 0))
        d[e["decision"]] += 1
    out.append(table(["线", *DECISIONS], [[k, *v.values()] for k, v in sorted(per.items())]))

    paused = pause_projects.paused_projects(backlog)
    paused_rows = {(s, m[2]) for s, line in sections_lines(backlog)
                   if (m := LINE_RE.fullmatch(line)) and pause_projects.is_paused(m[1])}
    waiting = []
    for section, sec in secs.items():
        role = tk.SECTION_ROLES.get(section)
        last, queued = tk.last_executor_runs(runs, role), {}
        for status, rows in (("todo", sec["todo"]), ("review", sec["review_rows"])):
            for _, item in rows:
                if item.removeprefix("（返工）").lstrip().startswith(tuple(paused.get(section, ()))) or (section, item) in paused_rows:
                    continue
                queued.setdefault(tk.starve_key(item), []).append(status)
        waiting += [((now - last[k]).total_seconds() / 3600 if k in last else float("inf"), section, k, states, last.get(k))
                    for k, states in queued.items()] if role else []
    waiting.sort(key=lambda w: -w[0])
    out.append(f"## 有待办或待审的项目，离上次执行（{len(waiting)} 个，久的在前）\n")
    out.append(table(["线", "项目", "未处理的行", "已等", "上次执行"], [
        [s, k.replace("|", "/"), f"todo {st.count('todo')} / review {st.count('review')}",
         "-" if h == float("inf") else f"{h:.1f} 小时", "从没跑过" if at is None else stamp(at)] for h, s, k, st, at in waiting])
        if waiting else "无\n")
    out.append("## 暂停中\n")
    out.append(table(["线", "项目"], [[s, p] for s, projects in paused.items() for p in projects]) if paused else "无\n")

    bad = {}
    for e in (x for x in tick_log if x["_at"] > now - timedelta(hours=24) and x.get("decision") == "malformed"):
        bad[e.get("row")] = e  # one row, one line (the log writes it once per UTC day)
    out.append(f"## 最近 24 小时的 malformed 行（{len(bad)} 行）\n")
    out.append(table(["时间", "行首 60 字", "原因"], [[stamp(e["_at"]), (r or "").replace("|", "/"), (e.get("reason") or "").replace("|", "/")]
                                                  for r, e in bad.items()]) if bad else "无\n")
    dead = [r for r in tk.running_rows(backlog) if r["live"] is not True]
    out.append(f"## 没有在跑的 running 行（{len(dead)} 行；调度器启动时自动放回的不在这里，这里是认不出或还没到下次启动的，总理手改）\n")
    out.append(table(["线", "开始", "能自动放回", "任务"], [
        [r["section"] or "?", r["hhmm"], "是（下次调度器启动）" if r["live"] is False else "否，认不出是哪种运行", r["item"][:50].replace("|", "/")]
        for r in dead]) if dead else "无\n")
    stuck, unknown = [], 0
    for section, status, item in open_rows(backlog):
        if status not in ("blocked", "review"):
            continue
        if status == "blocked" and (section, item) in paused_rows:
            continue
        # age of a review row: its executor run's end; of a blocked row: the review that blocked it; else when this
        # script first saw the row (seen), which is only known from the first run on
        kinds_ = ("executor",) if status == "review" else ("review", "executor")
        stamps = [r["_at"] for r in runs if r.get("item") == item and r.get("kind") in kinds_]
        first = kpi._when((seen or {}).get(row_key(status, item)))
        since_ = max(stamps) if stamps else first
        if since_ is None:
            unknown += 1
        elif (now - since_).total_seconds() / 3600 >= STUCK_H:
            stuck.append(((now - since_).total_seconds() / 3600, section, status, "账本" if stamps else "首次见到", item))
    stuck.sort(reverse=True)
    out.append(f"## blocked 或 review 超过 {STUCK_H} 小时的行（{len(stuck)} 行）\n")
    out.append(table(["线", "状态", "已等", "时间来源", "任务"], [
        [s, st, f"{h:.0f} 小时", src, i[:50].replace("|", "/")] for h, s, st, src, i in stuck[:STUCK_MAX]]) if stuck else "无\n")
    if len(stuck) > STUCK_MAX:
        out.append(f"另有 {len(stuck) - STUCK_MAX} 行更短的，没列。\n")
    if unknown:
        out.append(f"另有 {unknown} 行还没有时间记录（账本里没有这一行；从 {SEEN.name} 首次记下的时间算起，满 {STUCK_H} 小时后进表）。\n")
    out.append(blocked_ready_section(review_snapshot(backlog, [], tk, now)["rows"], now.astimezone()))
    out.append(idle_section(runs, tick_log, usage, secs, backlog, tk, now, window_h))
    seen = [u for u in usage if u["_at"] > since and isinstance(u.get("utilization"), (int, float))]
    out.append("## 额度（usage.jsonl）\n")
    if seen:
        peak = max(seen, key=lambda u: u["utilization"])
        out.append(f"最近一次 {seen[-1]['utilization']:.0%}（{stamp(seen[-1]['_at'])}）；窗口内最高 {peak['utilization']:.0%}"
                   f"（{stamp(peak['_at'])}）；{len(seen)} 次采样。\n")
    else:
        out.append("窗口内没有采样。\n")
    return "\n".join(out)


def load_seen() -> dict:
    try:
        data = json.loads(SEEN.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def note_seen(backlog: str, now: datetime) -> None:
    """Record the first sighting of each blocked/review row and forget rows that are gone."""
    old = load_seen()
    keys = [row_key(status, item) for _, status, item in open_rows(backlog) if status in ("blocked", "review")]
    SEEN.write_text(json.dumps({k: old.get(k) or now.isoformat() for k in keys}, ensure_ascii=False, indent=0), encoding="utf-8")


def review_snapshot(backlog: str, tick_log: list[dict], tk, now: datetime, branches: list[dict] | None = None) -> dict:
    """Freeze current rows and archived done, without writes or scheduler decisions."""
    rows = list(backlog_history.text_rows(backlog, BACKLOG))
    history = list(backlog_history.archived_rows(BACKLOG))
    for row in rows + history:
        row.pop("raw", None)
    dispatch = {}
    names = {role: sec for sec, role in tk.SECTION_ROLES.items()}
    names.update(line_map.SECTION_OF_ROLE)  # SECTION_ROLES also accepts the old 工作线 alias
    for row in sorted(tick_log, key=lambda r: r["_at"]):
        if row["_at"] <= now and row.get("decision") in ("spawn", "codex_spawn"):
            lane = (row.get("lane") or row.get("role") or "?").split(":")
            role = line_map.MOVED.get(lane[1], lane[0]) if lane[0] == "work" and len(lane) > 1 else lane[0]
            dispatch[names.get(role, role)] = {"at": row["_at"].isoformat(), "source": row.get("_source", "tick-log（行号未知）")}
    return {"version": 1, "as_of": now.isoformat(), "rows": rows, "history_done": history, "dispatch": dispatch,
            "branches": branches, "backlog_source": str(BACKLOG)}


WAIT_DATE_RE = re.compile(r"(等(?:日期|到)?|定时[，,:：\s]*|截止[^\d；;。]*?)\s*"
                          r"((?:20\d\d-)?\d{2}-\d{2})(?![\d-])(?:\s+([\dxX]+:[\dxX]+)(Z)?)?")
WAIT_DONE_RE = re.compile(r"等[^「；;。]*「([^「」]+)」(?:那行|一行)?(?:返工)?(?:过审|完成)")
DOTS_TASK_RE = re.compile(r"T-\d{8}-\d+\b")


def blocked_ready_section(rows: list[dict], now: datetime) -> str:
    """Report hints from status brackets only; `now` is in the runtime's local timezone. Never release rows."""
    # ponytail: literal hints only; compound conditions remain the coordinator's manual check.
    done = [r for r in rows if r["status"] == "done"]
    body = []
    for row in rows:
        state = row["state"]
        if row["status"] != "blocked" or any(s in state for s in ("等你决定", "暂停本项目", "等总理")):
            continue
        hits, evidence = [], []
        for match in WAIT_DATE_RE.finditer(state):
            prefix, value, hhmm, utc = match.groups()
            clock = now.astimezone(timezone.utc) if utc else now
            date = value if len(value) == 10 else f"{clock.year}-{value}"
            try:
                when = datetime.strptime(f"{date} {hhmm or '00:00'}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if when > clock.replace(tzinfo=None):
                continue
            task = DOTS_TASK_RE.search(state) if prefix.startswith("截止") else None
            hits.append("dotz 截止已过" if task else "等待日期已到")
            evidence.append(f"{task[0] + '：' if task else ''}{date}{' ' + hhmm if hhmm else ''}"
                            f"{'Z' if utc else '（本地时间）'}{'（无年按本次年份）' if len(value) == 5 else ''}")
        for match in WAIT_DONE_RE.finditer(state):
            source = next((r for r in done if r["section"] == row["section"] and match[1] in r["item"]), None)
            if source:
                hits.append("同小节前置已 done")
                evidence.append(f"「{match[1]}」：backlog.md:{source['line']} [{source['state'][:40]}]")
        if hits:
            body.append([row["section"], row["item"][:60].replace("|", "/"), "、".join(dict.fromkeys(hits)),
                         "；".join(evidence).replace("|", "/"), f"backlog.md:{row['line']}"])
    return (f"## 等待条件可能已满足的 blocked 行（{len(body)} 行）\n\n"
            "只报不改，命中不代表其他条件也已满足。日期按运行环境本地时间，Z 为 UTC；无年按本次年份，纯日期从当天 00:00 算。\n\n"
            + (table(["小节", "行首 60 字", "命中哪类", "依据", "来源"], body) if body else "无\n"))


def load_snapshot(path: Path) -> dict | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if (obj["version"] == 1 and kpi._when(obj["as_of"]) and isinstance(obj["dispatch"], dict)
                and isinstance(obj["rows"], list) and isinstance(obj.get("backlog_source"), str)):
            for row in obj["rows"]:
                if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ("section", "item", "status", "state")):
                    return None
                if not isinstance(row.get("line"), int) or row["line"] < 1:
                    return None
            if any(not isinstance(v, dict) or not kpi._when(v.get("at")) for v in obj["dispatch"].values()):
                return None
            return obj
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def branch_facts(rows: list[dict], tk) -> list[dict]:
    """Branches cited by non-done rows, against the project's local default."""
    grouped, out, waiting = {}, [], set()
    for row in rows:
        waiting_merge = re.search(r"等.{0,10}合|待合并|未合并|过审未合", row["state"])
        if row["status"] not in ("blocked", "review", "todo", "running"):
            continue
        project = tk.project_of(row["item"])
        for branch in review_packet.BRANCH_RE.findall(row["state"] + " " + row["item"]):
            grouped.setdefault(project, set()).add(branch)
            if waiting_merge:
                waiting.add((project, branch))
    for project, branches in sorted(grouped.items()):
        repo = tk.PROJECTS.get(project, (None,))[0]
        default = review_packet.git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD") if repo else None
        default = default.strip().removeprefix("refs/remotes/") if default else None
        if default:
            local = default.split("/", 1)[-1]
            if review_packet.git(repo, "rev-parse", "--verify", f"refs/heads/{local}"):
                default = local  # local merges are deliberately not pushed; origin's ref can be behind
        if repo and not default:
            default = next((b for b in ("master", "main") if review_packet.git(repo, "rev-parse", "--verify", b)), None)
        pending = review_packet.git(repo, "for-each-ref", f"--no-merged={default}", "--format=%(refname:short)",
                                    "refs/heads/claude/") if repo and default else None
        existing = review_packet.git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/claude/") if repo else None
        for branch in sorted(branches):
            state = "未知（仓库、默认分支或 git 不可读）"
            if pending is not None and existing is not None:
                state = "仍未合并" if branch in pending.splitlines() else "已合并" if branch in existing.splitlines() else "分支未找到"
            out.append({"project": project, "branch": branch, "default": default, "state": state,
                        "waiting": (project, branch) in waiting})
    return out


RECHECK_RE = re.compile(r"复查(?:日|日期)?[：:\s]*((?:20\d\d-)?\d{2}-\d{2})|((?:20\d\d-)?\d{2}-\d{2})\s*复查")
REFERENCE_RE = re.compile(r"(?:audits|records|projects|control|input)[\\/][^\s`'\"，。；、）)<>]*?\.(?:md|html|json|py|txt)\b")


MAX_LINES, MAX_CHARS = 80, 8000  # the two new sections together; the rest goes to the detail json
DETAIL = lambda: OUT.with_suffix(".diff-detail.json")


def diff_section(current: dict, previous: dict | None, detail: dict | None = None) -> str:
    if current.get("error"):
        return f"## 自上次复盘以来的变化\n来源不可用：{current['error']}；不能判断变化。\n"
    now = kpi._when(current["as_of"])
    prior = kpi._when(previous["as_of"]) if previous else None
    valid = prior is not None and prior < now
    key = lambda r: (r["section"], r["item"])
    old = {key(r): r for r in previous["rows"] if r["status"] == "blocked"} if valid else {}
    rows = {key(r): r for r in reversed(current["rows"] + current.get("history_done", []))}
    blocked = {k: r for k, r in rows.items() if r["status"] == "blocked"}
    out = ["## 自上次复盘以来的变化", f"本次：{current['as_of']}；上次：{previous['as_of'] if previous else '缺失或无效'}。"]
    if not valid:
        out.append("基线缺失、无效或不早于本次：不能判断新增／解除和派活变化；本次只列当前事实。")

    def listing(title, entries, cap=5):
        entries = list(entries)
        if detail is not None:
            detail[title] = entries
        shown = "\n".join(entries[:cap]) if entries else "无"
        if len(entries) > cap:
            shown += f"\n另有 {len(entries) - cap} 条，明细见 {DETAIL()}"
        out.append(f"### {title}（{len(entries)}）\n" + shown)

    where = lambda source, line: f"{Path(source).name if Path(source).name == 'backlog.md' else source}:{line}"
    describe = lambda r: f"{r['section']} · {short(r['item'], 70)}（{where(r.get('source', current['backlog_source']), r['line'])}）"
    if valid:
        listing("新增 blocked", ("- " + describe(r) for k, r in blocked.items() if k not in old), 6)
        listing("已解除 blocked（移至其他状态；不等于事项办完）",
                (f"- {describe(rows[k])} → {rows[k]['status']}" for k in old if k in rows and k not in blocked), 6)
        listing("原 blocked 行消失或改文，需核对（不算已解除）",
                (f"- {r['section']} · {short(r['item'], 70)}（上次 {where(previous['backlog_source'], r['line'])}）"
                 for k, r in old.items() if k not in rows), 3)
        listing("blocked 条件变化", (f"- {describe(r)}：{short(old[k]['state'], 40)} → {short(r['state'], 40)}"
                                 for k, r in blocked.items() if k in old and old[k]["state"] != r["state"]), 3)
    due = []
    day = now.astimezone().date()
    for r in current["rows"]:
        if r["status"] not in ("blocked", "todo", "review", "running"):
            continue
        for match in RECHECK_RE.finditer(r["state"] + " " + r["item"]):
            value = match[1] or match[2]
            date = value if len(value) == 10 else f"{day.year}-{value}"
            try:
                when = datetime.strptime(date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if when <= day:
                due.append(f"- {date}{'（原文无年，按本次年份）' if len(value) == 5 else ''} · {describe(r)}")
    listing("已到复查日（只识别明确的复查日期，不把事实核对日当复查日）", due)
    if current["branches"] is None:
        out.append("### 引用分支\n历史分支快照不可用，未用今天的 git 状态代替。")
    else:
        branches = current["branches"]
        historical = sum(r["status"] == "done" and bool(re.search(r"等.{0,10}合|待合并|未合并|过审未合", r["state"]))
                         for r in current["rows"] + current.get("history_done", []))
        out.append(f"done 行里的旧等合并引用：{historical} 行（只计数，不查 git、不列待行动）。")
        listing("等合并且仍没合的分支（只列被「等合并／待合并」行引用的）",
                (f"- {r['project']} · {r['branch']} → {r['default'] or '?'}" for r in branches
                 if r.get("waiting") and r["state"] == "仍未合并"), 6)
        rest, unreadable = {}, {}
        for r in branches:
            if r.get("waiting") and r["state"] == "仍未合并":
                continue
            if r["state"].startswith("未知"):
                unreadable[r["project"]] = unreadable.get(r["project"], 0) + 1
            else:
                counts = rest.setdefault(r["project"], {})
                counts[r["state"]] = counts.get(r["state"], 0) + 1
        lines = [f"- {p}：" + "、".join(f"{s} {n}" for s, n in c.items()) for p, c in sorted(rest.items())]
        lines += [f"- {p}：仓库、默认分支或 git 不可读，{n} 条分支未判断" for p, n in sorted(unreadable.items())]
        if detail is not None:
            detail["其余分支按项目计数"] = lines
        out.append(f"### 其余引用分支按项目计数（{len(lines)} 个项目；已合并和未列的不逐条写）\n"
                   + ("\n".join(lines[:5]) if lines else "无")
                   + (f"\n另有 {len(lines) - 5} 个项目，明细见 {DETAIL()}" if len(lines) > 5 else ""))
    out.append("### 各线上次派活时间（spawn / codex_spawn；非执行结束时间）")
    dispatch_old = previous["dispatch"] if valid else {}
    out.append(table(["线", "上次快照中的派活", "本次快照中的派活", "变化", "本次来源"], [
        [sec, dispatch_old.get(sec, {}).get("at", "未知"), current["dispatch"].get(sec, {}).get("at", "未知"),
         "无法比较" if not valid else "有变化" if dispatch_old.get(sec, {}).get("at") != current["dispatch"].get(sec, {}).get("at") else "未变",
         where(*current["dispatch"][sec]["source"].rsplit(":", 1)) if sec in current["dispatch"] and ":" in current["dispatch"][sec]["source"] else current["dispatch"].get(sec, {}).get("source", "无记录")]
        for sec in sorted(set(dispatch_old) | set(current["dispatch"]))]))
    return "\n".join(out) + "\n"


def clip(text: str, max_lines: int, max_chars: int) -> str:
    """Hard bound for a section: keep the head, say where the rest is."""
    lines = text.splitlines()
    if len(lines) <= max_lines and len(text) <= max_chars:
        return text if text.endswith("\n") else text + "\n"
    kept, size = [], 0
    for line in lines:
        if len(kept) >= max_lines - 1 or size + len(line) + 1 > max_chars - 120:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept) + f"\n（已截断，其余 {len(lines) - len(kept)} 行见 {DETAIL()}）\n"


def owner_candidates(current: dict, tk, data: dict, detail: dict | None = None) -> str:
    """Only cited records or records naming this row's branch/result; never infer from generic task keywords."""
    until = kpi._when(current["as_of"]).astimezone().date().isoformat()
    norm = lambda s: s.replace("\\\\", "/").replace("\\", "/").casefold()
    results = []
    for row in current["rows"]:
        if row["status"] != "blocked" or "等你决定" not in row["state"]:
            continue
        text = row["state"] + " " + row["item"]
        refs = REFERENCE_RE.findall(text)
        cited = tuple(sorted({p for p in refs if PureWindowsPath(p).parts[0].casefold() == "records"}))
        hits = records_index.candidates(data, paths=cited, until=until, limit=2)["hits"] if cited else []
        basis = "路径" if hits else ""
        result_paths = tuple(match[0] for match in REFERENCE_RE.finditer(text)
                             if "audits" in (part.casefold() for part in PureWindowsPath(match[0]).parts)
                             and re.search(r"结果(?:文件|路径)?[：:\s`\"']*$", text[:match.start()]))
        if len(hits) < 2 and result_paths:
            for hit in records_index.candidates(data, paths=result_paths, until=until, limit=2)["hits"]:
                if not any(old["file"] == hit["file"] for old in hits):
                    hits.append(hit)
                    basis = "路径"
                    if len(hits) == 2:
                        break
        keys = [m.group(1) for m in re.finditer(r"(?i)(?<![\w])claude/([a-z0-9][a-z0-9._-]*)", text)]
        keys += [PureWindowsPath(p).stem for p in result_paths
                 if "audits" in (part.casefold() for part in PureWindowsPath(p).parts)
                 and PureWindowsPath(p).suffix.casefold() in {".md", ".html", ".json", ".txt"}]
        records = sorted(data["rows"], key=lambda r: (r["date"], r["file"]), reverse=True)
        for key in dict.fromkeys(norm(k) for k in keys if k):
            for record in records:
                hay = norm(record["search_text"] + " " + record["file"])
                if (record["fields"] and record["date"] <= until and key in hay
                        and not any(hit["file"] == record["file"] for hit in hits)):
                    hits.append(record)
                    if not basis:
                        basis = "同名关键词"
                    if len(hits) == 2:
                        break
            if len(hits) == 2:
                break
        results.append((row, basis, hits))
    found = [x for x in results if x[2]]
    missing = [x for x in results if not x[2]]
    if detail is not None:
        detail["原话候选"] = [{"line": row["line"], "item": row["item"][:200], "basis": basis,
                            "hits": [{"file": h["file"], "date": h["date"], "fields": h["fields"]} for h in hits]}
                           for row, basis, hits in results]
    out = [f"## records 原话候选（只查「等你决定」行；{len(results)} 行，{len(found)} 行有候选）",
           "只供回原文判断，不代表仍有效或已批准；只按日期命中的不列；未找到不表示 owner 没答过。"]
    for row, basis, hits in found[:6]:
        out.append(f"- {short(row['item'], 50)}（{basis}命中）")
        for hit in hits:
            f = hit["fields"][0]
            out.append(f"  - {hit['date']} · {hit['file']}:{f['line']}：{f['text'].replace(chr(10), ' ')[:60]}")
    if len(found) > 6:
        out.append(f"另有 {len(found) - 6} 行有候选，明细见 {DETAIL()}")
    if missing:
        out.append("未找到：" + "；".join(short(r["item"], 24) for r, _, _ in missing[:4])
                   + (f"；另有 {len(missing) - 4} 行" if len(missing) > 4 else ""))
    return "\n".join(out) + "\n"

def build(tk, now: datetime | None = None, window_h: float = None, *, facts: dict | None = None,
          backlog_text: str | None = None, previous_path: Path | None = None, records_dir: Path | None = None,
          detail: dict | None = None) -> str:
    as_of = kpi._when(facts["as_of"]) if facts is not None else now or (kpi._when(AS_OF) if AS_OF else datetime.now(timezone.utc))
    if backlog_text is None:
        try:
            backlog = BACKLOG.read_text(encoding="utf-8")
        except OSError as exc:
            backlog = ""
            backlog_error = type(exc).__name__
        else:
            backlog_error = None
    else:
        backlog = backlog_text
        backlog_error = None
    tick_log = load_jsonl(DATA / "tick-log.jsonl", "at", as_of)
    text = render(line_map.fold_all(load_jsonl(DATA / "runs.jsonl", "at", as_of)), tick_log,
                  load_jsonl(DATA / "usage.jsonl", "observed_at", as_of), backlog, tk, as_of, window_h,
                  load_seen())
    current = review_snapshot(backlog, tick_log, tk, as_of) if facts is None else facts
    if backlog_error:
        current["error"] = f"{BACKLOG}：{backlog_error}"
    if now is None and not AS_OF:
        current["branches"] = branch_facts(current["rows"], tk)
    head, _, body = text.partition("\n")
    detail = {} if detail is None else detail
    diff = clip(diff_section(current, load_snapshot(previous_path or OUT.with_suffix(".json")), detail), 56, 5600)
    text = head + "\n\n" + diff + "\n" + body
    records_dir = records_dir if records_dir is not None else BACKLOG.parent.parent / "records"
    text += "\n" + clip(owner_candidates(current, tk, records_index.catalog(records_dir, records_dir.parent / "data/records-index/index.jsonl"), detail), 24, 2400)
    if now is None and not AS_OF:  # the quota reading is live: not for a replay of an earlier moment
        try:
            text += "\n" + codex_burn.section(tk, as_of.astimezone())
        except Exception as exc:  # noqa: BLE001 - the stats are written without this block rather than not at all
            text += f"\n## Codex 燃烧\n\n这一段没生成：{exc!r:.120}\n"
    return text


def write(tk) -> str:
    """Regenerate OUT; the coordinator's prompt tells it to read this file first."""
    if AS_OF:
        raise ValueError("Use build(now=...) for read-only replay; replay must not advance the live baseline")
    now = datetime.now(timezone.utc)
    previous = load_snapshot(OUT.with_suffix(".json"))
    if previous and kpi._when(previous["as_of"]) >= now:
        raise ValueError("The baseline is not earlier than now; refusing to replace it")
    backlog = BACKLOG.read_text(encoding="utf-8")  # missing source must not become a false empty baseline
    current = review_snapshot(backlog, load_jsonl(DATA / "tick-log.jsonl", "at", now), tk, now)
    detail: dict = {}
    text = build(tk, facts=current, backlog_text=backlog, detail=detail)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    try:
        note_seen(backlog, now)
    except OSError:
        pass
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(OUT)
    snapshot = OUT.with_suffix(".json")
    tmp = snapshot.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(snapshot)
    tmp = DETAIL().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(DETAIL())
    return text


AUDIT_WRITE = re.compile(r"(?:>>?|Set-Content|Out-File|Add-Content|tee)\s+[^\n;|]*?audits[\\/]+([\w.\-]+\.md)", re.I)
PY_AUDIT_PATH = re.compile(r'''(?i)\b([A-Za-z_]\w*)\s*=\s*(?:Path|open)\(\s*(["'])([^"'\n]*audits[\\/][^"'\n]+?\.md)\2''')
LIST_MAX = 2  # examples per list; remaining items are counted to keep the daily block small
COORDINATOR_FACT_MAX = 9000
LINE_FACT_MAX = 6000


def cap_daily(lines: list[str], item_spans: list[tuple[int, int]], limit: int) -> str:
    """Replace trailing whole item blocks until the report fits; never split a rendered line."""
    text = "\n".join(lines) + "\n\n"
    if len(text) <= limit:
        return text
    removals = [sum(len(line) + 1 for line in lines[start:end]) for start, end in item_spans]
    removed_chars = 0
    for count, removed in enumerate(reversed(removals), 1):
        removed_chars += removed
        note = f"另 {count} 条（超出字数上限，未展开）"
        if len(text) - removed_chars + len(note) + 1 <= limit:
            omitted = item_spans[-count:]
            first = omitted[0][0]
            skipped = {i for start, end in omitted for i in range(start, end)}
            kept = []
            for i, line in enumerate(lines):
                if i == first:
                    kept.append(note)
                if i not in skipped:
                    kept.append(line)
            return "\n".join(kept) + "\n\n"

    # A pathological fixed block can also exceed the budget; retain complete chunks and report the omitted tail.
    note = f"另 {max(1, len(item_spans))} 条（超出字数上限，未展开）"
    kept = []
    for line in lines:
        candidate = "\n".join([*kept, line, note]) + "\n\n"
        if len(candidate) > limit:
            break
        kept.append(line)
    return "\n".join([*kept, note]) + "\n\n"


def written_audits(run_file, *, full_paths: bool = False) -> list[str]:
    """audits/*.md files an executor run wrote: Write/Edit tool calls and shell redirects in its stream (Claude and Codex)."""
    names = []
    try:
        lines = Path(run_file).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return names
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        content = (event.get("message") or {}).get("content") if event.get("type") == "assistant" else None
        calls = [b.get("input") or {} for b in (content if isinstance(content, list) else [])
                 if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in ("Write", "Edit", "PowerShell", "Bash")]
        calls += [event["item"]] if isinstance(event.get("item"), dict) else []
        for call in calls:
            path = str(call.get("file_path") or "").replace("\\", "/")
            names += [path if full_paths else path.rsplit("/", 1)[1]] if "/audits/" in path and path.endswith(".md") else []
            command = str(call.get("command") or "")
            for match in AUDIT_WRITE.finditer(command):
                refs = fact_paths(match[0]) if full_paths else []
                names.append(refs[-1] if refs else ("audits/" if full_paths else "") + match[1])
            for variable, _, path in PY_AUDIT_PATH.findall(command):
                if re.search(rf"\b{re.escape(variable)}\.(?:write_text|write_bytes|write)\s*\(", command):
                    names.append(path if full_paths else path.replace("\\", "/").rsplit("/", 1)[-1])
    return names


def sections_lines(text: str) -> list[tuple[str, str]]:
    """(section, line) for every non-empty line under a `## section` header."""
    out, section = [], "?"
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        elif line.strip():
            out.append((section, line.strip()))
    return out


def short(item: str, n: int = 70) -> str:
    return (item if len(item) <= n else item[:n] + "…").replace("|", "/")


def fact_title(item: str) -> str:
    """Keep the task title; full references belong in the single selected path line below it."""
    for path in fact_paths(item):
        item = item.replace("'" + path + "'", " ").replace('"' + path + '"', " ").replace(path, " ")
    return short(re.sub(r"\s+", " ", item).strip(" /,;:，；"))


FACT_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/mnt/[a-z]/)[^\s`'\"，。；、）)<>]+|"
                       r"(?:audits|wt|records|projects|control|input)[\\/][^\s`'\"，。；、）)<>]+?"
                       r"\.(?:md|html|json|py|txt|png|jpg|pdf|mp4|csv)\b", re.I)


def fact_paths(text: str) -> list[str]:
    # Quoted absolute paths may contain spaces. Keep the complete path, never join by basename.
    quoted = re.compile(r"(['\"])([A-Za-z]:[\\/][^'\"\n]+)\1")
    paths = [m[2] for m in quoted.finditer(text)]
    text = quoted.sub("", text)
    paths += FACT_PATH.findall(text)
    return list(dict.fromkeys(p.rstrip(".,;:，。；）)") for p in paths
                             if PureWindowsPath(p.rstrip(".,;:，。；）)")).suffix.lower() in
                             {".md", ".html", ".json", ".py", ".txt", ".png", ".jpg", ".pdf", ".mp4", ".csv"}))


def is_record_path(raw: str) -> bool:
    return "records" in (part.casefold() for part in PureWindowsPath(raw).parts)


def is_worktree_script(raw: str) -> bool:
    parts = tuple(part.casefold() for part in PureWindowsPath(raw).parts)
    return ".py" == PureWindowsPath(raw).suffix.casefold() and "wt" in parts


def is_audit_path(raw: str) -> bool:
    return "audits" in (part.casefold() for part in PureWindowsPath(raw).parts)


def is_review_path(raw: str) -> bool:
    return bool(re.search(r"-review.*\.md$", PureWindowsPath(raw).name, re.I))


def is_wildcard_path(raw: str) -> bool:
    return any(char in raw for char in "*?[]")


def windows_path(raw: str) -> str | None:
    raw = re.sub(r"\\+", r"\\", raw)
    mount = re.match(r"^/mnt/([a-z])/(.*)", raw, re.I)
    if mount:
        raw = mount[1].upper() + ":/" + mount[2]
    path = PureWindowsPath(raw)
    return str(path) if path.is_absolute() and ".." not in path.parts and "<" not in raw else None


def fact_file(raw: str, root: Path, windows_root: str | None = None) -> dict:
    """Resolve an exact reference for this host and for the owner's Windows terminal; never search for a basename."""
    base = windows_path(windows_root or str(root))
    win = windows_path(raw)
    local = Path(raw)
    if win:
        if base and PureWindowsPath(win).is_relative_to(PureWindowsPath(base)):
            local = root.joinpath(*PureWindowsPath(win).relative_to(PureWindowsPath(base)).parts)
        elif sys.platform != "win32":
            p = PureWindowsPath(win)
            local = Path("/mnt") / p.drive[0].lower() / Path(*p.parts[1:])
    elif not local.is_absolute() and not PureWindowsPath(raw).drive:
        parts = PureWindowsPath(raw).parts
        if ".." not in parts:
            local = root.joinpath(*parts) if parts[0] != "wt" else root.parent.parent.joinpath(*parts)
            win = str(PureWindowsPath(base).joinpath(*parts)) if base and parts[0] != "wt" else (
                str(PureWindowsPath(base).parent.parent.joinpath(*parts)) if base else None)
    exists = local.is_file()
    return {"path": win or raw, "local": str(local), "key": (win or str(local)).casefold(), "exists": exists,
            "command": "Start-Process '" + win.replace("'", "''") + "'" if win else None,
            "check": ("存在" if exists else "缺文件") + ("；Windows 路径不完整" if not win else "")}


def daily_context(root: Path, now: datetime) -> dict:
    """Read-only inputs, injectable for historical replay. Do not clear cards or record a report here."""
    try:
        reports = json.loads((root / "control/data/reports.json").read_text(encoding="utf-8"))
        if not isinstance(reports, dict):
            reports = None
    except (OSError, ValueError):
        reports = None
    data = records_index.catalog(root / "records")
    today = now.astimezone().date()
    cutoff = (today - timedelta(days=13)).isoformat()
    records, visible = [], []
    for r in data["rows"]:
        meta = r.get("meta") if isinstance(r.get("meta"), dict) else {}
        at = next((at for key in records_index.TIME_KEYS if (at := kpi._when(str(meta.get(key, ""))))), None)
        if r["date"] > today.isoformat() or at and at > now:
            continue
        visible.append(r)
        try:
            date = datetime.fromisoformat(r["date"]).date()
        except ValueError:
            continue
        if cutoff <= r["date"]:
            records.append((date, r["file"].split("/")[-1], r["search_text"].casefold()))
    data = {**data, "rows": visible}
    return {"records": data, "card_records": sorted(records, reverse=True), "reports": reports,
            "replies": pending.load(root / "data", pending.REPLIES_FILE)}


def item_replies(sec: str, item: str, replies: list[dict], now: datetime) -> list[tuple[str, str, str]]:
    out = []
    for i, reply in enumerate(replies):
        at = kpi._when(reply.get("at"))
        if at and at <= now and reply.get("section") == sec and reply.get("head") == item[:80]:
            out.append((at.isoformat(), f"data/owner-replies.json:records[{i}]",
                        "已回复；待执行者处理" if reply.get("status") == "new" else "回复已处理"))
    return out


def owner_card_state(row: dict, context: dict, now: datetime) -> str | None:
    """Reuse the clearing rule; returns waiting, handled, or None for a done row's owner card."""
    if not row or not row["status"].startswith("done"):
        return None
    note = row["state"].split("原状态")[0]
    card = backlog_io._OWNER_CARD.search(note)
    seen = re.search(r"owner\s*已(?:处理|答|看)", note)
    if not card and not seen:
        return None
    cited = backlog_io.cited_by(card, context["card_records"], now.astimezone().date()) if card else None
    replies = item_replies(row["section"], row["item"], context["replies"], now)
    if cited or seen or replies:
        return "handled"
    return "waiting"


def daily_item(sec: str, item: str, rows: list[dict], runs: list[dict], root: Path, now: datetime,
               since: datetime | None, context: dict, windows_root: str | None = None) -> list[str]:
    """Facts, with uncertainty retained: exact task/path joins only. A reply never implies completed work."""
    matches = [r for r in rows if r["section"] == sec and r["item"] == item]
    if not matches and item:
        matches = [r for r in rows if r["section"] == sec and item in r["item"]]
    if not matches and item.startswith("已发日报引用："):
        keys = {fact_file(p, root, windows_root)["key"] for p in fact_paths(item)}
        matches = [r for r in rows if any(fact_file(p, root, windows_root)["key"] in keys
                                         for p in fact_paths(r["state"] + " " + r["item"]))]
    current_matches = [r for r in matches if Path(r.get("source", root / "projects/backlog.md")).name == "backlog.md"]
    matches = current_matches or matches
    row = matches[0] if len(matches) == 1 else None
    text = row["state"] + " " + row["item"] if row and not item.startswith("已发日报引用：") else item
    state = row["state"] if row else "当前行歧义" if matches else "当前行未找到"
    related = [r for r in runs if line_of(r) == sec and r.get("item") == item]
    wrote = list(dict.fromkeys(n for r in related if r.get("kind") == "executor"
                              for n in context["written"].get(r.get("run_file"), [])))
    refs = fact_paths(text)
    wildcards = list(dict.fromkeys(p for p in refs if is_wildcard_path(p)
                                   and not is_record_path(p) and not is_worktree_script(p)))
    refs = [p for p in refs if not is_wildcard_path(p) and not is_record_path(p) and not is_worktree_script(p)]
    wrote = [p for p in wrote if not is_record_path(p) and not is_worktree_script(p)]
    card = backlog_io._OWNER_CARD.search(row["state"]) if row and row["status"].startswith("done") else None
    cited = backlog_io.cited_by(card, context["card_records"], now.astimezone().date()) if card else None
    all_refs = list(dict.fromkeys(wrote + refs))
    files = {f["key"]: f for p in all_refs if (f := fact_file(p, root, windows_root))}
    owner_refs = [p for p in fact_paths(card["text"]) if not is_record_path(p) and not is_worktree_script(p) and not is_wildcard_path(p)] if card else []
    written_results = [p for p in wrote if PureWindowsPath(p).suffix.casefold() == ".md"]
    row_audits = [p for p in refs if is_audit_path(p)]
    review_result = bool(row and row["status"] == "review" and written_results)
    candidates = list(dict.fromkeys((written_results + row_audits) if review_result
                                    else owner_refs + written_results + row_audits))
    unique, seen = [], set()
    for path in candidates:
        key = fact_file(path, root, windows_root)["key"]
        if key not in seen:
            unique.append(path)
            seen.add(key)
    candidates = unique
    non_review = [p for p in candidates if not is_review_path(p)]
    candidates = candidates[:1] if review_result else non_review or (candidates[:1] if len(candidates) == 1 else [])
    selected = fact_file(candidates[0], root, windows_root) if candidates else None
    visible_files = [files[fact_file(p, root, windows_root)["key"]] for p in all_refs
                     if fact_file(p, root, windows_root)["key"] in files]
    if cited:
        action = "已答卡／owner 已看（清卡判定；不表示办完）"
    elif card and "owner 已看" in state:
        action = "已完成；owner 已看"
    elif card:
        action = "待 owner " + card["text"].split("，", 1)[0]
    elif row and row["status"].startswith("done"):
        action = "已完成；" + ("owner 已看" if "owner 已看" in state else "未见新的 owner 待行动卡")
    elif "等你决定" in state:
        action = "待 owner 决定"
    elif WAIT_OWNER_RE.match(state):
        action = "待 owner 操作"
    else:
        action = {"review": "待审查员验收", "todo": "待执行", "running": "执行中", "blocked": "等前置条件"}.get(
            row["status"] if row else "", "未知，需核对当前行")
    replies = item_replies(sec, row["item"] if row else item, context["replies"], now)
    reply_paths = list(refs)
    base = windows_path(windows_root or str(root))
    if selected and base and PureWindowsPath(selected["path"]).is_relative_to(PureWindowsPath(base)):
        reply_paths.append(PureWindowsPath(selected["path"]).relative_to(PureWindowsPath(base)).as_posix())
    hits = records_index.candidates(context["records"], paths=tuple(reply_paths),
                                    until=now.astimezone().date().isoformat(), limit=2)["hits"] if reply_paths else []
    evidence = [f"{h['file']}:{h['fields'][-1]['line']}（{h['date']} 文件日；引用候选，不代表批准）" for h in hits]
    cited_records = {p.replace("\\", "/").casefold() for p in REFERENCE_RE.findall(text) if p.startswith("records")}
    inline = [r for r in context["records"]["rows"] if r["file"].casefold() in cited_records and r["fields"]]
    if not evidence and inline:
        h = max(inline, key=lambda r: (r["date"], r["file"]))
        evidence.append(f"{h['file']}:{h['fields'][-1]['line']}（行内出处；是否本项最新回复未核）")
    if replies:
        latest = max(replies)
        evidence.insert(0, latest[1] + "（" + latest[2] + "）")
        if action.startswith("待 owner"):
            action = latest[2]
    if cited and not any(f"records/{cited[1]}:" in e for e in evidence):
        evidence.insert(0, "records/" + cited[1] + "（清卡依据）")
    out = [f"  待行动：{action}" + (f"（{'backlog' if Path(row.get('source', 'backlog.md')).name == 'backlog.md' else row['source']}:{row['line']}）" if row else ""),
           "  最新回复出处：" + ("；".join(evidence) if evidence else "未找到精确关联的回复，不能当作未答")]
    if len(hits) > 1 and hits[0]["date"] == hits[1]["date"]:
        out.append("  回复顺序：同日多份出处，日内先后未知。")
    out.extend(f"  通配路径，不生成打开命令：{path}" for path in wildcards[:1])
    if selected is None:
        if not wildcards:
            out.append("  成果路径：有引用但没有可选成果文件；打开命令：不可生成。" if files else
                       "  成果路径：缺路径；打开命令：不可生成；没有可确认的新结果。")
    else:
        f = selected
        reported = context["reported"].get(f["key"], [])
        repeat = "本时段已报（内容更新未知）" if reported else (
            "本时段未报（精确路径未命中）" if context["reports"] is not None else "本时段是否已报未知")
        out.append(f"  成果／引用：{f['path']}（{f['check']}）；打开：{f['command'] or '不可生成'}；{repeat}")
        context["file_names"].setdefault(PureWindowsPath(f["path"]).name.casefold(), {})[f["key"]] = f["path"]
    extras = len(files) - (1 if selected else 0) + max(0, len(wildcards) - 1)
    if extras:
        out.append(f"  另 {extras} 个引用（未展开）。")
    if not wrote and (selected is not None or wildcards or files):
        out.append("  新结果：本时段未确认写入。")
    for f in visible_files:
        name = PureWindowsPath(f["path"]).name.casefold()
        context.setdefault("candidate_file_names", {}).setdefault(name, {})[f["key"]] = f["path"]
    return out


def daily(runs: list[dict], backlog: str, day, section: str | None = None, usage: list[dict] = (),
          since: datetime | None = None, codes: dict | None = None, *, root: Path | None = None, now: datetime | None = None,
          context: dict | None = None, windows_root: str | None = None) -> str:
    """Facts for one day's report (owner\x2010-05, 程序化降本 3/4), in the machine's local day: reviews that passed, sent back
    or blocked today, rows now waiting on the owner, the audits files today's executor runs wrote and the day's cost per
    line. `section` limits it to one line (the role's own report); None = all lines (the coordinator's summary). Rows
    waiting on the owner come from backlog.md as it is now, so replaying a past day shows today's waiting rows. `since` (naive local time) replaces the calendar day with "from `since` until now"
    (the twice-daily reports: 21:00 to 09:00 and 09:00 to 21:00)."""
    now = now or datetime.now(timezone.utc)
    root = root or BACKLOG.parent.parent
    context = dict(context if context is not None else daily_context(root, now), written={}, reported={},
                   file_names={}, candidate_file_names={}, reported_refs={}, reported_patterns=[])
    rows = list(backlog_history.all_rows(root / "projects/backlog.md", backlog))
    local = lambda at: at.astimezone().date()
    inwin = (lambda at: local(at) == day) if since is None else (lambda at: at.astimezone().replace(tzinfo=None) >= since)
    when = "今天" if since is None else "本时段"
    today = [r for r in runs if inwin(r["_at"]) and r["_at"] <= now]
    for r in today:
        if r.get("kind") == "executor" and (stream := r.get("run_file")) not in context["written"]:
            context["written"][stream] = written_audits(stream, full_paths=True)
    for date, slots in sorted((context["reports"] or {}).items(), reverse=True):
        for key, report in sorted(slots.items()) if isinstance(slots, dict) else ():
            if not key.startswith("text:") or slots.get(key[5:]) != "sent" or "@" not in key or not isinstance(report, str):
                continue
            try:
                at = datetime.fromisoformat(date + "T" + key.rsplit("@", 1)[1])
            except ValueError:
                continue
            if at > now.astimezone().replace(tzinfo=None) or not inwin(at.astimezone()):
                continue
            role = key[5:].split("@")[0]
            sec = line_map.SECTION_OF_ROLE.get(role, "组织线" if role == "coordinator" else "?")
            for p in fact_paths(report):
                if is_record_path(p) or is_worktree_script(p):
                    continue
                if is_wildcard_path(p):
                    context["reported_patterns"].append(p)
                    continue
                f = fact_file(p, root, windows_root)
                if section is None or sec == section:
                    context["reported_refs"].setdefault(f["key"], (sec, f["path"]))
                context["reported"].setdefault(f["key"], []).append(
                    f"control/data/reports.json:{date}/{key}")
    mine = lambda sec: section is None or sec == section
    out = [f"【今日数据（{day:%m-%d}" + ("" if since is None else f"，自 {since:%m-%d %H:%M} 起")
           + f"，脚本整理，数字可信；{section or '全部各线'}）】"]
    for row in timed_rows.missed(backlog, root / "control" / "data" / timed_rows.STATE_FILE, now):
        if mine(row["section"]):
            out.append(f"- {row['message']}（{row['window_date']}；{row['section']}；{short(row['row'][9:])}）")

    described = set()
    item_spans = []

    def listing(title: str, rows: list[tuple[str, str]], kind: str | None = None) -> None:
        out.append(f"{title}（{len(rows)}）：" + ("无" if not rows else ""))
        # Keep master fact details and add the number shown on the owner page.
        mark = lambda item: f"[{codes[(kind, item[:80])]}] " if codes and (kind, item[:80]) in codes else ""
        for sec, item in rows[:LIST_MAX]:
            start = len(out)
            out.append(f"- {mark(item)}{sec + '：' if section is None else ''}{fact_title(item)}")
            if (sec, item) not in described:
                out.extend(daily_item(sec, item, backlog_rows, today, root, now, since, context, windows_root))
                described.add((sec, item))
            item_spans.append((start, len(out)))
        if len(rows) > LIST_MAX:
            out.append(f"- …另有 {len(rows) - LIST_MAX} 条（未展开路径和待行动）")

    backlog_rows = rows

    for verdict, label in (("pass", f"{when}审查通过"), ("rework", f"{when}判返工"), ("blocked", f"{when}判受阻")):
        listing(label, [(line_of(r), r.get("item") or "") for r in today
                        if r.get("kind") == "review" and r.get("verdict") == verdict and mine(line_of(r))])
    blocked = [(sec, WAIT_OWNER_RE.match(m.group(1)) is not None, m.group(2), m.group(1)) for sec, line in sections_lines(backlog)
               if mine(sec) and (m := LINE_RE.match(line)) and m.group(1).startswith("blocked")]
    listing("现在等你决定的行（和「等你决定」页同一个数）",
            [(it["section"], next(r["item"] for r in backlog_rows if r["line"] == it["line"] and Path(r["source"]).name == "backlog.md"))
             for it in pending.extract_text(backlog, now)["items"] if mine(it["section"])], "D")
    listing("另有等你操作的行（别的写法，如「等你粘命令」「等 owner 回…」，也在「要你的」页里，方括号里是卡片编号）",
            [(sec, item) for sec, owner, item, status in blocked if owner and not status.startswith("blocked: 等你决定")], "T")
    if any(not owner for _, owner, _, _ in blocked):
        out.append(f"另有 {sum(1 for _, owner, _, _ in blocked if not owner)} 行在等别的（定时、上一步、同事等），不用你动手。")
    cards, handled_cards = [], 0
    for r in backlog_rows:
        if not mine(r["section"]) or not r["status"].startswith("done"):
            continue
        seen = re.search(r"owner 已看\s*(\d{2})-(\d{2})", r["state"])
        seen_day = pending._date(seen[1], seen[2], now) if seen else None
        state = owner_card_state(r, context, now)
        recent_seen = seen_day and (now.astimezone().date() - seen_day.date()).days < 14
        if state == "waiting":
            cards.append((r["section"], r["item"]))
        elif state == "handled" or recent_seen:
            handled_cards += 1
    listing(f"成果卡（待 owner 动手 {len(cards)}；已答／已看 {handled_cards}；只读）", cards)
    if section:
        sprint = next((l for sec, l in sections_lines(backlog) if sec == section and l.startswith("冲刺")), None)
        out.append("本线冲刺行（明天冲刺那一行照它写）：" + (short(sprint, 160) if sprint else "没有"))
    listing(f"{when}执行成果（按事项关联，写入不等于验收通过）", list(dict.fromkeys(
        (line_of(r), r.get("item") or "") for r in today if r.get("kind") == "executor" and mine(line_of(r)))))
    out.append(f"本时段已报成果引用（{len(context['reported_refs'])} 个，不重复当作新成果；逐项见上方）。")
    patterns = list(dict.fromkeys(context["reported_patterns"]))
    for pattern in patterns[:3]:
        out.append(f"通配路径，不生成打开命令：{pattern}")
    if len(patterns) > 3:
        out.append(f"另有 {len(patterns) - 3} 个通配引用未展开。")
    paths = list(dict.fromkeys(n for r in today if r.get("kind") == "executor" and mine(line_of(r))
                               for n in context["written"].get(r.get("run_file"), [])))
    out.append(f"{when}执行者写的结果文件 audits/（{len(paths)}；路径按事项择一展开）。")
    if not any(context["written"].get(r.get("run_file")) for r in today if mine(line_of(r))):
        out.append("没有可确认的新结果；09:00／21:00 日报照常，不跳过。")
    all_names = dict(context["candidate_file_names"])
    for name, paths in context["file_names"].items():
        all_names.setdefault(name, {}).update(paths)
    for name, paths in all_names.items():
        if len(paths) > 1:
            out.append(f"同名不同文件：{name} 有 {len(paths)} 个完整路径（不消重；每项只展开一个）。")
    body = []
    for sec in sorted({line_of(r) for r in today if mine(line_of(r))}):
        rows = [r for r in today if line_of(r) == sec]
        ex = [r for r in rows if r.get("kind") == "executor"]
        body.append([sec, len(ex), sum(1 for r in ex if r.get("engine") == "codex"), len(rows) - len(ex), f"${cost(rows):.2f}"])
    out.append(f"{when}的运行和花费（Codex 走订阅记 0）：")
    out.append(table(["线", "执行", "其中 Codex", "其他（审查、规划、日报等）", "花费"], body) if body else "无运行")
    ups = [u for u in usage if inwin(u["_at"]) and isinstance(u.get("utilization"), (int, float))]
    if section is None and ups:
        peak = max(ups, key=lambda u: u["utilization"])
        out.append(f"用量（usage.jsonl）：{when}最高 {peak['utilization']:.0%}（{peak['_at'].astimezone():%H:%M}），"
                   f"最近一次 {ups[-1]['utilization']:.0%}（{ups[-1]['_at'].astimezone():%H:%M}）。")
    return cap_daily(out, item_spans, COORDINATOR_FACT_MAX if section is None else LINE_FACT_MAX)

def day_facts(day, section: str | None, now: datetime | None = None, since: datetime | None = None) -> str:
    """daily() over the live files; rows after `now` (default: now) are ignored, so a past day can be replayed."""
    as_of = now or datetime.now(timezone.utc)
    try:
        backlog = BACKLOG.read_text(encoding="utf-8")
    except OSError:
        backlog = ""
    return daily(line_map.fold_all(load_jsonl(DATA / "runs.jsonl", "at", as_of)), backlog, day, section,
                 load_jsonl(DATA / "usage.jsonl", "observed_at", as_of), since, card_codes(backlog), now=as_of)


def card_codes(backlog: str) -> dict:
    """{("D"|"T", row head): card number} the 要你的 page shows (input/pending.py's registry, numbering rows it has not yet seen)."""
    try:
        data_dir, keys = OWNER_DATA, []
        for it in pending.extract_text(backlog)["items"]:
            keys.append(("D", it["head"], pending.card_key("D", backlog, it)))
        for it in pending.owner_wait_items(backlog):
            keys.append(("T", it["head"], pending.card_key("T", backlog, it)))
        found = pending.codes_for(data_dir, [(k, key) for k, _, key in keys])
        return {(k, head): found[f"{k}:{key}"]["code"] for k, head, key in keys if f"{k}:{key}" in found}
    except Exception:  # a report must go out without the numbers rather than not at all
        return {}


if __name__ == "__main__":
    import tick  # noqa: E402  (only here: tick imports this module)
    sys.stdout.reconfigure(encoding="utf-8")
    print(build(tick))
