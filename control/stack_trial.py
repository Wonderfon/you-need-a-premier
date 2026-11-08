"""Trial-merge unmerged branches on master and run the tests on the result. Only dangling objects are made (no branch, no ref
moves). Usage: python stack_trial.py [--on-pending] [branch ...] [--stack-only]
No branches given: the `foreach ($b in '…','…')` list of the backlog's 「等你合并的孵化器分支」 row. --on-pending: that list first, then the
given branches (a name already in the list is kept once): the trial for a branch that will land after the pending ones.
Exit 1 on a conflict or a failed test."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

# ---- CONFIG ----
GIT = ic.V["git_exe"] if sys.platform == "win32" else "git"
REPO = Path(ic.V["workspace"] + r"\incore")
PY = ic.V["workspace"] + r"\incore\discord\.venv\Scripts\python.exe"
TMP_ROOT = Path(ic.V["wt_root"])  # the export goes into a temp folder here and is removed afterwards
BACKLOG = REPO / "projects" / "backlog.md"
ROW = "组织：等你合并的孵化器分支"
EXPORT = ("control", "backup", "input", "discord", "notify", "runtime", "records_capture.py", "test_records_capture.py",
          "projects/deployments.json")  # the last one is read by input's dashboard test
# group (input, notify, runtime, root) -> interpreter, for a group the discord venv lacks packages for (run with -B)
PY_FOR = {"runtime": ic.V["workspace"] + r"\incore\.runtime\usage-venv\Scripts\python.exe"}  # tzdata
CONFLICT_CONTEXT_CHARS = 6000  # per file, including line numbers and three context lines on each side
# ----------------


def git(repo: Path, *args: str, raw: bool = False) -> tuple[int, str]:
    p = subprocess.run([GIT, "-c", "safe.directory=*", "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com", "-C", str(repo), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout if raw else p.stdout.strip()


def branches_from_backlog(text: str) -> list[str]:
    """The quoted names in the first `foreach ($b in …)` of the first `- [blocked…]` row that starts with ROW."""
    for line in text.splitlines():
        if re.match(rf"- \[blocked[^\]]*\]\s*{ROW}", line) and (m := re.search(r"foreach \(\$b in ([^)]*)\)", line)):
            return re.findall(r"'([^']+)'", m.group(1))
    return []


def pick_branches(argv: list[str], backlog_text: str) -> list[str]:
    """Command-line branches, else the backlog list. With --on-pending: the backlog list first, then the command-line ones, each name once."""
    given = [a for a in argv if not a.startswith("--")]
    if "--on-pending" not in argv:
        return given or branches_from_backlog(backlog_text)
    return list(dict.fromkeys(branches_from_backlog(backlog_text) + given))


def report_untracked_additions(repo: Path, branch: str) -> bool:
    """Report added branch files already present but untracked in the main worktree."""
    rc, names = git(repo, "diff", "--name-only", "--diff-filter=A", "-z", "master", branch)
    if rc:
        print("cannot inspect additions", branch, names)
        return False
    for name in names.split("\0"):
        if not name:
            continue
        path = repo / name
        if not path.is_file():
            continue
        rc, _ = git(repo, "ls-files", "--error-unmatch", "--", name)
        if rc == 0:
            continue
        # hash through git so autocrlf / .gitattributes apply: a CRLF copy of the branch's LF file is the same content
        rc_disk, disk = git(repo, "hash-object", f"--path={name}", str(path))
        rc_blob, blob = git(repo, "rev-parse", f"{branch}:{name}")
        if rc_disk or rc_blob:
            print("cannot read branch file", branch, name)
            return False
        same = disk == blob
        action = "内容相同，可删" if same else "内容不同，交 owner 处理"
        print(f"主目录已有未入库同名文件：{branch}:{name}（{action}）")
    return True


def report_conflicts(repo: Path, head: str, branch: str, upstream: str) -> None:
    """Explain the conflict using the upstream history and the merge-tree's conflict blobs; no checkout."""
    rc, base = git(repo, "merge-base", head, branch)
    if rc:
        print("conflict details unavailable: no merge-base")
        return
    # NUL-delimited names handle spaces and non-ASCII paths without changing the original merge report.
    _, details = git(repo, "merge-tree", "--write-tree", "--name-only", "-z", head, branch)
    tree, *names = details.split("\0\0", 1)[0].split("\0")
    for name in names:
        print(f"conflict file: {name}; upstream: {upstream} ({head[:9]}); merge-base: {base[:9]}")
        rc, history = git(repo, "log", "--oneline", "-10", f"{base}..{head}", "--", name)
        print("upstream commits since merge-base (at most 10):")
        print(history[:2000] if not rc and history else "(none or unavailable)")
        rc, content = git(repo, "show", f"{tree}:{name}", raw=True)
        if rc:
            print("conflict context unavailable: no file in merge result")
            continue
        lines, excerpts, start = content.splitlines(), [], None
        for i, line in enumerate(lines):
            if re.match(r"^<{7,} ", line):
                start = i
            elif start is not None and re.match(r"^>{7,} ", line):
                lo, hi = max(0, start - 3), min(len(lines), i + 4)
                excerpts.append(f"conflict lines {start + 1}-{i + 1}; context lines {lo + 1}-{hi} (merge result):\n" +
                                "\n".join(f"{n + 1}: {lines[n]}" for n in range(lo, hi)))
                start = None
        context = "\n".join(excerpts) or "(no textual conflict markers; binary, delete or rename conflict)"
        print(context[:CONFLICT_CONTEXT_CHARS])
        if len(context) > CONFLICT_CONTEXT_CHARS:
            print(f"... conflict context truncated at {CONFLICT_CONTEXT_CHARS} characters for this file")


