"""Suggest ASR fixes from the owner's own edits (read-only on the store).

Per session: the latest machine text (asr or postprocess) before the first correction vs the last correction. Every token-diff
replacement between them is a candidate "heard -> corrected" pair (the hunk mining of asr_bench/mine_pp.py, with bench_lib's
tokenizer). Punctuation, fillers, digits, insertions/deletions and rewrites are not candidates. A pair is suggested when the same
edit shows up in >= MIN_SESSIONS distinct sessions and applying it would not have broken any confirmed text: every place where
the heard wording occurs in a machine text must have become the corrected wording in the owner's final text (the bench's
false-positive count, bench_pp_why.py). pypinyin is not in the input desk's venv, so bench_lib.classify is not used; the length
limits and the false-hit gate stand in for its 表达改写 class. Near candidates failed the zero-false-hit gate but disagree
at no more than half the matching places; they require the owner's explicit choice."""
import difflib
import importlib.util
import json
import re
from pathlib import Path

from . import rules as R

# ---- CONFIG ----
MIN_SESSIONS = 2          # same edit in this many distinct sessions
MAX_FALSE_HITS = 0        # places where the rule would fire but the owner's final text disagrees
CTX = 4                   # content characters on each side that must agree to call it "the same spot"
FILLERS = set("嗯呃额啊哦")
MAX_TOKENS = 6
SNIPPET = 14
# ----------------

_spec = importlib.util.spec_from_file_location("bench_lib", Path(__file__).parent / "asr_bench" / "bench_lib.py")
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)
_cache = {}


def _texts(store):
    """{session_id: {"machine": text, "final": text}} for sessions where the owner corrected a machine text."""
    sessions = {}
    with store._db() as db:
        stamp = tuple(db.execute("SELECT COUNT(*), MAX(rowid) FROM revisions").fetchone())
        cached = _cache.get(str(store.root))
        if cached and cached[0] == stamp:
            return cached[1], cached[2]
        rows = db.execute("SELECT session_id, kind, content, data_json FROM revisions "
                          "WHERE kind IN ('asr','postprocess','correction') AND source!='llm_postprocess' ORDER BY rowid").fetchall()
    for row in rows:
        s = sessions.setdefault(row["session_id"], {"machine": None, "final": None, "type": None, "corrected": False})
        if row["kind"] == "correction":
            try:
                kind = json.loads(row["data_json"]).get("correction_type")
            except (ValueError, AttributeError):
                kind = None
            s.update(final=row["content"], type=kind, corrected=True)
        elif not s["corrected"]:
            s["machine"] = row["content"]
    texts = {sid: {"machine": s["machine"], "final": s["final"]} for sid, s in sessions.items()
             if s["machine"] and s["final"] and s["type"] != "user_rewrite"}
    groups = _groups(texts)
    _cache[str(store.root)] = (stamp, texts, groups)
    return texts, groups


def _spans(text):
    return [(m.group(), m.start(), m.end()) for m in B.TOKEN.finditer(text)]


def _plausible(ta, tb, src, dst):
    toks = [t[0].lower() for t in ta + tb]
    if any(t[0].isdigit() for t in toks) or sum(len(t) == 1 and t in B.NUMERAL for t in toks) * 2 >= len(toks):
        return False
    if set(src) <= FILLERS or set(dst) <= FILLERS or src.lower() == dst.lower():
        return False
    if len(re.sub(r"\s", "", src)) < 2:
        return False
    if any(B.LATIN.match(t) for t in toks):
        return len(ta) <= MAX_TOKENS and len(tb) <= MAX_TOKENS
    return 2 <= len(ta) <= MAX_TOKENS and 2 <= len(tb) <= MAX_TOKENS and abs(len(ta) - len(tb)) <= 2


def _sentence(text, start, end):
    left = max((text.rfind(c, 0, start) for c in "。！？!?.\n"), default=-1)
    right = min((i for i in (text.find(c, end) for c in "。！？!?.\n") if i >= 0), default=len(text) - 1)
    return text[left + 1:right + 1].strip()


