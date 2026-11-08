"""Versioned local input backups; optionally stage copies in a verified Drive folder.

DriveFS writes are not cloud-delivery receipts. No models, credentials or network
clients are used here. Objects and manifests are immutable; nothing is deleted.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
try:  # machine paths come from control/instance_config.py; the stand-alone restore/backup.py copy has no control/ and keeps the literals
    sys.path.insert(0, str(ROOT / "control"))
    import instance_config as ic
    _V = ic.V
except ImportError:
    _V = {"sessions_dir": r"C:\orghome\.claude\projects\C--orghome-orgcore-workspace",
          "git_exe": r"C:\orghome\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe",
          "user_home": r"C:\orghome", "wsl_home": "/home/wsluser"}
MEMORY_DIR = Path(_V["sessions_dir"]) / "memory"  # outside ROOT
CONFIG_GIT = _V["git_exe"]
GIT = shutil.which("git") or CONFIG_GIT
# Organization state (owner\x2010-06 A): (folder under ROOT, glob filter or None = everything the usual rules allow)
ORG_SOURCES = (("projects", "*.md"), ("projects", "*.json"), ("audits", None), ("data/config-sync", None),
               ("meetings", None), ("review-audio", None), ("control/data/backlog-history/daily", None),
               ("data/arbitros-backup", None),  # owner\x2010-21 A: the site database copy rides the daily ZIP
               ("data/poetry-collection", None),  # owner\x2010-31: his poem ratings, feedback and tags 「保存好，管理好」
               ("discord/data/attachments", None),  # owner\x2011-04 A (Premier G4): the files he sent over Discord, original bytes
               ("notify/data", None))  # Premier G8: send receipts (sent.jsonl), so a restored machine does not resend
ORG_FLAT = (("control/data", ("*.json", "*.jsonl", "*.md")),)  # top level only: ticks/, runs/, *.lock stay out (owner\x2010-13)
# owner\x2010-21: his replies and ratings on the pending page are his own words; keep them like records
ORG_FILES = ("data/intel-pushed.jsonl", "data/owner-replies.json", "data/owner-ratings.json",
             "data/owner-todo-ids.json", "data/review-dismissed.json",  # Premier G1: card numbers and which cards he has dismissed (absent = skipped)
             "input/data/llm-postprocess.json",  # Premier G2: the polish settings (no secrets in it); private-https.json is NOT taken, the owner fills it in
             "data/machine-health.jsonl")  # Premier G8: audit history
THROTTLED = ("control/data/runs.jsonl", "control/data/tick-log.jsonl", "control/data/usage.jsonl", "data/machine-health.jsonl")  # re-stored at most hourly
THROTTLE_S = 3600
EXTERNAL_PREFIX = "external/claude-memory"  # where MEMORY_DIR lands in manifests and restores
HOME_PREFIX = "external/claude-home"
CLAUDE_HOME = Path(_V["user_home"]) / ".claude"
# manifest path -> single file outside ROOT. Only these three names are ever read from there (no credentials sit next to them).
EXTERNAL_FILES = {HOME_PREFIX + "/claude-settings.json.txt": CLAUDE_HOME / "settings.json",  # `settings.json` is in FORBIDDEN
                  HOME_PREFIX + "/CLAUDE.md": CLAUDE_HOME / "CLAUDE.md",
                  HOME_PREFIX + "/AGENTS.md": ROOT.parent / "AGENTS.md"}
LLM_PROMPT_PREFIX = "external/llm-prompt"  # the prompt file input/data/llm-postprocess.json points at (it lives outside ROOT)
LLM_SETTINGS = "input/data/llm-postprocess.json"
SKILLS_PREFIX = "external/claude-skills"
SKILLS_DIR = CLAUDE_HOME / "skills"
WSL_PREFIX = "external/wsl-home"
WSL_HOME = Path(r"\\wsl.localhost\Ubuntu" + _V["wsl_home"].replace("/", "\\"))
# Same hardcoded-map rule as EXTERNAL_FILES; auth.json and .credentials.json are refused even if mapped. Read through the UNC
# share only while the distro is already running (touching the share would start it).
WSL_FILES = {WSL_PREFIX + "/codex-config.toml": WSL_HOME / ".codex" / "config.toml",
             WSL_PREFIX + "/claude-settings.json.txt": WSL_HOME / ".claude" / "settings.json",
             WSL_PREFIX + "/gitconfig": WSL_HOME / ".gitconfig"}
RESTORE_NAMES = {HOME_PREFIX + "/claude-settings.json.txt": HOME_PREFIX + "/settings.json",  # restored under the original name
                 WSL_PREFIX + "/claude-settings.json.txt": WSL_PREFIX + "/settings.json"}
RESTORE_DIR = "restore"  # every ZIP carries these two, changed or not, so a new machine needs no code to start
RESTORE_FILES = {RESTORE_DIR + "/README.md": "RESTORE.md", RESTORE_DIR + "/backup.py": "backup.py"}  # member -> file in backup/
DRIVE_DB_GLOB = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "DriveFS"
DRIVE_RECHECK_S = 300
BUNDLE_PATH = "git/incore.bundle"
PROJECT_BUNDLE_DIR = "git/projects"
PROJECT_PARENTS = ("ClaudeMigration/project files", "projects")  # under ROOT's parent; each child with a .git gets one bundle
BUNDLE_MAX = 300 * 1024 * 1024
CREDENTIAL_FILE = re.compile(r"\.env(\..*)?$|\.pem$", re.I)
CREDENTIAL_OK = re.compile(r"\.(example|sample|template)$", re.I)
# "repo:path" exact pairs the coordinator reviewed on 10-13 as false positives (design tokens, code names, reference keys).
# Only these pass; the same name in another repo, or a new credential-type file, is still refused.
CREDENTIAL_ALLOW = {"ClaudeMigration/project files/technical-asset-handover:src/handover_scan/estimators/tokens.py",
                    "ClaudeMigration/project files/Uvox:Uvox-mainApp/static/v2/tokens.css",
                    "ClaudeMigration/project files/Uvox:Uvox-mainApp/tests/test_citation_keys.py",
                    "ClaudeMigration/project files/Uvox:Uvox-mainApp/tests/test_v2_tokens.py",
                    "projects/arbitrosai-website:server/templates/tpl/key_points.html",
                    "ClaudeMigration/project files/Uvox:Uvox-mainApp/tests/test_local_trial_key.py",
                    "projects/factoryalign:bench/token_columns.py",
                    "projects/factoryalign:decision_data/decouple_out/tokenizer-check.json",
                    "projects/factoryalign:decision_data/frozen/tokenizer-check.json",
                    "projects/factoryalign:decision_data/r3_out/tokenizer-check.json",
                    "projects/factoryalign:decision_data/tokenizer_check.py",
                    "ClaudeMigration/project files/style_rewriter:experiments/expand_v3_retrain_prep/tokenizer_v2.json",
                    "ClaudeMigration/project files/style_rewriter:experiments/expand_v3_retrain_prep/tokenizer_v3.json",
                    "ClaudeMigration/project files/style_rewriter:experiments/four_step/trainsets_tokens.json",
                    "ClaudeMigration/project files/style_rewriter:experiments/poetry_prereg/datagate/tokenizer_manifest.json",
                    "ClaudeMigration/project files/style_rewriter:scripts/token_harness_antislop.py",
                    "ClaudeMigration/project files/style_rewriter:scripts/token_harness_browser.py",
                    "ClaudeMigration/project files/style_rewriter:scripts/token_harness_report.py",
                    "ClaudeMigration/project files/style_rewriter:tests/test_token_harness_antislop.py",
                    "ClaudeMigration/project files/style_rewriter:experiments/blind_read_kit/blind_key.json",
                    "ClaudeMigration/project files/style_rewriter:experiments/blind_read_kit/two_routes/blind_key.json",
                    "ClaudeMigration/project files/style_rewriter:experiments/four_step/restyle_v3_blind/copy_acceptance/key.json",
                    "ClaudeMigration/project files/style_rewriter:experiments/four_step/restyle_v3_blind/demo/key.json",
                    "ClaudeMigration/project files/style_rewriter:experiments/four_step/restyle_v3_blind/ready/key.json"}  # 11-05 Premier 2b: tokenizer configs and token/key-named source; history checked, no secret patterns; the last 5 are blind-read answer keys (committed by the blind-read tool, a6bfb33 ffe9052 8cd9803 b7c52f1 94dd1bb; checked by the coordinator)
# Names of org-state files that are never saved: secrets, tokens, keys (a whole word, so keyboard.png stays), *.dpapi.
# `.env` and every other dot-name are already refused by _allowed(). records/ keeps its older, narrower rules.
SENSITIVE = re.compile(r"secret|token|(?<![a-z])keys?(?![a-z])|apikey|\.dpapi$", re.I)
DEFAULT_TARGET = Path(r"G:\My Drive\Org Core — Private Input Archive")
ANCHOR = "user-records-2026-09-28-startup.zip"
FOLDER_ID = "1-6_KSqsD0xGkUMvkPTXQ4VS3f7WsKwRc"
ARCHIVE_FORMAT = "zip_incremental_v1"
FORBIDDEN = {"secrets", "secret", "credentials", "tokens", "token", "config", "configs",
             "passwords", "credentials.json", "token.json", "access-token.json", "config.json",
             "settings.json", "apikey.txt", "api_key.txt", "logs", "models", "node_modules", "__pycache__"}


def _arbitros_step(root: Path) -> dict:
    import arbitros_pull  # next to this file (the watcher runs it as a script)
    return arbitros_pull.step(root)


ARBITROS_STEP = _arbitros_step  # tests set None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


_SKIP_SUFFIX = (".tmp", ".log", "-wal", "-shm")


def _part_ok(part: str) -> bool:
    name = part.lower()
    return not (name.startswith(".") or name in FORBIDDEN)


def _allowed(path: Path, root: Path) -> bool:
    return all(_part_ok(part) for part in path.relative_to(root).parts) and not path.name.endswith(_SKIP_SUFFIX)


UNREADABLE_LINK = 1920  # WinError 1920: Windows cannot follow a link made inside WSL (a WSL symlink, e.g. from pytest-cases)
unreadable_links: list[str] = []  # this snapshot's skipped links; snapshot_once clears it and puts the count in the receipt


def _skip_unreadable_link(source: Path, error: OSError) -> bool:
    """True (and the path is noted) for a link Windows cannot read. Every other read error is still a failure."""
    if getattr(error, "winerror", None) != UNREADABLE_LINK:
        return False
    unreadable_links.append(str(source))
    return True


def _object_from_busy_ok(source: Path, local: Path) -> dict | None:
    """_object_from for organization files. Executors write audits/ and backlog.md while a snapshot runs: a file that is
    gone, locked, or still changing after 3 tries is left for the next cycle (None) instead of failing the whole
    snapshot, since three failures stop the watcher. Any other error still propagates."""
    for attempt in range(3):
        try:
            return _object_from(source, local)
        except (FileNotFoundError, PermissionError):
            return None
        except OSError as error:
            if _skip_unreadable_link(source, error):
                return None
            raise
        except RuntimeError as error:
            if "changed" not in str(error):
                raise
            time.sleep(0.2)
    return None


def _org_object(source: Path, local: Path, key: str, old: dict, new: dict) -> dict | None:
    """_object_from_busy_ok, but a file whose size and mtime_ns are the ones of the last round (`old`, key -> size,
    mtime_ns, sha256) is not read again: its object is taken on trust if it is still in the store (stat only). The organization
    state is ~1.4 GB, mostly files that never change; records and databases are still hashed every round. `new` collects
    this round's entries."""
    try:
        st = source.stat()
    except OSError:
        return _object_from_busy_ok(source, local)
    hit = old.get(key) or {}
    if (key in THROTTLED and time.time() - hit.get("seen", 0) < THROTTLE_S and isinstance(hit.get("sha256"), str)
            and (local / "objects" / hit["sha256"][:2] / hit["sha256"]).is_file()):
        new[key] = hit  # an append-only ledger changes every minute: keep the last stored copy for an hour, not 6 MB per minute
        return {"sha256": hit["sha256"], "size": hit["size"]}
    if (hit.get("size"), hit.get("mtime_ns")) == (st.st_size, st.st_mtime_ns) and isinstance(hit.get("sha256"), str):
        stored = local / "objects" / hit["sha256"][:2] / hit["sha256"]
        if stored.is_file() and stored.stat().st_size == st.st_size:
            new[key] = hit
            return {"sha256": hit["sha256"], "size": st.st_size}
    saved = _object_from_busy_ok(source, local)
    try:
        after = source.stat()
    except OSError:
        return saved
    if saved is not None and (after.st_size, after.st_mtime_ns) == (st.st_size, st.st_mtime_ns):
        new[key] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": saved["sha256"], "seen": time.time()}
    return saved


