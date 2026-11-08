"""「等你决定」汇总页的数据：每次从 projects/backlog.md 现读，回复和评价写进 JSON，不改 backlog。"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sys as _sys
_sys.path.append(str(Path(__file__).resolve().parent.parent / "control"))
import instance_config as ic  # noqa: E402  (line titles; defaults = today's values)

BACKLOG_LIMIT = 32 * 1024 * 1024
STALE_DAYS = 7
RESULT_DAYS = 5
RESULT_LIMIT = 150
FULL_LIMIT = 1500
SECTIONS = tuple(ic.get("lines")[r] for r in ("coordinator", "work", "uvox", "quant", "intelligence", "life") if r in ic.get("lines"))  # the instance's lines (instance.json `lines`); the old 工作线 split in two (owner\x2010-26)
LEGACY_SECTION = "工作线"  # replies saved before the split carry it; new_replies routes them by where their row is now
LEGACY_TARGET = ic.get("lines").get("work", SECTIONS[0])  # a legacy reply whose row is no longer in backlog.md goes to the old work deputy, who kept the role key `work`
VERDICTS = ("用", "没用", "返工")
REPLIES_FILE = "owner-replies.json"
RATINGS_FILE = "owner-ratings.json"

_BLOCKED = re.compile(r"^- \[blocked: 等你决定([^\]]*)\]\s*(.*)$")
_DONE = re.compile(r"^- \[done (\d\d?)-(\d\d?)[^\]]*\]\s*(.*)$")
_FACT = re.compile(r"〔事实：(.+?)；\s*核对\s*(\d\d?)-(\d\d?)[^〕]*〕")
_COMMAND = re.compile(r"`([^`]*(?:Start-Process|wsl )[^`]*)`")
_DEADLINE = re.compile(r"(?<![\d-])\d{1,2}[-/月]\d{1,2}[日号]?\s*(?:前|之前|截止)|截止|时限|最晚|deadline", re.I)
_NO_DEADLINE = re.compile(r"(?:没有|无|不设)(?:截止日?|时限)")
_BY_WHO = re.compile(r"（(?:审查员|总理)[^）]*）|〔[^〕]*〕")
def _commands(text: str) -> list[str]:
    return [c for c in _COMMAND.findall(text) if len(c) > 20 and "<" not in c][:3]


_AUDIT_FILE = re.compile(r"audits/([\w.\-]+\.md)")


def _file_commands(root: Path, line: str) -> list[str]:
    """Open commands in the first 6 lines of the audits/ files a done row names (the result file, its review)."""
    found: list[str] = []
    for name in dict.fromkeys(_AUDIT_FILE.findall(line.replace("\\", "/"))):
        try:
            with (Path(root) / "audits" / name).open(encoding="utf-8-sig") as stream:
                head = "".join(stream.readline() for _ in range(6))
        except OSError:
            continue
        head = re.sub(r"```\w*", "", head)
        loose = [l.strip() for l in head.splitlines() if "`" not in l and len(l.strip()) > 20 and "<" not in l and re.search(r"Start-Process|python\.exe|wsl ", l)]  # a command in a fenced block
        found += [c for c in _commands(head) + loose if c not in found]
    return found


_BLOCKS = re.compile(r"挡着|挡住|卡着|卡住|阻塞|才能(?:继续|往下|开工|上线|合)")
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
_LOCK = threading.Lock()


def _read(root: Path) -> str:
    path = Path(root) / "projects" / "backlog.md"
    try:
        if not path.is_file() or path.stat().st_size > BACKLOG_LIMIT:
            return ""
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def _id(kind: str, head: str) -> str:
    return hashlib.sha1(f"{kind}:{head}".encode("utf-8")).hexdigest()[:10]


def _date(month: str, day: str, now: datetime) -> datetime | None:
    today = now.astimezone().date()
    for year in (today.year, today.year - 1):
        try:
            found = datetime(year, int(month), int(day), tzinfo=now.astimezone().tzinfo)
        except ValueError:
            return None
        if found.date() <= today + timedelta(days=1):
            return found
    return None


def _labelled(text: str, labels: list[str]) -> list[dict[str, str]]:
    """Options written as numbered labels in order, taking the longest run (latest if tied)."""
    pattern = "{}"
    spot = lambda label, pos: re.compile(pattern.format(re.escape(label))).search(text, pos)
    best: list[tuple[str, int]] = []
    for first in re.finditer(pattern.format(re.escape(labels[0])), text):
        run, pos = [(labels[0], first.end())], first.end()
        for label in labels[1:]:
            match = spot(label, pos)
            if not match:
                break
            run.append((label, match.end()))
            pos = match.end()
        if len(run) >= len(best):
            best = run
    if len(best) < 2:
        return []
    return [{"label": label, "text": re.sub(r"\s+", " ", re.split(f"[{_CIRCLED}]", text[end:end + 28])[0]).strip("：: ，。；、")}
            for label, end in best]

_LETTER_MARKER = re.compile(r'(?<![A-Za-z0-9])([A-E])(?![：:][\\/])(?=[：:、）)\s「"（(])')
_QUESTION = re.compile(r"问题\s*(\d+)\s*[：:，,]")


def _lettered(text: str, *, full_text: bool = False) -> list[dict[str, str]]:
    """Read top-level A–E options, ignoring labels quoted in examples or parentheses."""
    pairs = {"(": ")", "（": "）", "[": "]", "【": "】", "「": "」", "『": "』",
             "“": "”", "‘": "’", '"': '"', "`": "`"}
    markers, stack = [], []
    for pos, char in enumerate(text):
        if stack:
            if char == stack[-1]:
                stack.pop()
            elif char in pairs and char not in ('"', "`"):
                stack.append(pairs[char])
            continue
        if char in pairs:
            stack.append(pairs[char])
            continue
        match = _LETTER_MARKER.match(text, pos)
        if match:
            before = text[:match.start()].rstrip()
            if not before or before[-1] in "：:。！？；;，,、)]）】」』”":
                markers.append((match.group(1), match.start(), match.end()))

    best = []
    for index, marker in enumerate(markers):
        if marker[0] != "A":
            continue
        run, expected, incomplete = [marker], "B", False
        for candidate in markers[index + 1:]:
            if candidate[0] == expected:
                run.append(candidate)
                expected = chr(ord(expected) + 1)
                if expected > "E":
                    break
            elif candidate[0] == "A":
                break
            elif full_text:
                incomplete = True
                break
        if not incomplete and len(run) >= 2 and len(run) >= len(best):
            best = run
    if len(best) < 2:
        return []
    starts = [marker[1] for marker in best[1:]] + [None]
    options = []
    for (label, _, end), next_start in zip(best, starts):
        value = text[end:next_start] if full_text else text[end:min(end + 28, next_start)] if next_start is not None else text[end:end + 28]
        options.append({"label": label, "text": re.sub(r"\s+", " ", value).strip("：: ，。；、")})
    return options


def _options(body: str, *, full_text: bool = False) -> list[dict[str, str]]:
    questions = list(_QUESTION.finditer(body))
    if len(questions) > 1:
        options = []
        for index, question in enumerate(questions):
            end = questions[index + 1].start() if index + 1 < len(questions) else len(body)
            options.extend({**option, "label": f"{question.group(1)}{option['label']}"}
                           for option in _lettered(body[question.end():end], full_text=full_text))
        if options:
            return options
    return _lettered(body, full_text=full_text) or _labelled(body, list(_CIRCLED))


def _summary(body: str) -> str:
    plain = _BY_WHO.sub("", _COMMAND.sub("", body)).strip()
    first = re.split(r"[（。！？\n]", plain, maxsplit=1)[0].strip()
    first = first if len(first) >= 5 else plain[:90]
    return first if len(first) <= 100 else first[:97] + "…"


def _seen(root: Path) -> dict[str, str]:
    """review_stats' first sighting of each open row (control/data/review-stats-seen.json, refreshed each coordinator
    review): backlog.md keeps no times, and the page sorts by it when he picks newest/oldest first (owner\x2010-21)."""
    try:
        data = json.loads((Path(root) / "control" / "data" / "review-stats-seen.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def extract_pending(root: Path, now: datetime | None = None) -> dict[str, Any]:
    return extract_text(_read(root), now, _seen(root))


def extract_text(text: str, now: datetime | None = None, seen: dict[str, str] | None = None, *,
                 full_options: bool = False) -> dict[str, Any]:
    """Every `[blocked: 等你决定…]` row, sorted: has a deadline, then blocks other work, then the rest.
    The one place that counts them (the page, the daily report and review-stats all read it).
    `first_seen`: when review_stats first saw the row (key as review_stats.row_key), None if it has not yet."""
    now = now or datetime.now(timezone.utc)
    section, items = "?", []
    for index, raw in enumerate(text.split("\n")):
        line = raw.rstrip("\r")
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        match = _BLOCKED.match(line)
        if not match:
            continue
        detail, body = match.group(1).strip("（）() ：:"), match.group(2)
        facts = []
        for text, month, day in _FACT.findall(body):
            checked = _date(month, day, now)
            age = (now.astimezone().date() - checked.date()).days if checked else None
            facts.append({"text": text, "checked": f"{int(month):02d}-{int(day):02d}", "age_days": age,
                          "stale": age is None or age > STALE_DAYS})
        clean = _FACT.sub("", body)
        deadline = _DEADLINE.search(_NO_DEADLINE.sub("", clean))
        recommend = re.search(r"建议[^。；\n]{0,80}", clean)
        head = body[:80]
        items.append({
            "id": _id("pending", head), "head": head, "line": index + 1, "section": section,
            "summary": _summary(clean), "options": _options(_COMMAND.sub("", clean), full_text=full_options),
            "recommendation": recommend.group(0) if recommend else None,
            "deadline": deadline.group(0).strip() if deadline else None,
            "blocks": bool(_BLOCKS.search(clean.replace("不挡", ""))),
            "quiet": "不急" in detail or "不急" in clean[:100], "detail": detail or None,
            "commands": _commands(clean), "facts": facts, "facts_missing": not facts,
            "full": clean[:FULL_LIMIT], "first_seen": (seen or {}).get(f"blocked|{line.split('] ', 1)[-1][:160]}"),
        })
    items.sort(key=lambda it: (not it["deadline"], not it["blocks"], it["quiet"], it["line"]))
    counts = {name: 0 for name in SECTIONS}
    for item in items:
        counts[item["section"]] = counts.get(item["section"], 0) + 1
    return {"total": len(items), "by_section": counts, "items": items}


_ROW = re.compile(r"^- \[(.+?)\] (.+)$")
_WAIT_OWNER = re.compile(r"blocked[:：]\s*等\s*(?:你|owner)", re.I)  # same test as review_stats.WAIT_OWNER_RE
_ASK = re.compile(r"blocked[:：]\s*等你决定")


def owner_wait_items(text: str, now: datetime | None = None, seen: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """`[blocked: 等你… / 等 owner …]` rows that are not 等你决定: something he has to do or answer elsewhere (the 做 cards).
    `status` is what the row waits for, `head` the first 80 characters of the row text (the same id rule as 等你决定)."""
    now = now or datetime.now(timezone.utc)
    section, items = "?", []
    for index, raw in enumerate(text.split("\n")):
        line = raw.rstrip("\r")
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        match = _ROW.match(line)
        if not match or not _WAIT_OWNER.match(match.group(1)) or _ASK.match(match.group(1)):
            continue
        status, body = re.sub(r"^blocked[:：]\s*", "", match.group(1)).strip(), match.group(2)
        deadline = _DEADLINE.search(_NO_DEADLINE.sub("", body))
        items.append({
            "id": _id("owner-wait", body[:80]), "head": body[:80], "line": index + 1, "section": section,
            "status": status, "summary": _summary(body), "deadline": deadline.group(0).strip() if deadline else None,
            "blocks": bool(_BLOCKS.search(body.replace("不挡", ""))), "quiet": "不急" in match.group(1) or "不急" in body[:100],
            "commands": _commands(body), "full": body[:FULL_LIMIT],
            "first_seen": (seen or {}).get(f"blocked|{line.split('] ', 1)[-1][:160]}"),
        })
    return items


def card_key(kind: str, text: str, item: dict[str, Any]) -> str:
    """The 要你的 card's stable key for a blocked row (`item` from extract_text / owner_wait_items): the row text up to its first
    （ ( 〔 (notes are appended there), at most 80 characters, so a note added later does not make a new card. A row whose
    head is too short for that keeps the old `id`."""
    body = text.split("\n")[item["line"] - 1].rstrip("\r").split("] ", 1)[-1]
    head = re.split(r"[〔（(]", body, maxsplit=1)[0].strip()[:80]
    return hashlib.sha1(f"todo-{kind.lower()}:{head}".encode("utf-8")).hexdigest()[:10] if len(head) >= 12 else item["id"]


TODO_IDS_FILE = "owner-todo-ids.json"
_IDS_LOCK = threading.Lock()


@contextlib.contextmanager
def _file_lock(path: Path, timeout: float = 5.0, stale: float = 30.0):
    """The input server and the scheduler's report both number cards: one lockfile next to the registry (O_EXCL, stale ones broken)."""
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    end = time.time() + timeout
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError):
            try:
                if time.time() - lock.stat().st_mtime > stale:
                    lock.unlink()
                    continue
            except OSError:
                continue
            if time.time() > end:
                raise TimeoutError(f"{lock} busy")
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def codes_for(data_dir: Path, keys: list[tuple[str, str]], allocate: bool = True) -> dict[str, dict[str, Any]]:
    """Short card numbers (D12, V13, T14, K15: one counter, the letter says the kind at the time it was numbered) for
    (kind letter, stable row key). A key keeps its number for good, however its row's text is edited later; new keys get the
    next number when `allocate`. Returns {"D:<key>": {"code": "D12", "at": <UTC time first numbered>}} for the keys asked."""
    path = Path(data_dir) / TODO_IDS_FILE
    with _IDS_LOCK, _file_lock(path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        known, nxt, changed = data.get("codes", {}), int(data.get("next", 1)), False
        for kind, key in keys:
            if allocate and f"{kind}:{key}" not in known:
                known[f"{kind}:{key}"] = {"code": f"{kind}{nxt}", "at": _now()}
                nxt, changed = nxt + 1, True
        if changed:
            Path(data_dir).mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=data_dir, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"next": nxt, "codes": known}, stream, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
    return {f"{kind}:{key}": known[f"{kind}:{key}"] for kind, key in keys if f"{kind}:{key}" in known}


