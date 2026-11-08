"""Pull the Arbitros website's data (SQLite + uploads) from the server to this machine; one step of the backup watcher.

The server keeps one copy of the subscriber list and the articles. Every ARBITROS_EVERY_S (when due) this step
  1. runs SQLite's online backup API inside the web container (the host has no sqlite3) and streams the consistent copy
     back over ssh; it is verified (sha256 from the server, then `PRAGMA quick_check`) and kept as a new copy when its
     content changed or the newest copy is older than a day; the newest KEEP copies stay;
  2. mirrors the uploads and workspace-files folders: only files whose size or modification time differ from what was pulled before; nothing
     is deleted locally when a file disappears on the server.
Everything lands under data/arbitros-backup/. A failure never raises: it is kept in state.json and shown as
`arbitros_error` in backups/status.json (tick.backup_faults turns it into a Discord alert); a success clears it.
No credentials are read here: ssh uses the owner's own ~/.ssh config (host alias SERVER) in BatchMode, and the app's
env file is never touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- CONFIG ----
SERVER = "demo01-server"                                  # ssh host alias in the owner's ~/.ssh/config
CONTAINER = "arbitros-server-web-1"
REMOTE_DB = "/data/arbitros.sqlite3"                      # inside the container
REMOTE_UPLOADS = "/home\x2fwsluser/apps/arbitros-server/data/uploads"  # on the host (bind mount of the container's /data/uploads)
REMOTE_WORKSPACE = "/home\x2fwsluser/apps/arbitros-server/data/workspace-files"  # private attachments of the owner workspace
KEEP = 14                                                 # database copies kept
EVERY_S = 3600                                            # attempt this often
RETRY_S = 600                                             # after a failed attempt
DAILY_COPY_S = 24 * 3600                                  # an unchanged database still gets a copy this often
STALE_S = 26 * 3600                                       # no success for this long is itself an error
SSH_TIMEOUT_S = 120
MAX_UPLOAD_BYTES = 200 * 1024 * 1024                      # one upload larger than this is reported, not pulled
SSH = shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
# ----------------

# Runs in the container (python on stdin): consistent copy via the backup API, then "<sha256 hex>\n" + the bytes.
REMOTE_SCRIPT = f"""
import hashlib, os, sqlite3, sys, tempfile
fd, tmp = tempfile.mkstemp(suffix=".sqlite3"); os.close(fd)
try:
    src = sqlite3.connect("file:{REMOTE_DB}?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(tmp)
    src.backup(dst)
    dst.close(); src.close()
    data = open(tmp, "rb").read()
finally:
    os.path.exists(tmp) and os.remove(tmp)
sys.stdout.buffer.write(hashlib.sha256(data).hexdigest().encode() + b"\\n" + data)
"""


class PullError(Exception):
    pass


def _ssh(command: str, *, stdin: bytes | None = None) -> bytes:
    try:
        done = subprocess.run([SSH, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", SERVER, command], input=stdin,
                              capture_output=True, timeout=SSH_TIMEOUT_S,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as error:
        raise PullError(f"ssh {SERVER}: {type(error).__name__}") from error
    if done.returncode != 0:
        raise PullError(f"ssh {SERVER} exit {done.returncode}: {done.stderr.decode('utf-8', 'replace').strip()[-200:]}")
    return done.stdout


def _logical_hash(path: Path) -> str:
    """Hash of the database's content (SQL dump), so a byte-different but identical copy is not kept again."""
    digest = hashlib.sha256()
    with closing(sqlite3.connect(path.as_uri() + "?immutable=1", uri=True)) as db:
        for line in db.iterdump():
            digest.update(line.encode("utf-8"))
    return digest.hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def pull_database(folder: Path, state: dict, now: datetime) -> str:
    """Fetch, verify and (when new) keep a database copy. Returns "saved", "unchanged" or "unchanged-daily-skip"."""
    out = _ssh(f"docker exec -i {shlex.quote(CONTAINER)} python -", stdin=REMOTE_SCRIPT.encode())
    claimed, _, data = out.partition(b"\n")
    if hashlib.sha256(data).hexdigest().encode() != claimed or not data:
        raise PullError("database copy failed its sha256 check (truncated or empty stream)")
    db_dir = folder / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    temp = db_dir / f".incoming-{uuid.uuid4().hex}.tmp"
    try:
        temp.write_bytes(data)
        with closing(sqlite3.connect(temp.as_uri() + "?immutable=1", uri=True)) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise PullError("database copy failed PRAGMA quick_check")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "subscribers" not in tables:
            raise PullError("database copy has no subscribers table (wrong file?)")
        logical = _logical_hash(temp)
        last = state.get("last_copy") or {}
        fresh = last.get("at") and now - datetime.fromisoformat(last["at"]) < timedelta(seconds=DAILY_COPY_S)
        if last.get("logical") == logical and fresh and (db_dir / last.get("name", "")).is_file():
            return "unchanged"
        name = f"arbitros-{now:%Y%m%dT%H%M%S}Z.sqlite3"
        os.replace(temp, db_dir / name)
        state["last_copy"] = {"name": name, "at": now.isoformat(), "logical": logical, "bytes": len(data)}
    finally:
        temp.unlink(missing_ok=True)
    copies = sorted(db_dir.glob("arbitros-*.sqlite3"))
    for old in copies[:-KEEP]:
        old.unlink()
    return "saved"


def list_uploads(remote: str = REMOTE_UPLOADS) -> list[tuple[str, int, float]]:
    """(relative path, size, mtime) of every file under a server folder; a folder that does not exist (yet) has none."""
    out = _ssh(f"if [ -d {shlex.quote(remote)} ]; then find {shlex.quote(remote)} -type f -printf '%P\\0%s\\0%T@\\0'; fi")
    parts = out.split(b"\0")
    rows = []
    for i in range(0, len(parts) - 2, 3):
        rows.append((parts[i].decode("utf-8", "replace"), int(parts[i + 1]), float(parts[i + 2])))
    return rows


def pull_uploads(folder: Path, state: dict, remote: str = REMOTE_UPLOADS, name: str = "uploads") -> dict:
    """Copy new or changed files of a server folder into folder/<name>. Returns {"files", "pulled"}; a file that vanished
    or changed while being read is left for the next round."""
    mirror = folder / name
    seen = state.setdefault(name, {})
    rows, pulled, skipped = list_uploads(remote), 0, []
    for rel, size, mtime in rows:
        parts = rel.split("/")
        if not rel or rel.startswith("/") or "\\" in rel or any(p in ("", ".", "..") for p in parts):
            skipped.append(f"{rel!r}: unsafe name")
            continue
        if seen.get(rel) == [size, mtime]:
            continue
        if size > MAX_UPLOAD_BYTES:
            skipped.append(f"{rel}: {size} bytes over the limit")
            continue
        target = mirror.joinpath(*parts)
        try:
            data = _ssh(f"cat {shlex.quote(remote + '/' + rel)}")
        except PullError as error:
            skipped.append(f"{rel}: {error}")
            continue
        if len(data) != size:
            skipped.append(f"{rel}: size changed while reading")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
        try:
            temp.write_bytes(data)
            os.utime(temp, (mtime, mtime))
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        seen[rel] = [size, mtime]
        pulled += 1
    if skipped:
        raise PullError(f"{name}: {len(skipped)} not pulled ({'; '.join(skipped[:3])[:200]}); {pulled} pulled")
    return {"files": len(rows), "pulled": pulled}


def _read_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def step(root: Path, now: datetime | None = None, force: bool = False) -> dict:
    """Run the pull when due. Returns fields for status.json: {"arbitros": {...}} and, while unhealthy,
    {"arbitros_error": text}. Never raises."""
    now = now or _utc_now()
    folder = Path(root) / "data" / "arbitros-backup"
    state_path = folder / "state.json"
    state = _read_state(state_path)
    last_try = datetime.fromisoformat(state["tried_at"]) if state.get("tried_at") else None
    wait = RETRY_S if state.get("error") else EVERY_S
    if force or last_try is None or (now - last_try).total_seconds() >= wait:
        state["tried_at"] = now.isoformat()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            db = pull_database(folder, state, now)
            up, errors = {}, []
            for remote, name in ((REMOTE_UPLOADS, "uploads"), (REMOTE_WORKSPACE, "workspace-files")):
                try:  # one folder's trouble must not keep the other from being pulled
                    up[name] = pull_uploads(folder, state, remote, name)
                except PullError as error:
                    errors.append(str(error))
            if errors:
                raise PullError("; ".join(errors))
            state.update(error=None, ok_at=now.isoformat(), result={
                "database": db, **up["uploads"], "workspace_files": up["workspace-files"]["files"],
                "workspace_pulled": up["workspace-files"]["pulled"]})
        except (PullError, OSError, ValueError, sqlite3.Error) as error:
            state["error"] = str(error)[:300]
        try:
            tmp = state_path.with_name(f"state.json.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, state_path)
        except OSError:
            pass
    ok_at = datetime.fromisoformat(state["ok_at"]) if state.get("ok_at") else None
    error = state.get("error")
    if not error and (ok_at is None or (now - ok_at).total_seconds() > STALE_S):
        error = f"no successful Arbitros pull since {state.get('ok_at') or 'ever'}"
    summary = {"ok_at": state.get("ok_at"), "last_copy": (state.get("last_copy") or {}).get("name"),
               **(state.get("result") or {})}
    return {"arbitros": summary, **({"arbitros_error": error} if error else {})}