def stack(repo: Path, branches: list[str]) -> str | None:
    """Merge each branch onto the previous result starting at master; None after an inspection error or conflict."""
    head = git(repo, "rev-parse", "master")[1]
    upstream = "master"
    for b in branches:
        if not report_untracked_additions(repo, b):
            return None
        rc, out = git(repo, "merge-tree", "--write-tree", head, b)
        if rc != 0:
            print("CONFLICT", b)
            print("\n".join(l for l in out.splitlines()[1:] if l.strip())[:1500])
            report_conflicts(repo, head, b, upstream)
            return None
        head = git(repo, "commit-tree", out.splitlines()[0], "-p", head, "-p", b, "-m", "trial " + b)[1]
        print("ok", b, head[:9])
        upstream += " + " + b
    return head


def run(args: list[str], cwd: Path, py: str = PY) -> subprocess.CompletedProcess:
    pre = ["-B"] if py != PY else []  # another venv's interpreter must not write bytecode
    return subprocess.run([py, *pre, "-X", "utf8", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def test_jobs(root: Path) -> list[tuple[str, list[str], Path, str]]:
    """(name, interpreter args, cwd, group) for every test in the export: control modules, backup scripts, discord unittest,
    input's tests as `incore.input.<module>` from the root's parent (they import relatively and patch `incore.input.…`),
    notify, runtime and the root's own test."""
    jobs = [(m.stem, ["-m", "unittest", m.stem], root / "control", "control") for m in sorted((root / "control").glob("test_*.py"))]
    jobs += [(t.name, [t.name], root / "backup", "backup") for t in sorted((root / "backup").glob("test_*.py"))]
    jobs.append(("discord", ["-m", "unittest"], root / "discord", "discord"))
    jobs += [(f"input/{t.stem}", ["-m", "unittest", f"{root.name}.input.{t.stem}"], root.parent, "input") for t in sorted((root / "input").glob("test_*.py"))]
    for group in ("notify", "runtime"):
        jobs += [(f"{group}/{t.stem}", ["-m", "unittest", t.stem], root / group, group) for t in sorted((root / group).glob("test_*.py"))]
    jobs += [(t.stem, ["-m", "unittest", t.stem], root, "root") for t in sorted(root.glob("test_*.py"))]
    return jobs


def run_tests(repo: Path, head: str) -> bool:
    """Export `head` and run every job of test_jobs, each in its own process. A group that cannot run prints `未跑：<reason>` and fails."""
    tmp = TMP_ROOT / f"stack-trial-{Path(repo).name}-{head[:8]}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    ok = True
    try:
        tar, root = tmp / "t.tar", tmp / "incore"  # some tests derive paths from the folder name `incore`
        root.mkdir()
        subprocess.run([GIT, "-c", "safe.directory=*", "-C", str(repo), "archive", "-o", str(tar), head, *EXPORT], check=True)
        subprocess.run(["tar", "-xf", str(tar), "-C", str(root)], check=True)
        for name, args, cwd, group in test_jobs(root):
            py = PY_FOR.get(group, PY)
            if not Path(py).exists():
                print(f"{name}: 未跑：解释器不存在 {py}")
                ok = False
                continue
            p = run(args, cwd, py)
            lines = (p.stdout + p.stderr).strip().splitlines()
            if any("ModuleNotFoundError" in l for l in lines):
                print(f"{name}: 未跑：{next(l for l in lines if 'ModuleNotFoundError' in l)}")
                ok = False
                continue
            summary = [l for l in lines if l.startswith(("Ran", "OK", "FAILED"))] or lines[-1:]
            print(f"{name}: rc {p.returncode}", " | ".join(summary))
            ok &= p.returncode == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("temp removed", not tmp.exists())
    return ok


def main(argv: list[str], repo: Path = REPO) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # redirect_stdout in callers/tests may use StringIO
        sys.stdout.reconfigure(line_buffering=True)
    stack_only = "--stack-only" in argv
    branches = pick_branches(argv, BACKLOG.read_text(encoding="utf-8"))
    if not branches:
        print("no branches: give them on the command line, or the backlog row has no foreach list")
        return 2
    head = stack(repo, branches)
    if head is None:
        return 1
    print(f"{len(branches)} branches, no conflicts")
    return 0 if stack_only or run_tests(repo, head) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