def _result_rows(root: Path, now: datetime) -> list[dict[str, Any]]:
    from .dashboard import _control

    now = now or datetime.now(timezone.utc)
    found = []
    for row in _control("backlog_history").all_rows(Path(root) / "projects/backlog.md", _read(root),
                                         since=now.astimezone().date() - timedelta(days=RESULT_DAYS)):
        line = row["raw"]
        match = _DONE.match(line)
        if not match or "审查通过" not in line:
            continue
        commands = (_commands(line) or _file_commands(root, line))[:2]
        when = _date(match.group(1), match.group(2), now)
        if not commands or when is None or (now.astimezone().date() - when.date()).days < 0:
            continue
        status = line[len("- ["):line.index("]")]
        body = match.group(3) or status.split("：", 1)[-1]
        head = line[2:82]
        found.append({"id": _id("result", head), "head": head, "done": f"{when:%m-%d}", "line": row["line"], "source": row["source"],
                      "title": _summary(body)[:110], "commands": commands, "_when": when})
    found.sort(key=lambda it: (it["_when"], it["line"]), reverse=True)
    return found


def _result_window(root: Path, now: datetime) -> tuple[list[dict[str, Any]], int]:
    cutoff = now.astimezone().date()
    found, omitted = [], 0
    for item in _result_rows(root, now):
        if (cutoff - item["_when"].date()).days > RESULT_DAYS:
            omitted += 1
        elif len(found) < RESULT_LIMIT:
            del item["_when"]
            found.append(item)
    return found, omitted


