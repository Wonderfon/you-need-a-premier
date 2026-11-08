"""只读检查结果开头三句；命中只提醒，退出码仍为 0。仅用标准库。"""
from __future__ import annotations

import argparse
import json
import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG = {
    "paths": [ROOT / "audits"],  # 无参数时查这些文件/目录；目录只递归查 Markdown
    "sentences": 3,
    "since": "10-25",  # --backlog 模式按「给 owner MM-DD」日期，不按文件名日期
    "words": ["bootstrap"],
    "confidence": r"置信区间|95\s*[%％]",
    "plain_confidence": r"(?:大概|大致|可能).{0,16}(?:范围|之间|落在)|"
                        r"不确定范围|波动范围|多次重测|反复做.{0,12}(?:实验|试验)|每\s*100\s*次",
    "known_acronyms": ["OK"],
    "step_verbs": r"先|再|请|打开|运行|点击|检查|读|看|做|把|选择|核对|安装|复制|下载",
}


class HTMLBody(HTMLParser):
    """HTML-only pages: keep source line numbers; ignore headings, scripts and styles."""

    def __init__(self):
        super().__init__()
        self.parts = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = (bool(self.tags and self.tags[-1][1]) or
                  tag in {"head", "script", "style", "h1", "h2", "h3", "h4", "h5", "h6", "pre"} or
                  "hidden" in attrs or attrs.get("aria-hidden") == "true" or
                  "src" in (attrs.get("class") or "").split() or  # md2html's source attribution
                  bool(re.search(r"display\s*:\s*none", attrs.get("style") or "", re.I)))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.tags.append((tag, hidden))
        if tag in {"p", "div", "li", "br", "section", "tr"}:
            self.parts.append((self.getpos()[0], "\n\n"))

    def handle_endtag(self, tag):
        for i in range(len(self.tags) - 1, -1, -1):
            if self.tags[i][0] == tag:
                self.tags = self.tags[:i]
                break
        if tag in {"p", "div", "li", "section", "tr"}:
            self.parts.append((self.getpos()[0], "\n\n"))

    def handle_data(self, data):
        if not self.tags or not self.tags[-1][1]:
            for offset, line in enumerate(data.splitlines(keepends=True)):
                self.parts.append((self.getpos()[0] + offset, line))


def head_sentences(text: str, html: bool = False) -> list[dict]:
    """Preserve original line numbers, including wrapped Markdown and HTML-only pages."""
    if html:
        parser = HTMLBody()
        parser.feed(text)
        parts = parser.parts
    else:
        parts = list(enumerate(text.splitlines(keepends=True), 1))
    body, lines = [], []
    fence = None
    for i, (number, line) in enumerate(parts):
        stripped = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            continue
        next_line = parts[i + 1][1].strip() if i + 1 < len(parts) else ""
        if fence or re.match(r"^(?:#{1,6}\s|<!--|(?:[-=*]_?\s*){3,}$)", stripped):
            continue
        if next_line and re.fullmatch(r"[=-]{3,}", next_line):  # setext title
            continue
        if re.match(r"^(?:[-*>]\s*)?(?:打开(?:命令|报告|页面|文件)?|启动命令|只读复核命令)\s*[（(:：]", stripped):
            continue
        if re.match(r"^`?(?:Start-Process\b|wsl\s|(?:python3?|&\s*python)\s)", stripped, re.I):
            continue
        body.append(line)
        lines.extend([number] * len(line))
    joined = "".join(body)
    # ponytail: punctuation/paragraph boundaries, not a natural-language parser; review warnings manually.
    ends = list(re.finditer(r"[。！？!?]+|\.(?=\s|$)|\n\s*\n", joined))
    result, start = [], 0
    for end in [m.end() for m in ends] + [len(joined)]:
        segment = joined[start:end]
        if re.search(r"[\w①-⑳]", segment):  # closing ** / backticks alone are not a sentence
            first = start + len(segment) - len(segment.lstrip())
            result.append({"text": segment, "lines": lines[start:end], "line": lines[first]})
            if len(result) == CONFIG["sentences"]:
                break
        start = end
    return result


def explained(text: str, start: int, end: int) -> bool:
    """Only explicit adjacent Chinese glosses count, not unrelated Chinese prose."""
    before, after = text[:start], text[end:]
    if re.match(r"\s*[*`]*\s*[（(][^）)\n]*[\u4e00-\u9fff]{2}[^）)\n]*[）)]", after):
        return True
    if re.search(r"[\u4e00-\u9fff]{2,}\s*[（(]\s*[*`]*\s*$", before) and re.match(r"\s*[*`]*\s*[）)]", after):
        return True
    if re.match(r"\s*[*`]*\s*(?:是指|指的是|也就是|即|表示|代表|[=:：])\s*[\u4e00-\u9fff]{2}", after):
        return True
    # P/F/U（通过/失败/不确定）: apply the shared gloss to each letter.
    group = re.search(r"[PFU](?:/[PFU])+\s*[（(][^）)\n]*[\u4e00-\u9fff]{2}[^）)\n]*[）)]", text)
    return bool(group and group.start() <= start < group.end())


