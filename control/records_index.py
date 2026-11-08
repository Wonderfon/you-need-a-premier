"""Index of the owner's input records (stdlib only): records/*.json -> data/records-index/index.jsonl + README.md.

Mechanical fields are written here; the `labels` (tags / project / decision / one_line) are written by the executor
into labels.json and merged in. records/ is only ever read. Re-running is incremental: an unchanged file is skipped, a
changed file gets a new `changed` row and the old row stays. Run weekly: python records_index.py
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

# ---- CONFIG ----
RECORDS_DIR = Path(ic.V["workspace"] + r"\incore\records")
OUT_DIR = Path(ic.V["workspace"] + r"\incore\data\records-index")
INDEX_FILE = OUT_DIR / "index.jsonl"
LABELS_FILE = OUT_DIR / "labels.json"  # {file stem: {"tags": [...], "project": ..., "decision": ..., "one_line": ...}}
README_FILE = OUT_DIR / "README.md"
TEXT_KEYS = ("exact_text", "verbatim", "text_verbatim", "answer_verbatim", "original", "text", "verbatim_1",
             "exact_text_excerpts", "exact_text_excerpt", "exact_answer", "answers", "responses", "owner_message_exact",
             "answers_exact", "owner_text", "verbatim_answer", "answers_verbatim", "task_verbatim",
             "owner_verbatim", "owner_message_verbatim", "owner_messages_verbatim", "owner_answers_verbatim",
             "owner_question_verbatim", "owner_questions_verbatim", "owner_reply_verbatim", "owner_verbatim_question",
             "owner_verbatim_context", "owner_verbatim_sequence", "original_text", "previous_owner_text",
             "verbatim_question", "verbatim_idea", "verbatim_answers", "verbatim_chat", "verbatim_redacted",
             "clarification_verbatim", "threshold_clarification_verbatim", "execution_overrides_verbatim",
             "followup_verbatim", "followup1_verbatim", "followup2_verbatim", "followup3_verbatim",
             "followup4_verbatim", "followup5_verbatim", "followup6_verbatim", "message_1", "message_2",
             "owner_text_1", "owner_text_2", "owner_text_prev", "owner_message_verbatim_1", "owner_message_verbatim_2",
             "verbatim_2")
EXTRACTOR_VERSION = 2  # unchanged records must be re-extracted when field coverage changes
TIME_KEYS = ("received_local", "recorded_at", "captured_at", "received_utc", "received_at", "checked_at_utc",
             "date_local", "local_date", "recorded_on", "date")
TAGS = ("决策", "偏好", "纠正", "想法", "提问", "批准或否决", "任务")
# ----------------

_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[T ~]|\D{0,14}?)(\d{1,2}):(\d)(\d|x)?(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})?")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def extract_text(d) -> tuple[str | None, str]:
    """(field the owner's words were taken from, the words). Records name that field in many ways."""
    if isinstance(d, list):
        parts = [extract_text(x)[1] if isinstance(x, (dict, list)) else str(x) for x in d]
        return "text", "\n".join(p for p in parts if p)
    if not isinstance(d, dict):
        return None, ""
    keys = [k for k in TEXT_KEYS if k in d and d[k]]
    parts = [d[k] if isinstance(d[k], str) else "\n".join(
        x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in d[k])
        if isinstance(d[k], list) else json.dumps(d[k], ensure_ascii=False) for k in keys]
    # Some records only have messages/sequence/answers objects containing the text fields.
    parts += [text for k, v in d.items() if k not in keys and (isinstance(v, dict) or
              isinstance(v, list) and any(isinstance(x, (dict, list)) for x in v))
              if (text := extract_text(v)[1])]
    return (keys[0] if keys else "nested" if parts else None), "\n".join(parts)


def _to_local(t: datetime) -> datetime:
    return t.astimezone()  # this machine's zone, DST included (EDT -4 until 11-01, then EST -5)


def local_time(raw: str) -> tuple[str | None, int | None]:
    """(date, hour) in the owner's local time from a free-form timestamp; None where the record gives none."""
    m = _TIME.search(raw)
    if not m:
        d = _DATE.search(raw)
        return (d.group(0) if d else None), None
    date, hour, off = m.group(1), int(m.group(2)), m.group(5)
    if off and (off == "Z" or off[1:] == "00:00"):
        t = _to_local(datetime.fromisoformat(date).replace(hour=hour, tzinfo=timezone.utc))
        return t.strftime("%Y-%m-%d"), t.hour
    return date, hour if hour < 24 else None


def mechanical(path: Path) -> dict:
    raw = path.read_bytes()
    d = json.loads(raw.decode("utf-8"))
    key, text = extract_text(d)
    meta = d if isinstance(d, dict) else {}
    src = d[0].get("source") if isinstance(d, list) and d and isinstance(d[0], dict) else meta.get("source")
    tkey = next((k for k in TIME_KEYS if k in meta), None)
    received = str(meta[tkey]) if tkey else ""
    date, hour = local_time(received) if received else (None, None)
    date = date or (path.stem[:10] if _DATE.fullmatch(path.stem[:10]) else None)  # file names start with the date
    return {"file": path.stem, "received": received[:60], "received_key": tkey, "date_local": date, "hour_local": hour,
            "source": str(src or "")[:160], "text_key": key, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "chars": len(text), "file_sha256": hashlib.sha256(raw).hexdigest(), "extractor_version": EXTRACTOR_VERSION}


def read_rows(index_file: Path) -> list[dict]:
    try:
        lines = index_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def latest(rows: list[dict]) -> dict[str, dict]:
    return {r["file"]: r for r in rows}  # later rows win


def update(records_dir: Path, index_file: Path, labels_file: Path, now: datetime | None = None) -> dict:
    """Add rows for new and changed record files, fill labels from labels_file; returns the counts."""
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    rows = read_rows(index_file)
    cur = latest(rows)
    counts = Counter()
    for path in sorted(records_dir.glob("*.json")):
        m = mechanical(path)
        old = cur.get(m["file"])
        if old and old["file_sha256"] == m["file_sha256"] and old.get("extractor_version") == EXTRACTOR_VERSION:
            counts["unchanged"] += 1
            continue
        row = {**m, "status": "changed" if old else "new", "indexed_at": stamp, "labels": old.get("labels") if old else None}
        if old:
            row["prev_file_sha256"] = old["file_sha256"]
            row["labels_stale"] = bool(old.get("labels")) and (bool(old.get("labels_stale")) or old["file_sha256"] != m["file_sha256"])
        rows.append(row)
        cur[m["file"]] = row
        counts[row["status"]] += 1
    try:
        labels = json.loads(labels_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        labels = {}
    for name, row in cur.items():
        if name in labels and row.get("labels") != labels[name]:  # labels.json is the source; editing it clears `stale`
            row["labels"] = labels[name]
            row.pop("labels_stale", None)
            counts["labelled"] += 1
    index_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = index_file.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(index_file)
    return dict(counts)


def located_text(raw: str) -> list[dict]:
    """Recognized text blocks with their actual source lines, including compact nested message objects."""
    json.loads(raw)  # reject invalid JSON before inspecting fields
    decoder, out = json.JSONDecoder(), []
    for match in re.finditer(r'(?<!\\)"([^"\\]+)"\s*:\s*', raw):
        if match[1] not in TEXT_KEYS:
            continue
        value, _ = decoder.raw_decode(raw, match.end())
        if value:
            out.append({"field": match[1], "line": raw.count("\n", 0, match.start()) + 1,
                        "text": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)})
    return out


def catalog(records_dir: Path, index_file: Path | None = None) -> dict:
    """Read-only lookup; re-read originals so a missing/stale index cannot hide a reply. Never update the index."""
    try:
        indexed = latest(read_rows(index_file)) if index_file else {}
    except (OSError, ValueError, KeyError, TypeError):
        indexed = {}
    rows, gaps = [], []
    if not records_dir.is_dir():
        gaps.append(f"{records_dir}：目录不可读或不存在")
    for path in sorted(records_dir.glob("*.json")):
        try:
            content = path.read_bytes()
            raw = content.decode("utf-8")
            meta, fields = json.loads(raw), located_text(raw)
        except (OSError, ValueError) as exc:
            gaps.append(f"records/{path.name}：{type(exc).__name__}")
            continue
        old = indexed.get(path.stem)
        fresh = old and old.get("file_sha256") == hashlib.sha256(content).hexdigest()
        issues = []
        if not old:
            issues.append("索引缺项")
        elif not fresh or old.get("extractor_version") != EXTRACTOR_VERSION:
            issues.append("索引过期")
        if not fields:
            issues.append("未识别原话字段")
        gaps.extend(f"records/{path.name}：{s}" for s in issues)
        rows.append({"file": f"records/{path.name}", "date": path.name[:10], "fields": fields,
                     "search_text": raw + (json.dumps(old.get("labels"), ensure_ascii=False) if fresh and not old.get("labels_stale") else ""),
                     "issues": issues, "meta": meta})
    return {"rows": rows, "gaps": gaps}


def candidates(data: dict, project: tuple[str, ...] = (), paths: tuple[str, ...] = (),
               since: str | None = None, until: str | None = None, limit: int = 3) -> dict:
    """Literal project/label + full referenced path + inclusive file-date filters; returns candidates, never decisions.

    Every supplied dimension must match; paths within that dimension are alternatives. Dates are record filename
    dates, not proof of when a message was received. No basename-only matching and no semantic approval inference.
    """
    for value in (since, until):
        if value is not None:
            if not _DATE.fullmatch(value):
                raise ValueError("Dates must be YYYY-MM-DD")
            datetime.strptime(value, "%Y-%m-%d")
    if since and until and since > until:
        raise ValueError("since must not be after until")
    norm = lambda s: s.replace("\\\\", "/").replace("\\", "/").casefold()
    found = []
    for row in data["rows"]:
        if not _DATE.fullmatch(row["date"]) or (since and row["date"] < since) or (until and row["date"] > until):
            continue
        hay = norm(row["search_text"] + " " + row["file"])
        if project and not any(norm(p) in hay for p in project):
            continue
        matched = [p for p in paths if re.search(re.escape(norm(p)) + r"(?![\w./-])", hay)]
        if paths and not matched:
            continue
        if row["fields"]:
            found.append({k: row[k] for k in ("file", "date", "fields", "issues")} | {"paths": matched})
    found.sort(key=lambda r: (r["date"], r["file"]), reverse=True)
    return {"status": "候选" if found else "未找到", "total": len(found), "hits": found[:limit],
            "gaps": data["gaps"]}


def readme(rows: list[dict], now: datetime | None = None) -> str:
    cur = list(latest(rows).values())
    lab = [r["labels"] for r in cur if r.get("labels")]
    tags = Counter(t for x in lab for t in x.get("tags", []))
    proj = Counter(x.get("project", "general") for x in lab)
    dec = Counter(x["decision"] for x in lab if x.get("decision"))
    hours = Counter(r["hour_local"] for r in cur if r.get("hour_local") is not None)
    nohour = sum(1 for r in cur if r.get("hour_local") is None)
    fix = sorted((r["date_local"] or "", r["file"], r["labels"]["one_line"]) for r in cur
                 if r.get("labels") and "纠正" in r["labels"].get("tags", []))
    changed = [r["file"] for r in cur if r.get("status") == "changed"]
    unlabeled = [r["file"] for r in cur if not r.get("labels") or r.get("labels_stale")]
    out = ["# 输入记录索引", "",
           f"更新：{(now or datetime.now(timezone.utc)).isoformat(timespec='seconds')}　共 {len(cur)} 个记录文件，已标注 {len(lab)} 个。",
           "`index.jsonl` 每行一个记录文件（只追加；文件变了就加一行 `changed`，旧行保留）。原文不在这里，只有哈希和字数；原文在 `incore/records/`（只读）。",
           "脚本：`incore/control/records_index.py`（每周复盘时跑一次，增量）。标签写在 `labels.json`，脚本合并进每行的 `labels`。", "",
           "## 字段", "",
           "| 字段 | 含义 |", "|---|---|",
           "| file | 记录文件名（不含 .json） |",
           "| received / received_key | 记录里的时间原文，和它来自哪个字段（各文件叫法不一：received_local、recorded_at 等） |",
           "| date_local / hour_local | 本地日期和小时（UTC 时间按本机时区换算，自动算夏令时；记录里没给到小时的为空） |",
           "| source | 记录里的 source（截断 160 字） |",
           "| text_key / text_sha256 / chars | 取 owner 原话的字段名（exact_text 优先，其次 verbatim 等；没有原话的为空）、其 sha256、字数 |",
           "| file_sha256 | 整个文件的 sha256，用来判断是否被改 |",
           "| status / indexed_at / prev_file_sha256 | new 或 changed；入索引时间；changed 行记旧哈希 |",
           "| labels | tags（决策 偏好 纠正 想法 提问 批准或否决 任务）、project（tick.py PROJECTS 的 key，没有为 general）、decision（intel-decisions.md 的 D 几，无则空）、one_line（≤40 字） |",
           "| labels_stale | 文件变了而标签还是旧的；重读后改 labels.json 里这条（哪怕只改一个字）就会更新并清除 |", "",
           "## 标签条数（可多选，所以总和大于文件数）", "",
           "| 标签 | 条数 |", "|---|---|"]
    out += [f"| {t} | {tags.get(t, 0)} |" for t in TAGS]
    out += ["", "| 项目 | 条数 |", "|---|---|"] + [f"| {k} | {v} |" for k, v in proj.most_common()]
    out += ["", "| 决策 | 条数 |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(dec.items())]
    out += ["", f"## 纠正类一句话清单（{len(fix)} 条，做“纠正测试题”用）", ""]
    out += [f"- {d} `{f}`：{one}" for d, f, one in fix]
    out += ["", "## 按本地小时的消息分布", "", "| 小时 | 条数 |", "|---|---|"]
    out += [f"| {h:02d} | {hours.get(h, 0)} |" for h in range(24)]
    out += [f"| 未给出 | {nohour} |", "",
            "注：一条记录是 owner 的一次决定或一段话，不等于一条消息，时间多为“约”。作息分析只看大致分布。", ""]
    if changed:
        out += ["## 被改过的记录文件", ""] + [f"- {f}" for f in changed] + [""]
    if unlabeled:
        out += ["## 待标注", ""] + [f"- {f}" for f in unlabeled] + [""]
    return "\n".join(out)


def main() -> None:
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    find = sub.add_parser("find", help="read-only literal owner-word candidates; never updates records or index")
    find.add_argument("--records-dir", type=Path, default=RECORDS_DIR)
    find.add_argument("--index-file", type=Path, default=INDEX_FILE)
    find.add_argument("--project", action="append", default=[])
    find.add_argument("--path", action="append", default=[])
    find.add_argument("--since", help="inclusive record filename date, YYYY-MM-DD")
    find.add_argument("--until", help="inclusive record filename date, YYYY-MM-DD")
    args = parser.parse_args()
    if args.command == "find":
        print(json.dumps(candidates(catalog(args.records_dir, args.index_file), tuple(args.project), tuple(args.path),
                                    args.since, args.until), ensure_ascii=False, indent=2))
        return
    counts = update(RECORDS_DIR, INDEX_FILE, LABELS_FILE)
    README_FILE.write_text(readme(read_rows(INDEX_FILE)), encoding="utf-8")
    print(counts)


if __name__ == "__main__":
    main()
