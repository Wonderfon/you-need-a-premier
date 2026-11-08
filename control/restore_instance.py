"""Premier restore (block 5): install a `backup.py --restore-zip` output into a NEW instance, then serve an isolated copy.

    python control/restore_instance.py plan  <out-dir> <workspace>          dry run: what goes where, nothing is written
    python control/restore_instance.py apply <out-dir> <workspace> --yes    git bundle -> <workspace>/incore, then the
                                                                            whitelisted files; never overwrites; hash-checked
    python control/restore_instance.py run   <workspace> [port]             dashboard + input desk on 127.0.0.1:<port> (18765)

<out-dir> is what `backup.py --restore-zip <head.zip> --restore-to <out-dir>` wrote (the folder must not be the live tree).
<workspace> is the new instance's workspace; the incore folder lands in <workspace>/incore (the package name the input
desk imports as). Held back on purpose (listed, never installed here): external/* (Claude/Codex settings, memory, skills),
restore/ (the stand-alone rescue copy). Credentials are never in the out-dir; the owner logs in by hand.
`run` starts no scheduler, sends no Discord message or mail, runs no backup and no ASR; every path points into <workspace>.
Standard library plus aiohttp (already an input-desk dependency)."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Out-dir prefixes (and single files) that are installed under <workspace>/incore at the same relative path.
WHITELIST = ("records/", "input/data/", "projects/", "audits/", "meetings/", "review-audio/", "control/data/",
             "discord/data/attachments/", "notify/data/", "data/config-sync/", "data/arbitros-backup/",
             "data/poetry-collection/", "data/intel-pushed.jsonl", "data/owner-replies.json", "data/owner-ratings.json",
             "data/owner-todo-ids.json", "data/review-dismissed.json", "data/machine-health.jsonl")
BUNDLE = "git/incore.bundle"
PROJECT_BUNDLES = "git/projects/"
PORT = 18765


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _git() -> str:
    return shutil.which("git") or r"C:\orghome\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe"


def _run_git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run([_git(), "-c", "safe.directory=*", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if done.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def _tracked(root: Path) -> tuple[set[str], set[str]]:
    """(tracked files, tracked files changed in the working tree) of the repo at root; empty when it is not a repo yet."""
    if not (root / ".git").exists():
        return set(), set()
    return set(_run_git("ls-files", cwd=root).splitlines()), set(_run_git("ls-files", "-m", cwd=root).splitlines())


def plan(out: Path, workspace: Path) -> dict:
    """Every file under <out>, classified: install (new), same (already there, identical), replace (there only as an unmodified
    git checkout copy that differs: the backup is the later state and git keeps the old text), conflict (there, different, not
    a pristine checkout copy: left alone), held (not whitelisted)."""
    out, root = Path(out).resolve(), Path(workspace).resolve() / "incore"
    tracked, modified = _tracked(root)
    if not out.is_dir():
        raise SystemExit(f"{out} is not a restore output folder")
    rows, held, bundles = [], {}, []
    for f in sorted(p for p in out.rglob("*") if p.is_file()):
        rel = f.relative_to(out).as_posix()
        if rel == BUNDLE or rel.startswith(PROJECT_BUNDLES):
            bundles.append(rel)
        elif rel.startswith(WHITELIST):
            target = root / rel
            if not target.exists():
                status = "install"
            else:
                status = ("same" if sha256(target) == sha256(f) else "replace" if rel in tracked and rel not in modified
                          else "conflict")
            rows.append({"path": rel, "target": str(target), "status": status, "size": f.stat().st_size})
        else:
            key = "/".join(rel.split("/")[:2]) if rel.startswith("external/") else rel.split("/")[0]
            held[key] = held.get(key, 0) + 1
    return {"out": str(out), "workspace": str(Path(workspace).resolve()), "root": str(root), "files": rows,
            "bundles": bundles, "held_back": held}


def show(p: dict) -> None:
    counts = {}
    for r in p["files"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"restore plan: {p['out']} -> {p['root']}")
    print("  files:", counts or "none", "| bundles:", p["bundles"] or "none")
    for r in p["files"]:
        print(f"  {r['status']:8} {r['path']}  ->  {r['target']}")
    print("  held back (not installed here):", p["held_back"] or "none")


def _install_code(out: Path, root: Path, rel: str) -> str:
    """Clone the bundle into an empty/new folder; into a folder that already has a repo, only fetch refs under remotes/bundle."""
    bundle = out / rel
    if root.exists() and (root / ".git").exists():
        _run_git("fetch", "--quiet", str(bundle), "refs/heads/*:refs/remotes/bundle/*", cwd=root)
        return "fetched into refs/remotes/bundle (existing repo kept)"
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"{root} exists and is not a git repo: not touching it")
    _run_git("clone", "--quiet", str(bundle), str(root))
    return "cloned " + _run_git("rev-parse", "--short", "HEAD", cwd=root)


def apply(out: Path, workspace: Path) -> dict:
    p = plan(out, workspace)
    out, ws, root = Path(p["out"]), Path(p["workspace"]), Path(p["root"])
    receipt = {"out": p["out"], "root": p["root"], "code": None, "projects": [], "files": [], "held_back": p["held_back"]}
    if BUNDLE in p["bundles"]:
        receipt["code"] = _install_code(out, root, BUNDLE)
        p = plan(out, ws)  # the checkout may already hold some of the whitelisted files
    for rel in (b for b in p["bundles"] if b.startswith(PROJECT_BUNDLES) and b.endswith(".bundle")):
        dest = ws / "restored-git" / Path(rel).stem
        if dest.exists():
            receipt["projects"].append({"bundle": rel, "status": "exists, skipped"})
            continue
        _run_git("clone", "--quiet", str(out / rel), str(dest))
        receipt["projects"].append({"bundle": rel, "status": "cloned", "to": str(dest)})
    for r in p["files"]:
        src, dst = out / r["path"], Path(r["target"])
        if r["status"] not in ("install", "replace"):
            receipt["files"].append({"path": r["path"], "status": r["status"]})  # same: nothing to do; conflict: left alone
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        old = sha256(dst) if r["status"] == "replace" else None
        tmp = dst.with_name(dst.name + ".restoring")
        with open(src, "rb") as s, open(dst if old is None else tmp, "xb") as d:  # "x": a new file is never written over another
            shutil.copyfileobj(s, d)
        if old is not None:  # only an unmodified git checkout copy gets here; `git checkout -- <path>` brings the old text back
            os.replace(tmp, dst)
        ok = sha256(src) == sha256(dst)
        receipt["files"].append({"path": r["path"], "sha256": sha256(dst), **({"old_sha256": old} if old else {}),
                                 "status": ("installed" if old is None else "replaced-checkout-copy") if ok else "HASH MISMATCH"})
        if not ok:
            raise SystemExit(f"hash mismatch after copy: {r['path']}")
    receipt_path = ws / "restore-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=1, ensure_ascii=False), encoding="utf-8")
    receipt["receipt"] = str(receipt_path)
    return receipt


def write_instance(workspace: Path) -> Path:
    """An instance file that moves the whole tree under <workspace>; nothing points at the old user's folders."""
    ws = Path(workspace).resolve()
    path = ws / "instance.json"
    path.write_text(json.dumps({"workspace": str(ws), "user_home": str(ws / "home"), "owner_name": "restored-owner",
                                "owner_aliases": ["restored-owner"], "owner_email": "owner@example.invalid",
                                "sender_email": "sender@example.invalid"}, indent=1), encoding="utf-8")
    return path


