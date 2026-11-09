"""Read-only before/after test leak check; JSON to stdout, test output to stderr.

Usage: python control/leak_check.py --root <incore> [--suite control|input]
Defaults to both suites. Exit 0: clean; 1: tests failed; 2: changes or incomplete
snapshots. This observes writes, including concurrent lanes; it never repairs them.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---- CONFIG ----
CONFIG = {
    "root": Path(__file__).resolve().parents[1],
    "watch": ("control/data", "projects", "data", "input/data", "audits"),
    "ignore": ("control/data/ticks", "control/data/backlog-history", "data/backlog-history",
               "data/backups/objects", "data/backups/packages", "data/backups/snapshots",  # archive payloads; status/receipts stay watched
               "**/__pycache__", "*.pyc"),
    # Credentials are never opened, even to hash. Patterns apply to every component.
    "private": (".env", ".env.*", "*.dpapi", "02_secrets", ".secrets", "*token*", "*key*", "*secret*", "*credential*", "*.pem"),
    "max_hash_bytes": 1024 * 1024,
    "commands": {
        "control": {"cwd": "control", "argv": [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"]},
        "input": {"cwd": "..", "argv": [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "{root_name}/input", "-t", ".", "-p", "test_*.py"]},
    },
}
# ----------------


def ignored(relative: str, config: dict) -> bool:
    path = relative.casefold()
    return (any(fnmatch.fnmatchcase(path, pattern.casefold()) or
                path.startswith(pattern.casefold().rstrip("/") + "/") for pattern in config["ignore"])
            or any(fnmatch.fnmatchcase(part, pattern.casefold())
                   for part in path.split("/") for pattern in config["private"]))


def snapshot(root: Path, config: dict) -> dict:
    """Only metadata and bounded binary hashing; no parsing or text decoding."""
    result = {"files": {}, "errors": [], "missing_roots": [], "skipped_links": []}

    def visit(path: Path) -> None:
        relative = path.relative_to(root).as_posix()
        if ignored(relative, config):
            return
        try:
            info = path.lstat()
            # Windows junctions can lead outside the watched tree too.
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                result["skipped_links"].append(relative)
            elif stat.S_ISDIR(info.st_mode):
                for child in sorted(path.iterdir()):
                    visit(child)
            elif stat.S_ISREG(info.st_mode):
                entry = {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "sha256": None}
                result["files"][relative] = entry
                if info.st_size <= config["max_hash_bytes"]:
                    digest = hashlib.sha256()
                    remaining = config["max_hash_bytes"] + 1
                    with path.open("rb") as stream:
                        while remaining:
                            chunk = stream.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            digest.update(chunk)
                            remaining -= len(chunk)
                    after = path.stat()
                    if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns) or not remaining:
                        result["errors"].append({"path": relative, "error": "file changed while hashing"})
                    else:
                        entry["sha256"] = digest.hexdigest()
        except OSError as exc:
            result["errors"].append({"path": relative, "error": type(exc).__name__})

    for relative in config["watch"]:
        print(f"  Snapshot root: {relative}", file=sys.stderr, flush=True)
        path = root / relative
        if not path.exists() and not path.is_symlink():
            result["missing_roots"].append(relative)
        else:
            visit(path)
    return result


def compare(before: dict, after: dict) -> dict:
    old, new = before["files"], after["files"]
    return {"created": sorted(new.keys() - old.keys()),
            "modified": sorted(path for path in old.keys() & new.keys() if old[path] != new[path]),
            "deleted": sorted(old.keys() - new.keys())}


def check(root: Path, commands: list[dict], config: dict = CONFIG) -> dict:
    started = datetime.now(timezone.utc).isoformat()
    print("Scanning before snapshot...", file=sys.stderr, flush=True)
    before = snapshot(root, config)
    runs = []
    try:
        for command in commands:
            print(f"Running tests in {command['cwd']}", file=sys.stderr, flush=True)
            record = {"argv": command["argv"], "cwd": str(command["cwd"])}
            runs.append(record)
            try:
                process = subprocess.run(command["argv"], cwd=command["cwd"],
                                         env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
                                         stdout=sys.stderr, stderr=sys.stderr, check=False)
                record["returncode"] = process.returncode
            except OSError as exc:
                record.update(returncode=1, error=type(exc).__name__)
    finally:
        print("Scanning after snapshot...", file=sys.stderr, flush=True)
        after = snapshot(root, config)
    changes = compare(before, after)
    incomplete = any(s["errors"] or s["missing_roots"] for s in (before, after))
    exit_code = 2 if incomplete or any(changes.values()) else int(any(r["returncode"] for r in runs))
    return {"root": str(root), "started_at_utc": started, "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "warning": "并行在跑的 lane、输入台和调度器也会写这些目录；变化只表示观测窗口内有写入，不能单凭本报告归因于测试。只读检查，不还原、不删除。",
            "watch": list(config["watch"]), "ignore": list(config["ignore"]),
            "private_ignore": list(config["private"]), "max_hash_bytes": config["max_hash_bytes"],
            "limits": "大文件只记大小和 mtime；不会发现保留这两项的大文件内容变化。input/data 和 data/poetry-collection 仅 stat 和二进制哈希，不解析内容。链接不跟随。",
            "commands": runs, "before": before, "after": after, "changes": changes,
            "summary": {**{name: len(paths) for name, paths in changes.items()},
                        "before_files": len(before["files"]), "after_files": len(after["files"]),
                        "before_unhashed": sum(e["sha256"] is None for e in before["files"].values()),
                        "after_unhashed": sum(e["sha256"] is None for e in after["files"].values()),
                        "incomplete": incomplete}, "exit_code": exit_code}


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=CONFIG["root"], help="incore root to watch AND run tests in")
    parser.add_argument("--suite", action="append", choices=CONFIG["commands"], help="repeat to select suites; default: control + input")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        parser.error("root must be an existing directory")
    commands = [{"cwd": root / CONFIG["commands"][suite]["cwd"],
                 "argv": [part.format(root_name=root.name) for part in CONFIG["commands"][suite]["argv"]]}
                for suite in dict.fromkeys(args.suite or CONFIG["commands"])]
    report = check(root, commands)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
