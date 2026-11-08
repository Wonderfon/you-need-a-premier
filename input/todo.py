"""「要你的」页的数据：「等你决定」页和待审页并成一页（owner\x2010-27 终端）。

每次从 projects/backlog.md 现读，不缓存。一张卡对应 backlog 里的一行，四种：
  D 定      `[blocked: 等你决定]` 行：选项按钮加附言，回复写 data/owner-replies.json（副总理下次运行照办）
  T 做      其余 `[blocked: 等你… / 等 owner …]` 行：命令一键复制，「做完了」同样写进 owner-replies.json
  V 看/试   7 天内审查通过、写了「给 owner」或「等 owner 试用/看」的 done 行：打开，用／没用／返工（owner-ratings.json）／看过了
  K 知道就行 同上但没有可打开的东西：看过了
「看过了」「已经处理了」写 data/review-dismissed.json（只有哈希和时间）。卡片编号（D12）在 data/owner-todo-ids.json 里按
稳定的行键编一次，行文以后怎么改都不变。records/ 里 owner_verbatim 提到某编号的，视为他已在别处答了。
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import dashboard as d
from . import pending, review

STALE_DAYS = 3  # a card older than this gets 可能已过时 and the 已经处理了 button
HANDLED_DAYS = 7
RECORD_DAYS = 14  # how far back records/ is read for answers given elsewhere
KINDS = {"D": "定", "V": "看/试", "T": "做", "K": "知道就行"}
_RANK = {"D": 0, "T": 1, "V": 2, "K": 3}
_MINUTES = {"D": 2, "T": 3, "V": 5, "K": 1}
_CODE = re.compile(r"(?<![A-Za-z0-9])([DTVK]\d{1,5})(?![A-Za-z0-9])")
_CODE_ONLY = re.compile(r"[DTVK]\d{1,5}")
_TAIL_RE = re.compile(r"\s?〔owner \d\d-\d\d 点「已经处理了」〕")
_MIN = re.compile(r"约\s*(\d{1,3})\s*分钟")
_DUE = re.compile(r"(\d{1,2})[-/月](\d{1,2})[日号]?\s*(?:前|之前|截止|止)|截止\s*(?:到|于)?\s*(\d{1,2})[-/月](\d{1,2})")
_WHY = re.compile(r"[^。；（）()\n]*(?<![不无没])(?:花钱|付费|预算|对外|只有你|只有他|只有 owner|非你不可|非他不可|改方向|实盘)[^。；（）()\n]*")
_CMD = re.compile(r"`([^`]{12,})`")
_CMD_WORDS = ("Start-Process", "wsl ", "git ", "python")
_DAILY = ("D", "T")
_ACTIONS = {"reply", "rate", "seen", "handled", "undo"}


def _control(name: str) -> Any:
    return d._control(name)


@functools.cache
def _backlog_io() -> Any:
    return _control("backlog_io")


def _cmds(text: str) -> list[str]:
    return [c for c in _CMD.findall(text) if any(w in c for w in _CMD_WORDS) and "<" not in c][:3]


def _due(text: str, today: date) -> date | None:
    found = _DUE.search(text)
    if not found:
        return None
    month, day = (int(x) for x in (found.group(1), found.group(2)) if x) if found.group(1) else (int(found.group(3)), int(found.group(4)))
    for year in (today.year, today.year + 1):
        try:
            when = date(year, month, day)
        except ValueError:
            return None
        if when >= today - timedelta(days=180):
            return when
    return None


def _day(iso: str | None) -> date | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().date() if iso else None
    except ValueError:
        return None


def _dkey(kind: str, key: str) -> str:
    return hashlib.sha256(f"todo|{kind}|{key}".encode("utf-8")).hexdigest()[:24]


_LABELS = ("ABCDEF", "①②③④⑤⑥")
_SEGMENT = re.compile(r"「([A-F①②③④⑤⑥])(?![A-Za-z0-9])[\s：:、．.)）]*([^」]*)」")
_REC = re.compile(r"(?=(建议\s*(?:[：:]\s*|(?=[A-F①-⑥](?![A-Za-z0-9]))))([^。〔]*))")  # lookahead: a second 建议 inside the first one's sentence is found too
OPTION_MAX, RECOMMENDATION_MAX = 200, 160


def _clean_option_text(text: str, body: str) -> str:
    recommendation = clean_recommendation(body)
    if recommendation:
        text = re.sub(re.escape(recommendation) + r"\s*[。！？；;，,]?", "", text, count=1)
    text = re.sub(r"打开\s*[，,]\s*", "", text)
    return text.strip("：: ，。；、")


def _balanced_option(text: str) -> bool:
    pairs = {"(": ")", "（": "）", "[": "]", "【": "】", "「": "」", "『": "』",
             "“": "”", "‘": "’", '"': '"', "`": "`"}
    stack = []
    for char in text:
        if stack:
            if char == stack[-1]:
                stack.pop()
            elif char in pairs and char not in ('"', "`"):
                stack.append(pairs[char])
        elif char in pairs:
            stack.append(pairs[char])
    return not stack


def _clean_letter_options(body: str, options: list[dict[str, str]]) -> list[dict[str, str]]:
    if not options or any(not isinstance(o, dict) or not isinstance(o.get("label"), str)
                          or not isinstance(o.get("text"), str) for o in options):
        return []
    questions = [m.group(1) for m in pending._QUESTION.finditer(body)]
    labels = [o["label"] for o in options]
    if not questions and re.search(r"①[^②]*②", body, re.S):
        return []
    if len(questions) > 1:
        groups: dict[str, list[str]] = {}
        for label in labels:
            match = re.fullmatch(r"(\d+)([A-E])", label)
            if not match:
                return []
            groups.setdefault(match.group(1), []).append(match.group(2))
        if list(groups) != questions or any(group != list("ABCDE"[:len(group)]) or len(group) < 2
                                             for group in groups.values()):
            return []
    elif labels != list("ABCDE"[:len(labels)]) or len(labels) < 2:
        return []
    cleaned = [{"label": o["label"], "text": _clean_option_text(o["text"], body)} for o in options]
    if any(not (text := o["text"]) or len(text) > OPTION_MAX or "〔" in text or "〕" in text
           or not _balanced_option(text)
           for o in cleaned):
        return []
    return cleaned


def clean_options(body: str, parsed_options: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    """Return only complete quoted or parsed options; never expose a clipped button label."""
    # Use pending's top-level parse first so a quoted example is not mistaken for the row's choices.
    parsed_options = pending._options(body, full_text=True) if parsed_options is None else parsed_options
    if any(re.fullmatch(r"\d*[A-E]", o.get("label", "")) for o in parsed_options):
        return _clean_letter_options(body, parsed_options)

    found = [(m.group(1), m.group(2).strip()) for m in _SEGMENT.finditer(body)]
    best: list[tuple[str, str]] = []
    for labels in _LABELS:
        for start, (label, _) in enumerate(found):
            if label != labels[0]:
                continue
            run = [found[start]]
            for label2, text2 in found[start + 1:]:
                if label2 == labels[len(run)] if len(run) < len(labels) else False:
                    run.append((label2, text2))
            if len(run) >= len(best) and len(run) >= 2:
                best = run
    if best:
        nxt = next((labels[len(best)] for labels in _LABELS if best[0][0] == labels[0] and len(best) < len(labels)), None)
        later = {label for label, _ in found[found.index(best[-1]) + 1:]}
        cleaned = [(label, _clean_option_text(text, body)) for label, text in best]
        if nxt in later or any(not t or len(t) > OPTION_MAX or re.search(r"[「〔」]|[①②③④⑤⑥]", t)
                               for _, t in cleaned):
            return []
        return [{"label": label, "text": text} for label, text in cleaned]

    return []


def clean_recommendation(body: str) -> str | None:
    """From 「建议」 (followed by ： or an option letter, so not 建议日期 or 建议）) to the next 。, whole or nothing. A row that
    recommends two different letters (「建议 A，更正后改建议 B」, 「新建议 D」) has no single recommendation to follow: nothing."""
    found: list[str] = []
    for match in _REC.finditer(body):
        text = (match.group(1) + match.group(2)).strip().rstrip("，,；; ")
        while text.endswith(("）", ")")) and text.count("（") + text.count("(") < text.count("）") + text.count(")"):
            text = text[:-1].rstrip()
        balanced = text.count("「") == text.count("」") and text.count("（") + text.count("(") == text.count("）") + text.count(")")
        if balanced and len(text) <= RECOMMENDATION_MAX and len(re.sub(r"^建议\s*[：:]?\s*", "", text)) >= 1:
            found.append(text)
    letters = {re.match(r"建议\s*([A-F①-⑥])?", t)[1] for t in found} - {None}
    return found[0] if found and len(letters) <= 1 else None


def _card(kind: str, key: str, **fields: Any) -> dict[str, Any]:
    return {"kind": kind, "kind_label": KINDS[kind], "key": key, "dkey": _dkey(kind, key), **fields}


def build_cards(root: Path, today: date, now: datetime) -> list[dict[str, Any]]:
    """Every row that wants something from him, as a card (no state yet, no code). `tailed`: the row carries 〔owner MM-DD
    点「已经处理了」〕, he said it is handled and the line's next review checks it."""
    text = pending._read(root)
    lines = text.split("\n")
    seen = pending._seen(root)
    cards: list[dict[str, Any]] = []
    for it in pending.extract_text(text, now, seen, full_options=True)["items"]:
        due = _due(it["full"], today)
        why = _WHY.search(it["full"])
        body = pending._FACT.sub("", lines[it["line"] - 1].rstrip("\r").split("] ", 1)[-1])
        options = clean_options(body, it["options"])
        cards.append(_card(
            "D", pending.card_key("D", text, it), legacy=it["id"], section=it["section"], line=it["line"],
            head=it["head"], title=it["summary"] or it["head"],
            do="从下面选一个，可以补一句话" if options else "这一行没写成「A 做什么」「B 做什么」的选项，看全文后回一句话",
            why=re.sub(r"^为什么(?:非[他你]不可|要你定)[：:]\s*", "", why.group(0).strip(" ，、"))[:80] if why else None,
            recommendation=clean_recommendation(body),
            options=options, deadline=it["deadline"], due=due, blocks=it["blocks"], quiet=it["quiet"],
            commands=it["commands"], links=[], urls=[], facts=it["facts"], facts_missing=it["facts_missing"],
            full=it["full"], since=_day(it["first_seen"]), minutes=_MINUTES["D"], rowtext=lines[it["line"] - 1]))
    for it in pending.owner_wait_items(text, now, seen):
        cards.append(_card(
            "T", pending.card_key("T", text, it), legacy=it["id"], section=it["section"], line=it["line"],
            head=it["head"], title=it["summary"] or it["head"],
            do=it["status"][:160] or "照行里写的做", why=None, recommendation=None, options=[], deadline=it["deadline"],
            due=_due(it["full"] + it["status"], today), blocks=it["blocks"], quiet=it["quiet"],
            commands=it["commands"] or _cmds(it["full"]), links=[], urls=[], facts=[], facts_missing=False,
            full=it["full"], since=_day(it["first_seen"]), minutes=_MINUTES["T"], rowtext=lines[it["line"] - 1]))
    occurrence: dict[str, int] = {}
    for t in review.trials(root, today):
        t["title"] = _TAIL_RE.sub("", t["title"]).strip()  # our own note at the end of the row is not part of its name
        base = hashlib.sha256(f"view|{t['project']}|{t['title']}".encode("utf-8")).hexdigest()[:24]
        occurrence[base] = occurrence.get(base, 0) + 1
        key = base if occurrence[base] == 1 else hashlib.sha256(f"{base}|{occurrence[base]}".encode()).hexdigest()[:24]
        target = bool(t["commands"] or t["links"] or t["urls"])
        kind = "V" if target else "K"
        minutes = _MIN.search(t["summary"])
        title = f"{t['project']}：{t['title']}" if t["project"] else t["title"]
        cards.append(_card(
            kind, key, section=t["section"], line=t["n"] + 1, head=title[:80], title=title,
            do=(t["summary"] or "打开看一下，再点用／没用／返工") if kind == "V" else (t["summary"] or "知道就行，不用做什么"),
            why=None, recommendation=None, options=[], deadline=None, due=None, blocks=False, quiet=False,
            commands=t["commands"], links=t["links"], urls=t["urls"], facts=[], facts_missing=False, full=t["summary"],
            since=t["day"], minutes=int(minutes[1]) if minutes else _MINUTES[kind], legacy=t["id"],
            rowtext=lines[t["n"]] if t["n"] < len(lines) else ""))
    for c in cards:
        c["tailed"] = bool(_TAIL_RE.search(c["rowtext"]))
    return cards


