"""One-off: build data/runs.jsonl from the tick log (`ran`, `owner_ran`) and the report run files in ticks/.

Cost and tokens come from each run file's result event; effort is the inherited one (sonnet high, opus xhigh);
session is `fresh` for executors and section-less runs, else `resume`. Idempotent: run files already in the ledger
are skipped, runs whose file is gone are skipped. Run by the coordinator at cutover.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tick  # noqa: E402

# ---- CONFIG ----
LOG_FILE = tick.LOG_FILE
TICKS_DIR = tick.TICKS_DIR
RUNS_FILE = tick.RUNS_FILE
INHERITED_EFFORT = {"sonnet": "high", "opus": "xhigh"}
# ----------------

REPORT_FILE_RE = re.compile(r"^(\d{8}-\d{6})-report-(\w+?)(-retry)?\.jsonl$")


def _row(at, role, section, kind, model, session, item, run_file: Path, code=None, elapsed=None):
    ok, result, _ = tick.final_result(run_file)
    code = 0 if code is None and ok else code
    return tick.ledger_row(role, section, kind, INHERITED_EFFORT.get(model, "high"), session, None, item, elapsed,
                           code, ok and code == 0, result, run_file, at=at, model=model)


def candidates():
    """(run_file, builder kwargs) for every historical run, log entries first, then report files."""
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("decision") not in ("ran", "owner_ran") or not e.get("run_file"):
            continue
        if e["decision"] == "owner_ran":
            role, kind, section = e.get("role"), "owner", tick.role_section(e["role"]) if e.get("role") in tick.ROLE_NAMES else None
        else:
            role, kind, section = tick.SECTION_ROLES.get(e.get("section")), e.get("kind"), e.get("section")
        model = e.get("model") or tick.MODELS.get(kind)
        session = "fresh" if kind == "executor" or not role else "resume"
        yield Path(e["run_file"]), dict(at=e.get("at"), role=role, section=section, kind=kind, model=model,
                                        session=session, item=e.get("item") or "", code=e.get("exit_code"),
                                        elapsed=e.get("elapsed_s"))
    for f in sorted(TICKS_DIR.rglob("*-report-*.jsonl")):
        m = REPORT_FILE_RE.match(f.name)
        if m and m.group(2) in tick.ROLE_NAMES:
            at = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").astimezone().isoformat(timespec="seconds")
            yield f, dict(at=at, role=m.group(2), section=tick.role_section(m.group(2)), kind="report",
                          model=tick.MODELS["report"], session="new" if m.group(3) else "resume", item="")


def backfill() -> int:
    """Append the missing rows to RUNS_FILE; returns how many were added."""
    seen = set()
    try:
        for line in RUNS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line).get("run_file"))
            except (ValueError, AttributeError):
                pass
    except OSError:
        pass
    added = 0
    for run_file, kw in candidates():
        if str(run_file) in seen or not run_file.exists():
            continue
        seen.add(str(run_file))
        tick.append_jsonl(RUNS_FILE, _row(run_file=run_file, **kw))
        added += 1
    return added


if __name__ == "__main__":
    print(f"added {backfill()} rows to {RUNS_FILE}")
