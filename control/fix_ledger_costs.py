"""One-off: correct cost_usd (and, for terminal rows, token counts) of historical rows in control/data/runs.jsonl.

Why: a resumed `claude -p` restores the session's cumulative cost, so the stream's result.total_cost_usd was recorded
as the session total, not this run's cost (audits/2026-10-01-cost-delta.md). New runs are fixed in tick.py.

Baseline rule (checked on sessions af5b1df5, e92f653e, f4f8e77a): one `-p` process and one interactive process each
write one `cost-state` entry (the interactive one twice, same value) at exit, with the session's cumulative total.
So for a stream whose session transcript has cost-state totals T, a run with result total R has the baseline
max(t for t in T if t < R - EPS), i.e. the previous process's total; 0 when none is lower (new session: cost kept).
A process that died without writing its entry makes the baseline too low, never too high.
Terminal rows (run_file is the transcript): recomputed with tick.transcript_stats(path, epoch(at) - elapsed_s).
Only cost_usd (and terminal token fields) change. Rows whose stream or transcript is missing are left alone.

Safety: DRY_RUN prints the before/after table and touches nothing. A real run refuses if any runs.jsonl.bak-* exists,
writes runs.jsonl.bak-<stamp> first, and aborts without replacing if runs.jsonl changed (size/mtime) meanwhile.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tick  # noqa: E402

# ---- CONFIG ----
DRY_RUN = True
RUNS_FILE = tick.RUNS_FILE
SESSIONS_DIR = tick.SESSIONS_DIR
EPS = 1e-6
TOKEN_FIELDS = ("input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens")
# ----------------


def cost_states(sid: str) -> list[float]:
    try:
        lines = (SESSIONS_DIR / f"{sid}.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        if '"cost-state"' in line:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("type") == "cost-state" and e.get("totalCostUSD") is not None:
                found.append(float(e["totalCostUSD"]))
    return found


def stream_facts(path: Path) -> tuple[str | None, float | None, bool]:
    """(session_id of system/init, result total_cost_usd, has cost_baseline line)"""
    sid, total, has_base = None, None, False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("type") == "system" and e.get("subtype") == "init":
            sid = e.get("session_id")
        elif e.get("type") == "result":
            total = e.get("total_cost_usd")
        elif e.get("type") == "cost_baseline":
            has_base = True
    return sid, total, has_base


def fixed_fields(row: dict) -> tuple[dict, str | None]:
    """(changed fields, session id used) for one row; {} when nothing to fix."""
    path = Path(row.get("run_file") or "")
    if not path.is_file():
        return {}, None
    if row.get("kind") == "terminal":
        end = datetime.fromisoformat(row["at"]).timestamp()
        kept = []  # cut at the first entry after the row's end, so later processes' cost-state entries do not count
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            when = tick._when(json.loads(line).get("timestamp")) if '"timestamp"' in line else None
            if when and when.timestamp() > end:
                break
            kept.append(line)
        with tempfile.TemporaryDirectory() as tmp:
            cut = Path(tmp) / path.name
            cut.write_text("\n".join(kept), encoding="utf-8")
            stats = tick.transcript_stats(cut, end - (row.get("elapsed_s") or 0))
        return ({k: stats[k] for k in ("cost_usd", *TOKEN_FIELDS)} if stats else {}), row.get("session_id")
    sid, total, has_base = stream_facts(path)
    if not sid or total is None or has_base:
        return {}, sid
    below = [t for t in cost_states(sid) if t < total - EPS]
    return ({"cost_usd": round(total - max(below), 6)} if below else {}), sid


def main() -> None:
    raw = RUNS_FILE.read_bytes()
    before = RUNS_FILE.stat()
    if not DRY_RUN and list(RUNS_FILE.parent.glob(RUNS_FILE.name + ".bak-*")):
        sys.exit("a runs.jsonl.bak-* already exists; move it away first")
    out, per_session, n_changed = [], {}, 0
    print(f"{'#':>3} {'at':19} {'role':12} {'kind':9} {'session':8} {'old':>9} {'new':>9}")
    for i, line in enumerate(raw.split(b"\n")):
        eol = b"\r" if line.endswith(b"\r") else b""
        try:
            row = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        new, sid = fixed_fields(row)
        old_cost = row.get("cost_usd")
        key = (sid or row.get("session_id") or "-")[:8]
        tot = per_session.setdefault(key, [0.0, 0.0])
        tot[0] += old_cost or 0.0
        tot[1] += new.get("cost_usd", old_cost) or 0.0
        if new and any(row.get(k) != v for k, v in new.items()):
            n_changed += 1
            show = lambda c: "None" if c is None else f"{c:.4f}"
            print(f"{i:>3} {row['at'][:19]} {str(row.get('role')):12} {row['kind']:9} {key:8} "
                  f"{show(old_cost):>9} {show(new.get('cost_usd', old_cost)):>9}")
            row.update(new)
            line = json.dumps(row, ensure_ascii=False).encode("utf-8") + eol
        out.append(line)
    print(f"\n{n_changed} rows changed\n{'session':8} {'old total':>10} {'new total':>10}")
    for key, (a, b) in per_session.items():
        print(f"{key:8} {a:10.3f} {b:10.3f}")
    print(f"{'ALL':8} {sum(v[0] for v in per_session.values()):10.3f} {sum(v[1] for v in per_session.values()):10.3f}")
    if DRY_RUN or not n_changed:
        print("dry run: nothing written" if DRY_RUN else "nothing to change")
        return
    backup = RUNS_FILE.with_name(RUNS_FILE.name + datetime.now().strftime(".bak-%Y%m%d-%H%M%S"))
    shutil.copy2(RUNS_FILE, backup)
    tmp = RUNS_FILE.with_name(RUNS_FILE.name + ".tmp")
    tmp.write_bytes(b"\n".join(out))
    now = RUNS_FILE.stat()
    if (now.st_size, now.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        tmp.unlink()
        sys.exit("runs.jsonl changed while the script ran; nothing replaced (backup kept)")
    os.replace(tmp, RUNS_FILE)
    print(f"written; backup {backup}")


if __name__ == "__main__":
    main()