def _urgency(card: dict[str, Any], today: date) -> tuple:
    due = card["due"]
    tier = (0 if due and due <= today + timedelta(days=3) else 1 if due or card["deadline"] else 2 if card["blocks"]
            else 4 if card["quiet"] else 3)
    age = (today - card["since"]).days if card["since"] else 0
    return tier, due or date.max, _RANK[card["kind"]], -age, card["line"]


def _elsewhere(root: Path, codes: dict[str, dict[str, Any]], today: date) -> dict[str, tuple[str, str]]:
    """{card code: (records file name, date)} for codes his own words (`owner_verbatim`) or `answers_cards` mention in a record dated
    on or after the day the card was numbered. Only those two fields: a record's other text may talk about the page."""
    out: dict[str, tuple[str, str]] = {}
    numbered = {v["code"]: _day(v.get("at")) for v in codes.values()}
    try:
        files = sorted(Path(root, "records").glob("20??-??-??-*.json"))
    except OSError:
        return out
    for path in files:
        try:
            day = date.fromisoformat(path.name[:10])
            if day < today - timedelta(days=RECORD_DAYS) or path.stat().st_size > 512 * 1024:
                continue
            record = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        said = record.get("owner_verbatim")
        said = " ".join(said) if isinstance(said, list) and all(isinstance(x, str) for x in said) else said if isinstance(said, str) else ""
        listed = [x for x in (record.get("answers_cards") or []) if isinstance(x, str)] if isinstance(record.get("answers_cards"), list) else []
        for code in set(_CODE.findall(said)) | {x for x in listed if _CODE_ONLY.fullmatch(x)}:
            first = numbered.get(code)
            if first and day >= first:
                out.setdefault(code, (path.name, day.isoformat()))
    return out