def _org_files(root: Path, external: dict[str, Path], external_files: dict[str, Path] | None = None):
    """(manifest path, source) of the organization state: ORG_SOURCES, ORG_FLAT and ORG_FILES under root, plus `external`
    {prefix: folder} and `external_files` {manifest path: file}. Skips sensitive names and paths the archive format could not carry."""
    def usable(path: str, relative: str) -> bool:
        if any(SENSITIVE.search(part) for part in relative.split("/")):
            return False
        try:
            _safe_relative(path)
        except ValueError:
            return False
        return True

    sources = [(root / folder, root, "", pattern) for folder, pattern in ORG_SOURCES]
    sources += [(folder, folder, prefix + "/", None) for prefix, folder in external.items()]
    for directory, base, prefix, pattern in sources:
        for source in _files_below(directory, base):
            relative = source.relative_to(base).as_posix()
            if (pattern and not source.match(pattern)) or not usable(prefix + relative, relative):
                continue
            yield prefix + relative, source
    singles = [root / name for name in ORG_FILES]
    for folder, patterns in ORG_FLAT:
        if (root / folder).is_dir():
            singles += [p for p in sorted((root / folder).iterdir()) if any(p.match(x) for x in patterns)]
    for source in singles:
        relative = source.relative_to(root).as_posix()
        if source.is_file() and not source.is_symlink() and _allowed(source, root) and usable(relative, relative):
            yield relative, source
    for path, source in (external_files or {}).items():
        names = {source.name.lower(), PurePosixPath(path).name.lower()}
        if (source.is_file() and not source.is_symlink() and usable(path, path)
                and not any((n.startswith(".") and n != ".gitconfig") or n == "auth.json" for n in names)  # login files, whatever the map says
                and PurePosixPath(path).name.lower() not in FORBIDDEN):
            yield path, source


