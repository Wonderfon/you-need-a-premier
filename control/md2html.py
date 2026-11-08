"""md -> one self-contained HTML page with a fixed calm style, no dependencies (owner\x2010-16: long things for him are HTML).
Usage: python md2html.py <file.md> [<file.md> ...]   writes <file>.html next to each md and prints its path.
Covers what our result files use: headings, paragraphs, lists (nested by indent), tables, fenced code, quotes, bold, inline code, links."""
import html
import re
import sys
from pathlib import Path

CSS = """
:root{--bg:#faf8f4;--fg:#26231f;--muted:#6f675d;--line:#e4ded4;--accent:#2f6f6a;--code:#f1ede6}
@media (prefers-color-scheme:dark){:root{--bg:#1d1b19;--fg:#ece7df;--muted:#a59c90;--line:#3a3631;--accent:#7cc0b8;--code:#2a2724}}
body{background:var(--bg);color:var(--fg);font:16px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0}
main{max-width:860px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:1.6em;margin:.2em 0 .8em}h2{font-size:1.25em;margin:1.8em 0 .6em;padding-bottom:.25em;border-bottom:1px solid var(--line)}
h3{font-size:1.08em;margin:1.4em 0 .4em}p,li{margin:.35em 0}a{color:var(--accent)}
code{background:var(--code);padding:.1em .35em;border-radius:4px;font:.9em Consolas,monospace;word-break:break-all}
pre{background:var(--code);padding:12px 14px;border-radius:6px;overflow-x:auto}pre code{padding:0;background:none;word-break:normal}
table{border-collapse:collapse;margin:.8em 0;display:block;overflow-x:auto}th,td{border:1px solid var(--line);padding:6px 10px;vertical-align:top}
th{background:var(--code);text-align:left}blockquote{margin:.6em 0;padding:.2em 1em;border-left:3px solid var(--accent);color:var(--muted)}
.src{color:var(--muted);font-size:.85em;margin-bottom:1.5em}
@media(max-width:600px){
main{padding:24px 16px 48px;overflow-wrap:anywhere}
pre,pre code{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}
table{display:table;width:100%;table-layout:fixed;overflow:visible}
th,td{padding:4px;font-size:13px;overflow-wrap:anywhere}
img,svg,video{max-width:100%;height:auto}
ul,ol{padding-left:1.4em}
}
"""


def inline(s):
    parts = re.split(r"(`[^`]+`)", s)
    out = []
    for p in parts:
        if p.startswith("`") and p.endswith("`") and len(p) > 1:
            out.append("<code>" + html.escape(p[1:-1]) + "</code>")
            continue
        p = html.escape(p)
        p = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", p)
        p = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', p)
        out.append(p)
    return "".join(out)


def convert(text):
    lines, out, i = text.splitlines(), [], 0
    stack = []  # open list levels

    def close_list():
        item = stack.pop()
        out.append("</li></" + item["tag"] + ">")

    def close_lists():
        while stack:
            close_list()

    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            close_lists()
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(lines[i + 1:j])) + "</code></pre>")
            i = j + 1
            continue
        if re.match(r"^\s*\|.*\|\s*$", ln) and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            close_lists()
            cells = lambda r: [c.strip() for c in r.strip().strip("|").split("|")]
            out.append("<table><tr>" + "".join("<th>" + inline(c) + "</th>" for c in cells(ln)) + "</tr>")
            i += 2
            while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]):
                out.append("<tr>" + "".join("<td>" + inline(c) + "</td>" for c in cells(lines[i])) + "</tr>")
                i += 1
            out.append("</table>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", ln)
        if m:
            close_lists()
            n = len(m.group(1))
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
            i += 1
            continue
        m = re.match(r"^(\s*)(?:([-*])|(\d+)[.)])\s+(.*)", ln)
        if m:
            ind = len(m.group(1))
            number = int(m.group(3)) if m.group(3) else None
            tag = "ol" if number is not None else "ul"
            while stack and ind < stack[-1]["indent"]:
                close_list()
            if stack and ind == stack[-1]["indent"]:
                if tag == stack[-1]["tag"]:
                    out.append("</li>")
                else:
                    close_list()
            if not stack or ind > stack[-1]["indent"]:
                start = f' start="{number}"' if number not in (None, 1) else ""
                out.append(f"<{tag}{start}>")
                stack.append({"indent": ind, "tag": tag})
            out.append("<li>" + inline(m.group(4)))
            i += 1
            continue
        if ln.startswith(">"):
            close_lists()
            out.append("<blockquote>" + inline(ln.lstrip("> ")) + "</blockquote>")
            i += 1
            continue
        if not ln.strip():
            close_lists()
            i += 1
            continue
        close_lists()
        para = [ln]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,6}\s|\s*([-*]|\d+[.)])\s|\s*\||```|>)", lines[i]):
            para.append(lines[i])
            i += 1
        out.append("<p>" + "<br>".join(inline(p) for p in para) + "</p>")
    close_lists()
    return "\n".join(out)


for arg in sys.argv[1:]:
    src = Path(arg)
    text = src.read_text(encoding="utf-8")
    title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.startswith("#")), src.stem)
    page = (f'<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title[:40])}</title><style>{CSS}</style></head><body><main>"
            f'<div class="src">由 {html.escape(src.name)} 生成</div>{convert(text)}</main></body></html>')
    dst = src.with_suffix(".html")
    if dst.exists() and 'class="src">由 ' not in dst.read_text(encoding="utf-8", errors="replace"):
        print(f"skipped {dst}: a hand-made page already exists (10-16 this overwrote the blind-read packet's answer form)")
        continue
    dst.write_text(page, encoding="utf-8")
    print(dst)