def _ratings(data_dir: Path) -> dict[str, dict[str, Any]]:
    return {r.get("id"): r for r in pending.load(data_dir, pending.RATINGS_FILE) if not r.get("withdrawn_at")}


def _state(root: Path, data_dir: Path, now: datetime) -> dict[str, Any]:
    today = now.astimezone().date()
    cards = build_cards(root, today, now)
    codes = pending.codes_for(data_dir, [(c["kind"], c["key"]) for c in cards])
    replies, ratings = pending.live_replies(data_dir), _ratings(data_dir)
    dismissed = review._dismissed(root)
    migrate = {c["dkey"]: dismissed[c["legacy"]] for c in cards if c.get("legacy") in dismissed and c["dkey"] not in dismissed}
    if migrate:  # review-dismiss's hash was the whole row text: carry those records over to the key that survives edits (10-27 ⑩)
        with review._DISMISS_LOCK:
            dismissed = {**review._dismissed(root), **migrate}
            review._save_dismissed(root, dismissed)
    for c in cards:
        c["code"] = codes[f"{c['kind']}:{c['key']}"]["code"]
    away = _elsewhere(root, codes, today)
    active, waiting, handled = [], [], []
    for c in cards:
        reply = (replies.get(c["key"]) or replies.get(c["legacy"])) if c["kind"] in _DAILY else None  # legacy: id the old page used
        c["reply"] = reply
        c["age_days"] = (today - c["since"]).days if c["since"] else None
        c["stale"] = bool(c["age_days"] is not None and c["age_days"] > STALE_DAYS)
        if c["dkey"] in dismissed or c["tailed"]:
            rating = ratings.get(c["key"])
            at = dismissed.get(c["dkey"]) or (re.search(r"〔owner (\d\d-\d\d) 点", c["rowtext"]) or [None, ""])[1]
            at = at if "-" in at and len(at) > 5 else f"{today.year}-{at}" if at else None
            handled.append(_entry(c, "你点了「" + (rating["verdict"] if rating else "看过了" if c["kind"] in "VK" and not c["tailed"]
                                                 else "已经处理了") + "」", at, undo=True))
        elif c["code"] in away:
            name, day = away[c["code"]]
            handled.append(_entry(c, f"你在别处答了（records/{name}）", day, undo=False))
        elif reply and pending_answered(reply):
            waiting.append(c)
        else:
            active.append(c)
    live = {c["key"] for c in cards} | {c["legacy"] for c in cards if c["kind"] in _DAILY}
    for key, reply in replies.items():
        if key in live or reply.get("kind") != "reply":
            continue
        stamp = _day(reply.get("at"))
        if stamp and stamp >= today - timedelta(days=HANDLED_DAYS):
            code = next((v["code"] for k, v in codes.items() if k.endswith(f":{key}")), None) or reply.get("card")
            handled.append({"code": code, "kind": None, "kind_label": None, "title": reply.get("head", ""), "section": reply.get("section"),
                            "how": f"你选了「{reply.get('option') or '（只写了说明）'}」{('：' + reply['note']) if reply.get('note') else ''}，原行已经改了",
                            "at": reply.get("at"), "undo": False})
    handled = [h for h in handled if (_day(h["at"]) or today) >= today - timedelta(days=HANDLED_DAYS)
               or h["how"].startswith("你在别处")]
    handled.sort(key=lambda h: h["at"] or "", reverse=True)
    active.sort(key=lambda c: _urgency(c, today))
    waiting.sort(key=lambda c: c["reply"].get("at", ""), reverse=True)
    return {"today": today, "active": active, "waiting": waiting, "handled": handled, "all": {c["code"]: c for c in cards}}