def recent_results(root: Path, now: datetime | None = None) -> list[dict[str, Any]]:
    """Done rows from the last RESULT_DAYS days that passed review and carry an open command."""
    return _result_window(root, now or datetime.now(timezone.utc))[0]


def older_results_count(root: Path, now: datetime | None = None) -> int:
    return _result_window(root, now or datetime.now(timezone.utc))[1]


def load(data_dir: Path, name: str) -> list[dict[str, Any]]:
    try:
        records = json.loads((Path(data_dir) / name).read_text(encoding="utf-8"))["records"]
        return records if isinstance(records, list) else []
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _append(data_dir: Path, name: str, record: dict[str, Any]) -> None:
    data_dir = Path(data_dir)
    with _LOCK:
        records = load(data_dir, name) + [record]
        data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=data_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"records": records}, stream, ensure_ascii=False, indent=1)
        os.replace(tmp, data_dir / name)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_reply(root: Path, data_dir: Path, item_id: str, option: str, note: str) -> dict[str, Any]:
    option, note = str(option).strip()[:60], str(note).strip()[:1000]
    if not option and not note:
        raise ValueError("请选一个选项或写几个字。")
    item = next((it for it in extract_pending(root)["items"] if it["id"] == item_id), None)
    if item is None:
        raise LookupError("这一条已经不在「等你决定」里了，请刷新页面。")
    record = {"at": _now(), "kind": "reply", "id": item_id, "section": item["section"], "head": item["head"],
              "option": option, "note": note, "status": "new"}
    _append(data_dir, REPLIES_FILE, record)
    return record