def check_text(text: str, html: bool = False) -> dict:
    head = head_sentences(text, html)
    hits = []
    letters = r"(?<![A-Za-z0-9_])[PFU](?![A-Za-z0-9_])|(?<![A-Za-z0-9_])[A-Z][A-Z0-9]{1,9}(?![A-Za-z0-9_])"
    glossed = {m[0] for s in head for m in re.finditer(letters, s["text"])
               if explained(s["text"], m.start(), m.end())}
    for sentence in head:
        value = sentence["text"]

        def add(match, kind):
            first = match.start() + len(match[0]) - len(match[0].lstrip())
            hits.append({"line": sentence["lines"][first], "term": match[0].strip(), "kind": kind})

        for word in CONFIG["words"]:
            for match in re.finditer(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])", value, re.I):
                add(match, "术语")
        for match in re.finditer(r"[①-⑳]", value):
            prefix = value[:match.start()].strip(" \t\r\n-*>")
            suffix = value[match.end():].lstrip()
            if not prefix and re.match(CONFIG["step_verbs"], suffix):
                continue
            add(match, "圈号组名（待人工确认）")
        for match in re.finditer(letters, value):
            if match[0] not in CONFIG["known_acronyms"] and match[0] not in glossed:
                add(match, "未解释字母/缩写")
        for match in re.finditer(CONFIG["confidence"], value):
            if not re.search(CONFIG["plain_confidence"], value[match.end():match.end() + 120]):
                add(match, "区间/95% 后缺白话解释")
        for match in re.finditer(r"(?<![A-Za-z0-9_])(?:claude|codex|feature|fix|hotfix|release)/[A-Za-z0-9_./-]+|"
                                 r"(?<=分支)[：:\s`]*[A-Za-z][A-Za-z0-9_./-]*", value):
            add(match, "分支")
        for match in re.finditer(r"(?<![A-Za-z0-9])[a-f0-9]{7,40}(?![A-Za-z0-9])", value, re.I):
            if re.search(r"[a-f]", match[0], re.I):
                add(match, "提交号（待人工确认）")
    hits.sort(key=lambda h: (h["line"], h["term"], h["kind"]))
    # Repeated occurrences on one source line are one reminder, not extra problems.
    hits = list({(h["line"], h["term"], h["kind"]): h for h in hits}.values())
    return {"head": [{"line": s["line"], "text": s["text"].strip()} for s in head], "hits": hits}


def owner_files(backlog: Path, since: str) -> list[dict]:
    """Read only done rows' dated owner-card fragments; never return the full backlog."""
    refs = {}
    with backlog.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not re.match(r"^\s*- \[done\b", line):
                continue
            # Cards may contain square brackets; stop at the next card, not the first ']'.
            cards = list(re.finditer(r"给 owner (\d{2}-\d{2})[：:]", line))
            for i, card in enumerate(cards):
                if card[1] < since:
                    continue
                fragment = line[card.end():cards[i + 1].start() if i + 1 < len(cards) else len(line)]
                fragment = fragment.split("] ", 1)[0]
                for match in re.finditer(r"audits[\\/][^\s`'\"，。；、）)<>\[\]]+?\.(?:md|html|pdf|png|jpg)\b", fragment, re.I):
                    ref = match[0].replace("\\", "/")
                    if ".." in Path(ref).parts:
                        continue
                    refs.setdefault(ref, {"file": ref, "backlog_lines": []})["backlog_lines"].append(number)
    return list(refs.values())


def scan(path: Path) -> dict:
    source = path.with_suffix(".md") if path.suffix.lower() == ".html" and path.with_suffix(".md").is_file() else path
    row = {"file": str(path), "source": str(source), "head": [], "hits": []}
    if source.suffix.lower() not in {".md", ".html"}:
        return {**row, "status": "未扫描：仅支持 Markdown/HTML 文本"}
    try:
        checked = check_text(source.read_text(encoding="utf-8-sig"), source.suffix.lower() == ".html")
    except (OSError, UnicodeError) as exc:
        return {**row, "status": f"未扫描：{type(exc).__name__}: {exc}"}
    return {**row, **checked, "status": "已扫描" if checked["head"] else "未扫描：没有正文"}


def markdown(rows: list[dict]) -> str:
    output = ["只报不拦；命中需人工确认，0 命中不代表易懂。", "", "| 文件 | 命中数 | 命中词 / 状态 |", "|---|---:|---|"]
    for row in rows:
        terms = "、".join(dict.fromkeys(h["term"].strip() for h in row["hits"])) or "—"
        count = str(len(row["hits"])) if row["status"] == "已扫描" else "未扫描"
        detail = terms if count != "未扫描" else row["status"]
        output.append(f"| {row['file']} | {count} | {detail.replace('|', '/')} |")
    output.append("\n逐条命中（对应 source 的原始行号）：")
    for row in rows:
        for hit in row["hits"]:
            output.append(f"- {row['source']}:{hit['line']}：`{hit['term'].strip()}`（{hit['kind']}）")
    return "\n".join(output) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="Markdown/HTML 文件，或递归查 Markdown 的目录")
    parser.add_argument("--backlog", type=Path, help="仅查 done 行「给 owner MM-DD」里的 audits 引用")
    parser.add_argument("--root", type=Path, help="audits 引用的仓库根；默认 backlog 的上上级")
    parser.add_argument("--since", default=CONFIG["since"], help="owner 卡日期下限 MM-DD（同一年）")
    parser.add_argument("--json", action="store_true", help="JSON 输出，包含抽取的三句话和来源行号")
    args = parser.parse_args(argv)
    if args.backlog and args.paths:
        parser.error("--backlog 与 paths 二选一")
    if not re.fullmatch(r"(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])", args.since):
        parser.error("--since 需要 MM-DD")
    rows = []
    if args.backlog:
        root = args.root or args.backlog.resolve().parent.parent
        for ref in owner_files(args.backlog, args.since):
            rows.append({**scan(root / ref["file"]), **ref})
    else:
        for path in args.paths or CONFIG["paths"]:
            for source in sorted(path.rglob("*.md")) if path.is_dir() else [path]:
                rows.append(scan(source))
    print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else markdown(rows), end="\n" if args.json else "")
    return 0  # Advisory only, including missing/unsupported reports; argparse usage errors remain errors.


if __name__ == "__main__":
    raise SystemExit(main())
