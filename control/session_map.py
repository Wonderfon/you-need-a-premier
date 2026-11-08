"""Session map and export for resident roles and project executors (Premier gap G3).

  map     deputies.json (+ runs.jsonl for Codex ids) -> table: session id, owner, engine, session file, size, mtime,
          cwd, CLI version. Metadata only: the first lines of a file are parsed and just `cwd` / `cli_version` /
          `version` are kept; no message text is read, printed or stored.
  export  copy the session files (and the Codex index lines of those ids) into a private directory with a manifest
          (sha256). Credentials (`.credentials.json`, `auth.json`, anything else) are never listed, so never copied.
  restore put an export back under a NEW cwd: the Claude project folder name is rebuilt from the new cwd; Codex
          files keep their date folders. Never overwrites an existing file.

Sources: Windows ~/.claude/projects (Claude Code), WSL ~/.codex/sessions (Codex, read through \\\\wsl.localhost).
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEPUTIES = HERE / "data" / "deputies.json"
RUNS = HERE / "data" / "runs.jsonl"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_HOME = Path(r"\\wsl.localhost\Ubuntu\home\wsluser\.codex")
HEAD_LINES = 3000  # stops at the first line that has both keys; cwd/version first appear after a few queue/attachment lines


def enc(cwd: str) -> str:
    """Claude Code's project folder name for a cwd: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def deputy_sessions(path=DEPUTIES):
    """(label, session_id) for every session pointer in deputies.json, in file order, duplicates kept once."""
    out, seen = [], set()

    def walk(o, p):
        if isinstance(o, dict):
            sid = o.get("session_id")
            if isinstance(sid, str) and sid and sid not in seen:
                seen.add(sid)
                out.append((p or "/", sid))
            for k, v in o.items():
                if k not in ("last", "current") and isinstance(v, (dict, list)):
                    walk(v, f"{p}/{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{p}[{i}]")

    walk(json.loads(Path(path).read_text(encoding="utf-8")), "")
    return out


def codex_ids(path=RUNS):
    """session_ids that runs.jsonl recorded for a GPT model (the Codex executors; deputies.json does not hold them)."""
    ids = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if '"gpt' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if str(r.get("model", "")).startswith("gpt") and r.get("session_id"):
                ids[r["session_id"]] = f'{r.get("role")}/{r.get("kind")}'
    return ids


def head_meta(file: Path) -> dict:
    """cwd and CLI version from the first lines. Only these keys are kept; message text is not retained."""
    meta = {}
    with open(file, encoding="utf-8", errors="replace") as f:
        for _, line in zip(range(HEAD_LINES), f):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            p = o.get("payload") if isinstance(o.get("payload"), dict) else {}
            for k, v in (("cwd", o.get("cwd") or p.get("cwd")), ("cli_version", o.get("version") or p.get("cli_version"))):
                if v and k not in meta:
                    meta[k] = v
            if meta.get("cwd") and meta.get("cli_version"):
                break
    return {k: v for k, v in meta.items() if v}


def find_claude(sid, root=CLAUDE_PROJECTS):
    return next((p for p in Path(root).glob(f"*/{sid}.jsonl")), None)


def find_codex(sid, home=CODEX_HOME):
    return next((p for p in (Path(home) / "sessions").rglob(f"rollout-*-{sid}.jsonl")), None)


