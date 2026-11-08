"""Independent stdlib recount. Never imports the dashboard counters or InputStore.

Run with --root INCUBATOR --url http://127.0.0.1:PORT --out evidence.json.
Only aggregate numbers are written to the requested evidence file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import urllib.request
from collections import Counter
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def database(path):
    query = "?mode=ro" if Path(str(path) + "-wal").exists() else "?immutable=1&mode=ro"
    db = sqlite3.connect(path.resolve().as_uri() + query, uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    try:
        yield db
        assert db.total_changes == 0
    finally:
        db.close()


def independent_counts(root):
    counts = Counter()
    for channel in ("terminal", "discord", "other"):
        counts[f"quotes_{channel}"] = counts[f"chars_{channel}"] = 0

    def count_quote(row, name):
        channel = "other"
        for source in (row.get("channel"), row.get("source")):
            source = str(source or "").lower()
            candidates = [("discord", ("discord",)),
                          ("terminal", ("terminal", "终端", "chat", "conversation", "current user", "codex thread"))]
            matched = [key for key, words in candidates if any(word in source for word in words)]
            if matched:
                channel = matched[0]
                break
        if channel == 'other':
            for kind in ('terminal', 'discord'):
                if f'-owner-{kind}-' in name.lower():
                    channel = kind
                    break
        for key in ("owner_verbatim", "owner_message_verbatim", "owner_text", "exact_text", "verbatim", "text", "original", "message"):
            content = row.get(key)
            if isinstance(content, str) and content.strip():
                texts = [content]
                break
            if isinstance(content, list):
                texts = [s for s in content if isinstance(s, str) and s.strip()]
                break
        else:
            return False
        counts[f"quotes_{channel}"] += len(texts)
        counts[f"chars_{channel}"] += sum(sum(not char.isspace() for char in text) for text in texts)
        return bool(texts)

    linked = set()
    for path in sorted((root / "records").glob("*.json")):
        parsed = json.loads(path.read_text(encoding="utf-8-sig"))
        for row in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(row, dict) and count_quote(row, path.name) and row.get("source_record_key"):
                linked.add(row["source_record_key"])
    archive = root / "records/current-chat/messages.jsonl"
    if archive.is_file():
        for line in archive.read_text(encoding="utf-8-sig").splitlines():
            row = json.loads(line)
            if row.get("record_key") in linked:
                continue
            native = row["source_jsonl_record"]
            payload = (json.loads(native) if isinstance(native, str) else native)["payload"]
            if payload.get("role") == "user":
                content = payload.get("content", [])
                text = content if isinstance(content, str) else "\n".join(
                    part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
                count_quote({"channel": "terminal", "owner_verbatim": text}, "")

    with database(root / "discord/data/discord.sqlite3") as db:
        counts["discord_messages"] = db.execute("SELECT count(*) FROM inbound").fetchone()[0]
        counts["discord_attachments"] = 0
        ids = {row[0] for row in db.execute("SELECT message_id FROM inbound")}
        base = root / "discord/data/attachments"
        if base.is_dir():
            for directory in base.iterdir():
                if directory.name in ids and directory.name.isdecimal() and directory.is_dir():
                    counts["discord_attachments"] += len([f for f in directory.iterdir() if f.is_file() and not f.is_symlink()])

    with database(root / "input/data/input.sqlite3") as db:
        revisions = {r["id"]: dict(r) for r in db.execute("SELECT * FROM revisions ORDER BY rowid")}
        sessions = {s["id"]: dict(s) for s in db.execute("SELECT * FROM sessions")}
        checks = {}
        for row in db.execute("SELECT * FROM verifications WHERE scope='full_recording' ORDER BY verified_at,rowid"):
            checks[row["session_id"]] = dict(row)
        grouped = {}
        asrs = {}
        for row in revisions.values():
            if row["kind"] != "instruction":
                grouped.setdefault(row["session_id"], []).append(row)
            if row["kind"] == "asr":
                asrs[row["session_id"]] = row

        def label(row):
            if row["kind"] == "postprocess" and row["source"] == "llm_postprocess":
                return "machine"
            return json.loads(row["data_json"]).get("correction_type") if row["kind"] == "correction" else None

        def confirmed(sid):
            check = checks.get(sid)
            if not check or check["revision_id"] not in revisions:
                return None
            revision = revisions[check["revision_id"]]
            if (sessions[sid]["original_sha256"] != check["original_sha256"] or
                    revision["sha256"] != check["revision_sha256"] or
                    hashlib.sha256(revision["content"].encode("utf-8")).hexdigest() != check["revision_sha256"] or
                    not re.search(r"[㐀-鿿A-Za-z\d]", revision["content"])):
                return None
            rows = grouped[sid]
            ids = [r["id"] for r in rows]
            later = rows[ids.index(revision["id"]) + 1:]
            if later and not (all(label(r) in ("draft", "user_rewrite", "machine") for r in later) and
                              (label(later[-1]) == "machine" or label(later[-1]) == "user_rewrite" and later[-1]["content"] != revision["content"])):
                return None
            seen = set()
            current = revision
            while current and current["id"] not in seen:
                seen.add(current["id"])
                if current["kind"] == "asr":
                    return current if not current["parent_id"] else None
                if current["kind"] == "correction":
                    if current is revision and label(current) != "recognition_correction":
                        return None
                elif current["kind"] != "postprocess" or current["source"] not in ("asr_postprocess", "llm_postprocess"):
                    return None
                current = revisions.get(current["parent_id"])
            return None

        for key in ("confirmed_count", "confirmed_hours", "unconfirmed_count", "unconfirmed_hours"):
            counts[key] = 0
        for sid, latest_asr in asrs.items():
            root_asr = confirmed(sid)
            duration = json.loads((root_asr or latest_asr)["data_json"]).get("metadata", {}).get("duration_seconds")
            known = type(duration) in (int, float) and math.isfinite(duration) and duration > 0
            state = "confirmed" if root_asr and known else "unconfirmed"
            counts[f"{state}_count"] += 1
            counts[f"{state}_hours"] += duration / 3600 if known else 0
        counts["corrections"] = db.execute("SELECT count(*) FROM revisions WHERE kind='correction' AND source='user_correction'").fetchone()[0]

    rows = json.loads((root / "data/owner-replies.json").read_text(encoding="utf-8-sig"))["records"]
    counts["replies"] = sum(r.get("status") != "withdrawn" for r in rows)
    rows = json.loads((root / "data/owner-ratings.json").read_text(encoding="utf-8-sig"))["records"]
    for verdict, key in (("用", "rating_use"), ("没用", "rating_no"), ("返工", "rating_rework")):
        counts[key] = sum(r.get("verdict") == verdict for r in rows)
    materials = (root / "projects/materials.md").read_text(encoding="utf-8-sig")
    counts["materials"] = len(set(re.findall(r"^\|\s*(M\d+)\s*\|", materials, re.MULTILINE)))
    return dict(counts)


def compare(root, url):
    independent = independent_counts(root)
    with urllib.request.urlopen(url.rstrip('/') + '/api/accumulation', timeout=30) as response:
        page = json.load(response)
    rows = [{"key": m["key"], "label": m["label"], "independent": round(independent[m["key"]], 6),
             "page": m["total"], "match": m["total"] is not None and abs(independent[m["key"]] - m["total"]) <= 0.000001}
            for m in page["metrics"]]
    return {"pass": all(r["match"] for r in rows), "metrics": rows, "page_generated_at": page["generated_at"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--url', required=True)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r'http://127\.0\.0\.1:\d+', args.url):
        parser.error('--url must be an isolated loopback port')
    if args.url.endswith(':8765'):
        parser.error('use an isolated port, not production 8765')
    report = compare(args.root, args.url)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"{sum(r['match'] for r in report['metrics'])}/{len(report['metrics'])} counts match")
    raise SystemExit(0 if report['pass'] else 1)


if __name__ == '__main__':
    main()