def save_rating(root: Path, data_dir: Path, item_id: str, verdict: str, note: str,
                now: datetime | None = None) -> dict[str, Any]:
    if verdict not in VERDICTS:
        raise ValueError("评价只能是「用」「没用」「返工」。")
    item = next((it for it in recent_results(root, now) if it["id"] == item_id), None)
    if item is None:
        raise LookupError("这一件已经不在「请你评价」里了，请刷新页面。")
    record = {"at": _now(), "kind": "rating", "id": item_id, "head": item["head"], "done": item["done"],
              "verdict": verdict, "note": str(note).strip()[:1000]}
    _append(data_dir, RATINGS_FILE, record)
    return record


def live_replies(data_dir: Path) -> dict[str, dict[str, Any]]:
    """The latest reply per row id, withdrawn ones ignored (the owner's 撤回 on the 要你的 page)."""
    return {r.get("id"): r for r in load(data_dir, REPLIES_FILE) if r.get("status") != "withdrawn"}


def snapshot(root: Path, data_dir: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    pending = extract_pending(root, now)
    results, results_omitted = _result_window(root, now)
    replies = live_replies(data_dir)
    ratings = {r.get("id"): r for r in load(data_dir, RATINGS_FILE)}
    for item in pending["items"]:
        item["reply"] = replies.get(item["id"])
    for item in results:
        item["rating"] = ratings.get(item["id"])
    results.sort(key=lambda item: item["rating"] is not None)  # rated ones sink; the order within each group stays newest first
    waiting = {item["id"] for item in pending["items"]}
    handled = sorted((r for i, r in replies.items() if i not in waiting), key=lambda r: r.get("at", ""), reverse=True)
    return {"generated_at_utc": _now(), "pending": pending, "results": results, "results_omitted": results_omitted,
            "handled": handled,  # replied rows no longer waiting
            "files": {"replies": str(Path(data_dir) / REPLIES_FILE), "ratings": str(Path(data_dir) / RATINGS_FILE)}}


def _rewrite(data_dir: Path, name: str, records: list[dict[str, Any]]) -> None:
    fd, tmp = tempfile.mkstemp(dir=data_dir, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"records": records}, stream, ensure_ascii=False, indent=1)
    os.replace(tmp, Path(data_dir) / name)


