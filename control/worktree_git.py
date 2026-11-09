"""Check worktree ownership before git: python control/worktree_git.py <repo> status --short

Also accepts merge-tree and its arguments. Cross-side worktrees fail before git starts,
with a PowerShell command for the creating side; .git metadata is never rewritten.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

from review_packet import CONFIG_GIT as WINDOWS_GIT, GIT


def worktree_side(repo: Path) -> str | None:
    """Absolute gitdir paths identify the creator. Main repos/relative gitdirs use native git."""
    dotgit = Path(repo) / ".git"
    if not dotgit.is_file():
        return None
    text = dotgit.read_text(encoding="utf-8-sig").strip()
    if not text.startswith("gitdir: ") or not text[8:].strip():
        raise ValueError(f"无法识别工作树的 .git 文件：{dotgit}")
    gitdir = text[8:].strip()
    if re.match(r"^[A-Za-z]:[/\\]|^\\\\|^//", gitdir):
        return "windows"
    return "wsl" if gitdir.startswith("/") else None


def side_git(repo: Path) -> list[str]:
    """Recommended invocation from Windows: git.exe for Windows, wsl -e git for WSL."""
    side = worktree_side(repo)
    return [WINDOWS_GIT] if side == "windows" else ["wsl", "-e", "git"] if side == "wsl" else [GIT]


def git_command(repo: Path, *args: str, native_git: str = GIT) -> list[str]:
    """Native command, or an actionable error before the wrong git can touch a worktree."""
    side = worktree_side(repo)
    current = "windows" if sys.platform == "win32" else "wsl"
    if side and side != current:
        path = str(Path(repo).resolve()).replace("\\", "/")
        if side == "wsl" and re.match(r"^[A-Za-z]:/", path):
            path = f"/mnt/{path[0].lower()}/{path[3:]}"
        elif side == "windows" and (m := re.match(r"^/mnt/([a-z])/(.*)", path, re.I)):
            path = f"{m[1].upper()}:/{m[2]}"
        command = side_git(repo) + ["-c", "safe.directory=*", "-C", path, *args]
        quoted = " ".join("'" + a.replace("'", "''") + "'" for a in command)
        label = "WSL" if side == "wsl" else "Windows"
        raise ValueError(f"工作树由 {label} 创建（{Path(repo) / '.git'} 的 gitdir）；当前 git 在另一侧，已停止。"
                         f"请在 Windows PowerShell 用创建侧的 git 执行：\n& {quoted}")
    return [native_git, "-c", "safe.directory=*", "-C", str(repo), *args]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("command", choices=("status", "merge-tree"), nargs="?", default="status")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        command = git_command(args.repo, args.command, *args.args)
        return subprocess.run(command).returncode
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
