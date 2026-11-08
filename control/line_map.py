"""Work-line split (owner\x2010-26/10-27): the old 工作线 became Arbitros 线 (role `work`) and Beacon Listener 线 (role `uvox`).
Shared by tick.py, kpi.py and review_stats.py (stdlib only, so none of them has to import another to read the old ledger).

runs.jsonl is never rewritten. Rows written before the split carry section "工作线"; `fold` gives such a row the line it
belongs to now (in a copy), so seven-day cost, starvation, cache heat and the KPI tables do not start from zero on the new line.
Plan: audits/2026-10-26-work-line-split-plan.md section 6.2."""
from __future__ import annotations

LEGACY_SECTION = "工作线"
ARBITROS, LISTENER, ORG = "Arbitros 线", "Beacon Listener 线", "组织线"
# projects that left the old work line -> the role that owns them now (everything else of the old line stays with `work`)
MOVED = {"uvox": "uvox", "beaconlistener": "uvox", "fieldlistener": "uvox", "kiki": "uvox", "umzy": "uvox", "input": "coordinator"}  # umzy: owner\x2011-07 moved it to the Beacon Listener line
SECTION_OF_ROLE = {"work": ARBITROS, "uvox": LISTENER, "coordinator": ORG}
# item-text prefixes of those projects (the same ones tick.PROJECTS routes by); used for rows that carry no project
MOVED_PREFIXES = (("Uvox", "uvox"), ("Beacon Listener", "beaconlistener"), ("Field Observer", "fieldlistener"),
                  ("车载", "fieldlistener"), ("Kiki", "kiki"), ("Umzy", "umzy"), ("输入台", "input"), ("语音", "input"))
# owner\x2010-26 "照建议": the old 36% became 24 + 12; ledger cost that names no project is shared in that ratio
LEGACY_SHARE = {ARBITROS: 24.0, LISTENER: 12.0}
EXECUTOR_SIDE = ("executor", "lessons", "compact", "terminal")  # ledger kinds a project's own session writes


def project_of_item(item) -> str | None:
    """Moved project whose prefix the item text starts with (after a leading （返工）), else None."""
    text = str(item or "").removeprefix("（返工）").lstrip()
    return next((p for prefix, p in MOVED_PREFIXES if text.startswith(prefix)), None)


def fold(row: dict) -> dict:
    """The row as the split line structure sees it. Only section "工作线" rows change, in a copy:
    project (or item prefix) of a moved project -> that line's section; an executor-side row of a moved project also gets
    the new role (its session moved with it); a deputy-level row with no project evidence goes to Arbitros with
    `legacy_split` set, and tick.cost_7d shares its cost by LEGACY_SHARE; an executor row of a project that stayed is Arbitros'."""
    if row.get("section") != LEGACY_SECTION:
        return row
    own = row.get("project") if row.get("project") in MOVED else None
    moved = own or (project_of_item(row.get("item")) if row.get("project") in (None, "", "general") else None)
    new = {**row, "legacy_section": LEGACY_SECTION}
    if moved is None:
        new["section"] = ARBITROS
        if row.get("kind") != "executor" and not row.get("project"):
            new["legacy_split"] = True
        return new
    role = MOVED[moved]
    new["section"] = SECTION_OF_ROLE[role]
    if own and row.get("kind") in EXECUTOR_SIDE:
        new["role"] = role
    return new


def fold_all(rows) -> list[dict]:
    return [fold(r) for r in rows]
