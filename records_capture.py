"""Append exact native user records from this one Codex thread; no models/network.

The source JSONL record is preserved verbatim inside each archive entry. The
index is derived and replaceable; messages.jsonl is never rewritten. This module
does not start a service. Call capture_once() before the existing backup snapshot.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

THREAD_ID = "01a0e6ca-e69b-70e0-aa82-1e216d8c4e02"
ROOT = Path(__file__).resolve().parent
SESSION_ROOT = Path.home() / ".codex" / "sessions"
ATTACHMENT_ROOT = Path.home() / ".codex" / "attachments"
DESTINATION = ROOT / "records" / "current-chat"
SECRET_PATTERN = re.compile(r"(?:mfa\.[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,110}|sk-ant-[A-Za-z0-9_-]{15,})")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@contextmanager
def capture_lock():
    # Native byte-range locking keeps manual capture and the existing worker
    # from appending the same record simultaneously. No stale-lock deletion.
    import msvcrt

    DESTINATION.mkdir(parents=True, exist_ok=True)
    with (DESTINATION / ".capture.lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def existing_texts():
    """Comparison only; summaries never replace a source payload."""
    values = []

    def collect(value, location):
        if isinstance(value, str):
            values.append((location, value))
        elif isinstance(value, dict):
            for key, child in value.items():
                collect(child, location + "." + key)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                collect(child, location + f"[{i}]")

    for path in sorted((ROOT / "records").glob("*")):
        if path.is_file() and path.suffix == ".json":
            collect(json.loads(path.read_text(encoding="utf-8-sig")), path.name)
        elif path.is_file() and path.suffix == ".txt":
            values.append((path.name, path.read_text(encoding="utf-8-sig")))
    return values


def capture_attachment(path: Path, record_key: str) -> dict:
    resolved = path.resolve()
    if not resolved.is_relative_to(ATTACHMENT_ROOT.resolve()) or path.is_symlink() or not path.is_file():
        return {"record_key": record_key, "source_path": str(path), "status": "unavailable_or_outside_attachment_root"}
    data = path.read_bytes()
    if path.suffix.lower() in {".txt", ".json", ".md"} and SECRET_PATTERN.search(data.decode("utf-8", errors="replace")):
        return {"record_key": record_key, "source_path": str(path), "status": "credential_pattern_detected", "sha256": digest(data)}
    checksum = digest(data)
    target = DESTINATION / "attachments" / (checksum + path.suffix.lower())
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        if digest(target.read_bytes()) != checksum:
            raise ValueError("attachment_archive_checksum_mismatch")
    else:
        with target.open("xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    return {"record_key": record_key, "source_path": str(path), "archive_path": str(target.relative_to(ROOT)),
            "status": "copied_and_verified", "sha256": checksum, "bytes": len(data)}


def _capture_once() -> dict:
    archive = DESTINATION / "messages.jsonl"
    saved = {}
    if archive.exists():
        for line in archive.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item["thread_id"] != THREAD_ID or digest(item["source_jsonl_record"].encode("utf-8")) != item["source_record_sha256"]:
                raise ValueError("existing_archive_integrity_failure")
            saved[item["record_key"]] = item
    texts = existing_texts()
    sources, candidates, excluded, deferred = [], {}, [], []
    paths = sorted(SESSION_ROOT.rglob(f"*{THREAD_ID}*.jsonl"))
    for path in paths:
        if path.is_symlink():
            raise ValueError("source_symlink_not_allowed")
        # A file can still be appended by Codex. Read one immutable prefix and
        # defer an unfinished final line to the next capture.
        data = path.read_bytes()
        lines = data.splitlines(keepends=True)
        meta = json.loads(lines[0]).get("payload", {})
        if meta.get("id") != THREAD_ID or meta.get("session_id") != THREAD_ID:
            continue
        complete_bytes = 0
        user_count = 0
        for number, raw in enumerate(lines, 1):
            if not raw.endswith(b"\n"):
                deferred.append({"source_path": str(path), "line": number, "reason": "incomplete_final_line"})
                break
            event = json.loads(raw)
            start_offset = complete_bytes
            complete_bytes += len(raw)
            payload = event.get("payload", {})
            if not (event.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user"):
                continue
            metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
            kinds = metadata.get("content_item_kinds") or []
            if not any(kind.startswith("user.") for kind in kinds):
                if kinds and all(kind.startswith(("environments.", "agents_md.")) for kind in kinds):
                    excluded.append({"source_path": str(path), "line": number, "kinds": kinds, "reason": "not_native_user_input"})
                else:
                    deferred.append({"source_path": str(path), "line": number, "reason": "unrecognized_user_metadata"})
                continue
            user_count += 1
            payload_hash = digest(canonical(payload))
            key = digest(canonical([THREAD_ID, payload.get("id"), payload_hash]))
            content = payload.get("content", [])
            text = "\n".join(part.get("text", "") for part in content if part.get("type") in {"input_text", "text"})
            origin = {"source_path": str(path), "source_line": number, "source_byte_offset": start_offset,
                      "source_timestamp": event.get("timestamp"), "source_record_sha256": digest(raw)}
            if key not in candidates:
                candidates[key] = {
                    "record_key": key, "native_message_id": payload.get("id"), "payload_sha256": payload_hash,
                    "text_sha256": digest(text.encode("utf-8")), "text_characters": len(text),
                    "source_create_time": metadata.get("create_time"), "content_item_kinds": kinds,
                    "content_types": [part.get("type") for part in content], "origins": [],
                    "prior_exact_text_matches": [location for location, value in texts if value == text],
                    "prior_text_matches_ignoring_outer_whitespace": [location for location, value in texts if value.strip() == text.strip()],
                    "prior_verbatim_fragment_matches": [location for location, value in texts if len(value) >= 16 and value in text and any(label in location for label in ("verbatim", "exact_text"))],
                    "credential_pattern_detected": bool(SECRET_PATTERN.search(raw.decode("utf-8"))),
                    "attachments": [],
                    "source_jsonl_record": raw.decode("utf-8"),
                }
                if "# Files pasted by the user:" in text:
                    for match in re.finditer(r"(?m)^## .*?: ([A-Za-z]:[\\/][^\r\n]+)$", text):
                        candidates[key]["attachments"].append(capture_attachment(Path(match[1]), key))
            candidates[key]["origins"].append(origin)
        sources.append({"path": str(path), "captured_prefix_bytes": complete_bytes,
                        "captured_prefix_sha256": digest(data[:complete_bytes]), "native_user_records": user_count,
                        "history_base": meta.get("history_base")})
    if not sources:
        if saved:  # the thread ended 09-29 and is fully archived; Codex later compressed its history (.jsonl.zst, 10-14)
            return {"status": "ok", "thread_id": THREAD_ID, "checked_at_utc": utc_now(), "source_files": 0,
                    "archived_record_versions": len(saved), "appended": 0, "note": "thread_history_not_found_archive_kept"}
        raise ValueError("matching_thread_history_unavailable")
    appended = 0
    now = utc_now()
    with archive.open("a", encoding="utf-8", newline="\n") as out:
        for item in sorted(candidates.values(), key=lambda i: (i["source_create_time"] or 0, i["record_key"])):
            if item["record_key"] in saved or item["credential_pattern_detected"]:
                continue
            origin = item["origins"][0]
            entry = {"schema_version": 1, "thread_id": THREAD_ID, "record_key": item["record_key"],
                     "native_message_id": item["native_message_id"], "payload_sha256": item["payload_sha256"],
                     "captured_at_utc": now, **origin, "source_jsonl_record": item["source_jsonl_record"]}
            out.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            saved[item["record_key"]] = entry
            appended += 1
        out.flush()
        os.fsync(out.fileno())
    records = []
    for item in candidates.values():
        indexed = {key: value for key, value in item.items() if key != "source_jsonl_record"}
        indexed["archived"] = item["record_key"] in saved
        records.append(indexed)
    records.sort(key=lambda item: item["source_create_time"] or 0)
    blocked = [item["record_key"] for item in records if not item["archived"]]
    index = {"schema_version": 1, "thread_id": THREAD_ID, "checked_at_utc": now, "sources": sources,
             "archive_path": str(archive), "archive_sha256": digest(archive.read_bytes()),
             "record_count": len(records), "archived_record_count": sum(item["archived"] for item in records),
             "appended_this_capture": appended, "blocked_record_keys": blocked,
             "excluded_runtime_records": excluded, "deferred_source_records": deferred, "records": records,
             "audio_limit": "Only native payload and explicit pasted-file attachments are preserved. Text is not original audio.",
             "coverage_basis": "All user.* native message payload versions in this thread's discovered histories, including pre-rewind variants; no summaries or other threads."}
    index_path = DESTINATION / "index.json"
    previous = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    # Assistant/tool output continues to grow in the native history. It is not
    # new user data and must not create a new backup snapshot every minute.
    meaningful_fields = ("thread_id", "archive_sha256", "records", "blocked_record_keys", "excluded_runtime_records")
    index_updated = any(previous.get(key) != index[key] for key in meaningful_fields)
    if index_updated:
        temporary = DESTINATION / ".index.tmp"
        with temporary.open("w", encoding="utf-8") as out:
            json.dump(index, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, index_path)
    return {"status": "partial" if blocked or deferred else "ok", "thread_id": THREAD_ID,
            "checked_at_utc": now, "source_files": len(sources), "native_user_record_versions": len(records),
            "archived_record_versions": len(saved), "appended": appended, "blocked": len(blocked),
            "excluded_runtime_records": len(excluded), "deferred_source_records": len(deferred),
            "pasted_attachments": sum(len(item["attachments"]) for item in records),
            "archive_sha256": index["archive_sha256"], "index_updated": index_updated, "index_path": str(index_path)}


def capture_once() -> dict:
    """Synchronous one-thread capture; returns metadata only, never source text."""
    try:
        with capture_lock():
            return _capture_once()
    except Exception as exc:
        # Exception messages can contain source text or file contents. Keep the
        # public result controlled and leave the prior append-only archive intact.
        return {"status": "blocked", "thread_id": THREAD_ID, "reason": type(exc).__name__, "checked_at_utc": utc_now()}


if __name__ == "__main__":
    result = capture_once()
    print(json.dumps(result, ensure_ascii=True))
    sys.exit(0 if result["status"] == "ok" else 2)
