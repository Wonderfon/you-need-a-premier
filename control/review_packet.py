"""Pre-review packet (owner\x2010-05, 程序化降本 1/4): the facts a reviewer would otherwise dig out with git/ls/Glob calls,
plus the hard condition under which the timer sends a [review] row straight back without calling an LLM.

build() inspects git (including merge-tree), the audits folder, the project folder; no worktree or ref is changed.
merge-tree may write unreachable objects. It runs nothing from
the project. `hard` is set only when the executor demonstrably produced nothing: no commit in the project's repo and no
result file inside its run window. Anything uncertain (no repo, executor run not found, git missing) leaves it None and
the row goes to the reviewer as before."""
import json
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

CONFIG_GIT = ic.V["git_exe"]
GIT = shutil.which("git") or CONFIG_GIT
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # the lane process has no console; without this each child opens one
BRANCH_RE = re.compile(r"claude/[A-Za-z0-9][A-Za-z0-9._-]*")
RESULT_RE = re.compile(r"audits[/\\][^\s`'\"，。；、）)]*?\.md")
PROTECTED = re.compile(  # owner's raw data and credentials; the Umba data/ folder is added per project
    r"(^|/)(records|review-audio)/|(^|/)input/data/|(^|/)discord/data/|(^|/)02_secrets|(^|/)\.env(\.|$)|\.dpapi$|"
    r"\.(pem|key|token)$|(^|/)tokens?\.(txt|json)$|(^|/)[^/]*(secrets?|credentials?)(?![a-z])[^/]*$", re.I)  # not secretary
SHOW_FILES = 12
VERIFY_LINES = 15  # lines of the result file's 验证 section shown
CODEX_CHARS = 300  # characters of a Codex run's last message shown
WRITE_TOOLS = ("Write", "Edit", "NotebookEdit")
WRITE_CMD = re.compile(r"git\b.*\b(commit|add|merge|apply)\b|Set-Content|Out-File|Add-Content|Copy-Item|Move-Item|New-Item|"
                       r"Remove-Item|\bmkdir\b|>>?\s*\S|\btee\b|\bcp\b|\bmv\b|sed\s+-i|apply_patch|write_text|\.write\(|"
                       r"to_csv|savefig|pip\s+install|npm\s+install", re.I)  # a run with none of these changed nothing on disk
NOT_A_WRITE = re.compile(r"\d*>&\d+|\d*>\s*(/dev/null|\$null)")  # redirects that write nothing


