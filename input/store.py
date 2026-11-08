from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_SESSION_BYTES = 2 * 1024 * 1024 * 1024
MAX_TEXT_BYTES = 256 * 1024
MAX_CHUNKS = 200_000
ALLOWED_EXTENSIONS = {"aac", "flac", "m4a", "mp3", "mp4", "ogg", "opus", "wav", "webm", "3gp"}


class InputError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def _uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise InputError(404, "找不到这条记录。") from None
    if str(parsed) != value.lower():
        raise InputError(404, "找不到这条记录。")
    return str(parsed)


def _safe_filename(name: str) -> str:
    # Keep a display name only; never use a browser-supplied name as a path.
    name = re.split(r"[\\/]", str(name))[-1]
    name = "".join(c for c in name if c >= " " and c != "\x7f").strip()
    return name[:200] or "音频文件"


def _extension(name: str) -> str:
    suffix = Path(_safe_filename(name)).suffix.lower().lstrip(".")
    return suffix if suffix in ALLOWED_EXTENSIONS else "bin"


class InputStore:
    """Append-only revisions and atomically persisted audio under one fixed root."""

    def __init__(self, root: Path, rules_path: Path | None = None):
        self.root = Path(root).resolve()
        # The real desk keeps owner rules next to the backed-up config (data/config-sync); any other root (tests, copies) keeps its own file.
        default = Path(__file__).resolve().parent.parent / "data" / "config-sync" / "input-rules.json"
        self.rules_path = Path(rules_path) if rules_path else default if self.root == (Path(__file__).resolve().parent / "data") else self.root / "owner-rules.json"
        self.sessions_root = self.root / "sessions"
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "input.sqlite3"
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _db(self):
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _init_db(self) -> None:
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mime_type TEXT,
                    source_filename TEXT,
                    audio_extension TEXT,
                    original_sha256 TEXT,
                    total_bytes INTEGER,
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    seq INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, seq)
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    kind TEXT NOT NULL,
                    parent_id TEXT REFERENCES revisions(id),
                    content TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS verifications (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    revision_id TEXT NOT NULL UNIQUE REFERENCES revisions(id),
                    revision_sha256 TEXT NOT NULL,
                    original_sha256 TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS revisions_by_session
                    ON revisions(session_id, created_at);
                CREATE INDEX IF NOT EXISTS sessions_by_updated
                    ON sessions(updated_at DESC);
                """
            )

    def _session_dir(self, session_id: str) -> Path:
        sid = _uuid(session_id)
        return self.sessions_root / sid

    def _chunk_path(self, session_id: str, seq: int) -> Path:
        return self._session_dir(session_id) / "chunks" / f"{seq:08d}.part"

    def _session_row(self, db: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (_uuid(session_id),)).fetchone()
        if row is None:
            raise InputError(404, "找不到这条记录。")
        return row

    def create_audio_session(
        self, source: str, title: str, mime_type: str, filename: str = ""
    ) -> dict[str, Any]:
        if source not in {"audio_capture", "audio_upload"}:
            raise InputError(400, "音频来源无效。")
        title = str(title or "").strip()[:200] or ("录音" if source == "audio_capture" else "上传音频")
        mime_type = str(mime_type or "application/octet-stream")[:120]
        filename = _safe_filename(filename) if filename else ""
        ext = _extension(filename) if filename else ("webm" if "webm" in mime_type else "ogg" if "ogg" in mime_type else "m4a" if "mp4" in mime_type else "bin")
        sid, stamp = str(uuid.uuid4()), now_utc()
        (self.sessions_root / sid / "chunks").mkdir(parents=True, exist_ok=False)
        with self._lock, self._db() as db:
            db.execute(
                "INSERT INTO sessions(id,created_at,updated_at,source,title,status,mime_type,source_filename,audio_extension) "
                "VALUES(?,?,?,?,?,'collecting',?,?,?)",
                (sid, stamp, stamp, source, title, mime_type, filename or None, ext),
            )
        return self.get_session(sid)

    def create_text_session(self, title: str, content: str) -> tuple[dict[str, Any], dict[str, Any]]:
        content = str(content)
        if not content.strip():
            raise InputError(400, "请输入一些文字。")
        if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
            raise InputError(413, "文字太长，请分成几条保存。")
        title = str(title or "文字输入").strip()[:200] or "文字输入"
        sid, stamp, rid = str(uuid.uuid4()), now_utc(), str(uuid.uuid4())
        with self._lock, self._db() as db:
            db.execute(
                "INSERT INTO sessions(id,created_at,updated_at,source,title,status) VALUES(?,?,?,?,?,'text_saved')",
                (sid, stamp, stamp, "text", title),
            )
            db.execute(
                "INSERT INTO revisions(id,session_id,kind,parent_id,content,sha256,created_at,source,data_json) "
                "VALUES(?,?, 'original_text',NULL,?,?,?,?, '{}')",
                (rid, sid, content, sha256_text(content), stamp, "user_text"),
            )
        return self.get_session(sid), self.get_revision(rid)

    def put_chunk(self, session_id: str, seq: int, data: bytes, supplied_hash: str) -> dict[str, Any]:
        sid = _uuid(session_id)
        if not isinstance(seq, int) or seq < 0 or seq >= MAX_CHUNKS:
            raise InputError(400, "音频片段编号无效。")
        if not data or len(data) > MAX_CHUNK_BYTES:
            raise InputError(413, "音频片段为空或超过 8 MiB。")
        actual_hash = sha256_bytes(data)
        if not re.fullmatch(r"[0-9a-f]{64}", str(supplied_hash or "")) or actual_hash != supplied_hash.lower():
            raise InputError(400, "音频片段校验失败，请重试。")
        target = self._chunk_path(sid, seq)
        with self._lock:
            with self._db() as db:
                session = self._session_row(db, sid)
                if session["status"] != "collecting":
                    old = db.execute("SELECT sha256,size FROM chunks WHERE session_id=? AND seq=?", (sid, seq)).fetchone()
                    if old and old["sha256"] == actual_hash and old["size"] == len(data):
                        return {"duplicate": True, "stored": True}
                    raise InputError(409, "这条录音已完成，片段不能再更改。")
                old = db.execute("SELECT sha256,size FROM chunks WHERE session_id=? AND seq=?", (sid, seq)).fetchone()
                if old:
                    if old["sha256"] == actual_hash and old["size"] == len(data):
                        return {"duplicate": True, "stored": True}
                    raise InputError(409, "同一片段编号已收到不同内容，原内容已保留。")
                stored = db.execute("SELECT COALESCE(SUM(size),0) FROM chunks WHERE session_id=?", (sid,)).fetchone()[0]
                if stored + len(data) > MAX_SESSION_BYTES:
                    raise InputError(413, "单条音频不能超过 2 GiB。")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    on_disk = target.read_bytes()
                    if sha256_bytes(on_disk) != actual_hash or len(on_disk) != len(data):
                        raise InputError(409, "发现冲突片段，原内容已保留。")
                else:
                    temp = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
                    try:
                        with temp.open("xb") as f:
                            f.write(data)
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(temp, target)
                    finally:
                        temp.unlink(missing_ok=True)
                stamp = now_utc()
                db.execute(
                    "INSERT INTO chunks(session_id,seq,sha256,size,stored_at) VALUES(?,?,?,?,?)",
                    (sid, seq, actual_hash, len(data), stamp),
                )
                db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (stamp, sid))
        return {"duplicate": False, "stored": True}

    def finalize_audio(self, session_id: str, chunk_count: int, total_bytes: int) -> dict[str, Any]:
        sid = _uuid(session_id)
        if not isinstance(chunk_count, int) or not 1 <= chunk_count <= MAX_CHUNKS:
            raise InputError(400, "片段数量无效。")
        if not isinstance(total_bytes, int) or not 1 <= total_bytes <= MAX_SESSION_BYTES:
            raise InputError(400, "音频长度无效。")
        with self._lock:
            with self._db() as db:
                session = self._session_row(db, sid)
                if session["status"] != "collecting":
                    if session["original_sha256"] and session["chunk_count"] == chunk_count and session["total_bytes"] == total_bytes:
                        return self.get_session(sid)
                    raise InputError(409, "录音状态已变化，原始内容保持不变。")
                chunks = db.execute(
                    "SELECT seq,sha256,size FROM chunks WHERE session_id=? ORDER BY seq", (sid,)
                ).fetchall()
                if len(chunks) != chunk_count or [r["seq"] for r in chunks] != list(range(chunk_count)):
                    raise InputError(409, "还有音频片段未安全上传；可重试后再完成。")
                if sum(r["size"] for r in chunks) != total_bytes:
                    raise InputError(409, "音频片段总长度不一致；原始片段仍已保留。")
                ext = session["audio_extension"] or "bin"
                final_path = self._session_dir(sid) / f"original.{ext}"
                temp_path = final_path.with_name(final_path.name + ".tmp")
                hasher = hashlib.sha256()
                written = 0
                try:
                    with temp_path.open("wb") as out:
                        for row in chunks:
                            part = self._chunk_path(sid, row["seq"])
                            with part.open("rb") as src:
                                part_hash = hashlib.sha256()
                                part_size = 0
                                while True:
                                    block = src.read(1024 * 1024)
                                    if not block:
                                        break
                                    out.write(block)
                                    hasher.update(block)
                                    part_hash.update(block)
                                    written += len(block)
                                    part_size += len(block)
                            if part_size != row["size"] or part_hash.hexdigest() != row["sha256"]:
                                raise InputError(409, "磁盘上的音频片段校验失败；原始片段保留，请联系维护者。")
                        out.flush()
                        os.fsync(out.fileno())
                    if written != total_bytes:
                        raise InputError(409, "原始音频总长度校验失败。")
                    os.replace(temp_path, final_path)
                finally:
                    temp_path.unlink(missing_ok=True)
                digest, stamp = hasher.hexdigest(), now_utc()
                db.execute(
                    "UPDATE sessions SET status='queued',updated_at=?,original_sha256=?,total_bytes=?,chunk_count=?,error=NULL WHERE id=?",
                    (stamp, digest, written, chunk_count, sid),
                )
        return self.get_session(sid)

    def append_revision(
        self,
        session_id: str,
        kind: str,
        content: str,
        parent_id: str | None = None,
        source: str = "user_correction",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sid = _uuid(session_id)
        if kind not in {"correction", "instruction", "asr", "postprocess"}:
            raise InputError(400, "版本类型无效。")
        content = str(content)
        if not content.strip():
            raise InputError(400, "内容不能为空。")
        if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
            raise InputError(413, "文字太长，无法保存。")
        rid, stamp = str(uuid.uuid4()), now_utc()
        metadata = json.dumps(data or {}, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._db() as db:
            self._session_row(db, sid)
            if parent_id:
                parent_id = _uuid(parent_id)
                parent = db.execute("SELECT session_id FROM revisions WHERE id=?", (parent_id,)).fetchone()
                if parent is None or parent["session_id"] != sid:
                    raise InputError(400, "所选父版本不属于这条记录。")
            if kind == "instruction" and not parent_id:
                raise InputError(400, "请先选择一个原文或版本。")
            db.execute(
                "INSERT INTO revisions(id,session_id,kind,parent_id,content,sha256,created_at,source,data_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (rid, sid, kind, parent_id, content, sha256_text(content), stamp, source, metadata),
            )
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (stamp, sid))
        return self.get_revision(rid)

    def append_asr_result(self, session_id: str, result: dict[str, Any]) -> dict[str, Any]:
        from .rules import process
        raw = self.append_revision(session_id, "asr", result["text"], source="asr", data=result)
        text, hits, VERSION = process(raw["content"], self.rules_path)
        if not hits:
            return raw
        return self.append_revision(session_id, "postprocess", text, raw["id"], "asr_postprocess",
                                    {"processor_version": VERSION, "applied_rules": hits,
                                     "human_verified": False, "review_required": True,
                                     "metadata": result.get("metadata", {})})

    def _current_text_revision(self, db: sqlite3.Connection, session_id: str) -> str | None:
        # rowid is insertion order; created_at can tie within one millisecond.
        row = db.execute(
            "SELECT id FROM revisions WHERE session_id=? AND kind!='instruction' ORDER BY rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return row["id"] if row else None

    @staticmethod
    def _check_still_valid(db: sqlite3.Connection, session_id: str, revision_id: str) -> bool:
        """A check holds while its revision is the latest, or only drafts follow and the last is a
        user_rewrite whose text differs: the verbatim version stays on file next to the rewrite."""
        rows = db.execute(
            "SELECT id,kind,content,data_json,source FROM revisions WHERE session_id=? AND kind!='instruction' ORDER BY rowid",
            (session_id,),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if revision_id not in ids:
            return False
        checked = rows[ids.index(revision_id)]
        later = rows[ids.index(revision_id) + 1:]
        if not later:
            return True

        def label(row: sqlite3.Row) -> str | None:
            if row["kind"] == "postprocess" and row["source"] == "llm_postprocess":
                return "machine"  # a later machine polish leaves an earlier human check standing
            if row["kind"] != "correction":
                return None
            try:
                data = json.loads(row["data_json"])
            except (json.JSONDecodeError, TypeError):
                return None
            return data.get("correction_type") if isinstance(data, dict) else None

        return (all(label(row) in ("draft", "user_rewrite", "machine") for row in later)
                and (label(later[-1]) == "machine" or label(later[-1]) == "user_rewrite" and later[-1]["content"] != checked["content"]))

    @staticmethod
    def _literal_chain(db: sqlite3.Connection, revision_id: str) -> bool:
        """True for original ASR, or a recognition correction descending from it.

        Only the confirmed revision's own label counts: drafts and earlier labels in between are
        superseded by the purpose chosen when the user confirms (a mislabel can be corrected).
        """
        seen: set[str] = set()
        current: str | None = revision_id
        while current and current not in seen:
            seen.add(current)
            row = db.execute("SELECT kind,parent_id,data_json,source FROM revisions WHERE id=?", (current,)).fetchone()
            if row is None:
                return False
            if row["kind"] == "asr":
                return row["parent_id"] is None
            if row["kind"] == "postprocess" and row["source"] in ("asr_postprocess", "llm_postprocess") and row["parent_id"]:
                current = row["parent_id"]
                continue  # Eligible only after explicit full-recording human verification (the machine never writes one).
            if row["kind"] != "correction" or not row["parent_id"]:
                return False
            try:
                data = json.loads(row["data_json"])
            except (json.JSONDecodeError, TypeError):
                return False
            if current == revision_id and (not isinstance(data, dict) or data.get("correction_type") != "recognition_correction"):
                return False
            current = row["parent_id"]
        return False

    def verify_revision(self, session_id: str, revision_id: str, original_sha256: str) -> dict[str, Any]:
        """Record a human full-recording check bound to one revision and the original audio hash."""
        sid, rid = _uuid(session_id), _uuid(revision_id)
        supplied = str(original_sha256 or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", supplied):
            raise InputError(400, "原始音频校验值无效。")
        with self._lock, self._db() as db:
            session = self._session_row(db, sid)
            if not session["original_sha256"]:
                raise InputError(409, "请先完成并保存原始音频。")
            if session["original_sha256"] != supplied:
                raise InputError(409, "原始音频校验值不一致；请刷新后重新核对。")
            revision = db.execute("SELECT session_id,sha256 FROM revisions WHERE id=?", (rid,)).fetchone()
            if revision is None or revision["session_id"] != sid:
                raise InputError(404, "找不到这个版本。")
            if not self._literal_chain(db, rid):
                raise InputError(400, "只有原始转写或听写纠错版本可以标记为已核对；表达改写和指令不可用。")
            if self._current_text_revision(db, sid) != rid:
                raise InputError(409, "这个版本之后已有新的转写或修订；请核对最新版本。")
            existing = db.execute("SELECT * FROM verifications WHERE revision_id=?", (rid,)).fetchone()
            if existing:
                return dict(existing)
            vid = str(uuid.uuid4())
            db.execute(
                "INSERT INTO verifications(id,session_id,revision_id,revision_sha256,original_sha256,scope,verified_at,source) "
                "VALUES(?,?,?,?,?,'full_recording',?,'human_review')",
                (vid, sid, rid, revision["sha256"], session["original_sha256"], now_utc()),
            )
            return dict(db.execute("SELECT * FROM verifications WHERE id=?", (vid,)).fetchone())

    def list_verified_training_rows(self) -> list[dict[str, Any]]:
        """Select rows a future training export may use; nothing is written or sent."""
        result = []
        with self._db() as db:
            rows = db.execute(
                "SELECT v.*,r.content,r.kind AS revision_kind,r.sha256 AS stored_revision_sha256,"
                "s.original_sha256 AS stored_original_sha256 FROM verifications v "
                "JOIN revisions r ON r.id=v.revision_id JOIN sessions s ON s.id=v.session_id "
                "ORDER BY v.verified_at,v.id"
            ).fetchall()
            for row in rows:
                if (row["stored_original_sha256"] != row["original_sha256"]
                        or row["stored_revision_sha256"] != row["revision_sha256"]
                        or not self._check_still_valid(db, row["session_id"], row["revision_id"])
                        or not self._literal_chain(db, row["revision_id"])):
                    continue
                result.append({
                    "session_id": row["session_id"],
                    "revision_id": row["revision_id"],
                    "revision_kind": row["revision_kind"],
                    "revision_sha256": row["revision_sha256"],
                    "original_sha256": row["original_sha256"],
                    "text": row["content"],
                    "scope": row["scope"],
                    "verified_at": row["verified_at"],
                })
        return result

    def get_revision(self, revision_id: str) -> dict[str, Any]:
        rid = _uuid(revision_id)
        with self._db() as db:
            row = db.execute("SELECT * FROM revisions WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise InputError(404, "找不到这个版本。")
        out = dict(row)
        try:
            out["data"] = json.loads(out.pop("data_json"))
        except (json.JSONDecodeError, TypeError):
            out["data"] = {}
        return out

    def get_session(self, session_id: str) -> dict[str, Any]:
        sid = _uuid(session_id)
        with self._db() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise InputError(404, "找不到这条记录。")
        out = dict(row)
        out["audio_ready"] = bool(out.get("original_sha256"))
        out.pop("audio_extension", None)
        return out

    def get_session_path(self, session_id: str) -> Path:
        sid = _uuid(session_id)
        row = self.get_session(session_id)
        if not row["audio_ready"]:
            raise InputError(404, "原始音频尚未完成。")
        # Only the extension recorded by the server is used to build this path.
        with self._db() as db:
            ext = self._session_row(db, sid)["audio_extension"]
        path = self._session_dir(sid) / f"original.{ext}"
        if not path.is_file():
            raise InputError(404, "找不到完整原始音频。")
        return path

    def session_detail(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM revisions WHERE session_id=? ORDER BY rowid", (session["id"],)
            ).fetchall()
            checks = {
                row["revision_id"]: dict(row)
                for row in db.execute("SELECT * FROM verifications WHERE session_id=?", (session["id"],))
            }
            valid = {rid for rid in checks if self._check_still_valid(db, session["id"], rid)}
        revisions = []
        for row in rows:
            item = dict(row)
            try:
                item["data"] = json.loads(item.pop("data_json"))
            except (json.JSONDecodeError, TypeError):
                item["data"] = {}
            check = checks.get(item["id"])
            item["verification"] = None if check is None else {
                "verified_at": check["verified_at"],
                "scope": check["scope"],
                "original_sha256": check["original_sha256"],
                "current": item["id"] in valid and check["original_sha256"] == session["original_sha256"]
                and check["revision_sha256"] == item["sha256"],
            }
            revisions.append(item)
        session["revisions"] = revisions
        return session

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 200").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["audio_ready"] = bool(item.get("original_sha256"))
            item.pop("audio_extension", None)
            result.append(item)
        return result

    def list_instructions(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT i.id AS instruction_revision_id,i.session_id AS source_document_id,"
                "i.parent_id AS source_revision_id,i.content AS instruction_text,i.sha256 AS instruction_sha256,"
                "i.created_at,i.source,p.sha256 AS source_revision_sha256,p.kind AS source_revision_kind,s.title "
                "FROM revisions i JOIN sessions s ON s.id=i.session_id "
                "LEFT JOIN revisions p ON p.id=i.parent_id WHERE i.kind='instruction' "
                "ORDER BY i.created_at,i.id LIMIT 1000"
            ).fetchall()
        return [
            {**dict(row), "dispatch_status": "unknown_to_input_service"}
            for row in rows
        ]

    def set_status(self, session_id: str, status: str, error: str | None = None) -> None:
        if status not in {"queued", "transcribing", "transcribed", "transcription_failed", "transcription_unavailable"}:
            raise ValueError("invalid internal status")
        with self._lock, self._db() as db:
            self._session_row(db, session_id)
            db.execute(
                "UPDATE sessions SET status=?,error=?,updated_at=? WHERE id=?",
                (status, error, now_utc(), _uuid(session_id)),
            )

    def pending_transcriptions(self) -> list[str]:
        with self._db() as db:
            rows = db.execute(
                "SELECT id FROM sessions WHERE status IN ('queued','transcribing') AND original_sha256 IS NOT NULL"
            ).fetchall()
        return [row["id"] for row in rows]

    def retry_transcription(self, session_id: str) -> None:
        with self._lock, self._db() as db:
            row = self._session_row(db, session_id)
            if not row["original_sha256"]:
                raise InputError(409, "请先完成并保存原始音频。")
            db.execute(
                "UPDATE sessions SET status='queued',error=NULL,updated_at=? WHERE id=?",
                (now_utc(), row["id"]),
            )