def _files_below(directory: Path, root: Path):
    """Files under `directory` that `_allowed` accepts, none of them a link. The walk never follows a linked folder, so one
    resolve of `directory` and one name check per file are enough (a resolve and a pathlib walk per file cost ~60% of a cycle)."""
    if not directory.exists() or not directory.resolve().is_relative_to(root):
        return
    if not all(_part_ok(part) for part in directory.relative_to(root).parts):
        return
    for parent, directories, filenames in os.walk(directory, followlinks=False):
        directories[:] = [name for name in directories
                          if _allowed(Path(parent) / name, root)
                          and not (Path(parent) / name).is_symlink()
                          and not (Path(parent) / name).is_junction()]
        for name in sorted(filenames):
            if _part_ok(name) and not name.endswith(_SKIP_SUFFIX) and not os.path.islink(os.path.join(parent, name)):
                yield Path(parent) / name


def _llm_prompt_files(root: Path) -> dict[str, Path]:
    """{manifest path: file} for the polish prompt named in the settings file, if it is a small readable text file with an ordinary name."""
    try:
        path = Path(str(json.loads((root / LLM_SETTINGS).read_text(encoding="utf-8-sig")).get("prompt_path") or ""))
        if path.is_file() and path.stat().st_size <= 1024 * 1024 and not SENSITIVE.search(path.name):
            return {f"{LLM_PROMPT_PREFIX}/{path.name}": path}
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def _database_paths(root: Path):
    fixed = [root / "discord/data/discord.sqlite3", root / "input/data/input.sqlite3"]
    control = root / "control/data"
    if control.is_dir():
        fixed += sorted(control.glob("*.sqlite3")) + sorted(control.glob("*.db"))
    return [path for path in fixed if path.is_file() and _allowed(path, root)
            and not path.is_symlink() and path.resolve().is_relative_to(root)]