def run(workspace: Path, port: int = PORT) -> None:
    ws = Path(workspace).resolve()
    root = ws / "incore"
    os.environ["INCORE_INSTANCE"] = str(write_instance(ws))  # before tick.py is imported: tick.WORKSPACE follows it
    sys.path.insert(0, str(ws))
    sys.path.insert(0, str(root / "control"))
    import importlib
    from aiohttp import web
    server = importlib.import_module("incore.input.server")
    store_mod = importlib.import_module("incore.input.store")

    async def no_worker(app):  # no ASR here: recordings are shown and played, never re-transcribed
        return None

    server.transcribe_queue = no_worker
    store_mod.InputStore.pending_transcriptions = lambda self: []
    (ws / "wt").mkdir(exist_ok=True)
    app = server.create_app(root / "input" / "data", expected_port=port, dashboard_root=root, pending_dir=root / "data",
                            wt_root=ws / "wt")
    web.run_app(app, host="127.0.0.1", port=port, access_log=None)


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] in ("plan", "apply"):
        out, ws = Path(argv[1]), Path(argv[2])
        if argv[0] == "plan":
            show(plan(out, ws))
            return 0
        if "--yes" not in argv:
            show(plan(out, ws))
            print("dry run only; add --yes to install")
            return 2
        print(json.dumps(apply(out, ws), indent=1, ensure_ascii=False))
        return 0
    if len(argv) >= 2 and argv[0] == "run":
        run(Path(argv[1]), int(argv[2]) if len(argv) > 2 else PORT)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