def build_map(claude_root=CLAUDE_PROJECTS, codex_home=CODEX_HOME, deputies=DEPUTIES, runs=RUNS):
    rows = []
    cx = codex_ids(runs)
    for label, sid in deputy_sessions(deputies):
        rows.append(("claude", label, sid))
    in_deputies = {r[2] for r in rows}
    for sid, label in sorted(cx.items(), key=lambda kv: kv[1]):
        if sid not in in_deputies:
            rows.append(("codex", f"codex:{label}", sid))
    out = []
    for engine, label, sid in rows:
        f = find_claude(sid, claude_root) if engine == "claude" else find_codex(sid, codex_home)
        row = {"engine": engine, "owner": label, "session_id": sid, "file": str(f) if f else None}
        if f:
            st = f.stat()
            side = f.with_suffix("")
            row.update(size=st.st_size, mtime=datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                       sidecar_dir=str(side) if side.is_dir() else None, **head_meta(f))
        else:
            row["missing"] = True
        out.append(row)
    return out


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def export(rows, dest, only=None, codex_home=CODEX_HOME):
    """Copy the files of `rows` (optionally only these session ids) into the new directory `dest`."""
    dest = Path(dest)
    if dest.exists():
        raise SystemExit(f"{dest} exists; export goes to a new directory")
    man = []
    index = {}
    idx = Path(codex_home) / "session_index.jsonl"
    if idx.exists():
        for line in idx.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                index[json.loads(line)["id"]] = line
            except (ValueError, KeyError):
                pass
    index_lines = []
    for r in rows:
        if not r.get("file") or (only and r["session_id"] not in only):
            continue
        src = Path(r["file"])
        rel = Path(r["engine"]) / (src.parent.name if r["engine"] == "claude" else "") / src.name
        if r["engine"] == "codex":
            rel = Path("codex") / src.relative_to(Path(codex_home) / "sessions")
        tgt = dest / rel
        tgt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, tgt)
        side = r.get("sidecar_dir")
        if side:
            shutil.copytree(side, tgt.with_suffix(""))
        man.append({**{k: r.get(k) for k in ("engine", "owner", "session_id", "cwd", "cli_version", "size")},
                    "path": rel.as_posix(), "sha256": sha256(tgt)})
        if r["engine"] == "codex" and r["session_id"] in index:
            index_lines.append(index[r["session_id"]])
    if index_lines:
        (dest / "codex" / "session_index.jsonl").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    (dest / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    return man


def restore(src, new_cwd, only=None, claude_root=CLAUDE_PROJECTS, codex_home=CODEX_HOME):
    """Put exported sessions back. Claude files go under the folder name of `new_cwd`; Codex files keep their date folders."""
    src = Path(src)
    man = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    done = []
    for m in man:
        if only and m["session_id"] not in only:
            continue
        f = src / m["path"]
        if sha256(f) != m["sha256"]:
            raise SystemExit(f"hash mismatch: {m['path']}")
        if m["engine"] == "claude":
            tgt = Path(claude_root) / enc(new_cwd) / f.name
        else:
            tgt = Path(codex_home) / "sessions" / Path(m["path"]).relative_to("codex")
        if tgt.exists():
            raise SystemExit(f"{tgt} exists; not overwriting")
        tgt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, tgt)
        side = f.with_suffix("")
        if side.is_dir():
            shutil.copytree(side, tgt.with_suffix(""))
        done.append(str(tgt))
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("map")
    m.add_argument("--out")
    m.add_argument("--data", default=str(HERE / "data"), help="control/data directory holding deputies.json and runs.jsonl")
    e = sub.add_parser("export")
    e.add_argument("dest")
    e.add_argument("--data", default=str(HERE / "data"))
    e.add_argument("--id", action="append")
    r = sub.add_parser("restore")
    r.add_argument("src")
    r.add_argument("--cwd", required=True)
    r.add_argument("--codex-home", default=str(CODEX_HOME), help="where Codex files go (an isolated CODEX_HOME for a trial)")
    r.add_argument("--id", action="append")
    a = ap.parse_args(argv)
    if a.cmd == "restore":
        print("\n".join(restore(a.src, a.cwd, set(a.id or []), codex_home=a.codex_home)))
        return 0
    d = Path(a.data)
    rows = build_map(deputies=d / "deputies.json", runs=d / "runs.jsonl")
    if a.cmd == "map":
        text = json.dumps(rows, ensure_ascii=False, indent=1)
        Path(a.out).write_text(text, encoding="utf-8") if a.out else print(text)
        return 0
    print(len(export(rows, a.dest, set(a.id or []))), "sessions exported")
    return 0


if __name__ == "__main__":
    sys.exit(main())