def _groups(texts):
    """{(heard key, corrected): {"heard", "corrected", "occurrences": {sid: occurrence}}} for every replacement edit."""
    groups = {}
    for sid, t in texts.items():
        a, b = _spans(t["machine"]), _spans(t["final"])
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, [x[0] for x in a], [x[0] for x in b], autojunk=False).get_opcodes():
            if tag != "replace":
                continue
            ta, tb = a[i1:i2], b[j1:j2]
            if not all(B.is_content(x[0]) for x in ta + tb):
                continue
            src, dst = t["machine"][ta[0][1]:ta[-1][2]], t["final"][tb[0][1]:tb[-1][2]]
            if not _plausible(ta, tb, src, dst):
                continue
            g = groups.setdefault((R.key(src), dst), {"heard": src, "corrected": dst, "occurrences": {}})
            g["occurrences"].setdefault(sid, {"session_id": sid, "before": t["machine"][max(0, ta[0][1] - SNIPPET):ta[-1][2] + SNIPPET],
                                              "after": t["final"][max(0, tb[0][1] - SNIPPET):tb[-1][2] + SNIPPET],
                                              "original": _sentence(t["machine"], ta[0][1], ta[-1][2]),
                                              "edited": _sentence(t["final"], tb[0][1], tb[-1][2])})
    return groups


def check(texts, heard, corrected, *, false_evidence=None):
    """(places the heard wording occurs in machine texts, places where the owner's final text disagrees, sessions checked)."""
    pattern, want = R.pattern_for(heard), B.content_text(corrected)
    hits = false = 0
    for sid, t in texts.items():
        final = B.content_text(t["final"])
        for m in pattern.finditer(t["machine"]):
            hits += 1
            if B.content_text(t["machine"][:m.start()])[-CTX:] + want + B.content_text(t["machine"][m.end():])[:CTX] not in final:
                false += 1
                if false_evidence is not None and not false_evidence:
                    false_evidence.append({"session_id": sid, "original": _sentence(t["machine"], m.start(), m.end())})
    return hits, false, len(texts)


def recommendations(store, rules_path):
    """Separate zero-false-hit suggestions from near candidates, with evidence for both."""
    texts, groups = _texts(store)
    data = R.load(rules_path)
    ignored = {(i["from"], i["to"]) for i in data["ignored"]}
    owned = {R.key(r["from"]) for r in data["rules"]}
    out, near = [], []
    for (heard_key, corrected), g in groups.items():
        if len(g["occurrences"]) < MIN_SESSIONS or (heard_key, corrected) in ignored or heard_key in owned:
            continue
        if R.covered(g["heard"], corrected, rules_path):
            continue
        false_evidence = []
        hits, false, checked = check(texts, g["heard"], corrected, false_evidence=false_evidence)
        if false > MAX_FALSE_HITS and false * 2 > hits:
            continue
        occurrences = list(g["occurrences"].values())
        item = {"from": g["heard"], "to": corrected, "count": len(occurrences), "hits": hits, "false_hits": false,
                "checked": checked, "evidence": occurrences[:2], "evidence_sessions": [o["session_id"] for o in occurrences]}
        if false > MAX_FALSE_HITS:
            item["false_evidence"] = false_evidence
            near.append(item)
        else:
            out.append(item)
    return {"suggestions": sorted(out, key=lambda s: (-s["count"], s["from"])),
            "near": sorted(near, key=lambda s: (-s["count"], s["from"]))}


def suggestions(store, rules_path):
    return recommendations(store, rules_path)["suggestions"]


def accept(store, rules_path, heard, corrected, near=False):
    """Recheck the selected list before adding; near candidates need an explicit opt-in."""
    candidates = recommendations(store, rules_path)["near" if near else "suggestions"]
    found = next((s for s in candidates if (R.key(s["from"]), s["to"]) == (R.key(heard), corrected)), None)
    if found is None:
        raise R.RuleError(409, "这条推荐已经变化或不再成立，请刷新后再看。")
    return R.add_rule(rules_path, found["from"], found["to"], "suggested", found["evidence_sessions"])
