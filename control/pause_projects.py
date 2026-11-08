"""Pause/resume configured projects under the backlog writer lock.

python control/pause_projects.py pause --section "生活线" --backlog <copy.md>
python control/pause_projects.py resume --section "生活线" --project "Thinking Engines" --backlog <copy.md>
Only todo and the owner-pause state below are reversible; other blocked rows stay intact.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import backlog_io

# ---- CONFIG ----
CONFIG = {
    "sections": {
        "Arbitros 线": ("FOS", "Arbitros 官网"),
        "Beacon Listener 线": ("Uvox", "Beacon Listener 官网", "Field Observer", "Kiki", "Umzy"),
        "生活线": ("项目七", "驾校模拟器", "Thinking Engines"),
    },
    "owner": "owner\x2011-02 15:5x",
    "source": "终端「暂停名单我同意你的建议」，records\x2f2026-11-02-owner-terminal-pause-list-restyle-a.json",
}
# Writing/poetry continues: records\x2f2026-11-02-owner-terminal-poetry-not-paused.json.
# ----------------

PAUSE_END = "；不派、不补行、不催，等 owner 说恢复时改回 todo"
PAUSE_RE = re.compile(r"blocked: owner .+ 暂停本项目（.+）" + re.escape(PAUSE_END))
SUMMARY_RE = re.compile(r"^(暂停项目(?:（.*?）)?[：:])(.*)$")


def is_paused(state: str) -> bool:
    return PAUSE_RE.fullmatch(state) is not None


def project_name(token: str) -> str:
    return re.split(r"[（(]", token, maxsplit=1)[0].strip()


def summary_parts(line: str):
    """Keep the existing provenance and prose, including parenthesized exceptions."""
    match = SUMMARY_RE.fullmatch(line.rstrip("\r"))
    if not match:
        return None
    tokens, start, depth = [], 0, 0
    text = match[2]
    for i, char in enumerate(text):
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth -= 1
        elif depth == 0 and char in "、。":
            tokens.append(text[start:i])
            start = i + 1
            if char == "。":
                return match[1], tokens, text[i:]
    tokens.append(text[start:])
    return match[1], [t for t in tokens if t.strip()], ""


def paused_projects(text: str) -> dict[str, list[str]]:
    """Read section summaries only; no scheduler or dispatch behaviour is changed."""
    result, section = {}, ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        parts = summary_parts(line)
        if parts:
            result.setdefault(section, []).extend(project_name(t) for t in parts[1])
    return result


def transform(lines: list[str], action: str, section: str, projects: tuple[str, ...]) -> list[str]:
    rows = backlog_io._section_rows(lines, section)
    summaries = [i for i in rows if summary_parts(lines[i])]
    if len(summaries) > 1:
        raise ValueError(f"{section}: more than one 暂停项目 summary")
    state = f"blocked: {CONFIG['owner']} 暂停本项目（{CONFIG['source']}）{PAUSE_END}"
    for i in rows:
        row = lines[i].removesuffix("\r")
        match = backlog_io.TASK_RE.fullmatch(row)
        if not match or not match[2].removeprefix("（返工）").lstrip().startswith(projects):
            continue
        if action == "pause" and match[1] == "todo":
            lines[i] = f"- [{state}] {match[2]}" + lines[i][len(row):]
        elif action == "resume" and is_paused(match[1]):
            lines[i] = f"- [todo] {match[2]}" + lines[i][len(row):]
    if summaries:
        i = summaries[0]
        prefix, tokens, suffix = summary_parts(lines[i])
        if action == "pause":
            names = {project_name(t) for t in tokens}
            tokens += [p for p in projects if p not in names]
        else:
            tokens = [t for t in tokens if project_name(t) not in projects]
        if tokens:
            cr = "\r" if lines[i].endswith("\r") else ""
            lines[i] = prefix + "、".join(tokens) + suffix + cr
        else:
            del lines[i]
    elif action == "pause":
        # Insert after the existing preamble so tick's weight/sprint line offsets stay intact.
        at = next((i for i in rows if lines[i].startswith("- [")), rows.stop)
        if at == len(lines) and lines and not lines[-1]:
            at -= 1
        cr = "\r" if lines[rows.start - 1].endswith("\r") else ""
        lines.insert(at, f"暂停项目（{CONFIG['owner']} {CONFIG['source']}）：" + "、".join(projects) + cr)
    return lines


def apply(action: str, section: str, projects: tuple[str, ...] | None = None,
          path: Path = backlog_io.BACKLOG) -> None:
    import tick  # lazy: review_stats imports the summary reader above

    if action not in ("pause", "resume") or section not in CONFIG["sections"]:
        raise ValueError("unknown action or section")
    projects = CONFIG["sections"][section] if projects is None else tuple(dict.fromkeys(projects))
    if not projects or any(p not in CONFIG["sections"][section] for p in projects):
        raise ValueError("project prefixes must be in CONFIG for this section")

    def update(lines):
        changed = transform(lines, action, section, projects)
        tick.parse_backlog("\n".join(changed))
        return changed

    backlog_io.edit(update, path, require_snapshot=True)
    tick.parse_backlog(path.read_text(encoding="utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pause", "resume"))
    parser.add_argument("--section", required=True, choices=CONFIG["sections"])
    parser.add_argument("--project", action="append", help="configured prefix; repeat to select several (default: all)")
    parser.add_argument("--backlog", type=Path, default=backlog_io.BACKLOG)
    args = parser.parse_args(argv)
    try:
        apply(args.action, args.section, args.project, args.backlog)
    except (OSError, ValueError, TimeoutError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"{args.action}: {args.section} — {args.backlog}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