def new_replies(data_dir: Path, section: str, backlog_text: str | None = None) -> list[dict[str, Any]]:
    """Replies of one line (`## ` heading, e.g. 组织线) the deputy has not handled yet, oldest first. A reply saved with the old
    section 工作线 goes to the line its row sits in now (`backlog_text`, the current backlog.md); a row that is gone: Arbitros 线
    (LEGACY_TARGET); a row whose 80-character head is under two different lines: nobody, it stays `new` for a person to sort out."""
    found = [r for r in load(data_dir, REPLIES_FILE) if r.get("status") == "new"]
    if not any(r.get("section") == LEGACY_SECTION for r in found):
        return [r for r in found if r.get("section") == section]
    where: dict[str, str | None] = {}
    for it in (extract_text(backlog_text)["items"] if backlog_text else []):
        now_in = LEGACY_TARGET if it["section"] == LEGACY_SECTION else it["section"]
        where[it["id"]] = now_in if where.get(it["id"], now_in) == now_in else None
    return [r for r in found if (where.get(r.get("id"), LEGACY_TARGET) if r.get("section") == LEGACY_SECTION
                                 else r.get("section")) == section]


def replies_block(data_dir: Path, section: str, script: Path | str, backlog_text: str | None = None) -> str:
    """Prompt text for a deputy's run: the owner's new replies verbatim, and the command that marks one handled."""
    found = new_replies(data_dir, section, backlog_text)
    if not found:
        return ""
    lines = ["", "", "【owner 在「要你的」页的回复，先处理这些（原样附上）】"]
    for r in found:
        card = f"，卡 {r['card']}" if r.get("card") else ""
        lines.append(f"- 回复 id {r['id']}（{r.get('at', '')}{card}）\n  行首：{r.get('head', '')}\n  选的选项：{r.get('option') or '（没选）'}\n  说明：{r.get('note') or '（无）'}\n  处理完运行：`python {script} done {r['id']}`")
    lines.append("照 owner 的选择办：改原行（选定后把它的 `[blocked: 等你决定]` 改成 todo 或 done，并写出处），然后对处理完的每条回复运行它下面那条命令把它标成 done。没处理完的不要标。"
                 "选项是「做完了」的，是 owner 说他做完了那一件（`[blocked: 等你…]` 行）：核对后把原行改成 todo 或 done。")
    return "\n".join(lines)


