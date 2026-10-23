"""Scan the working tree and the full git history for terms listed in a private ban list kept OUTSIDE this repository.

    python tools/check_private.py <banned.txt> [repo-dir]

Ban list: one term per line; '#' starts a comment; a plain term is a case-insensitive substring;
're:<pattern>' is a regular expression. Hits are reported as location only (file and line, or commit and file),
never the matched text, so the report itself is safe to paste. Exit code: 0 clean, 1 hits, 2 usage or setup error.
Git is taken from $ORGCORE_GIT or PATH.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".venv"}


def load_terms(path: Path) -> list[re.Pattern]:
    terms = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        terms.append(re.compile(line[3:]) if line.startswith("re:") else re.compile(re.escape(line), re.IGNORECASE))
    if not terms:
        raise ValueError(f"ban list has no terms: {path}")
    return terms


def hit(terms: list[re.Pattern], text: str) -> bool:
    return any(term.search(text) for term in terms)


def scan_tree(root: Path, terms: list[re.Pattern]) -> list[str]:
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            if hit(terms, rel):
                found.append(f"{rel}: file name")
            data = path.read_bytes()
            if b"\0" in data[:4096]:
                continue
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if hit(terms, line):
                    found.append(f"{rel}:{number}")
    return found


def run_git(git: str, root: Path, *args: str) -> str:
    result = subprocess.run([git, "-c", "safe.directory=*", "-C", str(root), *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8", errors="replace")


def scan_history(git: str, root: Path, terms: list[re.Pattern]) -> list[str]:
    found = []
    refs = run_git(git, root, "for-each-ref", "--format=%(refname)")
    for ref in refs.splitlines():
        if hit(terms, ref):
            found.append("ref name: a branch or tag name matches (see git for-each-ref)")
    log = run_git(git, root, "log", "--all", "-p", "--format=@@COMMIT %H%n%an%n%ae%n%cn%n%ce%n%B@@BODY")
    commit, where, number, in_meta = "", "", 0, False
    for line in log.splitlines():
        if line.startswith("@@COMMIT "):
            commit, where, number, in_meta = line[9:17], "commit metadata", 0, True
            continue
        if in_meta and line == "@@BODY":
            in_meta, where, number = False, "", 0
            continue
        if not in_meta and line.startswith("diff --git "):
            where, number = line.split(" b/", 1)[-1], 0
            if hit(terms, where):
                found.append(f"history {commit}: {where}: file name")
            continue
        number += 1
        if hit(terms, line):
            found.append(f"history {commit}: {where or 'diff preamble'}:{number}")
    return found


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    banned = Path(argv[1])
    root = Path(argv[2] if len(argv) == 3 else Path(__file__).resolve().parent.parent).resolve()
    try:
        terms = load_terms(banned)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    git = os.environ.get("ORGCORE_GIT") or shutil.which("git")
    if not git:
        print("error: git not found; set ORGCORE_GIT", file=sys.stderr)
        return 2
    found = scan_tree(root, terms) + scan_history(git, root, terms)
    for entry in found:
        print(entry)
    print(f"hits: {len(found)}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
