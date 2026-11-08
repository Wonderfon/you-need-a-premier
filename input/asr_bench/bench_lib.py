"""Shared helpers of the ASR bench: tokens, character error rate, edit classification. Pure Python (+ pypinyin, jieba for classification)."""
import difflib
import re

TOKEN = re.compile(r"[㐀-鿿]|[A-Za-z][A-Za-z']*|[0-9]+(?:[.,][0-9]+)*|\S")
CJK = re.compile(r"[㐀-鿿]")
LATIN = re.compile(r"[A-Za-z]")
NUMERAL = set("零〇一二三四五六七八九十百千万亿两半")
MERGE_GAP = 2
PUNCT_CHARS =set("，。！？；：、,.!?;:\"'“”‘’（）()[]【】《》<>…—-~·`/\\|")


def tokens(text):
    return TOKEN.findall(text)


def is_content(tok):
    return bool(CJK.match(tok) or LATIN.match(tok) or tok[0].isdigit())


def content_text(text):
    """Text without punctuation and whitespace, lower-cased: the string the CER is computed on."""
    return "".join(t.lower() for t in tokens(text) if is_content(t))


def edit_distance(a, b):
    """Levenshtein distance between two sequences (rolling rows)."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def cer(ref, hyp, punct=False):
    """(errors, reference length). punct=False compares content tokens only (CJK char, Latin word, number), lower-cased;
    punct=True also counts punctuation marks as tokens."""
    pick = (lambda t: t.lower()) if punct else (lambda t: t.lower() if is_content(t) else None)
    r = [x for x in map(pick, tokens(ref)) if x]
    h = [x for x in map(pick, tokens(hyp)) if x]
    return edit_distance(r, h), len(r)


def diff_ops(old, new):
    """Edit operations between two texts: [(tag, old_text, new_text, old_tokens, new_tokens)] for non-equal opcodes of the token diff."""
    a, b = tokens(old), tokens(new)
    hunks = []                                   # edits separated by <= MERGE_GAP unchanged content tokens are one hunk
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if hunks and sum(is_content(t) for t in a[hunks[-1][1]:i1]) <= MERGE_GAP:
            hunks[-1][1], hunks[-1][3] = i2, j2
        else:
            hunks.append([i1, i2, j1, j2])
    out = []
    for i1, i2, j1, j2 in hunks:
        fmt = lambda ts: " ".join(ts) if any(LATIN.match(t) for t in ts) else "".join(ts)
        tag = "insert" if i1 == i2 else "delete" if j1 == j2 else "replace"
        out.append((tag, fmt(a[i1:i2]), fmt(b[j1:j2]), a[i1:i2], b[j1:j2]))
    return out


_py = None


def _pinyin(s):
    global _py
    if _py is None:
        from pypinyin import Style, lazy_pinyin
        _py = lambda x: lazy_pinyin(x, style=Style.NORMAL)
    return _py(s)


def classify(old_toks, new_toks):
    """One edit -> (class, sub). Classes: 标点断句, 数字, 专有名词, 英文, 同音字, 漏字多字, 表达改写."""
    oc = [t.lower() for t in old_toks if is_content(t)]
    nc = [t.lower() for t in new_toks if is_content(t)]
    if oc == nc:
        return "标点断句", ""
    allc = oc + nc
    nums = sum(1 for t in allc if t[0].isdigit() or (len(t) == 1 and t in NUMERAL))
    if any(t[0].isdigit() for t in allc) or (allc and nums / len(allc) >= 0.5):
        return "数字", ""
    if any(LATIN.match(t) for t in allc):
        new_latin = [t for t in new_toks if LATIN.match(t)]
        if any(any(c.isupper() for c in t) for t in new_latin):
            return "专有名词", "英文名/缩写（含大写）"
        return "英文", "英文词" if new_latin else "英文被删"
    o, n = "".join(oc), "".join(nc)
    if not o or not n:
        return ("漏字多字", "漏字" if not o else "多字") if max(len(o), len(n)) <= 2 else ("表达改写", "增删短语")
    po, pn = _pinyin(o), _pinyin(n)
    if po == pn:
        return "同音字", "同音"
    if len(o) == len(n) and len(o) <= 4 and sum(x == y for x, y in zip(po, pn)) >= max(1, len(o) // 2):
        return "同音字", "近音（部分同音）"
    return "表达改写", "换说法/增删"