def pending_answered(reply: dict[str, Any] | None) -> bool:
    """A reply files the card away unless it was a bare 再想想 (still undecided)."""
    return bool(reply) and (reply.get("option") != "再想想" or bool(reply.get("note")))


def _entry(card: dict[str, Any], how: str, at: str | None, undo: bool) -> dict[str, Any]:
    return {"code": card["code"], "kind": card["kind"], "kind_label": card["kind_label"], "title": card["title"],
            "section": card["section"], "how": how, "at": at, "undo": undo}


def _public(card: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in card.items() if k not in {"rowtext", "dkey", "legacy", "since", "tailed"}}
    out["old"] = old_view(card)
    out["due"] = card["due"].strftime("%m-%d") if card["due"] else None
    out["since"] = card["since"].isoformat() if card["since"] else None
    return out


def old_view(card: dict[str, Any]) -> bool:
    """The 「可能已过时」 group: every 知道就行 card and any 看/试 card hanging for more than STALE_DAYS. The page folds it and the
    headline (count, minutes, default list) leaves it out (owner\x2010-27 rework 2)."""
    return card["kind"] == "K" or (card["kind"] == "V" and card["stale"])


def _headline(active: list[dict[str, Any]]) -> dict[str, Any]:
    folded = sum(old_view(c) for c in active)
    active = [c for c in active if not old_view(c)]
    top = next((c for c in active if not c["stale"]), None)
    return {"count": len(active), "folded": folded, "minutes": sum(c["minutes"] for c in active),
            "top": {"code": top["code"], "title": top["title"][:40], "due": top["due"].strftime("%m-%d") if top["due"] else None,
                    "blocks": top["blocks"]} if top else None}


