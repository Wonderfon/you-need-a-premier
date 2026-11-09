"""Read-only Claude todo tag report. Usage: python control/claude_tag_check.py [backlog] [--section 小节名]."""
import argparse
import re
from pathlib import Path

# ---- CONFIG ----
CONFIG = {
    "sections": (),  # Empty means all ## sections; otherwise use exact heading names.
    "reason_words": (
        "Windows", "venv", "环境变量", "claude -p", "SEC", "efts",
        "Start-ScheduledTask", "PowerShell", "WinError", "python.exe", "GetLastInputInfo",
    ),
}

BACKLOG = Path(__file__).resolve().parent.parent / "projects" / "backlog.md"
TAG = "〔执行者：Claude〕"
TODO = re.compile(r"^- \[todo\]\s+(.+)$")


def check(text, sections=None, reason_words=None):
    """Yield source-ordered rows, including tags found only inside 「」 quotes.

    Keyword hits are evidence to inspect, not proof that Windows is required.
    Project is the task's prefix before ： or :, with a leading （返工） removed.
    """
    sections = CONFIG["sections"] if sections is None else sections
    reason_words = CONFIG["reason_words"] if reason_words is None else reason_words
    words = [(word, re.compile(r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])", re.I))
             for word in reason_words]
    section = ""
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        match = TODO.fullmatch(line)
        if not match or TAG not in line or (sections and section not in sections):
            continue
        body = match[1]
        project = re.split(r"[：:]", body.removeprefix("（返工）").lstrip(), maxsplit=1)[0]
        reasons = [word for word, pattern in words if pattern.search(body)]
        status = "有理由词" if reasons else "可能不需要"
        starts, quoted = [], []
        for position, char in enumerate(body):
            if char == "「":
                starts.append(position)
            elif char == "」" and starts:
                quoted.append((starts.pop(), position))
        if all(any(start < tag.start() < end for start, end in quoted)
               for tag in re.finditer(re.escape(TAG), body)):
            status = "引用，不是标签"
        yield {"line": number, "section": section, "project": project, "preview": body[:80],
               "reasons": reasons, "status": status}


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读列出 todo 的 Claude 标签和理由词；判断需人工复核。")
    parser.add_argument("backlog", nargs="?", type=Path, default=BACKLOG)
    parser.add_argument("--section", action="append", help="只看指定 ## 小节；可重复，默认按 CONFIG（全部）。")
    args = parser.parse_args(argv)
    try:
        text = args.backlog.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        parser.exit(1, f"无法读取 backlog：{exc}\n")
    rows = list(check(text, sections=args.section))
    print("行号\t小节\t项目\t前80字\t理由词\t判断")
    for row in rows:
        print("\t".join((str(row["line"]), row["section"], row["project"], row["preview"],
                         "、".join(row["reasons"]) or "无", row["status"])))
    print(f"共 {len(rows)} 条（词表命中仅供人工复核）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