def mark_withdrawn(data_dir: Path, reply_id: str) -> int:
    """Every `new` reply with this id becomes `withdrawn` (the deputy never sees it); `done` ones stay; returns how many."""
    with _LOCK:
        records, count = load(data_dir, REPLIES_FILE), 0
        for r in records:
            if r.get("id") == reply_id and r.get("status") == "new":
                r["status"], r["withdrawn_at"] = "withdrawn", _now()
                count += 1
        if count:
            _rewrite(Path(data_dir), REPLIES_FILE, records)
    return count


def mark_done(data_dir: Path, reply_id: str) -> int:
    """Every `new` reply with this id becomes `done` (with the time); returns how many."""
    with _LOCK:
        records, count = load(data_dir, REPLIES_FILE), 0
        for r in records:
            if r.get("id") == reply_id and r.get("status") == "new":
                r["status"], r["done_at"] = "done", _now()
                count += 1
        if count:
            _rewrite(Path(data_dir), REPLIES_FILE, records)
    return count


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "done":
        n = mark_done(Path(__file__).resolve().parent.parent / "data", sys.argv[2])
        print(f"marked {n} reply(ies) done" if n else "no new reply with that id")
        sys.exit(0 if n else 1)
    print("usage: python pending.py done <reply id>")
    sys.exit(2)