def run_wrote(run_file: Path) -> bool | None:
    """Whether the executor run could have changed files: a Write/Edit tool call, a file_change item, or a command
    that looks like it writes. None when the stream is unreadable."""
    try:
        lines = Path(run_file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item") or {}
        cmds = [item.get("command") or ""] if item.get("type") != "file_change" else [None]
        for b in ((event.get("message") or {}).get("content") or []) if event.get("type") == "assistant" else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                if b.get("name") in WRITE_TOOLS:
                    return True
                cmds.append(str((b.get("input") or {}).get("command") or ""))
        if any(c is None or WRITE_CMD.search(NOT_A_WRITE.sub("", c)) for c in cmds):
            return True
    return False


def run_text(run_file: Path | None) -> str:
    """The executor run stream as text ('' when absent or unreadable)."""
    try:
        return Path(run_file).read_text(encoding="utf-8", errors="replace") if run_file else ""
    except OSError:
        return ""


def git(repo: Path, *args: str) -> str | None:
    """stdout of `git -C repo ...`, None when git fails or is missing."""
    try:
        done = subprocess.run([GIT, "-c", "safe.directory=*", "-C", str(repo), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def merge_check(repo: Path, branch: str) -> str:
    """Trial merge against master without changing any worktree, index or ref."""
    try:
        done = subprocess.run([GIT, "-c", "safe.directory=*", "-C", str(repo), "merge-tree", "--write-tree",
                               "--name-only", "-z", "master", branch], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=20, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return "未检查：超时"
    except (OSError, subprocess.SubprocessError):
        return "未检查：git 不可用"
    if done.returncode == 0:
        return "无冲突"
    if done.returncode == 129:
        return "未检查：git 版本"
    if done.returncode == 1:
        # -z --name-only: tree OID, conflicted paths, empty field, then conflict messages.
        paths = done.stdout.split("\0")[1:]
        paths = paths[:paths.index("")] if "" in paths else []
        if paths:
            return "冲突文件：" + "、".join(paths)
    return f"未检查：git 错误（退出码 {done.returncode}，请核对 master 和分支）"


def test_command(cwd: Path, changed: tuple = ()) -> str:
    """How this project's tests run, found from files in cwd and its direct subfolders; never guessed. `changed` (paths
    relative to cwd): only the subfolders holding a changed file are listed, plus cwd itself when a changed file sits
    in no listed subfolder that has tests; empty or matching nothing lists them all."""
    tests = cwd / "tests"
    root_tests = (tests / "__init__.py").is_file()
    if not root_tests:
        try:
            readme = (cwd / "README.md").read_text(encoding="utf-8")
        except OSError:
            readme = ""
        root = r"(?:repository|repo|project)?\s*root(?:\s+directory)?"
        command = r"python\s+-m\s+unittest"
        root_tests = bool(re.search(rf"{root}.{{0,120}}{command}|{command}.{{0,120}}{root}", readme, re.I | re.S))

    def commands(d: Path) -> list[str]:
        try:
            names = {p.name for p in d.iterdir()}
        except OSError:
            return []
        where = "" if d == cwd or (d == tests and root_tests) else f"cd {d.name}; "
        found = []
        if any(n.startswith("test_") and n.endswith(".py") for n in names):
            found.append(f"{where}python -m unittest")
        elif {"pytest.ini", "tox.ini"} & names:
            found.append(f"{where}pytest")
        if "package.json" in names:
            try:
                scripts = json.loads((d / "package.json").read_text(encoding="utf-8-sig")).get("scripts") or {}
            except (OSError, ValueError):
                scripts = {}
            if "test" in scripts:
                found.append(f"{where}npm test")
        if "Cargo.toml" in names:
            found.append(f"{where}cargo test")
        return found

    subs = sorted(p for p in cwd.iterdir() if p.is_dir() and not p.name.startswith("."))
    rel = [c.replace("\\", "/") for c in changed]
    tops = {c.split("/")[0] for c in rel if "/" in c}
    picked = [p for p in subs if p.name in tops]
    if rel:
        covered = {p.name for p in picked if commands(p)}
        if any(c.split("/")[0] not in covered for c in rel):
            picked = [cwd, *picked]
    else:
        picked = [cwd, *subs]
    found = [c for d in picked for c in commands(d)]
    return "；".join(found[:4]) + "（未跑，请重跑）" if found else "未找到"

def verification_section(path: Path) -> str:
    """The first VERIFY_LINES lines of the first section whose heading contains 验证 (up to the next heading of the same
    or a higher level), or ""."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for i, line in enumerate(lines):
        m = re.match(r"(#+)\s.*验证", line)
        if m:
            end = next((j for j in range(i + 1, len(lines)) if re.match(rf"#{{1,{len(m.group(1))}}}\s", lines[j])), len(lines))
            return "\n".join(lines[i:end][:VERIFY_LINES])
    return ""


def codex_final_message(run_file: Path) -> str:
    """The last agent_message of a `codex exec --json` stream, first CODEX_CHARS characters; "" for other streams."""
    text = ""
    try:
        for line in Path(run_file).read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = (json.loads(line) or {}).get("item") or {}
            except (ValueError, AttributeError):
                continue
            if item.get("type") == "agent_message":
                text = item.get("text") or text
    except OSError:
        return ""
    return " ".join(text.split())[:CODEX_CHARS]


def result_refs(item: str) -> list[str]:
    """The audits/ paths of the row that name its result file: only the first one after `结果写` when the row has one
    (10-09: a background audit file the row merely cites, touched in the window, passed for the result), else every one."""
    after = RESULT_RE.findall(item.partition("结果写")[2])[:1]
    return after or RESULT_RE.findall(item)


def result_files(item: str, audits: Path, start: datetime, until: datetime) -> tuple[list[str], list[str]]:
    """(named, fresh): the audits/ paths the row names (`<日期>` style placeholders become wildcards) that exist now,
    and every audits/*.md created or modified inside [start, until]. Timestamps are naive local, like the ledger's."""
    inside = lambda t: start <= datetime.fromtimestamp(t) <= until
    fresh = [p.name for p in sorted(audits.glob("*.md")) if inside(p.stat().st_mtime) or inside(p.stat().st_ctime)]
    named = []
    for ref in result_refs(item):
        # `<日期>` and a bare `xx` (audits/2026-11-xx-name.md, 11-03: 43 intel rows looked result-less) are wildcards
        pattern = re.sub(r"<[^>]*>|(?<![A-Za-z])xx(?![A-Za-z])", "*", re.split(r"[/\\]", ref, maxsplit=1)[1])
        named += [p.name for p in audits.glob(pattern) if p.name in fresh]
    return sorted(set(named)), fresh


def build(item: str, cwd: Path, audits: Path, start: datetime, until: datetime, umba: bool = False,
          run_file: Path | None = None) -> dict:
    """{"text": the packet for the top of the review prompt, "hard": None | reason the row goes back as rework,
    "facts": the raw numbers}. start/until: the executor run's start and now (review start), naive local datetimes."""
    root = (git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    repo = Path(root) if root else None
    named = [b for b in dict.fromkeys(BRANCH_RE.findall(item)) if not b.endswith("-")]
    row_repos = []
    if named:
        # Life's poetry rows use bare C:\...\wt\poetry-mask paths; spaces still require backticks.
        bare_paths = re.findall(r"(?<!\w)[A-Za-z]:[\\/][^\s`'\"，。；、（）()]+|(?<![\w:/])/(?!/)[^\s`'\"，。；、（）()]+", item)
        for raw in dict.fromkeys([*re.findall(r"`([^`]+)`", item), *bare_paths]):
            path = Path(raw)
            path_root = git(path, "rev-parse", "--show-toplevel") if path.is_absolute() else None
            if path_root and Path(path_root.strip()) not in row_repos:
                row_repos.append(Path(path_root.strip()))
        for candidate in row_repos:
            refs = git(candidate, "for-each-ref", "refs/heads/claude", "--format=%(refname:short)")
            if refs and any(branch in refs.splitlines() for branch in named):
                repo = candidate
                break
        else:
            if row_repos:
                repo = row_repos[0]
    lines, facts = [], {"repo": str(repo) if repo else None}
    stamp = start.strftime("%Y-%m-%d %H:%M:%S")
    until_s = until.strftime("%Y-%m-%d %H:%M:%S")

    commits = None
    if repo:
        out = git(repo, "log", "--all", f"--since={stamp}", f"--until={until_s}", "--format=%h %s")
        commits = None if out is None else out.splitlines()
    facts["commits"] = None if commits is None else len(commits)

    base = None
    if repo:
        base = next((b for b in ("main", "master") if git(repo, "rev-parse", "--verify", "-q", b)), None)
        common_dir = git(repo, "rev-parse", "--git-common-dir")
        if common_dir:
            common_dir = Path(common_dir.strip())
            main_repo = (common_dir if common_dir.is_absolute() else repo / common_dir).resolve().parent
            facts["main_repo"] = str(main_repo)  # stable identity even when repo itself is an isolated worktree
            if not base and main_repo.name == "poetry-mask":
                # This repo has only claude/* heads. Freeze the full branch since its sole root, never guess a topic tip
                # as the baseline (which could hide the task's own changes). No default branch is created or merged.
                roots = (git(repo, "rev-list", "--max-parents=0", "--all") or "").splitlines()
                if len(roots) == 1:
                    base = roots[0]
                    lines.append(f"仓库无 main/master；以唯一初始提交 {base} 冻结整条分支（范围较大，非合并目标）")
            main_branch = (git(main_repo, "branch", "--show-current") or "").strip()
            if main_branch and main_branch not in ("main", "master"):
                lines.append(f"⚠ 仓库主目录停在 {main_branch}，不在默认分支")
    refs = git(repo, "for-each-ref", "refs/heads/claude", "--format=%(refname:short)|%(committerdate:unix)") if repo else None
    tips = {n: int(t) for n, _, t in (r.partition("|") for r in (refs or "").splitlines())}
    own = run_text(run_file)  # only branches this run's own record mentions: other executors' commits in the window are not its work
    recent = [n for n, t in tips.items() if start <= datetime.fromtimestamp(t) <= until and n in own]
    branches = [b for b in named if b in tips] if named else recent
    if named:
        missing = [b for b in named if b not in tips]
        if missing:
            where = f"{repo} 仓库" if repo else "项目仓库"
            lines.append(f"行里写的分支在 {where}没找到：" + "、".join(missing))
        lines.append("引用的分支：" + "、".join(f"{b}{'' if b in tips else '（不存在）'}" for b in named))
    elif recent:
        lines.append("行里没写分支；执行窗口内有提交的 claude/* 分支：" + "、".join(recent[:5]))
    else:
        lines.append("分支：行里没写，执行窗口内也没有 claude/* 分支更新" if repo else "分支：项目目录不在 git 仓库里")
    for b in named:
        status = merge_check(repo, b) if repo and b in tips else "未检查：分支不存在" if repo else "未检查：项目目录不在 git 仓库里"
        lines.append(f"{b} 和当前 master 试合并：{status}")
    changed, pinned = [], {}
    base_sha = (git(repo, "rev-parse", base) or "").strip() if base else None
    for b in branches[:3]:
        tip = (git(repo, "rev-parse", b) or "").strip()
        if tip:
            pinned[b] = tip
        ahead = git(repo, "rev-list", "--count", f"{base_sha}..{tip}") if base_sha and tip else None
        names = git(repo, "diff", "--no-renames", "--name-only", "-z", f"{base_sha}...{tip}") if base_sha and tip else None
        names = [n for n in names.split("\0") if n] if names else []
        changed += names
        ahead = (ahead or "?").strip()
        lines.append(f"  {b} 比 {base} 多 {ahead} 个提交，改 {len(names)} 个文件" + ("（已并入或没有新提交）" if ahead == "0" else ""))
    facts["refs"] = pinned
    facts["base"] = base_sha
    facts["changed"] = sorted(set(changed))
    if changed:
        shown = sorted(set(changed))
        lines.append("改动文件：" + "、".join(shown[:SHOW_FILES]) + (f" 等 {len(shown)} 个" if len(shown) > SHOW_FILES else ""))
        risky = [f for f in shown if PROTECTED.search(f) or (umba and re.search(r"(^|/)data/", f))]
        lines.append("⚠ 碰了敏感路径：" + "、".join(risky) if risky else "敏感路径（records、input/data、discord/data、凭据）：未碰")
        facts["risky"] = risky
    window = f"{start:%m-%d %H:%M} 起"
    lines.append(f"执行窗口（{window}）内仓库提交：" + ("未知" if commits is None else f"{len(commits)} 个"
                 + (f"，最新 {commits[0]}" if commits else "")))
    test_root, prefix = cwd, ""
    if repo:
        try:
            relative = Path(cwd).resolve().relative_to(repo.resolve()).as_posix()
            prefix = "" if relative == "." else relative + "/"
        except ValueError:
            test_root = repo
    test_files = tuple(f[len(prefix):] for f in changed if not prefix or f.startswith(prefix))
    lines.append("测试命令：" + test_command(test_root, test_files))

    named_files, fresh = result_files(item, audits, start, until)
    if named_files:
        lines.append("结果文件：" + "、".join(named_files) + "（窗口内有更新）")
        section = verification_section(audits / named_files[0])
        if section:
            lines.append(f"结果文件“验证”一节（前 {VERIFY_LINES} 行）：\n" + section)
    else:
        lines.append("结果文件：行里点名的未找到" + (f"；窗口内 audits 有 {len(fresh)} 个别的文件" if fresh else "；窗口内 audits 没有任何更新"))
    facts["result_file"], facts["named"] = bool(named_files), bool(result_refs(item))
    facts["result_files"] = named_files

    hard = None
    wrote = run_wrote(run_file) if run_file else None
    facts["wrote"] = wrote
    final = codex_final_message(run_file) if run_file else ""
    if final:
        lines.append(f"Codex 最后一条回复（前 {CODEX_CHARS} 字）：{final}")
    if commits == [] and wrote is False and not named_files and (facts["named"] or not fresh):
        hard = f"自动返工：执行窗口（{window}）内项目仓库没有新提交，行里点名的结果文件不存在，执行者的运行记录里也没有任何写文件的操作，没有开工"
    facts["hard"] = hard
    return {"text": "【审查前材料（脚本自动整理，只列事实，结论由你下）】\n" + "\n".join(lines) + "\n\n", "hard": hard, "facts": facts}


# Explicit offline collection is deliberately separate from build()/tick's review policy.
def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative_path(value: str) -> Path:
    # Reject Windows traversal even when validating on POSIX (and vice versa).
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError(f"invalid relative path: {value!r}")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"path escapes snapshot: {value!r}")
    return path


def export_snapshot(repo: Path, commit: str, target: Path) -> list[str]:
    """Export pinned regular files, without hooks, raw data, credentials, symlinks or submodules.

    stack_trial's archive approach, with a generic file list instead of its Incore-only EXPORT.
    No clone/worktree registration or source checkout mutation. This is file isolation, not an OS sandbox.
    """
    tree = git(repo, "ls-tree", "-rz", "--full-tree", commit)
    if tree is None:
        raise ValueError("cannot list pinned tree")
    names, excluded = [], []
    for entry in tree.split("\0"):
        if not entry:
            continue
        meta, name = entry.split("\t", 1)
        relative_path(name)
        if (meta.split()[0] not in ("100644", "100755") or PROTECTED.search(name)
                or name == "projects/backlog.md"):
            excluded.append(name)
        else:
            names.append(name)
    target.mkdir(parents=True)
    # Bounded argument length also works with Windows CreateProcess's command-line limit.
    while names:
        batch, size = [], 0
        while names and size + len(names[0]) < 8000:
            name = names.pop(0)
            batch.append(name)
            size += len(name) + 3
        if not batch:
            raise ValueError("archive path exceeds command-line limit")
        archive = target.parent / "snapshot.tar"
        subprocess.run([GIT, "-c", "safe.directory=*", "--literal-pathspecs", "-C", str(repo),
                        "archive", "--format=tar", "-o", str(archive), commit, "--", *batch],
                       check=True, capture_output=True, timeout=60, creationflags=NO_WINDOW)
        with tarfile.open(archive) as tar:
            for member in tar:
                path = target / relative_path(member.name)
                if member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as src, path.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                elif not member.isdir():
                    raise ValueError(f"unsupported archive entry: {member.name}")
        archive.unlink()
    return excluded


def test_counts(output: str, parser: str) -> dict:
    """Only supported, unambiguous terminal summaries become counts. Unknown is never zero passed."""
    counts = dict.fromkeys(("passed", "skipped", "failed", "errors", "expected_failures", "unexpected_successes"))
    output = re.sub(r"\x1b\[[0-9;]*m", "", output)
    if parser == "unittest":
        summaries = re.findall(r"^Ran (\d+) tests? in [^\n]+\n\s*\n(OK|FAILED|NO TESTS RAN)([^\n]*)$", output, re.M)
        if len(summaries) == 1:
            total, status, details = summaries[0]
            found = {k.strip(): n for k, n in re.findall(r"([a-z ]+)=(\d+)", details)}
            counts = {k: int(found.get(k.replace("_", " "), 0)) for k in counts}
            counts["failed"] = int(found.get("failures", 0))
            counts["passed"] = int(total) - sum(v for k, v in counts.items() if k != "passed")
            # Subtest outcomes are not test-case counts; do not infer how many cases passed.
            if counts["passed"] < 0 or re.search(r"^(?:FAIL|ERROR): .*\(.*=.*\)", output, re.M):
                counts["passed"] = None
            if status == "FAILED" and not any(counts[k] for k in ("failed", "errors", "unexpected_successes")):
                counts["passed"] = None
    elif parser == "pytest":
        summaries = [line for line in output.splitlines() if re.search(r"\d+ (?:passed|failed|skipped|error|xfailed|xpassed).* in \d", line)]
        if len(summaries) == 1:
            found = dict((k, int(n)) for n, k in re.findall(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)\b", summaries[0]))
            counts = {"passed": found.get("passed", 0), "skipped": found.get("skipped", 0),
                      "failed": found.get("failed", 0), "errors": found.get("error", found.get("errors", 0)),
                      "expected_failures": found.get("xfailed", 0), "unexpected_successes": found.get("xpassed", 0)}
    return counts


def interpreter_identity(interpreter: str) -> dict:
    path = Path(interpreter).absolute()  # preserve the venv's entrypoint, even if it is a symlink
    if not Path(interpreter).is_absolute() or not path.is_file():
        raise ValueError("interpreter must be an existing absolute Python executable")
    probe = subprocess.run([str(path), "-I", "-c",
                            "import sys,json,platform,importlib.metadata as m; "
                            "print(json.dumps([sys.version,sys.prefix,platform.platform(),"
                            "sorted((d.metadata['Name'],d.version) for d in m.distributions())]))"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=True,
                           creationflags=NO_WINDOW)
    return {"path": str(path), "executable_sha256": file_hash(path), "environment": json.loads(probe.stdout)}


def rerun_test(repo: Path, commit: str, command: dict, identity: dict, artifacts: list,
               output: Path, force: bool = False) -> dict:
    cache_key = fingerprint({"version": 1, "repo": str(repo), "commit": commit, "command": command,
                             "interpreter": identity, "artifacts": artifacts})
    receipt_path = output / "receipts" / (cache_key + ".json")
    if receipt_path.is_file() and not force:
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if (receipt["cache_key"] == cache_key
                    and all(file_hash(Path(receipt[k])) == receipt[k + "_sha256"] for k in ("stdout", "stderr"))):
                return {**receipt, "cached": True}
        except (OSError, ValueError, KeyError, TypeError):
            pass  # incomplete/tampered output is not a reusable receipt
    run_dir = Path(tempfile.mkdtemp(prefix=cache_key[:12] + "-", dir=output / "runs"))
    snapshot = run_dir / "snapshot"
    excluded = export_snapshot(repo, commit, snapshot)
    cwd = snapshot / relative_path(command["cwd"])
    if not cwd.is_dir():
        raise ValueError(f"test cwd missing in pinned snapshot: {command['cwd']}")
    argv = [identity["path"], *command["args"]]
    started, start_time = datetime.now().astimezone().isoformat(), time.monotonic()
    stdout, stderr = run_dir / "stdout.txt", run_dir / "stderr.txt"
    code, error = None, None
    # Files avoid memory growth; no shell, no command extracted from task text or result files.
    with stdout.open("w", encoding="utf-8") as out, stderr.open("w", encoding="utf-8") as err:
        try:
            proc = subprocess.run(argv, cwd=cwd, stdout=out, stderr=err, timeout=command["timeout"],
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                                  creationflags=NO_WINDOW)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            error = "timeout"
        except OSError as exc:
            error = str(exc)
    counts = test_counts(stdout.read_text(encoding="utf-8", errors="replace") + "\n" +
                         stderr.read_text(encoding="utf-8", errors="replace"), command["parser"])
    receipt = {"cache_key": cache_key, "commit": commit, "command": command, "interpreter": identity,
               "cached": False, "started": started, "elapsed_s": round(time.monotonic() - start_time, 3),
               "exit_code": code, "error": error, "counts": counts, "snapshot": str(snapshot),
               "excluded": excluded, "stdout": str(stdout), "stderr": str(stderr),
               "stdout_sha256": file_hash(stdout), "stderr_sha256": file_hash(stderr)}
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def collect_manifest(manifest_path: Path, output: Path, commands_path: Path | None = None,
                     force: bool = False) -> dict:
    """Reviewer-invoked facts only. Manifest tests are IDs; only the separate command file can execute code."""
    manifest_path, output = Path(manifest_path).absolute(), Path(output).absolute()
    facts = {"errors": [], "warnings": [], "results": [], "tests": [], "manual_review_required": True}
    output_ready = False
    errors, warnings = facts["errors"], facts["warnings"]
    try:
        for path in (manifest_path, output, commands_path):
            if path and (PROTECTED.search(Path(path).as_posix()) or PROTECTED.search(Path(path).resolve().as_posix())):
                raise ValueError("manifest, commands and output must not use protected paths")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("version") != 1 or manifest.get("kind") not in ("code", "docs", "dotz"):
            raise ValueError("manifest requires version=1 and kind=code/docs/dotz")
        for field in ("repository", "baseline", "commit", "results", "tests"):
            if field not in manifest:
                raise ValueError(f"manifest missing field: {field}")
        if not isinstance(manifest["results"], list) or not manifest["results"]:
            raise ValueError("manifest requires explicit result paths")
        tests = manifest["tests"]
        if (not isinstance(tests, list) or any(not isinstance(t, str) or not t for t in tests)
                or len(tests) != len(set(tests))):
            raise ValueError("tests must be unique reviewer command IDs")
        if not tests and (manifest["kind"] == "code" or not manifest.get("tests_reason")):
            raise ValueError("code requires tests; docs/dotz without tests require tests_reason")
        facts["kind"], facts["tests_reason"] = manifest["kind"], manifest.get("tests_reason")
        repo = Path(manifest["repository"]).absolute() if manifest["repository"] else None
        if repo:
            if not Path(manifest["repository"]).is_absolute():
                raise ValueError("repository must be absolute")
            root = git(repo, "rev-parse", "--show-toplevel")
            if not root or Path(root.strip()).resolve() != repo.resolve():
                raise ValueError(f"repository is missing or is not its git root: {repo}")
            for field in ("baseline", "commit"):
                sha = manifest[field]
                if not isinstance(sha, str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", sha):
                    raise ValueError(f"{field} must be a full immutable commit ID")
                if git(repo, "rev-parse", "--verify", sha + "^{commit}") is None:
                    raise ValueError(f"{field} commit is missing: {sha}")
            if git(repo, "merge-base", "--is-ancestor", manifest["baseline"], manifest["commit"]) is None:
                raise ValueError("baseline is not an ancestor of commit")
            changed = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
                          manifest["baseline"], manifest["commit"])
            if changed is None:
                raise ValueError("cannot compare baseline and commit")
            facts["changed"] = [p for p in changed.split("\0") if p]
            facts["risky"] = [p for p in facts["changed"] if PROTECTED.search(p)]
        elif manifest["kind"] == "code" or tests or manifest["baseline"] is not None or manifest["commit"] is not None:
            raise ValueError("code/tests require a repository; unversioned docs/dotz must use null baseline/commit")
        facts.update(repository=str(repo) if repo else None, baseline=manifest["baseline"], commit=manifest["commit"])
        # Output must not overwrite a source artifact or existing source tree.
        if repo and (output.resolve() == repo.resolve() or output.resolve() in repo.resolve().parents):
            raise ValueError("output must not contain the source repository")
        if output.resolve() in manifest_path.resolve().parents:
            raise ValueError("output must not contain the source manifest")
        if commands_path and output.resolve() in Path(commands_path).resolve().parents:
            raise ValueError("output must not contain the reviewer command file")
        if output.exists() and any(output.iterdir()) and not (output / "manifest.json").is_file():
            raise ValueError("output must be a dedicated empty or previous packet directory")
        sources = []
        for i, result in enumerate(manifest["results"]):
            raw = result["path"]
            source = Path(raw)
            source = source if source.is_absolute() else manifest_path.parent / relative_path(raw)
            if (PROTECTED.search(source.as_posix()) or PROTECTED.search(source.resolve().as_posix())
                    or source.is_symlink() or not source.is_file()):
                errors.append(f"result missing or protected: {raw}")
                continue
            if output.resolve() == source.resolve() or output.resolve() in source.resolve().parents:
                raise ValueError("output must not contain source results")
            data = source.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if result.get("sha256") and digest != result["sha256"]:
                errors.append(f"result hash mismatch: {raw}")
            facts["results"].append({"source": str(source.absolute()), "sha256": digest, "bytes": len(data),
                                     "frozen": f"results/{i}/{source.name}"})
            sources.append((facts["results"][-1]["frozen"], data))
        # Freeze results before any reviewer command runs. Validation runs even on a cache hit.
        output.mkdir(parents=True, exist_ok=True)
        output_ready = True
        for name, data in sources:
            dest = output / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        facts["source_checks"] = [{"slot": i, "source": None, "checked": False} for i in range(1, 6)] if manifest["kind"] == "dotz" else []
        commands = json.loads(Path(commands_path).read_text(encoding="utf-8")) if commands_path else {}
        if errors:
            warnings.append("tests not run because delivery validation failed")
            facts["tests"] = [{"id": t, "exit_code": None, "error": "not run: delivery validation failed"} for t in tests]
        else:
            (output / "runs").mkdir(exist_ok=True)
            (output / "receipts").mkdir(exist_ok=True)
            for test_id in tests:
                try:
                    if test_id not in commands:
                        raise ValueError("not run: no reviewer command supplied")
                    cmd = commands[test_id]
                    args = cmd["args"]
                    if not isinstance(args, list) or not args or any(not isinstance(a, str) for a in args):
                        raise ValueError("args must be a nonempty argument array, never a shell string")
                    relative_path(cmd["cwd"])
                    if cmd["parser"] not in ("unittest", "pytest", "none") or not 0 < cmd["timeout"] <= 600:
                        raise ValueError("parser must be unittest/pytest/none; timeout must be >0 and <=600 seconds")
                    identity = interpreter_identity(cmd["interpreter"])
                    receipt = rerun_test(repo, manifest["commit"], cmd, identity, facts["results"], output, force)
                    facts["tests"].append({"id": test_id, **receipt})
                    if receipt["exit_code"] != 0:
                        errors.append(f"{test_id}: exit_code={receipt['exit_code']}, error={receipt['error']}")
                    if receipt["counts"]["passed"] is None:
                        warnings.append(f"{test_id}: test counts unknown")
                    if receipt["counts"]["skipped"]:
                        warnings.append(f"{test_id}: skipped={receipt['counts']['skipped']}")
                    if receipt["counts"]["passed"] == 0 and not any(receipt["counts"].values()):
                        warnings.append(f"{test_id}: no tests executed")
                except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                    facts["tests"].append({"id": test_id, "error": str(exc), "exit_code": None})
                    errors.append(f"{test_id}: {exc}")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        errors.append(str(exc))
    lines = [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in facts.items()]
    return {"text": "【显式离线取证：只列事实，结论由审查员下】\n" + "\n".join(lines),
            "hard": None, "facts": facts, "output_ready": output_ready}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Collect an explicit delivery; never decide a review verdict.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--commands", type=Path, help="separate reviewer-authored JSON command map; absent means do not run")
    parser.add_argument("--force", action="store_true", help="ignore cached test receipts")
    args = parser.parse_args(argv)
    packet = collect_manifest(args.manifest, args.output, args.commands, args.force)
    path = args.output / "packet.json"
    if packet["output_ready"]:
        path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(packet["text"])
    if packet["output_ready"]:
        print(f"packet: {path.absolute()}")
    return int(bool(packet["facts"]["errors"] or packet["facts"]["warnings"]))


if __name__ == "__main__":
    sys.exit(main())
