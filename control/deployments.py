"""Registry of products deployed to demo01-server: projects/deployments.json (stdlib only).

After every deployment run:  python control/deployments.py record <product> <commit> [--branch <branch>]
The organization dashboard reads the same file and compares the recorded commit with the branch HEAD.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

ROOT = Path(__file__).resolve().parent.parent  # incore/
DEPLOYMENTS = ROOT / "projects" / "deployments.json"
GIT_FALLBACK = ic.V["git_exe"]
GIT_TIMEOUT_SECONDS = 5


def load(path: Path = DEPLOYMENTS) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8-sig")).get("products", [])


def save(products: list[dict], path: Path = DEPLOYMENTS) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"products": products}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def repo_path(entry: dict, root: Path = ROOT) -> Path | None:
    """Repo paths are relative to the workspace folder that holds incore/ (absolute paths also work)."""
    return root.parent / entry["repo"] if entry.get("repo") else None


def git(repo: Path, *args: str) -> str | None:
    """stdout of `git -C repo ...`, or None when git is missing, the repo/ref is unknown or git is slow."""
    exe = shutil.which("git") or (GIT_FALLBACK if os.path.exists(GIT_FALLBACK) else None)
    if not exe or not repo.is_dir():
        return None
    try:
        done = subprocess.run([exe, "-c", "safe.directory=*", "-C", str(repo), *args], capture_output=True, text=True,
                              encoding="utf-8", timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def now_eastern() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds")
    except Exception:  # no tz database (Windows without tzdata): local time with its own offset
        return datetime.now().astimezone().isoformat(timespec="seconds")


def record(name: str, commit: str, branch: str | None = None, path: Path = DEPLOYMENTS, root: Path = ROOT) -> dict:
    products = load(path)
    hits = [p for p in products if name.lower() == p["name"].lower()] or \
           [p for p in products if name.lower() in p["name"].lower()]
    if len(hits) != 1:
        raise SystemExit(f"product {name!r} matches {len(hits)} entries; known: {', '.join(p['name'] for p in products)}")
    entry = hits[0]
    repo = repo_path(entry, root)
    full = git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}") if repo else None
    if repo and not full:
        raise SystemExit(f"commit {commit!r} not found in {repo}")
    entry["commit"] = (full or commit)[:12]
    if branch:
        entry["branch"] = branch
    entry["deployed_at"] = now_eastern()
    save(products, path)
    return entry


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="update one product after a deployment")
    rec.add_argument("product")
    rec.add_argument("commit")
    rec.add_argument("--branch")
    args = parser.parse_args()
    done = record(args.product, args.commit, args.branch)
    print(f"{done['name']}: {done.get('branch') or '-'} {done['commit']} deployed {done['deployed_at']}")
