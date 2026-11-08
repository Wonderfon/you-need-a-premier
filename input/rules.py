"""Owner post-processing rules. The built-in postprocess.RULES stay in code (can only be disabled); owner rules live in one JSON file
and run after them. Every change writes a log entry that can be undone. Only future transcriptions are affected: old revisions and
machine originals are never rewritten."""
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import postprocess

LOCK = threading.RLock()
MAX_LOG = 200
MAX_TEXT = 60
LATIN_EDGE = re.compile(r"[A-Za-z0-9_]")


class RuleError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _empty():
    return {"schema_version": 1, "rules": [], "disabled_builtin": [], "ignored": [], "log": []}


def load(path, strict=False):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("rules", []), list):
            raise ValueError("bad shape")
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError):
        if strict:
            raise RuleError(500, "规则文件无法读取，请先检查文件再操作。")
        return _empty()
    return {**_empty(), **data}


def _save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def key(text):
    """Identity of a wording: whitespace collapsed, lower-cased when it has Latin letters (the machine output varies in case)."""
    text = " ".join(str(text).split())
    return text.lower() if re.search(r"[A-Za-z]", text) else text


def pattern_for(source):
    """Literal match of the heard wording: flexible spaces, whole-word edges next to ASCII letters, no regex from the user."""
    body = r"[ ]+".join(re.escape(word) for word in source.split())
    start = r"(?<![A-Za-z0-9_])" if LATIN_EDGE.match(source[0]) else ""
    end = r"(?![A-Za-z0-9_])" if LATIN_EDGE.match(source[-1]) else ""
    return re.compile(start + body + end, re.I if re.search(r"[A-Za-z]", source) else 0)


def _clean(source, target):
    source, target = " ".join(str(source).split()), str(target).strip()
    if not source or not target or source == target:
        raise RuleError(400, "「听成」和「改成」都要填，而且不能相同。")
    if len(source) > MAX_TEXT or len(target) > MAX_TEXT or "\n" in target:
        raise RuleError(400, f"每一项最多 {MAX_TEXT} 个字，且不能换行。")
    if len(re.sub(r"\s", "", source)) < 2:
        raise RuleError(400, "「听成」至少两个字，单字替换太容易误伤。")
    return source, target


def active_rules(path):
    """(rules tuple for postprocess.apply, version string). Built-ins first, then owner rules longest wording first."""
    data = load(path)
    off = set(data["disabled_builtin"])
    rules = tuple(r for r in postprocess.RULES if r[0] not in off)
    owner = sorted(data["rules"], key=lambda r: -len(r["from"]))
    rules += tuple(("owner:" + r["id"], pattern_for(r["from"]), r["to"].replace("\\", "\\\\"), (), None) for r in owner)
    if not off and not owner:
        return rules, postprocess.VERSION
    digest = hashlib.sha256(json.dumps([sorted(off), [(r["from"], r["to"]) for r in owner]], ensure_ascii=False).encode()).hexdigest()[:8]
    return rules, f"{postprocess.VERSION}+rules-{digest}"


def process(text, path):
    rules, version = active_rules(path)
    out, hits = postprocess.apply(text, rules)
    return out, hits, version


def covered(source, target, path):
    """Do the active rules already turn this wording into the target?"""
    return postprocess.apply(source, active_rules(path)[0])[0].casefold() == target.casefold()


def _log(data, action, **detail):
    entry = {"id": uuid.uuid4().hex[:8], "at": _now(), "action": action, "undone": False, **detail}
    data["log"] = (data["log"] + [entry])[-MAX_LOG:]
    return entry


def _change(path, fn):
    with LOCK:
        data = load(path, strict=True)
        result = fn(data)
        _save(path, data)
        return result


