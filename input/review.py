"""资料库 (the old 待审页, since 10-27 /todo is the page he acts on; GET /review redirects there): reports, screenshot
folders, deployed demos and the audit file server (/review/a, /review/s, /review/w, /review/g) the cards link into.

Sources: audits/ (served under /review/a/<path>, allow-listed extensions only), projects/backlog.md (done rows that
say they wait for the owner), projects/deployments.json (name, url, access, commit, time; never passwords), and
data/review-dismissed.json (backlog row ids and dismissal times only).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import html
import json
import os
import re
import secrets
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from . import dashboard as d

DAYS = 7
MD_LIMIT = 2 * 1024 * 1024  # larger .md files are streamed as plain text instead of rendered
CSRF_COOKIE = "demo01_input_csrf"
VIA_HTTPS = "via_private_https"
ROOT_KEY = web.AppKey("review_root", Path)
TYPES = {".html": "text/html", ".md": "text/plain", ".txt": "text/plain", ".json": "application/json",
         ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
         ".webp": "image/webp", ".svg": "image/svg+xml", ".mp4": "video/mp4", ".webm": "video/webm",
         ".mp3": "audio/mpeg", ".pdf": "application/pdf", ".csv": "text/plain", ".css": "text/css", ".js": "text/javascript",
         ".mjs": "text/javascript", ".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".woff2": "font/woff2", ".woff": "font/woff"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
# Non-HTML audit files never run scripts: they share an origin with the input page's APIs.
FILE_CSP = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; font-src data:; media-src 'self'; "
            "script-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
# Under /review/s/<key>/ (the pending page's open links) .html pages (tick sheets, sketches) run their scripts in a
# sandbox without allow-same-origin: an opaque origin, so trust_boundary refuses their calls to the input page's APIs,
# while their relative file loads stay under the same unguessable key, which trust_boundary lets through.
PAGE_KEY = web.AppKey("review_page_key", str)
WT_KEY = web.AppKey("review_wt_root", Path)
PAGE_CSP = ("sandbox allow-scripts allow-popups allow-modals allow-downloads; default-src 'self' data: blob:; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' data: https://fonts.gstatic.com; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
_OPEN = re.compile(r"Start-Process\s+(?:notepad\s+)?'((?:[A-Za-z]:\\|/)[^']+)'")
GALLERY_LIMIT = 400

# A done row waits for the owner to try or look when its completion note (text after "原状态" is history) says
# 等/交/待 owner|你|他 试用|看|听 ("等 owner 试用", "等你试用", "等 owner 看", "交 owner 看", "待 owner 听"),
# or "owner|你 看过再定", or carries the reviewer's tag "给 owner [MM-DD]：<做什么>" (owner\x2010-21: the page should show
# what is for him). Waiting for a merge/restart or a decision is not this page's business (owner-pending page).
WAITING = re.compile(r"(?:等|交|待)\s*(?:owner|你|他)\s*(?:试用|看|听)|(?:owner|你)\s*看过再|给\s*(?:owner|你)\s*(?:\d{2}-\d{2}\s*)?[：:]")
_TAG_DAY = re.compile(r"给\s*(?:owner|你)\s*(\d{2})-(\d{2})")  # date for done rows whose stamp has no day ("done 16:56")
_TAG = re.compile(r"给\s*(?:owner|你)\s*(?:\d{2}-\d{2}\s*)?[：:]\s*(.+)$")  # with the tag, show only what he is asked to do
_OWNER_HANDLED = re.compile(r"owner\s*已(?:处理|答|看)")  # "owner 已处理 10-21", "owner 已答，records/…"; text after 原状态 is history, cut off before this runs
_TRIAL_ID = re.compile(r"[0-9a-f]{24}")
_DISMISSED_FILE = "review-dismissed.json"
_DISMISS_LOCK = threading.Lock()
_REPORT = re.compile(r"(\d{4}-\d{2}-\d{2})-.+\.(md|html)", re.I)
_AUDIT = re.compile(r"audits[\\/][^\s`'\"<>（）()\[\]，；。：、]+")
_URL = re.compile(r"https://[^\s`'\"<>（）()\[\]，；。、]+")
_e = html.escape
_u = lambda rel: quote(rel, safe="/")  # a path inside a /review/... link


def audit_path(root: Path, rel: str, want_dir: bool = False) -> Path | None:
    """The real file (or folder) audits/<rel>, or None for anything else: odd separators, drive letters or
    streams, '..', device names, links that leave audits/, other extensions."""
    return _inside(Path(root) / "audits", rel, want_dir)


# wt\ (branch worktrees: demos, sketches) also holds repos' config, so nothing under these folders or with a
# credential-looking name is served there.
_WT_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "02_secrets", ".ssh", "secrets", "site-packages", ".claude", "user-data"}
_WT_SECRETISH = re.compile(r"secret|token|credential|passw|api[_-]?key|\.env|dpapi|private[_-]?key|cookie", re.I)


def wt_path(wt: Path, rel: str) -> Path | None:
    """A file under wt\\<rel> (same checks as audit_path), never inside _WT_DIRS or with a _WT_SECRETISH name."""
    parts = [p.lower() for p in rel.split("/")]
    if (any(p in _WT_DIRS for p in parts) or any(k in p for p in parts[:-1] for k in ("profile", "user data", "user-data")) or _WT_SECRETISH.search(rel)
            or re.fullmatch(r"auth.*\.json", parts[-1])
            or (parts[0].startswith("umba") and "data" in parts[1:-1])):
        return None
    return _inside(Path(wt), rel, False)


def _inside(folder: Path, rel: str, want_dir: bool) -> Path | None:
    base = folder.resolve()
    parts = rel.split("/")
    if (not rel or "\\" in rel or ":" in rel or "\x00" in rel
            or any(p in ("", ".", "..") or p != p.rstrip(". ") or p.split(".")[0].strip().upper() in RESERVED
                   for p in parts)):
        return None
    try:
        target = base.joinpath(*parts).resolve(strict=True)
        target.relative_to(base)
    except (OSError, ValueError):
        return None
    if want_dir:
        return target if target.is_dir() else None
    return target if target.is_file() and target.suffix.lower() in TYPES else None


def audit_href(root: Path, command: str, key: str, wt: Path | None = None) -> str | None:
    """This browser's link for a `Start-Process '<file or folder in audits, or a file in wt>'` command (owner\x2010-21:
    over SSH or Tailscale those commands open on the home PC, not where he is reading); None for anything else."""
    found = _OPEN.match(command.strip())
    if found and wt is not None:
        try:
            rel = Path(found[1].replace("\\", os.sep)).relative_to(wt).as_posix()
            return f"/review/w/{key}/{_u(rel)}" if wt_path(wt, rel) else None
        except ValueError:
            pass
    try:
        rel = Path(found[1].replace("\\", os.sep)).relative_to(Path(root) / "audits").as_posix() if found else ""
    except ValueError:
        return None
    if rel and audit_path(root, rel):
        return f"/review/s/{key}/{_u(rel)}"
    return f"/review/g/{_u(rel)}" if rel and audit_path(root, rel, want_dir=True) else None


_junction = getattr(os.path, "isjunction", lambda path: False)  # a Windows junction looks like a plain folder but can leave audits/


def _plain_dir(entry: os.DirEntry) -> bool:
    return entry.is_dir(follow_symlinks=False) and not _junction(entry.path)


def _count_images(folder: str) -> int:
    total = 0
    for here, dirs, names in os.walk(folder):
        dirs[:] = [x for x in dirs if not _junction(os.path.join(here, x))]
        total += sum(Path(n).suffix.lower() in IMAGES for n in names)
    return total


def _report_files(root: Path, today: date) -> list[dict]:
    """audits/YYYY-MM-DD-*.md|html of the last DAYS days; a page and its md source count once (the page wins)."""
    cut, stems = today - timedelta(days=DAYS), {}
    try:
        entries = list(os.scandir(Path(root) / "audits"))
    except OSError:
        return []
    for e in entries:
        m = _REPORT.fullmatch(e.name)
        if not m or not e.is_file(follow_symlinks=False):
            continue
        try:
            day = date.fromisoformat(m[1])
        except ValueError:
            continue
        if day >= cut:
            item = stems.setdefault(e.name.rsplit(".", 1)[0], {"day": day, "mtime": 0.0})
            item[m[2].lower()] = e.name
            item["mtime"] = max(item["mtime"], e.stat().st_mtime)
    return sorted(stems.values(), key=lambda i: (i["day"], i["mtime"]), reverse=True)


def _title(path: Path, md: bool) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            head = stream.read(16384)
    except OSError:
        return path.stem
    if md:
        found = next((ln.lstrip("# ").strip() for ln in head.splitlines() if ln.startswith("#")), "")
    else:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", head, re.S | re.I) or re.search(r"<title[^>]*>(.*?)</title>", head, re.S | re.I)
        found = html.unescape(re.sub(r"<[^>]+>", "", m[1])).strip() if m else ""
    return " ".join(found.split())[:120] or path.stem


def reports(root: Path, today: date) -> list[dict]:
    out = []
    for i in _report_files(root, today):
        name = i.get("html") or i["md"]
        out.append({"day": i["day"].isoformat(), "name": name, "md": i.get("md") if "html" in i else None,
                    "title": _title(Path(root) / "audits" / name, name.endswith(".md"))})
    return out


def galleries(root: Path, today: date) -> list[dict]:
    """Top-level audits/ folders of the last DAYS days (name date, else modified date) that hold images."""
    cut, out = today - timedelta(days=DAYS), []
    try:
        entries = list(os.scandir(Path(root) / "audits"))
    except OSError:
        return []
    for e in entries:
        if not _plain_dir(e):
            continue
        try:
            day = date.fromisoformat(e.name[:10])
        except ValueError:
            day = date.fromtimestamp(e.stat().st_mtime)
        if day >= cut and (n := _count_images(e.path)):
            out.append({"name": e.name, "day": day.isoformat(), "images": n})
    return sorted(out, key=lambda g: (g["day"], g["name"]), reverse=True)


def _commands(text: str) -> list[str]:
    spans = re.findall(r"`([^`]+)`", text)
    found = [s.strip() for s in spans if re.match(r"(?:Start-Process|wsl)\s+\S", s.strip())]
    found += re.findall(r"Start-Process\s+'[^']+'", re.sub(r"`[^`]+`", " ", text))
    return list(dict.fromkeys(d._SECRET.sub("[已隐藏]", c) for c in found))[:6]


def _links(root: Path, text: str) -> list[dict]:
    out = {}
    for m in _AUDIT.finditer(text):
        rel = m[0].replace("\\", "/")[len("audits/"):].rstrip(".,;/")
        if audit_path(root, rel):
            out[rel] = f"/review/a/{_u(rel)}"
        elif audit_path(root, rel, want_dir=True):
            out[rel] = f"/review/g/{_u(rel)}"
    return [{"label": f"audits/{rel}", "href": href} for rel, href in list(out.items())[:4]]


def trials(root: Path, today: date) -> list[dict]:
    """Done rows of the last DAYS days whose completion note says they wait for the owner to try or look."""
    _, text, _ = d._read_text(root, "projects", "backlog.md", limit=d.BACKLOG_LIMIT)
    cut, rows = today - timedelta(days=DAYS), []
    for row in d._control("backlog_history").all_rows(Path(root) / "projects/backlog.md", text or "", since=cut):
        n, line = row["line"] - 1, row["raw"]
        item = d._PLAN_LINE.match(line.rstrip("\r"))
        status = d._PLAN_STATUS.match(item[1].strip()) if item else None
        if not status or status[1] != "done":
            continue
        note = status[2].split("原状态")[0]
        stamp = re.match(r"(\d{2})-(\d{2})", note) or _TAG_DAY.search(note)
        try:
            day = date(today.year, int(stamp[1]), int(stamp[2])) if stamp else None
        except ValueError:
            day = None
        if not day or not cut <= day <= today or not WAITING.search(note) or _OWNER_HANDLED.search(note):
            continue
        head, sep, rest = item[2].partition("：")
        project, title = (head, rest) if sep and len(head) <= 24 else ("", item[2])
        tag = _TAG.search(note)
        src = tag[1] if tag else note
        summary = _AUDIT.sub("", src).strip(" ，,；;") if tag else ""
        links = _links(root, src)
        urls = list(dict.fromkeys(u.rstrip(".,;:") for u in _URL.findall(src)))[:4]
        rows.append({"id": hashlib.sha256(item[2].strip().encode("utf-8")).hexdigest()[:24],
                     "day": day, "n": n, "line": row["line"], "source": row["source"],
                     "section": row["section"], "project": project, "tagged": bool(tag),
                     "title": re.sub(r"[：:]\s*验收通过$", "", re.split(r"[（(；。]", title, 1)[0].strip())[:90],
                     "summary": d._SECRET.sub("[已隐藏]", summary)[:300],
                     "commands": _commands(src) if tag else [], "urls": urls if tag else urls[:1],
                     "links": links if tag else links[:1]})
    # Line numbers are provenance within each file, not a global ordering.
    return sorted(rows, key=lambda r: (r["day"], r["project"], r["title"], r["summary"]), reverse=True)


def _dismissed(root: Path) -> dict[str, str]:
    path = Path(root) / "data" / _DISMISSED_FILE
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {}
        return {key: at for key, at in value.items()
                if isinstance(key, str) and _TRIAL_ID.fullmatch(key) and isinstance(at, str)}
    except (OSError, ValueError, TypeError):
        return {}


def _save_dismissed(root: Path, dismissed: dict[str, str]) -> None:
    data_dir = Path(root) / "data"
    path = data_dir / _DISMISSED_FILE
    if data_dir.is_symlink() or path.is_symlink():
        raise OSError("review-dismissed.json must be a regular file under data/")
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=data_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(dismissed, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _set_dismissed(root: Path, item_id: str, action: str) -> bool:
    today = datetime.now().astimezone().date()
    with _DISMISS_LOCK:
        if item_id not in {item["id"] for item in trials(root, today)}:
            return False
        dismissed = _dismissed(root)
        if action == "dismiss":
            dismissed[item_id] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            dismissed.pop(item_id, None)
        _save_dismissed(root, dismissed)
    return True


def deployments(root: Path) -> list[dict]:
    state, data, _ = d._read_json(root, "projects", "deployments.json")
    items = data.get("products", []) if state == "ok" and isinstance(data, dict) else []
    out = []
    for p in items:
        if isinstance(p, dict) and str(p.get("url")).startswith(("https://", "http://")):
            stamp = str(p.get("deployed_at") or "")
            out.append({"name": d._text(p.get("name")) or p["url"], "url": p["url"], "password": p.get("access") == "password",
                        "commit": str(p.get("commit") or "")[:12], "at": re.sub(r"^(\S+?)T(\d\d:\d\d).*", r"\1 \2", stamp)})
    return sorted(out, key=lambda p: p["at"], reverse=True)


def counts(root: Path, today: date | None = None) -> dict:
    """Numbers for the dashboard card; names only, no file contents read."""
    today = today or datetime.now().astimezone().date()
    def build() -> dict:
        dismissed = _dismissed(root)
        return {"reports": len(_report_files(root, today)),
                "trials": sum(item["id"] not in dismissed for item in trials(root, today)),
                "deployments": len(deployments(root))}
    return d._guard(build, None)


@functools.cache
def _md_module():
    argv, sys.argv = sys.argv, ["md2html"]  # control/md2html.py converts sys.argv files when imported
    try:
        return d._control("md2html")
    finally:
        sys.argv = argv


CSS = """
:root{--bg:#fff;--text:#171717;--muted:#555;--soft:#686868;--surface:#f5f6f8;--pale:#dce7ff;--blue:#4268f6;--line:#d4d6db;--hair:#e6e8ec;
 --warn:#8a4b00;--warn-soft:#fff4e0;--bad:#b42318}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 "Nunito Sans",system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
a{color:var(--blue);text-decoration:none;border-radius:4px}a:hover{text-decoration:underline}
:focus-visible{outline:3px solid var(--blue);outline-offset:2px}
.shell{max-width:1120px;margin:0 auto;padding:18px 20px 56px}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:12px;padding-bottom:12px;border-bottom:1px solid var(--hair)}
.topbar nav{display:flex;gap:16px}.brand{font-weight:700;color:var(--text)}.muted{color:var(--muted)}.small{font-size:12.5px;margin:6px 0 0}
.hero{margin:22px 0 6px}.eyebrow{margin:0;font-size:12px;letter-spacing:.08em;color:var(--blue);font-weight:700}
h1{margin:4px 0 8px;font-size:26px;font-weight:800;line-height:1.3}h2{font-size:16px;font-weight:700;margin:0}h3{font-size:15px;margin:0;font-weight:600}
.jump{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 0}
.jump a{display:inline-block;padding:4px 12px;border:1px solid var(--line);border-radius:999px;color:var(--text)}
.jump a:hover{background:var(--pale);border-color:var(--blue);text-decoration:none}
.panel{border:1px solid var(--hair);border-radius:14px;padding:16px 18px;margin-top:16px;min-width:0}
.panel-head{display:flex;justify-content:space-between;align-items:baseline;gap:4px 12px;flex-wrap:wrap;margin-bottom:12px}
.panel-note{font-size:12.5px;color:var(--soft)}
.list{border-top:1px solid var(--hair)}.row{padding:10px 2px;border-bottom:1px solid var(--hair);word-break:break-word}
.row-head{display:flex;align-items:center;gap:6px 8px;flex-wrap:wrap}.row-title{font-weight:600}
.row-meta{font-size:13px;margin-top:2px;color:var(--muted)}
.badge{display:inline-block;font-size:12px;line-height:1.5;padding:1px 8px;border-radius:999px;background:var(--surface);color:var(--muted);white-space:nowrap}
.badge.blue{background:var(--pale);color:#1d3fc4}.badge.warn{background:var(--warn-soft);color:var(--warn)}
.empty{color:var(--muted);font-size:14px;padding:8px 2px;margin:0}
.day{border:1px solid var(--line);border-radius:12px;margin-top:10px}
.day>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px;padding:10px 14px;font-weight:700}
.day>summary::-webkit-details-marker{display:none}
.day>summary::before{content:'▸';color:var(--blue);transition:transform .15s}.day[open]>summary::before{transform:rotate(90deg)}
.day>summary:hover{background:var(--surface);border-radius:11px}.day .list{padding:0 14px 6px}
.filter{width:100%;max-width:420px;min-height:36px;padding:4px 12px;border:1px solid var(--line);border-radius:8px;font:inherit}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.chip{display:inline-flex;gap:6px;align-items:baseline;padding:4px 12px;border:1px solid var(--line);border-radius:999px;color:var(--text);word-break:break-all}
.chip:hover{background:var(--pale);border-color:var(--blue);text-decoration:none}
.cmd{display:flex;align-items:flex-start;gap:8px;min-width:0;margin-top:6px}
.cmd code{flex:1 1 0;min-width:0;padding:4px 8px;background:var(--surface);border:1px solid var(--hair);border-radius:8px;font:12.5px/1.5 ui-monospace,Consolas,monospace;overflow-wrap:anywhere;user-select:all}
.copy-btn{flex:none;min-height:32px;padding:3px 14px;border:1px solid var(--blue);border-radius:8px;background:var(--pale);color:#1d3fc4;font:inherit;font-size:13px;font-weight:600;cursor:pointer}
.copy-btn:hover{background:var(--blue);color:#fff}.copy-btn:active{transform:translateY(1px)}
.copy-btn.done{background:#e6f6ec;border-color:#0a6b3d;color:#0a6b3d}.copy-btn.fail{background:#fef3f2;border-color:var(--bad);color:var(--bad)}
.dismiss-btn{flex:none;min-height:34px;padding:4px 12px;border:1px solid var(--blue);border-radius:8px;background:var(--pale);color:#1d3fc4;font:inherit;font-size:13px;font-weight:700;cursor:pointer}
.dismiss-btn:hover{background:var(--blue);color:#fff}.dismiss-btn:active{transform:translateY(1px)}
.dismiss-btn.restore{border-color:#7cc19a;background:#e8f6ee;color:#1a6b3a}.dismiss-btn.restore:hover{background:#1a6b3a;color:#fff}
.trial-status{margin:8px 0 0;color:var(--bad);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.grid figure{margin:0}.grid img{display:block;width:100%;height:150px;object-fit:cover;border:1px solid var(--hair);border-radius:8px;background:var(--surface)}
.grid figcaption{font-size:12px;color:var(--muted);word-break:break-all;margin-top:4px}
.footer{margin-top:36px;font-size:12.5px;color:var(--soft)}
@media(max-width:760px){.shell{padding:14px 16px 40px}h1{font-size:22px}.panel{padding:14px}.cmd{flex-direction:column;align-items:stretch}.cmd code{flex:none}}
"""

SCRIPT = """
(() => {
  'use strict';
  const CSRF = '__CSRF_TOKEN__';
  const copyText = (text) => ((navigator.clipboard && window.isSecureContext) ? navigator.clipboard.writeText(text) : Promise.reject())
    .catch(() => new Promise((resolve, reject) => {  // plain-http page or refused: copy through a temporary textarea
      const box = document.createElement('textarea');
      box.value = text; box.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.append(box); box.select();
      try { document.execCommand('copy') ? resolve() : reject(new Error('refused')); } catch (err) { reject(err); } finally { box.remove(); }
    }));
  document.addEventListener('click', (event) => {
    const btn = event.target.closest('.copy-btn');
    if (!btn) return;
    copyText(btn.dataset.copy).then(() => { btn.textContent = '已复制 ✓'; btn.className = 'copy-btn done'; },
      () => { btn.textContent = '复制失败，请手动选中'; btn.className = 'copy-btn fail'; });
    clearTimeout(btn.timer);
    btn.timer = setTimeout(() => { btn.textContent = '复制'; btn.className = 'copy-btn'; }, 2500);
  });
  const active = document.getElementById('trial-list');
  const archived = document.getElementById('dismissed-list');
  const archive = document.getElementById('dismissed-box');
  function updateTrials() {
    const activeCount = active.querySelectorAll('.row').length;
    const archivedCount = archived.querySelectorAll('.row').length;
    document.getElementById('trial-count').textContent = `${activeCount} 条待你试用`;
    document.getElementById('jump-trial').textContent = `给你的 ${activeCount}`;
    document.getElementById('dismissed-summary').textContent = `已看过 ${archivedCount} 条`;
    for (const [list, text] of [[active, '现在没有等你试用的成果。'], [archived, '还没有已看过的成果。']]) {
      let empty = list.querySelector('.empty');
      if (list.querySelector('.row')) { if (empty) empty.remove(); }
      else if (!empty) { empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = text; list.append(empty); }
    }
  }
  document.addEventListener('click', async (event) => {
    const button = event.target.closest('.dismiss-btn');
    if (!button || button.disabled) return;
    const dismiss = button.dataset.action === 'dismiss';
    const row = button.closest('.row');
    const source = row.parentElement;
    const next = row.nextSibling;
    const wasOpen = archive.open;
    const action = dismiss ? 'dismiss' : 'restore';
    button.disabled = true;
    if (dismiss) archived.append(row);
    else {
      const before = Array.from(active.querySelectorAll('.row')).find(candidate =>
        candidate.dataset.day < row.dataset.day ||
        (candidate.dataset.day === row.dataset.day && Number(candidate.dataset.line) < Number(row.dataset.line)));
      active.insertBefore(row, before || null);
    }
    button.dataset.action = dismiss ? 'restore' : 'dismiss';
    button.classList.toggle('restore', dismiss);
    button.textContent = dismiss ? '撤回' : '看过了';
    if (dismiss) archive.open = true;
    const status = document.getElementById('trial-status');
    status.hidden = true;
    updateTrials();
    try {
      const response = await fetch('/api/review/dismiss', { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
        body: JSON.stringify({ id: button.dataset.dismissId, action }) });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || '没有保存成功，请刷新页面后重试。');
    } catch (error) {
      source.insertBefore(row, next && next.parentElement === source ? next : null);
      button.dataset.action = action;
      button.classList.toggle('restore', !dismiss);
      button.textContent = dismiss ? '看过了' : '撤回';
      archive.open = wasOpen;
      status.textContent = error.message;
      status.hidden = false;
      updateTrials();
    } finally { button.disabled = false; }
  });
  updateTrials();
  const q = document.getElementById('q');
  if (q) q.addEventListener('input', () => {
    const word = q.value.trim().toLowerCase();
    let shown = 0;
    for (const day of document.querySelectorAll('.day')) {
      let any = 0;
      for (const row of day.querySelectorAll('.rep')) {
        row.hidden = word !== '' && !row.textContent.toLowerCase().includes(word);
        if (!row.hidden) any += 1;
      }
      day.hidden = any === 0;
      if (word) day.open = true;
      shown += any;
    }
    document.getElementById('q-count').textContent = word ? `匹配 ${shown} 份` : '';
  });
})();
"""


def _badge(text: str, tone: str = "") -> str:
    return f'<span class="badge {tone}">{_e(text)}</span>'


def _cmd(text: str, link: bool = False) -> str:
    code = f'<a href="{_e(text)}" target="_blank" rel="noopener noreferrer">{_e(text)}</a>' if link else _e(text)
    return (f'<div class="cmd"><code>{code}</code><button type="button" class="copy-btn" '
            f'data-copy="{_e(text)}" title="复制到剪贴板">复制</button></div>')


def _section(sid: str, title: str, note: str, body: str) -> str:
    return (f'<section class="panel" id="{sid}" aria-labelledby="{sid}-t"><div class="panel-head"><h2 id="{sid}-t">{title}</h2>'
            f'<span class="panel-note">{note}</span></div>{body}</section>')


def _trial_row(t: dict, action: str = "dismiss") -> str:
    button_text = "看过了" if action == "dismiss" else "撤回"
    button_class = "dismiss-btn" if action == "dismiss" else "dismiss-btn restore"
    button = (f'<button type="button" class="{button_class}" data-dismiss-id="{_e(t["id"])}" '
              f'data-action="{action}">{button_text}</button>')
    if not t["tagged"]:
        link = t["links"][0] if t["links"] else ({"label": "打开", "href": t["urls"][0]} if t["urls"] else None)
        opened = f'<div class="row-meta"><a href="{_e(link["href"])}">{_e(link["label"])}</a></div>' if link else ""
        return (f'<div class="row" data-day="{t["day"].isoformat()}" data-line="{t["n"]}">'
                f'<div class="row-head"><span class="row-title">{_e(t["title"])}</span>{button}'
                f'</div>{opened}</div>')
    links = "".join(f'<div class="row-meta"><a href="{_e(k["href"])}">{_e(k["label"])}</a></div>' for k in t["links"])
    return (f'<div class="row" data-day="{t["day"].isoformat()}" data-line="{t["n"]}"><div class="row-head">'
            + _badge(t["day"].strftime("%m-%d"), "blue")
            + (_badge(t["project"]) if t["project"] else "") + f'<span class="row-title">{_e(t["title"])}</span>{button}</div>'
            f'<div class="row-meta">{_e(t["summary"])}</div>' + "".join(_cmd(c) for c in t["commands"])
            + "".join(_cmd(u, True) for u in t["urls"]) + links + '</div>')


def _deploy_row(p: dict) -> str:
    name = f'<a class="row-title" href="{_e(p["url"])}" target="_blank" rel="noopener noreferrer">{_e(p["name"])}</a>'
    meta =" · ".join(x for x in (f'版本 {p["commit"][:7]}' if p["commit"] else "无仓库版本", f'部署 {p["at"]}' if p["at"] else "") if x)
    return (f'<div class="row"><div class="row-head">{name}' + _badge("密码访问" if p["password"] else "公开", "warn" if p["password"] else "")
            + f'</div><div class="row-meta">{_e(meta)} · {_e(p["url"])}</div></div>')


def _report_days(items: list[dict]) -> str:
    out = []
    for day in dict.fromkeys(i["day"] for i in items):
        rows = [i for i in items if i["day"] == day]
        body = "".join(
            f'<div class="row rep"><a class="row-title" href="/review/a/{_u(i["name"])}">{_e(i["title"])}</a>'
            f'<div class="row-meta">{_e(i["name"])}'
            + (f' · <a href="/review/a/{_u(i["md"])}">md 原文</a>' if i["md"] else "") + "</div></div>" for i in rows)
        out.append(f'<details class="day"><summary>{day} {_badge(f"{len(rows)} 份")}</summary>'
                   f'<div class="list">{body}</div></details>')
    return "".join(out)


def review_html(root: Path, today: date, nonce: str, csrf_token: str = "") -> str:
    all_trials, dep, rep, gal = trials(root, today), deployments(root), reports(root, today), galleries(root, today)
    dismissed = _dismissed(root)
    t = [item for item in all_trials if item["id"] not in dismissed]
    seen = [item for item in all_trials if item["id"] in dismissed]
    empty = lambda text: f'<p class="empty">{text}</p>'
    sections = [
        _section("trial", "给你的：要你读、勾或试", f'<span id="trial-count">{len(t)} 条待你试用</span> · 近 7 天已验收、标了给你或等你试用的成果',
                 f'<div class="list" id="trial-list">{"".join(_trial_row(x) for x in t) or empty("现在没有等你试用的成果。")}</div>'
                 '<p id="trial-status" class="trial-status" role="status" hidden></p>'),
        _section("deploy", "已部署的 demo", "登记于 projects/deployments.json · 密码不在这里",
                 '<div class="list">' + "".join(_deploy_row(p) for p in dep) + "</div>" if dep else empty("还没有部署登记。")),
        _section("reports", "其余报告", "audits/ 近 7 天全部文件，大多是给审查看的 · 默认收起 · 点日期展开",
                 ('<p><input id="q" class="filter" type="search" placeholder="按标题或文件名筛选" aria-label="筛选报告"> '
                  '<span id="q-count" class="panel-note" role="status" aria-live="polite"></span></p>' + _report_days(rep))
                 if rep else empty("近 7 天没有报告。")),
        _section("shots", "截图文件夹", "audits/ 里近 7 天含图片的文件夹 · 点开看缩略图",
                 '<div class="chips">' + "".join(f'<a class="chip" href="/review/g/{_u(g["name"])}">{_e(g["name"])} '
                                                  + _badge(str(g["images"]) + " 张") + "</a>" for g in gal) + "</div>"
                 if gal else empty("近 7 天没有截图文件夹。")),
    ]
    jump = (f'<a id="jump-trial" href="#trial">给你的 {len(t)}</a>'
            + "".join(f'<a href="#{i}">{label} {n}</a>' for i, label, n in
                      (("deploy", "已部署", len(dep)), ("reports", "报告", len(rep)), ("shots", "截图", len(gal)))))
    dismissed_section = (f'<details class="panel" id="dismissed-box"><summary id="dismissed-summary">已看过 {len(seen)} 条</summary>'
                         f'<div class="list" id="dismissed-list">{"".join(_trial_row(x, "restore") for x in seen) or empty("还没有已看过的成果。")}</div></details>')
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>资料库</title><style nonce="{nonce}">{CSS}</style></head><body><main class="shell">'
            '<header class="topbar"><div><a class="brand" href="/review/all">资料库</a><span class="muted"> · 收起状态保存在本机</span></div>'
            '<nav><a href="/todo">要你的</a><a href="/dashboard">组织看板</a><a href="/">输入台</a></nav></header>'
            f'<section class="hero"><p class="eyebrow">报告、截图、已部署的 demo</p><h1>资料库</h1><p class="muted small">'
            f'读取于 {datetime.now().strftime("%m-%d %H:%M")} · 刷新页面即更新 · 报告和图片都在本页内打开</p>'
            f'<nav class="jump" aria-label="跳到">{jump}</nav></section>' + "".join(sections) + dismissed_section
            + '<footer class="footer">报告和部署信息只读；「看过了」状态写入 data/review-dismissed.json，只保存任务行标识和时间。'
              '报告里的 .html 页面在隔离沙箱里运行：能勾选、能生成回复文本，调用不到输入台的接口；其余格式只读，不运行脚本。</footer>'
            f'</main><script nonce="{nonce}">{SCRIPT.replace("__CSRF_TOKEN__", csrf_token)}</script></body></html>')


def gallery_html(root: Path, folder: Path, rel: str, nonce: str) -> str:
    subs, files, images = [], [], []
    for e in sorted(os.scandir(folder), key=lambda e: e.name):
        suffix = Path(e.name).suffix.lower()
        if _plain_dir(e) and (n := _count_images(e.path)):
            subs.append(f'<a class="chip" href="/review/g/{_u(rel)}/{_u(e.name)}">{_e(e.name)}/ ' + _badge(f"{n} 张") + "</a>")
        elif e.is_file(follow_symlinks=False) and suffix in IMAGES:
            images.append(e.name)
        elif e.is_file(follow_symlinks=False) and suffix in (".md", ".html"):
            files.append(f'<a class="chip" href="/review/a/{_u(rel)}/{_u(e.name)}">{_e(e.name)}</a>')
    shown = images[:GALLERY_LIMIT]
    grid = "".join(f'<figure><a href="/review/a/{_u(rel)}/{_u(n)}"><img loading="lazy" src="/review/a/{_u(rel)}/{_u(n)}" alt="{_e(n)}"></a>'
                   f'<figcaption>{_e(n)}</figcaption></figure>' for n in shown)
    parts = [f'<div class="chips">{"".join(subs)}</div>' if subs else "", f'<div class="chips">{"".join(files[:60])}</div>' if files else "",
             f'<div class="grid">{grid}</div>' if grid else "" if subs else '<p class="empty">这个文件夹里没有图片。</p>',
             f'<p class="panel-note">共 {len(images)} 张，只显示前 {GALLERY_LIMIT} 张。</p>' if len(images) > GALLERY_LIMIT else ""]
    up = rel.rpartition("/")[0]
    back = f'<a href="/review/g/{_u(up)}">← 上一级</a> · ' if up else ""
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{_e(rel.rsplit("/", 1)[-1])}</title><style nonce="{nonce}">{CSS}</style></head><body><main class="shell">'
            '<header class="topbar"><div><a class="brand" href="/todo">要你的</a><span class="muted"> · 只读</span></div>'
            '<nav><a href="/dashboard">组织看板</a></nav></header>'
            f'<section class="hero"><h1>{_e(rel)}</h1><p class="muted small">{back}<a href="/review/all">← 资料库</a> · '
            f'点图片看原图</p></section><section class="panel">{"".join(parts)}</section></main></body></html>')


def _page(text: str, nonce: str) -> web.Response:
    response = web.Response(text=text, content_type="text/html", charset="utf-8")
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
    return response


async def to_todo(request: web.Request) -> web.Response:
    return web.Response(status=302, headers={"Location": "/todo"})


async def review_page(request: web.Request) -> web.Response:
    nonce = secrets.token_urlsafe(16)
    token = request.cookies.get(CSRF_COOKIE, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        token = secrets.token_urlsafe(32)
    today = datetime.now().astimezone().date()
    response = _page(await asyncio.to_thread(review_html, request.app[ROOT_KEY], today, nonce, token), nonce)
    response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="Strict", path="/",
                        secure=bool(request.get(VIA_HTTPS)))
    return response


async def dismiss_trial(request: web.Request) -> web.Response:
    if request.content_type != "application/json":
        return web.json_response({"error": "请求格式不正确。"}, status=400)
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return web.json_response({"error": "请求格式不正确。"}, status=400)
    if not isinstance(body, dict) or set(body) != {"id", "action"}:
        return web.json_response({"error": "请求字段不正确。"}, status=400)
    item_id, action = body["id"], body["action"]
    if (not isinstance(item_id, str) or not _TRIAL_ID.fullmatch(item_id)
            or not isinstance(action, str) or action not in {"dismiss", "restore"}):
        return web.json_response({"error": "操作无效，请刷新页面后重试。"}, status=400)
    if not await asyncio.to_thread(_set_dismissed, request.app[ROOT_KEY], item_id, action):
        return web.json_response({"error": "这条已不在待审页，请刷新后重试。"}, status=409)
    return web.json_response({"id": item_id, "action": action})


async def gallery_page(request: web.Request) -> web.Response:
    rel = request.match_info["path"].strip("/")
    folder = audit_path(request.app[ROOT_KEY], rel, want_dir=True)
    if folder is None:
        raise web.HTTPNotFound()
    nonce = secrets.token_urlsafe(16)
    return _page(await asyncio.to_thread(gallery_html, request.app[ROOT_KEY], folder, rel, nonce), nonce)


def _render_md(path: Path, name: str) -> str:
    md = _md_module()
    text = path.read_text(encoding="utf-8", errors="replace")
    title = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), path.stem)
    return ('<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_e(title[:60])}</title><style>{md.CSS}</style></head><body><main>"
            f'<p class="src"><a href="/todo">← 要你的</a> · {_e(name)}</p>{md.convert(text)}</main></body></html>')


async def audit_file(request: web.Request, sandboxed: bool = False, wt: bool = False) -> web.StreamResponse:
    rel = request.match_info["path"]
    path = wt_path(request.app[WT_KEY], rel) if wt else audit_path(request.app[ROOT_KEY], rel)
    if path is None:
        raise web.HTTPNotFound()
    if not sandboxed and path.suffix.lower() == ".html":
        # All HTML entry points (report links, md2html, owner_html, TM sheets) use the existing opaque origin.
        # Keep FILE_CSP strict; allowing scripts without the sandbox would expose the input page's APIs.
        location = request.rel_url.with_path(f"/review/s/{request.app[PAGE_KEY]}/{rel}").with_query(request.query)
        return web.Response(status=302, headers={"Location": str(location)})
    kind = TYPES[path.suffix.lower()]
    if path.suffix.lower() == ".md" and path.stat().st_size <= MD_LIMIT:
        response: web.StreamResponse = web.Response(
            text=await asyncio.to_thread(_render_md, path, path.name), content_type="text/html", charset="utf-8")
    else:
        response = web.FileResponse(path)  # streamed, with Range support
        response.content_type = kind
        if kind.startswith("text/") or kind == "application/json":
            response.charset = "utf-8"
    response.headers["Content-Security-Policy"] = PAGE_CSP if sandboxed and path.suffix.lower() == ".html" else FILE_CSP
    return response


async def sandboxed_file(request: web.Request, wt: bool = False) -> web.StreamResponse:
    if not secrets.compare_digest(request.match_info["key"], request.app[PAGE_KEY]):
        raise web.HTTPNotFound()
    response = await audit_file(request, sandboxed=True, wt=wt)
    if request.headers.get("Origin") == "null":  # the sandboxed page's own fetch(); the key already gates who can ask
        response.headers["Access-Control-Allow-Origin"] = "null"
    return response


async def wt_file(request: web.Request) -> web.StreamResponse:
    return await sandboxed_file(request, wt=True)


def add_routes(app: web.Application, root: Path, wt_root: Path | None = None) -> None:
    app[ROOT_KEY] = Path(root)
    app[WT_KEY] = Path(wt_root) if wt_root is not None else Path(root).resolve().parent.parent / "wt"  # C:\orghome\wt
    app[PAGE_KEY] = secrets.token_urlsafe(24)  # new on every start; only same-origin pages (the pending page's API) see it
    app.router.add_get("/review", to_todo)  # the old 待审页 address: now the 要你的 page (input/todo.py)
    app.router.add_get("/review/all", review_page)  # what is left of it: reports, screenshot folders, deployed demos
    app.router.add_post("/api/review/dismiss", dismiss_trial)
    app.router.add_get("/review/a/{path:.+}", audit_file)
    app.router.add_get("/review/s/{key}/{path:.+}", sandboxed_file)
    app.router.add_get("/review/w/{key}/{path:.+}", wt_file)
    app.router.add_get("/review/g/{path:.+}", gallery_page)
