"""Read-only progress estimate; never exports data or starts training.

Counting policy: round2_prep.py / audits/2026-10-31-input-asr-round2-prep.md.
The page checks stored hashes only. Audio bytes must be rechecked at export time.
"""
from __future__ import annotations

import difflib
import json
import math
import sqlite3
from pathlib import Path

from .asr_bench.bench_lib import cer, content_text, is_content, tokens
from .store import InputStore, sha256_text

TARGET_HOURS = 10
HOLDOUT_START, HOLDOUT_END = "2026-10-28", "2026-10-31"
MAX_RATIO = 0.35
HERE = Path(__file__).resolve().parent


def count_hours(recordings, bench_ids, round1_ids):
    """Pure tally of one normalized row per recording (seconds, no rounding).

    Keep the original training pool; new pairs need <=35% content-token edits.
    The frozen evaluation dates never move forward with newly recorded audio.
    """
    totals = {"train_seconds": 0.0, "holdout_seconds": 0.0,
              "unconfirmed_seconds": 0.0, "unconfirmed_count": 0}
    for row in recordings:
        dur = row["dur"]
        known_duration = (isinstance(dur, (int, float)) and not isinstance(dur, bool)
                          and math.isfinite(dur) and dur > 0)
        if not row["confirmed"] or not known_duration:
            totals["unconfirmed_count"] += 1
            if known_duration:
                totals["unconfirmed_seconds"] += dur
            continue
        if row["sid"] in bench_ids or HOLDOUT_START <= row["created"][:10] <= HOLDOUT_END:
            totals["holdout_seconds"] += dur
            continue
        if row["sid"] not in round1_ids and row["conf"] != row["asr"] and _over_edited(row):
            continue
        totals["train_seconds"] += dur
    return totals


_RATIO_CACHE = {}


def _over_edited(row):
    """True when the confirmed text differs from the ASR text by > MAX_RATIO content tokens.

    Cached per (recording id, confirmed-text sha256, asr-text sha256): only new or changed confirmations are recomputed.
    """
    key = (row["sid"], sha256_text(row["conf"]), sha256_text(row["asr"]))
    if key not in _RATIO_CACHE:
        if len(_RATIO_CACHE) > 20000:
            _RATIO_CACHE.clear()
        ref, hyp = _units(row["conf"]), _units(row["asr"])
        # A diff script is an upper bound on edit distance: if it already passes, the exact distance passes too.
        ops = difflib.SequenceMatcher(None, ref, hyp, autojunk=False).get_opcodes()
        bound = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal")
        if ref and bound / len(ref) <= MAX_RATIO:
            _RATIO_CACHE[key] = False
        else:
            errors, units = cer(row["conf"], row["asr"])
            _RATIO_CACHE[key] = not units or errors / units > MAX_RATIO
    return _RATIO_CACHE[key]


def _units(text):
    return [t.lower() for t in tokens(text) if is_content(t)]


def readonly_uri(db_path: Path):
    # An offline WAL-mode DB with no WAL needs no shared-memory sidecars.
    # Live DBs must keep WAL visibility: immutable would silently miss recent rows.
    offline = not Path(str(db_path) + "-wal").exists()
    return db_path.resolve().as_uri() + ("?immutable=1&mode=ro" if offline else "?mode=ro")


def read_recordings(db_path: Path):
    """Read a consistent DB snapshot, reusing the desk's confirmation rules.

    Latest full-recording confirmation wins, as in round2_prep.load_rows.
    No audio reads, training modules, writes, or 200-row history-list limit.
    """
    db = sqlite3.connect(readonly_uri(db_path), uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        asr_rows = {r["sid"]: dict(r) for r in db.execute("""
            SELECT s.id AS sid, s.created_at AS created, r.data_json, r.content AS asr
            FROM sessions s JOIN revisions r ON r.session_id=s.id AND r.kind='asr'
            ORDER BY r.rowid"""
        )}
        checks = {r["session_id"]: r for r in db.execute("""
            SELECT v.*, r.content, r.sha256 AS stored_text_sha,
                   s.original_sha256 AS stored_audio_sha
            FROM verifications v JOIN revisions r ON r.id=v.revision_id
            JOIN sessions s ON s.id=v.session_id
            WHERE v.scope='full_recording' ORDER BY v.verified_at, v.rowid"""
        )}
        for sid, row in asr_rows.items():
            check = checks.get(sid)
            row["confirmed"] = bool(check and
                check["stored_audio_sha"] == check["original_sha256"] and
                check["stored_text_sha"] == check["revision_sha256"] and
                sha256_text(check["content"]) == check["revision_sha256"] and
                InputStore._check_still_valid(db, sid, check["revision_id"]) and
                InputStore._literal_chain(db, check["revision_id"]) and content_text(check["content"]))
            if row["confirmed"]:
                cur = check["revision_id"]
                while True:
                    root = db.execute("SELECT * FROM revisions WHERE id=? AND session_id=?", (cur, sid)).fetchone()
                    if root["kind"] == "asr":
                        break
                    cur = root["parent_id"]
                row.update(asr=root["content"], data_json=root["data_json"], conf=check["content"])
            try:
                row["dur"] = json.loads(row["data_json"]).get("metadata", {}).get("duration_seconds")
            except (ValueError, TypeError, AttributeError):
                row["dur"] = None
        assert db.total_changes == 0
    finally:
        db.close()
    return list(asr_rows.values())


def read_summary(store: InputStore):
    recordings = read_recordings(store.db_path)
    bench_ids = {r["id"] for r in json.loads((HERE / "asr_bench/bench40.json").read_text(encoding="utf-8"))["items"]}
    round1_ids = set(json.loads((HERE / "asr_hours_policy.json").read_text(encoding="utf-8"))["round1_training_ids"])
    return count_hours(recordings, bench_ids, round1_ids)


def summary_text(summary):
    hours = summary["train_seconds"] / 3600
    gap = max(0, TARGET_HOURS - hours)
    progress = f"还差 {gap:.2f} 小时" if gap else "已到目标"
    return (f"可训练语音 {hours:.2f} 小时（目标约 {TARGET_HOURS} 小时，{progress}）；"
            f"另有 {summary['unconfirmed_count']} 条未确认录音共 {summary['unconfirmed_seconds'] / 3600:.2f} 小时，确认后可计入")