def snapshot(root: Path, data_dir: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    state = _state(root, data_dir, now)
    results, omitted = pending._result_window(root, now)
    shown = {c["line"] for c in state["active"] + state["waiting"]}
    rated = _ratings(data_dir)
    results = [dict(r, rating=rated.get(r["id"])) for r in results if r["line"] not in shown]
    results.sort(key=lambda r: r["rating"] is not None)
    return {"generated_at_utc": pending._now(), "headline": _headline(state["active"]),
            "cards": [_public(c) for c in state["active"]], "waiting": [_public(c) for c in state["waiting"]],
            "handled": state["handled"], "recent": results, "recent_omitted": omitted,
            "files": {"replies": str(Path(data_dir) / pending.REPLIES_FILE), "ratings": str(Path(data_dir) / pending.RATINGS_FILE)}}


def codes_for_report(root: Path, data_dir: Path, now: datetime | None = None) -> dict[tuple[str, str], str]:
    """{(kind, row head): code} for the rows the daily-report facts list (D and T), numbering the new ones."""
    now = now or datetime.now(timezone.utc)
    cards = [c for c in build_cards(root, now.astimezone().date(), now) if c["kind"] in _DAILY]
    found = pending.codes_for(data_dir, [(c["kind"], c["key"]) for c in cards])
    return {(c["kind"], c["head"]): found[f"{c['kind']}:{c['key']}"]["code"] for c in cards}


def _mark_rows(root: Path, cards: list[dict[str, Any]], add: bool, now: datetime) -> int:
    """Put 〔owner MM-DD 点「已经处理了」〕 at the end of each card's backlog row (before a trailing 〔时段：…〕, which the scheduler
    reads at the end of the line), or take it off again. One edit through control/backlog_io (the lock every writer of backlog.md
    takes, one history copy however many rows). A row is only touched while it is still exactly the text the card was built from.
    Returns how many rows were changed or already right."""
    path = Path(root) / "projects" / "backlog.md"
    note = f" 〔owner {now.astimezone():%m-%d} 点「已经处理了」〕"
    plan: dict[int, tuple[str, str]] = {}
    for card in cards:
        old = card["rowtext"].rstrip("\r")
        if not old or card["head"][:40] not in old:
            continue
        window = re.search(r"〔时段：[^〕]*〕$", old)
        new = (old if _TAIL_RE.search(old) else old + note if not window else old[:window.start()].rstrip() + note + " " + window.group(0)) \
            if add else _TAIL_RE.sub("", old)
        plan[card["line"] - 1] = (old, new)
    done = 0

    def edit(lines: list[str]) -> list[str]:
        nonlocal done
        for index, (old, new) in plan.items():
            if index < len(lines) and lines[index].rstrip("\r") == old:
                lines[index] = new + lines[index][len(old):]
                done += 1
        return lines
    if any(old != new for old, new in plan.values()):
        _backlog_io().edit(edit, path)
    else:
        done = len(plan)
    return done


def _mark_row(root: Path, card: dict[str, Any], add: bool, now: datetime) -> bool:
    return _mark_rows(root, [card], add, now) == 1


def act_batch(root: Path, data_dir: Path, codes: list[str], action: str, now: datetime | None = None) -> dict[str, Any]:
    """「这一栏全部已经处理了」 (action `handled`) and its 撤回 (`undo`) for the 可能已过时 group, each row exactly as the single card's
    已经处理了 / 撤回 does it. Codes that are not (or no longer) in the right state are skipped and counted."""
    now = now or datetime.now(timezone.utc)
    if (action not in {"handled", "undo"} or not isinstance(codes, list) or not 0 < len(codes) <= 500
            or not all(isinstance(c, str) and _CODE_ONLY.fullmatch(c) for c in codes)):
        raise ValueError("操作无效，请刷新页面后重试。")
    state = _state(root, data_dir, now)
    active = {c["code"] for c in state["active"] if old_view(c)}
    handled_now = {h["code"] for h in state["handled"] if h.get("undo")}
    if action == "handled":
        chosen = [state["all"][c] for c in dict.fromkeys(codes) if c in active]
        edited = _mark_rows(root, chosen, True, now)
        with review._DISMISS_LOCK:
            found = review._dismissed(root)
            for card in chosen:
                found[card["dkey"]] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            review._save_dismissed(root, found)
        message = f"已记下 {len(chosen)} 件，行尾各加了一句，各线下次审查核对。" if chosen else "这些卡现在不在这一栏里，请刷新页面。"
    else:
        chosen = [state["all"][c] for c in dict.fromkeys(codes) if c in handled_now]
        _mark_rows(root, chosen, False, now)
        with review._DISMISS_LOCK:
            found = review._dismissed(root)
            for card in chosen:
                found.pop(card["dkey"], None)
            review._save_dismissed(root, found)
        for card in chosen:
            _withdraw_rating(data_dir, card["key"])
        edited = len(chosen)
        message = f"已撤回 {len(chosen)} 件，回到原处。" if chosen else "没有可撤回的卡，请刷新页面。"
    return {"ok": True, "done": len(chosen), "skipped": len(set(codes)) - len(chosen), "edited": edited,
            "codes": [c["code"] for c in chosen], "message": message}


def _dismiss(root: Path, dkey: str, on: bool, now: datetime) -> None:
    with review._DISMISS_LOCK:
        found = review._dismissed(root)
        if on:
            found[dkey] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            found.pop(dkey, None)
        review._save_dismissed(root, found)


def act(root: Path, data_dir: Path, code: str, action: str, option: str = "", note: str = "",
        now: datetime | None = None) -> dict[str, Any]:
    """One click on a card. ValueError = bad request (400), LookupError = the card is not there any more or the state does not
    allow it (409); the page shows the message."""
    now = now or datetime.now(timezone.utc)
    if not _CODE_ONLY.fullmatch(code or "") or action not in _ACTIONS:
        raise ValueError("操作无效，请刷新页面后重试。")
    option, note = str(option).strip()[:60], str(note).strip()[:1000]
    state = _state(root, data_dir, now)
    card = state["all"].get(code)
    if card is None:
        raise LookupError("这一张已经不在「要你的」里了，请刷新页面。")
    where = "active" if card in state["active"] else "waiting" if card in state["waiting"] else "handled"
    kind, line_name = card["kind"], card["section"]
    if action == "reply":
        if kind not in _DAILY or where == "handled":
            raise LookupError("这一张现在不能回复，请刷新页面。")
        if not option and not note:
            raise ValueError("请选一个选项或写几个字。")
        record = {"at": pending._now(), "kind": "reply", "id": card["key"], "section": line_name, "head": card["head"],
                  "option": option, "note": note, "status": "new", "card": code}
        pending._append(data_dir, pending.REPLIES_FILE, record)
        answered = pending_answered(record)
        return {"ok": True, "code": code, "answered": answered,
                "message": f"已收到，{line_name}会照办。" if answered else "已记下「再想想」，这一张留在上面。"}
    if action == "rate":
        if kind != "V" or where == "handled":
            raise LookupError("这一张不能评价，请刷新页面。")
        if option not in pending.VERDICTS:
            raise ValueError("评价只能是「用」「没用」「返工」。")
        done = card["since"].strftime("%m-%d") if card["since"] else ""
        pending._append(data_dir, pending.RATINGS_FILE, {"at": pending._now(), "kind": "rating", "id": card["key"], "card": code,
                                                         "head": card["head"], "done": done, "verdict": option, "note": note})
        _dismiss(root, card["dkey"], True, now)
        return {"ok": True, "code": code, "message": f"已记下评价「{option}」，总理每周对照审查结论。"}
    if action in {"seen", "handled"}:
        if where == "handled" or (action == "seen" and kind not in "VK"):
            raise LookupError("这一张现在不能这样处理，请刷新页面。")
        edited = action == "handled" and _mark_row(root, card, True, now)
        _dismiss(root, card["dkey"], True, now)
        return {"ok": True, "code": code, "message": ("已记下，行尾加了一句，" + f"{line_name}下次审查核对。") if edited
                else ("已收起。" if action == "seen" else "已收起（没能在任务行上加注，等审查时再说）。")}
    # undo
    if where == "waiting":
        if not pending.mark_withdrawn(data_dir, card["reply"]["id"]):
            raise LookupError(f"{line_name}已经处理了这条回复，页面撤不回来；要改请在 Discord 或终端里说。")
        return {"ok": True, "code": code, "message": "已撤回，这一张回到上面。"}
    if where == "handled" and (card["dkey"] in review._dismissed(root) or card["tailed"]):
        _dismiss(root, card["dkey"], False, now)
        _withdraw_rating(data_dir, card["key"])
        _mark_row(root, card, False, now)
        return {"ok": True, "code": code, "message": "已撤回，这一张回到上面。"}
    raise LookupError("这一张在别处答过了，页面撤不回来；要改请在终端或 Discord 里告诉总理。")


def _withdraw_rating(data_dir: Path, key: str) -> None:
    with pending._LOCK:
        records, changed = pending.load(data_dir, pending.RATINGS_FILE), False
        for r in records:
            if r.get("id") == key and not r.get("withdrawn_at"):
                r["withdrawn_at"], changed = pending._now(), True
        if changed:
            pending._rewrite(Path(data_dir), pending.RATINGS_FILE, records)
