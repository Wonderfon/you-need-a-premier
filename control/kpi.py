"""Executor/run KPI from data/runs.jsonl (stdlib only): 7-day window -> data/kpi.json + data/kpi.md.

Imported by tick.py (coordinate run calls write()); never needs the network or the claude CLI.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import line_map  # noqa: E402  (a row of the old 工作线 is read as the line it belongs to now)
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

# ---- CONFIG ----
DATA = Path(ic.V["workspace"] + r"\incore\control\data")
RUNS_FILE = DATA / "runs.jsonl"
DEPUTIES_FILE = DATA / "deputies.json"
KPI_JSON = DATA / "kpi.json"
KPI_MD = DATA / "kpi.md"
WINDOW_DAYS = 7
VERDICTS = ("pass", "rework", "blocked")
# ----------------


def _when(value) -> datetime | None:
    try:
        when = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def load(now: datetime) -> list[dict]:
    """Ledger rows inside the window, each with a parsed `_at`, sorted by time."""
    rows = []
    try:
        lines = RUNS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        at = _when(row.get("at")) if isinstance(row, dict) else None
        if at and at > now - timedelta(days=WINDOW_DAYS):
            rows.append(line_map.fold({**row, "_at": at}))
    return sorted(rows, key=lambda r: r["_at"])


def _med(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(values), 4) if values else None


def _sum(values):
    return round(sum(v for v in values if isinstance(v, (int, float))), 4)


def review_join(rows: list[dict]) -> dict[int, str]:
    """index of an executor row -> verdict of the next review of the same item in the same section (before the
    next executor run of that item)."""
    out = {}
    for i, row in enumerate(rows):
        if row.get("kind") != "executor":
            continue
        key = (row.get("section"), row.get("item"))
        nxt = next((r["_at"] for r in rows[i + 1:] if r.get("kind") == "executor"
                    and (r.get("section"), r.get("item")) == key), None)
        for r in rows[i + 1:]:
            if nxt and r["_at"] >= nxt:
                break
            if r.get("kind") == "review" and (r.get("section"), r.get("item")) == key:
                out[i] = r.get("verdict") or "unknown"
                break
    return out


def load_deputies() -> dict:
    try:
        data = json.loads(DEPUTIES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def executor_sessions(rows: list[dict], joined: dict[int, str], deputies: dict) -> list[dict]:
    """One entry per (role, project, session): executor runs, first-pass rate, reworks, cost of all the session's runs
    (executor, compact, lessons) and its state from deputies.json (live: reworks_in_row / retire flag; retired)."""
    groups: dict[tuple, list[int]] = {}
    for i, r in enumerate(rows):
        if r.get("kind") == "executor":
            groups.setdefault((r.get("role"), r.get("project"), r.get("session_id") or "?"), []).append(i)
    out = []
    for (role, project, sid), idx in groups.items():
        verdicts = [joined[i] for i in idx if joined.get(i) in VERDICTS]
        ex = (((deputies.get(role) or {}).get("executors") or {}).get(project)) or {}
        live = ex.get("session_id") == sid
        retired = any(x.get("session_id") == sid for x in ex.get("retired") or [] if isinstance(x, dict))
        out.append({"role": role, "project": project, "session": sid[:8], "runs": len(idx),
                    "reviewed_n": len(verdicts), "pass": verdicts.count("pass"), "rework": verdicts.count("rework"),
                    "first_pass_rate": round(verdicts.count("pass") / len(verdicts), 3) if verdicts else None,
                    "cost_usd": _sum(r.get("cost_usd") for r in rows if r.get("session_id") == sid
                                     and r.get("kind") in ("executor", "compact", "lessons")),
                    "reworks_in_row": ex.get("reworks_in_row") if live else None,
                    "state": "retired" if retired else ("retire" if live and ex.get("retire") else "live" if live else "-")})
    return out


def summarize(rows: list[dict], deputies: dict | None = None) -> dict:
    joined = review_join(rows)
    groups: dict[tuple, list[int]] = {}
    for i, r in enumerate(rows):
        groups.setdefault((r.get("kind"), r.get("model"), r.get("effort"), r.get("session")), []).append(i)
    out = []
    for (kind, model, effort, session), idx in groups.items():
        g = [rows[i] for i in idx]
        entry = {"kind": kind, "model": model, "effort": effort, "session": session, "n": len(g),
                 "ok_rate": round(sum(1 for r in g if r.get("ok")) / len(g), 3),
                 "median_elapsed_s": _med(r.get("elapsed_s") for r in g),
                 "median_cost_usd": _med(r.get("cost_usd") for r in g),
                 "total_cost_usd": _sum(r.get("cost_usd") for r in g),
                 "median_output_tokens": _med(r.get("output_tokens") for r in g)}
        if kind == "executor":
            verdicts = [joined[i] for i in idx if joined.get(i) in VERDICTS]
            counts = {v: verdicts.count(v) for v in VERDICTS}
            entry.update(reviewed_n=len(verdicts), **counts,
                         first_pass_rate=round(counts["pass"] / len(verdicts), 3) if verdicts else None)
        out.append(entry)
    total = _sum(r.get("cost_usd") for r in rows)
    roles: dict[str, float] = {}
    for r in rows:
        roles[r.get("role") or "?"] = roles.get(r.get("role") or "?", 0) + (r.get("cost_usd") or 0)
    share = {k: {"cost_usd": round(v, 4), "share": round(v / total, 3) if total else None, "n": sum(
        1 for r in rows if (r.get("role") or "?") == k)} for k, v in sorted(roles.items(), key=lambda x: -x[1])}
    return {"window_days": WINDOW_DAYS, "n_runs": len(rows), "total_cost_usd": total, "groups": out,
            "role_cost": share, "executor_sessions": executor_sessions(rows, joined, deputies or {})}


def _fmt(v, nd=2):
    return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def render_md(summary: dict, now: datetime) -> str:
    lines = [f"# KPI（近 {summary['window_days']} 天，生成于 {now.strftime('%Y-%m-%d %H:%M')} UTC）", "",
             f"总运行 n={summary['n_runs']}，总成本 ${_fmt(summary['total_cost_usd'])}（API 等价）。"
             "first-pass 只统计有 pass/rework/blocked 结论的验收，unknown 不计入。", "",
             "## 按 (kind, model, effort, session)", "",
             "| kind | model | effort | session | n | ok率 | 中位耗时s | 中位成本$ | 总成本$ | 中位输出tok |"
             " 已验收n | pass | rework | blocked | 一次通过率 |", "|" + "---|" * 15]
    for g in sorted(summary["groups"], key=lambda x: (str(x["kind"]), str(x["model"]), str(x["effort"]))):
        ex = g["kind"] == "executor"
        lines.append("| " + " | ".join([
            str(g["kind"]), str(g["model"]), str(g["effort"]), str(g["session"]), str(g["n"]), _fmt(g["ok_rate"], 2),
            _fmt(g["median_elapsed_s"], 0), _fmt(g["median_cost_usd"], 3), _fmt(g["total_cost_usd"]),
            _fmt(g["median_output_tokens"], 0),
            *([str(g["reviewed_n"]), str(g["pass"]), str(g["rework"]), str(g["blocked"]),
               _fmt(g["first_pass_rate"], 2)] if ex else ["-"] * 5)]) + " |")
    lines += ["", "## 各角色成本占比", "", "| role | n | 成本$ | 占比 |", "|---|---|---|---|"]
    for role, v in summary["role_cost"].items():
        lines.append(f"| {role} | {v['n']} | {_fmt(v['cost_usd'])} | {_fmt(v['share'], 2)} |")
    lines += ["", "## 执行者会话（按角色、项目；project 为 - 的是按项目分之前的整线会话）", "",
              "| role | project | 会话 | runs | 已验收n | pass | rework | 一次通过率 | 成本$ | 连续返工 | 状态 |",
              "|" + "---|" * 11]
    for s in sorted(summary.get("executor_sessions", []), key=lambda x: (str(x["role"]), str(x["project"]), x["session"])):
        lines.append("| " + " | ".join([str(s["role"]), str(s["project"] or "-"), s["session"], str(s["runs"]),
                                        str(s["reviewed_n"]), str(s["pass"]), str(s["rework"]), _fmt(s["first_pass_rate"], 2),
                                        _fmt(s["cost_usd"]), _fmt(s["reworks_in_row"]), s["state"]]) + " |")
    return "\n".join(lines) + "\n"


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    summary = summarize(load(now), load_deputies())
    _atomic(KPI_JSON, json.dumps(summary, ensure_ascii=False, indent=2))
    _atomic(KPI_MD, render_md(summary, now))
    return summary
