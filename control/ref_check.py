"""Report broken local references; never change sources or block dispatch.

Usage: python control/ref_check.py [--root INCUBATOR] [--files GLOB ...]
Only reads the selected Markdown files and lists referenced directories.
"""
import argparse
import difflib
import os
import re
from pathlib import Path

# ---- CONFIG ----
CONFIG = {
    "root": Path(__file__).resolve().parents[1],
    "files": (
        "control/roles/*.md",  # the public templates (placeholders)
        "data/config-sync/private-layer/roles/*.md",  # the real rules and role files (control/layered.py)
        "projects/intent/*.md",
        "projects/taste/*.md",
        "projects/lessons/*.md",
        "../AGENTS.md",  # workspace root, outside the incore repository
    ),
}
# ----------------

# Accept the incore/ spelling used by the rules, but ignore absolute paths
# and URLs. Keep <placeholders> together so they can be skipped, not truncated.
REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_./\\-])(?:incore/)?"
    r"((?:records|audits|projects|control)/"
    r"(?:<[^<>\n]+>|[^\s`\"'<>，。；：、！？「」『』（）〔〕【】《》〈〉()\[\]{};,!?|])*)"
)


def references(line):
    for match in REFERENCE.finditer(line):
        path = match.group(1).rstrip(".")
        if path.endswith("**") and not path.endswith("/**"):
            path = path[:-2]  # closing Markdown bold, not a directory wildcard
        path = re.sub(r":\d+(?:[-–,]\d+)*$", "", path)
        yield path


def scan(root, patterns):
    root = Path(root).resolve()
    files = sorted({p.resolve() for pattern in patterns for p in root.glob(pattern) if p.is_file()})
    result = {"files": len(files), "references": 0, "skipped": 0, "missing": []}
    for source in files:
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            for path in references(line):
                # Wildcards, like <dates>, describe a family rather than a file.
                if "<" in path or any(c in path for c in "*?"):
                    result["skipped"] += 1
                    continue
                result["references"] += 1
                target = root / path
                if target.exists():
                    continue
                names = sorted(p.name for p in target.parent.iterdir() if p.is_file()) if target.parent.is_dir() else []
                candidates = [str(Path(path).parent / name).replace("\\", "/")
                              for name in difflib.get_close_matches(target.name, names, n=3, cutoff=0)]
                result["missing"].append({
                    "source": Path(os.path.relpath(source, root)).as_posix(),
                    "line": number,
                    "path": path,
                    "candidates": candidates,
                })
    return result


def render(result):
    lines = [f"扫了 {result['files']} 个文件，引用 {result['references']} 条，缺 {len(result['missing'])} 条。",
             f"占位或通配引用跳过 {result['skipped']} 条；重复引用按出现次数计数。只报不拦，不自动替换。"]
    previous = None
    for item in result["missing"]:
        if item["source"] != previous:
            lines.extend(["", f"### {item['source']}", ""])
            previous = item["source"]
        candidates = "、".join(f"`{p}`" for p in item["candidates"]) or "无相近文件或同目录不存在"
        lines.append(f"- 第 {item['line']} 行：`{item['path']}`；候选（仅供核对）：{candidates}。")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=CONFIG["root"])
    parser.add_argument("--files", nargs="+", default=CONFIG["files"], help="globs relative to --root")
    args = parser.parse_args(argv)
    print(render(scan(args.root, args.files)))
    return 0  # missing references are observations, not a gate


if __name__ == "__main__":
    raise SystemExit(main())