def snapshot_database(source: Path, destination: Path) -> None:
    deadline = time.monotonic() + 20
    def check_deadline(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite backup was busy for more than 20 seconds.")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=2)) as original:
        with closing(sqlite3.connect(destination)) as copied:
            # One step (pages=-1): with 256-page steps, every commit by another connection between two steps restarts the copy,
            # so a writer that commits often (input.sqlite3 during a transcription) keeps it from ever finishing.
            original.backup(copied, pages=-1, progress=check_deadline, sleep=0.1)
            if copied.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite snapshot failed integrity verification.")


def _object_from(source: Path, destination_root: Path) -> dict:
    """Copy once into a content-addressed object; detect concurrent source edits."""
    before = source.stat()
    checksum = sha256(source)
    target = destination_root / "objects" / checksum[:2] / checksum
    if target.is_file():
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("A source file changed during snapshot; retry next cycle.")
        if target.stat().st_size != before.st_size or sha256(target) != checksum:
            raise RuntimeError("Existing backup object failed its checksum; not overwritten.")
        return {"sha256": checksum, "size": before.st_size}
    temp_dir = destination_root / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temporary = temp_dir / uuid.uuid4().hex
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                outgoing.write(block)
                digest.update(block)
                size += len(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("A source file changed during snapshot; retry next cycle.")
        if digest.hexdigest() != checksum:
            raise RuntimeError("A source file changed during copy; retry next cycle.")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.stat().st_size != size or sha256(target) != checksum:
                raise RuntimeError("Existing backup object failed its checksum; not overwritten.")
        else:
            os.replace(temporary, target)
        if sha256(target) != checksum:
            raise RuntimeError("Backup object failed readback verification.")
        return {"sha256": checksum, "size": size}
    finally:
        temporary.unlink(missing_ok=True)


def validate_target(target: Path, root: Path) -> Path:
    """An already-present verified archive is the Drive mount identity anchor."""
    target = Path(target)
    if not target.is_absolute() or not target.is_dir() or target.is_symlink() or target.is_junction():
        raise ValueError("Drive target must be an existing absolute directory; it will not be created.")
    target = target.resolve()
    if target.is_relative_to(root) or root.is_relative_to(target):
        raise ValueError("Drive target cannot contain, or be inside, the incore source.")
    local_anchor = root / "data/backups" / ANCHOR
    remote_anchor = target / ANCHOR
    if not local_anchor.is_file() or not remote_anchor.is_file():
        raise ValueError("Drive identity anchor is missing; folder/account must be verified again.")
    if sha256(local_anchor) != sha256(remote_anchor):
        raise ValueError("Drive identity anchor differs; refusing the target.")
    return target


def _safe_relative(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or name != path.as_posix() or path.is_absolute() or "\\" in name
            or any(part in {".", ".."} or part.endswith((".", " "))
                   or re.search(r'[\x00-\x1f:<>"|?*]', part)
                   or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.I)
                   for part in path.parts)):
        raise ValueError("Unsafe archive path.")
    return name


def _object_path(local: Path, entry: dict) -> Path:
    return local / "objects" / entry["sha256"][:2] / entry["sha256"]


def _member_path(entry: dict) -> str:
    path = entry["path"]
    if path.startswith("input/data/sessions/"):
        session, name = path[len("input/data/sessions/"):].split("/", 1)
        if name.startswith("original."):
            member = f"audio/{session}." + name.split(".", 1)[1]
        elif name.startswith("transcript."):
            member = f"audio/{session}." + name.split(".", 1)[1]
        else:
            member = f"audio/{session}__" + name.replace("/", "__")
    elif entry["kind"] == "sqlite_snapshot":
        member = "databases/" + path.replace("/data/", "-").replace("/", "__")
    elif path.startswith("records/"):
        member = "records/" + path[len("records/"):].replace("/", "__")
    elif path == BUNDLE_PATH or path in ORG_FILES or path.startswith((
            "projects/", "audits/", "data/config-sync/", "data/arbitros-backup/", "data/poetry-collection/", "discord/data/attachments/", "notify/data/", LLM_PROMPT_PREFIX + "/", EXTERNAL_PREFIX + "/", HOME_PREFIX + "/", SKILLS_PREFIX + "/",
            WSL_PREFIX + "/", RESTORE_DIR + "/", PROJECT_BUNDLE_DIR + "/",
            "meetings/", "review-audio/", "control/data/")):
        member = path  # organization state keeps its folder layout inside the ZIP
    else:
        raise ValueError("Unknown backup path category.")
    return _safe_relative(member)


def _zip_files(snapshot: dict, local: Path) -> tuple[list[dict], list[dict]]:
    """Pair audio/text from the already-consistent SQLite snapshot, never live DB."""
    files = {entry["path"]: dict(entry) for entry in snapshot["files"]}
    database = files.get("input/data/input.sqlite3")
    omitted = []
    if database is None:
        return list(files.values()), omitted
    with closing(sqlite3.connect(_object_path(local, database).as_uri() + "?immutable=1", uri=True)) as db, \
            tempfile.TemporaryDirectory(prefix="transcripts-", dir=local) as temporary:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sessions" not in tables:
            return list(files.values()), omitted
        sessions = db.execute("SELECT * FROM sessions WHERE source IN ('audio_capture','audio_upload') ORDER BY id").fetchall()
        for row in sessions:
            session = dict(row)
            sid = str(uuid.UUID(session["id"]))
            base = f"input/data/sessions/{sid}"
            original_path = _safe_relative(base + "/original." + (session["audio_extension"] or "bin"))
            original = files.get(original_path)
            finalized = session["status"] in {"queued", "transcribing", "transcribed", "transcription_failed", "transcription_unavailable"}
            verified = bool(finalized and original and session["original_sha256"]
                            and original["sha256"] == session["original_sha256"]
                            and original["size"] == session["total_bytes"])
            if finalized and session["original_sha256"] and not verified:
                # Preserve local CAS/chunks but do not publish an apparently
                # complete recording whose snapshot hash does not match.
                raise RuntimeError("Finalized audio missing or inconsistent with database snapshot; local chunks retained.")
            if verified:
                removed = [path for path in files if path.startswith(base + "/chunks/")]
                for path in removed:
                    files.pop(path)
                omitted.append({"session_id": sid, "original_path": original_path,
                                "original_sha256": original["sha256"], "chunk_files_omitted": len(removed)})
            revisions = [dict(item) for item in db.execute("SELECT * FROM revisions WHERE session_id=? ORDER BY rowid", (sid,))]
            for revision in revisions:
                if hashlib.sha256(revision["content"].encode("utf-8")).hexdigest() != revision["sha256"]:
                    raise RuntimeError("Revision text does not match its saved hash.")
                revision["metadata"] = json.loads(revision.pop("data_json"))
            latest = next((item for item in reversed(revisions) if item["kind"] != "instruction"), None)
            verifications = [dict(item) for item in db.execute(
                "SELECT * FROM verifications WHERE session_id=? ORDER BY verified_at,id", (sid,)
            )] if "verifications" in tables else []
            transcript = {"schema_version": 2, "session": session,
                          "source": {"database_path": database["path"], "session_id": sid},
                          "original": {"path": original_path, "sha256": session["original_sha256"],
                                       "verified_finalized": verified},
                          "latest_body_revision_id": latest["id"] if latest else None,
                          "transcription_status": "available" if latest else "pending_or_unavailable",
                          "revisions": revisions, "human_verifications": verifications}
            payloads = {"transcript.json": json.dumps(transcript, ensure_ascii=False, indent=2).encode("utf-8"),
                        "transcript.txt": (latest["content"] if latest else "[待转写 / Transcript pending: no text revision is available.]\n").encode("utf-8")}
            for name, data in payloads.items():
                source = Path(temporary) / name
                source.write_bytes(data)
                path = base + "/" + name
                files[path] = {"path": path, "kind": "paired_transcript", **_object_from(source, local)}
    return sorted(files.values(), key=lambda entry: entry["path"]), omitted


def verify_zip(package: Path, expected_sha256: str | None = None) -> dict:
    if expected_sha256 is not None and sha256(package) != expected_sha256:
        raise ValueError("ZIP package SHA-256 mismatch.")
    with zipfile.ZipFile(package) as archive:
        names = []
        for info in archive.infolist():
            _safe_relative(info.filename)
            if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("ZIP directories and symlink entries are not supported.")
            names.append(info.filename)
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("Duplicate or case-conflicting ZIP members.")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("archive_format") != ARCHIVE_FORMAT or manifest.get("package_name") != package.name:
            raise ValueError("ZIP format or package identity mismatch.")
        files = {entry["path"]: entry for entry in manifest["files"]}
        if len(files) != len(manifest["files"]) or len({path.casefold() for path in files}) != len(files):
            raise ValueError("Duplicate or case-conflicting manifest paths.")
        for path, entry in files.items():
            _safe_relative(path)
            if not re.fullmatch("[0-9a-f]{64}", entry["sha256"]) or not isinstance(entry["size"], int) or entry["size"] < 0:
                raise ValueError("Invalid manifest size/hash.")
        included = manifest["included_paths"]
        if len(set(included)) != len(included) or not set(included) <= files.keys():
            raise ValueError("Invalid included paths.")
        members = [entry["member_path"] for entry in files.values()]
        if len({name.casefold() for name in members}) != len(members):
            raise ValueError("Conflicting flat archive names.")
        for name in members:
            _safe_relative(name)
        if set(names) != {"manifest.json", *(files[path]["member_path"] for path in included)}:
            raise ValueError("ZIP contains missing or unlisted files.")
        for path in included:
            entry = files[path]
            info = archive.getinfo(entry["member_path"])
            with archive.open(info) as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            if info.file_size != entry["size"] or checksum != entry["sha256"]:
                raise ValueError("ZIP payload checksum mismatch.")
    return manifest


def _zip_chain(head: Path, expected_sha256: str | None = None) -> list[tuple[Path, dict]]:
    chain, seen = [], set()
    path, checksum = head, expected_sha256
    while True:
        if path.name in seen:
            raise ValueError("ZIP dependency cycle.")
        seen.add(path.name)
        manifest = verify_zip(path, checksum)
        chain.append((path, manifest))
        prior = manifest["previous_package"]
        if prior is None:
            break
        name = _safe_relative(prior["name"])
        if "/" in name or not re.fullmatch("[0-9a-f]{64}", prior["sha256"]):
            raise ValueError("Invalid previous ZIP dependency.")
        path, checksum = head.parent / name, prior["sha256"]
    chain.reverse()
    available = {}
    for _, manifest in chain:
        included = set(manifest["included_paths"])
        for entry in manifest["files"]:
            if entry["path"] not in included and available.get(entry["path"]) != entry["sha256"]:
                raise ValueError("ZIP chain is missing a required file version.")
        available = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    return chain


def restore_zip_chain(head: Path, destination: Path) -> dict:
    """Restore a verified chain into a NEW directory; never extract arbitrary paths."""
    head, destination = Path(head).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError("Restore destination must not already exist.")
    chain = _zip_chain(head)
    locations = {}
    for package, manifest in chain:
        entries = {entry["path"]: entry for entry in manifest["files"]}
        for path in manifest["included_paths"]:
            locations[path] = (package, entries[path]["member_path"])
    latest = chain[-1][1]
    destination.mkdir(parents=True)
    for entry in latest["files"]:
        path = _safe_relative(entry["path"])
        target = destination.joinpath(*PurePosixPath(RESTORE_NAMES.get(path, path)).parts)
        if not target.resolve().is_relative_to(destination):
            raise ValueError("Restore path escapes destination.")
        target.parent.mkdir(parents=True, exist_ok=True)
        package, member = locations[path]
        with zipfile.ZipFile(package) as archive, archive.open(member) as source, target.open("xb") as out:
            shutil.copyfileobj(source, out)
        if target.stat().st_size != entry["size"] or sha256(target) != entry["sha256"]:
            raise RuntimeError("Restored file failed its checksum.")
        if entry["kind"] == "sqlite_snapshot":
            with closing(sqlite3.connect(target.as_uri() + "?immutable=1", uri=True)) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Restored SQLite failed integrity verification.")
    return {"restored_files": len(latest["files"]), "packages": len(chain), "snapshot_id": latest["snapshot_id"],
            "destination": str(destination), "verification": "zip_chain_files_sha256_verified"}


def _daily_window(now: datetime | None = None) -> tuple[str, bool, str]:
    if now is None:
        # Windows supplies the current local UTC offset including DST. Do not
        # hardcode -04:00 or silently schedule in a different machine timezone.
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
            zone = winreg.QueryValueEx(key, "TimeZoneKeyName")[0].rstrip("\0")
        if zone != "Eastern Standard Time":
            raise ValueError("Daily archive requires Windows Eastern Standard Time (America/New_York).")
        now = datetime.now().astimezone()
    if now.utcoffset() is None:
        raise ValueError("Daily archive clock must include a timezone offset.")
    passed_today = now.hour >= 20
    cutoff = now.date() if passed_today else now.date() - timedelta(days=1)
    return cutoff.isoformat(), passed_today, now.date().isoformat()


def _daily_waiting(state: dict | None) -> dict:
    return {"stage": "local_snapshot_verified", "archive_format": ARCHIVE_FORMAT,
            "daily_archive_waiting": True, "daily_archive_hour": 20,
            "daily_archive_timezone": "America/New_York",
            "last_staged_package": {key: state[key] for key in ("package_name", "package_sha256", "package_path", "staged_at")}
            | {"package_bytes": state.get("package_bytes")} if state and state.get("package_name") else None}


def _staged_zip_receipt(state: dict, target: Path, unchanged: bool) -> dict:
    return {"stage": "staged_for_drive_sync", "archive_format": ARCHIVE_FORMAT, **state,
            "package_unchanged": unchanged, "daily_archive_waiting": False,
            "daily_archive_hour": 20, "daily_archive_timezone": "America/New_York",
            "drive_target": str(target), "authorized_folder_id": FOLDER_ID,
            "cloud_received": False, "cloud_file_id": None,
            "message": "One complete ZIP staged and readback verified on DriveFS; independent cloud ZIP receipt is still required."}


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run([GIT, "-c", "safe.directory=*", "-C", str(root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _git_bundle(root: Path, local: Path) -> dict | None:
    """Manifest entry of `git bundle create --all` for the repo whose top level is root, made only when a ref moved
    since the last bundle (a bundle's bytes are not reproducible, so an unchanged repo reuses the stored object).
    None when root is not a git repo top level or git fails: the package is then made without it."""
    top = _git(root, "rev-parse", "--show-toplevel")
    refs = _git(root, "for-each-ref", "--format=%(objectname) %(refname)")
    head = _git(root, "rev-parse", "HEAD")
    if not top or refs is None or Path(top.strip()).resolve() != root:
        return None
    refs_hash = hashlib.sha256((refs + (head or "")).encode()).hexdigest()
    cache = local / "git-bundle.json"
    last = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else {}
    if last.get("refs_hash") == refs_hash and _object_path(local, last).is_file():
        return {"path": BUNDLE_PATH, "kind": "git_bundle", "sha256": last["sha256"], "size": last["size"]}
    with tempfile.TemporaryDirectory(prefix="bundle-", dir=local) as temporary:
        bundle = Path(temporary) / "incore.bundle"
        if _git(root, "bundle", "create", str(bundle), "--all") is None or not bundle.is_file():
            return None
        saved = _object_from(bundle, local)
    atomic_json(cache, {"refs_hash": refs_hash, **saved})
    return {"path": BUNDLE_PATH, "kind": "git_bundle", **saved}


def _credential_files(repo: Path) -> list[str] | None:
    """File names of credential type (.env, *.pem, and the SENSITIVE names) that any commit of any ref ever touched."""
    log = _git(repo, "-c", "core.quotepath=false", "log", "--all", "--name-only", "--format=")
    if log is None:
        return None
    hits = set()
    for line in log.splitlines():
        base = PurePosixPath(line.strip()).name
        if base and ((CREDENTIAL_FILE.search(base) and not CREDENTIAL_OK.search(base)) or SENSITIVE.search(base)):
            hits.add(line.strip())
    return sorted(hits)


def _project_bundles(workspace: Path, root: Path, local: Path) -> tuple[list[dict], dict]:
    """One `git bundle --all` per project repo (PROJECT_PARENTS under workspace), made only when a ref moved. A repo that ever
    committed a credential-type file name is not bundled (listed in the report), nor is a bundle over BUNDLE_MAX."""
    entries, report = [], {"bundled": 0, "blocked": {}, "too_large": {}, "failed": []}
    repos = [d for parent in PROJECT_PARENTS if (workspace / parent).is_dir() for d in sorted((workspace / parent).iterdir())
             if d.is_dir() and (d / ".git").exists() and d.resolve() != root]
    allow_hash = hashlib.sha256("\n".join(sorted(CREDENTIAL_ALLOW)).encode()).hexdigest()  # a changed allow set re-checks every repo
    for repo in repos:
        key = repo.relative_to(workspace).as_posix()
        name = key.replace("/", "__")
        path = f"{PROJECT_BUNDLE_DIR}/{name}.bundle"
        top, refs, head = _git(repo, "rev-parse", "--show-toplevel"), _git(repo, "for-each-ref", "--format=%(objectname) %(refname)"), _git(repo, "rev-parse", "HEAD")
        if not top or not (refs or "").strip() or Path(top.strip()).resolve() != repo.resolve():
            report["failed"].append(name)
            continue
        refs_hash = hashlib.sha256((refs + (head or "") + allow_hash).encode()).hexdigest()
        cache = local / ("git-bundle-" + hashlib.sha256(name.encode()).hexdigest()[:12] + ".json")
        last = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else {}
        if last.get("refs_hash") != refs_hash:
            last = {"refs_hash": refs_hash}
            hits = _credential_files(repo)
            if hits is None:
                report["failed"].append(name)
                continue
            hits = [hit for hit in hits if f"{key}:{hit}" not in CREDENTIAL_ALLOW]
            if hits:
                last["blocked"] = hits
            else:
                with tempfile.TemporaryDirectory(prefix="bundle-", dir=local) as temporary:
                    bundle = Path(temporary) / "repo.bundle"
                    if _git(repo, "bundle", "create", str(bundle), "--all") is None or not bundle.is_file():
                        report["failed"].append(name)
                        continue
                    if bundle.stat().st_size > BUNDLE_MAX:
                        last["too_large"] = bundle.stat().st_size
                    else:
                        last.update(_object_from(bundle, local))
            atomic_json(cache, last)
        if last.get("blocked"):
            report["blocked"][name] = last["blocked"]
        elif last.get("too_large"):
            report["too_large"][name] = last["too_large"]
        elif _object_path(local, last).is_file():
            entries.append({"path": path, "kind": "git_bundle", "sha256": last["sha256"], "size": last["size"]})
            report["bundled"] += 1
        else:
            cache.unlink(missing_ok=True)  # the stored object is gone: make it again next time
            report["failed"].append(name)
    return entries, report


def _wsl_running() -> bool:
    """Is the Ubuntu distro already up? `wsl -l --running` only lists; reading the UNC share would start it."""
    try:
        out = subprocess.run(["wsl.exe", "-l", "--running"], capture_output=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "Ubuntu" in out.decode("utf-16-le", "ignore")


def _restore_entries(root: Path, local: Path) -> list[dict]:
    """RESTORE_FILES from root/backup (the code and its restore notes), stored as ordinary objects."""
    return [{"path": member, "kind": "file", **_object_from(root / "backup" / name, local)}
            for member, name in RESTORE_FILES.items() if (root / "backup" / name).is_file()]


def _drive_evidence(name: str, size: int, db_files: list[Path] | None = None) -> str:
    """Drive for Desktop's local metadata DB (read only, immutable; only this ZIP's name is looked up) says the cloud has the
    ZIP: confirmed = cloud id present, cloud size == size, not trashed, no queued operation for it. pending = not (yet) seen.
    unavailable = no DB, DB unreadable or a different schema. Never raises, never a reason to fail a snapshot."""
    try:
        dbs = db_files if db_files is not None else sorted(DRIVE_DB_GLOB.glob("*/metadata_sqlite_db"))
        if not dbs:
            return "unavailable"
        for path in dbs:
            with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                rows = db.execute("SELECT stable_id, id, file_size, trashed, is_tombstone FROM items WHERE local_title=?", (name,)).fetchall()
                per_item = "item_stable_id" in {row[1] for row in db.execute("PRAGMA table_info(operations)")}
                for stable_id, cloud_id, cloud_size, trashed, tombstone in rows:
                    queued = db.execute("SELECT count(*) FROM operations WHERE item_stable_id=?", (stable_id,)).fetchone()[0] if per_item \
                        else db.execute("SELECT count(*) FROM operations").fetchone()[0]
                    if cloud_id and cloud_size == size and not trashed and not tombstone and queued == 0:
                        return "confirmed"
        return "pending"
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return "unavailable"


def _drive_evidence_cached(local: Path, name: str, size: int) -> str:
    """_drive_evidence, kept in local/drive-evidence.json: confirmed stays, anything else is re-asked every DRIVE_RECHECK_S."""
    cache = local / "drive-evidence.json"
    try:
        last = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = {}
    if last.get("package_name") == name and (last.get("result") == "confirmed" or time.time() - last.get("checked", 0) < DRIVE_RECHECK_S):
        return last["result"]
    result = _drive_evidence(name, size)
    try:
        atomic_json(cache, {"package_name": name, "result": result, "checked": time.time()})
    except OSError:
        pass
    return result


def _stage(snapshot: dict, local: Path, target: Path, root: Path, *, force: bool = False,
           now: datetime | None = None) -> dict:
    target = validate_target(target, root)
    state_path = local / ("zip-chain-" + hashlib.sha256(str(target).encode()).hexdigest()[:16] + ".json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
    cutoff, passed_today, today = _daily_window(now)
    due = force or (state is None and passed_today) or bool(state and state.get("daily_checked_cutoff_date", "") < cutoff)
    if not due:
        if state and state.get("package_snapshot_id") == snapshot["snapshot_id"]:
            return _staged_zip_receipt(state, target, True)
        return _daily_waiting(state)
    files, omitted = _zip_files(snapshot, local)
    bundle = _git_bundle(root, local)
    if bundle:
        files = sorted([*files, bundle], key=lambda entry: entry["path"])
    project_entries, project_report = _project_bundles(root.parent, root, local)
    if project_entries:
        files = sorted([*files, *project_entries], key=lambda entry: entry["path"])
    restore_entries = _restore_entries(root, local)
    if restore_entries:
        files = sorted([*files, *restore_entries], key=lambda entry: entry["path"])
    if not files:
        state = {**(state or {}), "daily_checked_cutoff_date": cutoff}
        atomic_json(state_path, state)
        return {**_daily_waiting(state), "daily_archive_empty": True}
    for entry in files:
        entry["member_path"] = _member_path(entry)
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    previous = None
    if state and state.get("package_name"):
        prior_path = target / state["package_name"]
        # Check only the direct predecessor during backup. Full chain validation
        # is performed by restore, never by every minute's local snapshot.
        prior_manifest = verify_zip(prior_path, state["package_sha256"])
        previous = {"name": prior_path.name, "sha256": state["package_sha256"]}
        prior_files = {entry["path"]: entry["sha256"] for entry in prior_manifest["files"]}
    else:
        prior_files = {}
    unchanged = bool(state and state.get("fingerprint") == fingerprint)
    if not unchanged:
        name = f"{today}-{snapshot['snapshot_id']}.zip"
        included = [entry["path"] for entry in files
                    if prior_files.get(entry["path"]) != entry["sha256"] or entry["path"].startswith(RESTORE_DIR + "/")]
        manifest = {"archive_format": ARCHIVE_FORMAT, "package_name": name, "snapshot_id": snapshot["snapshot_id"],
                    "created_at": snapshot["created_at"], "previous_package": previous,
                    "files": files, "included_paths": included, "omitted_completed_chunks": omitted,
                    "restore": "Restore this ZIP plus its named previous_package chain; baseline has all files. Audio omitted from a later package remains in a verified predecessor."}
        packages = local / "packages"
        packages.mkdir(exist_ok=True)
        package = packages / name
        if not package.exists():
            temporary = package.with_suffix(".tmp")
            try:
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
                    archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                    for entry in files:
                        if entry["path"] in included:
                            archive.write(_object_path(local, entry), entry["member_path"])
                os.replace(temporary, package)
            finally:
                temporary.unlink(missing_ok=True)
        if verify_zip(package) != manifest:
            raise RuntimeError("Existing ZIP conflicts with the snapshot; not overwritten.")
        checksum = sha256(package)
        remote = target / name
        if remote.exists():
            if sha256(remote) != checksum:
                raise RuntimeError("Existing Drive ZIP differs; not overwritten.")
        else:
            created = False
            try:
                with remote.open("xb") as out, package.open("rb") as source:
                    created = True
                    shutil.copyfileobj(source, out)
                    out.flush()
                    os.fsync(out.fileno())
                if sha256(remote) != checksum:
                    raise RuntimeError("Drive ZIP readback checksum mismatch.")
            except Exception:
                if created:
                    remote.unlink(missing_ok=True)  # Only this attempt's incomplete new file.
                raise
        verify_zip(remote, checksum)
        state = {"fingerprint": fingerprint, "package_name": name, "package_sha256": checksum,
                 "package_path": str(remote), "package_bytes": remote.stat().st_size,
                 "prior_package_name": previous["name"] if previous else None,
                 "prior_package_sha256": previous["sha256"] if previous else None,
                 "package_snapshot_id": snapshot["snapshot_id"], "staged_at": utc_now(),
                 "successful_archive_cutoff_date": cutoff}
    # A checked day with no changes also waits until the next day's cutoff.
    # Failure above never consumes that day's attempt or advances the chain.
    state["daily_checked_cutoff_date"] = cutoff
    atomic_json(state_path, state)
    return {**_staged_zip_receipt(state, target, unchanged), "project_bundles": project_report}


def _capture_current_chat() -> dict:
    # Load only the fixed local exporter; no sys.path edits or provider calls.
    spec = importlib.util.spec_from_file_location("incubator_records_capture", ROOT / "records_capture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.capture_once()


def snapshot_once(target: Path | None = None, *, root: Path = ROOT, archive_now: bool = False,
                  archive_time: datetime | None = None, external: dict[str, Path] | None = None,
                  external_files: dict[str, Path] | None = None) -> dict:
    root = root.resolve()
    unreadable_links.clear()
    chat_capture = None
    # Tests/restores using another root must never read this machine's real chat or memory.
    real = root == ROOT.resolve()
    if external is None:
        external = {EXTERNAL_PREFIX: MEMORY_DIR, SKILLS_PREFIX: SKILLS_DIR} if real else {}
    wsl_up = None
    if external_files is None:
        external_files = {**EXTERNAL_FILES, **_llm_prompt_files(root)} if real else {}
        if real:
            wsl_up = _wsl_running()
            if wsl_up:
                external_files = {**external_files, **WSL_FILES}
    if real:
        try:
            chat_capture = _capture_current_chat()
            if chat_capture.get("status") != "ok":
                chat_capture = {**chat_capture, "capture_status": chat_capture.get("status", "unknown"), "status": "failed"}
        except Exception as error:
            chat_capture = {"status": "failed", "reason": type(error).__name__}
    local = root / "data/backups"
    local.mkdir(parents=True, exist_ok=True)
    entries = []
    # Snapshot databases first. Audio files are immutable and committed before
    # their DB references; a later file scan can safely include extra new files.
    with tempfile.TemporaryDirectory(prefix="sqlite-", dir=local) as temporary:
        for number, source in enumerate(_database_paths(root)):
            saved = Path(temporary) / f"{number}.sqlite3"
            snapshot_database(source, saved)
            entries.append({"path": source.relative_to(root).as_posix(),
                            "kind": "sqlite_snapshot", **_object_from(saved, local)})
    for directory in (root / "records", root / "input/data/sessions"):
        for source in _files_below(directory, root):
            # Only immutable payload chunks/originals are stored in sessions.
            if directory.name == "sessions" and not (source.name.startswith("original.")
                                                       or source.parent.name == "chunks"):
                continue
            try:
                entries.append({"path": source.relative_to(root).as_posix(),
                                "kind": "file", **_object_from(source, local)})
            except OSError as error:
                if not _skip_unreadable_link(source, error):
                    raise
    skipped = 0
    saved_paths = set()
    cache_path = local / "org-hash-cache.json"
    try:
        old_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old_cache = {}
    new_cache: dict = {}
    for path, source in _org_files(root, external, external_files):
        saved = _org_object(source, local, path, old_cache if isinstance(old_cache, dict) else {}, new_cache)
        if saved is None:
            skipped += 1
        else:
            entries.append({"path": path, "kind": "file", **saved})
            saved_paths.add(path)
    atomic_json(cache_path, new_cache)
    entries.sort(key=lambda entry: entry["path"])
    fingerprint =hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    state_path = local / "latest-snapshot.json"
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    unchanged = previous.get("fingerprint") == fingerprint
    if unchanged:
        snapshot = json.loads((local / "snapshots" / previous["manifest"]).read_text(encoding="utf-8"))
    else:
        snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + fingerprint[:10]
        snapshot = {"schema_version": 1, "snapshot_id": snapshot_id, "created_at": utc_now(),
                    "fingerprint": fingerprint, "files": entries,
                    "consistency": "SQLite backup API per database; immutable files scanned afterwards",
                    "restore": "For each file, copy objects/<sha256[:2]>/<sha256> to a new restore root/path; verify SHA-256 first."}
        atomic_json(local / "snapshots" / (snapshot_id + ".json"), snapshot)
        atomic_json(state_path, {"fingerprint": fingerprint, "manifest": snapshot_id + ".json"})
    receipt = {"stage": "local_snapshot_verified", "checked_at": utc_now(),
               "snapshot_id": snapshot["snapshot_id"], "unchanged": unchanged,
               "file_count": len(entries), "total_logical_bytes": sum(e["size"] for e in entries),
               "manifest_path": str(local / "snapshots" / (snapshot["snapshot_id"] + ".json")),
               "manifest_sha256": sha256(local / "snapshots" / (snapshot["snapshot_id"] + ".json")),
               "cloud_received": False, "cloud_file_id": None}
    if skipped:
        receipt["org_files_skipped"] = skipped
    if unreadable_links:  # a note, not a failure; the watcher keeps going
        receipt["unreadable_links_skipped"] = {"count": len(unreadable_links), "first": unreadable_links[:5]}
    if chat_capture is not None:
        receipt["chat_capture"] = chat_capture
    if wsl_up is not None:  # a skipped or missing WSL file is a note, not a failure
        missing = [path for path in WSL_FILES if path not in saved_paths]
        receipt["wsl_files"] = "not_running" if not wsl_up else ("ok" if not missing else "missing: " + ", ".join(missing))
    if target is not None:
        try:
            receipt.update(_stage(snapshot, local, target, root, force=archive_now, now=archive_time))
        except (OSError, ValueError, RuntimeError, sqlite3.Error, zipfile.BadZipFile, KeyError) as error:
            receipt["staging_error"] = str(error)
        package = receipt if receipt.get("package_name") else (receipt.get("last_staged_package") or {})
        if package.get("package_name") and isinstance(package.get("package_bytes"), int):
            try:  # local evidence only; cloud_received stays False (that one means "downloaded and checked")
                receipt["drive_local_evidence"] = _drive_evidence_cached(local, package["package_name"], package["package_bytes"])
            except Exception:
                receipt["drive_local_evidence"] = "unavailable"
    if ARBITROS_STEP and root == ROOT.resolve():  # the Arbitros website's data (owner\x2010-11); test/restore roots never reach the server
        receipt.update(ARBITROS_STEP(root))  # never raises; a failure is `arbitros_error`, not a watcher failure
    atomic_json(local / "status.json", receipt)
    return receipt


def _wait_or_stop(seconds: int, stop_file: Path) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop_file.exists():
            return True
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    return stop_file.exists()


def _stopped(reason: str) -> int:
    print(json.dumps({"stage": "watcher_stopped", "reason": reason, "checked_at": utc_now()}), flush=True)
    return 0


def _command_line(pid: int) -> str:
    """Command line of a running process, '' when there is none (no psutil in the service venv)."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                         capture_output=True, text=True, timeout=30)
    return out.stdout.strip()


def _running_watcher(process_file: Path, command_line=None) -> int | None:
    """PID recorded in `process_file` if it is alive and runs `backup.py --watch`, else None. The recorded PID may be this
    process or its parent (the venv launcher starts the real interpreter as a child), which is not another watcher."""
    try:
        pid = json.loads(process_file.read_text(encoding="utf-8-sig")).get("ProcessId")  # start-services.ps1 writes a BOM
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(pid, int) or pid in (os.getpid(), os.getppid()):
        return None
    cmd = (command_line or _command_line)(pid)
    return pid if "backup.py" in cmd and "--watch" in cmd else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    mode.add_argument("--restore-zip", type=Path, metavar="LATEST_ZIP", help="Restore a verified ZIP dependency chain")
    parser.add_argument("--restore-to", type=Path, help="New destination directory for --restore-zip")
    parser.add_argument("--target", type=Path, help="Existing verified Drive folder; omission makes a local snapshot only")
    parser.add_argument("--drive", action="store_true", help="Use the already-verified default Drive folder")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--max-runs", type=int, help="Optional finite run limit; watch otherwise continues while successful")
    parser.add_argument("--stop-file", type=Path, default=ROOT / "data/backups/STOP",
                        help="Presence stops watch before the next cycle; never removed automatically")
    args = parser.parse_args(argv)
    if args.restore_zip:
        if not args.restore_to or args.drive or args.target:
            parser.error("--restore-zip requires --restore-to and cannot stage to Drive")
        print(json.dumps(restore_zip_chain(args.restore_zip, args.restore_to), ensure_ascii=False))
        return 0
    if args.restore_to:
        parser.error("--restore-to requires --restore-zip")
    if args.interval < 30 or (args.max_runs is not None and not 1 <= args.max_runs <= 10080):
        parser.error("interval must be >=30 seconds; optional max-runs must be 1..10080")
    if args.drive and args.target:
        parser.error("Choose --drive or --target, not both")
    target = DEFAULT_TARGET if args.drive else args.target
    if args.watch and (other := _running_watcher(args.stop_file.parent / "watcher-process.json")):
        print(json.dumps({"stage": "watcher_already_running", "pid": other, "checked_at": utc_now()}), flush=True)
        return 0
    failures = iteration = 0
    limit = args.max_runs if args.watch else 1
    try:
        while limit is None or iteration < limit:
            if args.watch and args.stop_file.exists():
                return _stopped("stop_file")
            try:
                receipt = snapshot_once(target, archive_now=args.once)
                failed = ("staging_error" in receipt
                          or receipt.get("chat_capture", {}).get("status", "ok") != "ok")
                failures = failures + 1 if failed else 0
                if not receipt["unchanged"] or failed or iteration == 0 or receipt.get("package_unchanged") is False:
                    print(json.dumps(receipt, ensure_ascii=False), flush=True)
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
                failures += 1
                failure = {"stage": "backup_failed", "checked_at": utc_now(), "error": str(error),
                           "cloud_received": False, "cloud_file_id": None}
                try:
                    atomic_json(ROOT / "data/backups/status.json", failure)
                except OSError:
                    pass
                print(json.dumps(failure, ensure_ascii=False), flush=True)
            iteration += 1
            if failures >= 3 or (args.once and failures):
                return 2
            if args.watch and (limit is None or iteration < limit):
                if _wait_or_stop(args.interval, args.stop_file):
                    return _stopped("stop_file")
    except KeyboardInterrupt:
        return _stopped("keyboard_interrupt")
    if args.watch:
        return _stopped("max_runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