def add_rule(path, source, target, origin, evidence=()):
    source, target = _clean(source, target)

    def go(data):
        if any(key(r["from"]) == key(source) for r in data["rules"]):
            raise RuleError(409, "已经有一条同样「听成」的规则。")
        rule = {"id": uuid.uuid4().hex[:8], "from": source, "to": target, "source": origin, "added_at": _now(),
                "evidence_sessions": list(evidence)}
        data["rules"].append(rule)
        data["ignored"] = [i for i in data["ignored"] if (i["from"], i["to"]) != (key(source), target)]
        _log(data, "accept" if origin == "suggested" else "add", rule=rule)
        return rule
    return _change(path, go)


def delete_rule(path, rule_id):
    def go(data):
        rule = next((r for r in data["rules"] if r["id"] == rule_id), None)
        if rule is None:
            raise RuleError(404, "找不到这条规则。")
        data["rules"].remove(rule)
        _log(data, "delete", rule=rule)
        return rule
    return _change(path, go)


def set_builtin(path, rule_id, enabled):
    if rule_id not in {r[0] for r in postprocess.RULES}:
        raise RuleError(404, "找不到这条内置规则。")

    def go(data):
        off = set(data["disabled_builtin"])
        if enabled == (rule_id not in off):
            return
        (off.discard if enabled else off.add)(rule_id)
        data["disabled_builtin"] = sorted(off)
        _log(data, "enable" if enabled else "disable", builtin_id=rule_id)
    return _change(path, go)


def ignore(path, source, target):
    def go(data):
        pair = {"from": key(source), "to": str(target).strip(), "at": _now()}
        if not any((i["from"], i["to"]) == (pair["from"], pair["to"]) for i in data["ignored"]):
            data["ignored"].append(pair)
            _log(data, "ignore", pair=pair)
    return _change(path, go)


def undo(path, log_id):
    def go(data):
        entry = next((e for e in data["log"] if e["id"] == log_id), None)
        if entry is None or entry["undone"]:
            raise RuleError(404, "这一步已经撤回，或日志里找不到。")
        action = entry["action"]
        if action in ("accept", "add"):
            before = len(data["rules"])
            data["rules"] = [r for r in data["rules"] if r["id"] != entry["rule"]["id"]]
            if len(data["rules"]) == before:
                raise RuleError(409, "这条规则已经不在了，无需撤回。")
        elif action == "delete":
            if any(key(r["from"]) == key(entry["rule"]["from"]) for r in data["rules"]):
                raise RuleError(409, "已有同样「听成」的规则，无法恢复。")
            data["rules"].append(entry["rule"])
        elif action in ("disable", "enable"):
            off = set(data["disabled_builtin"])
            (off.discard if action == "disable" else off.add)(entry["builtin_id"])
            data["disabled_builtin"] = sorted(off)
        elif action == "ignore":
            data["ignored"] = [i for i in data["ignored"] if (i["from"], i["to"]) != (entry["pair"]["from"], entry["pair"]["to"])]
        entry["undone"] = True
        _log(data, "undo", of=log_id, of_action=action)
    return _change(path, go)


# What each built-in rule hears, in words (the regex stays in postprocess.py).
BUILTIN_HEARD = {
    "q5-model-name": "Q五", "claude-code-name": "cloud code", "anthropic-claude-name": "anthropic cloud",
    "claude-standalone-name": "cloud（单独出现）", "codex-split": "code x", "fos-spelled": "f o s",
    "fabgrid-misheard": "fiber / five / cyber mind", "colab-misspelt": "collab / colan", "github-getup": "get up",
    "uvox-misheard": "woozy / woothie", "arbitros-misheard": "阿瑞斯猫 / 阿瑞斯莫斯 / arismo",
}


def snapshot(path):
    data = load(path)
    off = set(data["disabled_builtin"])
    builtin = [{"id": r[0], "pattern": BUILTIN_HEARD.get(r[0], r[1].pattern), "to": r[2], "context": list(r[3]), "enabled": r[0] not in off}
               for r in postprocess.RULES]
    return {"version": active_rules(path)[1], "builtin": builtin, "owner": data["rules"], "ignored": data["ignored"],
            "log": list(reversed(data["log"]))[:40]}
