"""This machine's private instance values (Premier gap G6): user folders, tool paths, WSL user, owner identity.

Code reads them from here instead of carrying C:\\orghome and friends. The values live in `instance/instance.json`
(not in git; `instance/example/instance.json` is the committed template). Without that file every default below is
what this machine has today, so behavior does not change. A key in the file overrides its default; a key that is
not known is an error (a typo must not silently fall back).

INCORE_INSTANCE=<path to a json file> picks another file (a sandbox, or a restore on a new user/path).
Only `workspace` (and optionally `user_home`) needs to be set to move the whole tree: wt_root, claude_projects,
sessions_dir and the WSL mount path follow from them unless set explicitly.
Standard library only. Nothing here touches quota, pacing, dispatch caps or budget gates."""
import json
import ntpath
import os
import re
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent  # the incore folder this code was loaded from
FILE = Path(os.environ.get("INCORE_INSTANCE") or CODE_ROOT / "instance" / "instance.json")

DEFAULTS = {  # = this machine today (Windows user 0000); the one place these literals live
    "user_home": r"C:\orghome",
    "workspace": r"C:\orghome\orgcore_workspace",
    "wt_root": None,  # default <user_home>\wt
    "claude_exe": None,  # default <user_home>\.local\bin\claude.exe
    "python_exe": None,  # standalone Python 3.12, default <user_home>\AppData\Local\Programs\Python\Python312\python.exe
    "pythonw_exe": None,  # default: pythonw.exe next to python_exe
    "git_exe": None,  # fallback when git is not on PATH, default <user_home>\.cache\codex-runtimes\...\git.exe
    "claude_projects": None,  # default <user_home>\.claude\projects
    "wsl_user": "wsluser",
    "wsl_home": None,  # default /home/<wsl_user>
    "models_dir": None,  # local model files, default <user_home>\models
    "tools_dir": None,  # local binaries (llama.cpp ...), default <user_home>\tools
    "private_layer_dir": None,  # private rules/role files (control/layered.py), default <workspace>\incore\data\config-sync\private-layer
    "coordinator_label": "Coordinator",  # dashboard hero line
    "coordinator_note": "",  # dashboard hero note (empty = no note)
    "role_names": {"coordinator": "Coordinator", "work": "Deputy A", "uvox": "Deputy B", "life": "Deputy C", "quant": "Deputy D", "intelligence": "Intelligence"},  # dashboard role names
    "lines": {"coordinator": "Organization"},  # backlog `## ` title of each line, by role key; a role left out has no line (no card, no dispatch)
    "projects": {},  # None = this machine's project list in control/tick.py; {key: {"cwd": folder, "prefixes": [...]}} = the instance's own (cwd relative to workspace or absolute)
    "language": "en",  # language of the fixed text on the dashboard, input station and to-do pages: zh or en (input/i18n.py)
    "secretary_card": False,  # show the backup-secretary card (Codex command) on the dashboard; the public export turns it off
    "secretary_model": "gpt-6.1-sol",  # model named in that card's command; the reply language follows `language`
    "owner_name": "Demo Owner",
    "owner_aliases": ["demo-owner", "Demo Owner", "Demo01", "Org Core"],
    "owner_email": "owner@example.invalid",  # recipient of the owner mails
    "sender_email": "sender@example.invalid",
}


ROLE_KEYS = ("coordinator", "work", "uvox", "quant", "intelligence", "life")  # the fixed deputy slots; a line title and a role name hang on them


def _check_lines_projects(v: dict) -> None:
    lines, projects = v["lines"], v["projects"]
    if not isinstance(lines, dict) or not lines or any(r not in ROLE_KEYS or not isinstance(s, str) or not s.strip() for r, s in lines.items()):
        raise ValueError(f"instance `lines` must map role keys {ROLE_KEYS} to non-empty titles, got {lines!r}")
    if len({s.strip() for s in lines.values()}) != len(lines):
        raise ValueError("instance `lines`: two roles share one title")
    if projects is not None and (not isinstance(projects, dict) or any(
            not isinstance(s, dict) or not isinstance(s.get("cwd"), str) or not s["cwd"]
            or not isinstance(s.get("prefixes"), list) or not s["prefixes"] or not all(isinstance(x, str) and x for x in s["prefixes"])
            for s in projects.values())):
        raise ValueError("instance `projects` must be null or {key: {\"cwd\": folder, \"prefixes\": [text, ...]}}")


def _derive(v: dict) -> dict:
    _check_lines_projects(v)
    h = v["user_home"]
    v["wt_root"] = v["wt_root"] or h + r"\wt"
    v["claude_exe"] = v["claude_exe"] or h + r"\.local\bin\claude.exe"
    v["python_exe"] = v["python_exe"] or h + r"\AppData\Local\Programs\Python\Python312\python.exe"
    v["pythonw_exe"] = v["pythonw_exe"] or ntpath.join(ntpath.dirname(v["python_exe"]), "pythonw.exe")  # ntpath: the value is a Windows path even when read under WSL
    v["git_exe"] = v["git_exe"] or h + r"\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe"
    v["claude_projects"] = v["claude_projects"] or h + r"\.claude\projects"
    v["wsl_home"] = v["wsl_home"] or "/home/" + v["wsl_user"]
    v["models_dir"] = v["models_dir"] or h + r"\models"
    v["tools_dir"] = v["tools_dir"] or h + r"\tools"
    v["private_layer_dir"] = v["private_layer_dir"] or v["workspace"] + r"\incore\data\config-sync\private-layer"
    # derived, not settable: the folder Claude files this workspace's sessions under, and the WSL view of the paths
    v["sessions_dir"] = v["claude_projects"] + "\\" + re.sub(r"[^A-Za-z0-9]", "-", v["workspace"])
    return v


def load(path: Path | None = None) -> dict:
    """The defaults overlaid with the instance file (missing file = defaults; unknown key or unreadable file = error)."""
    values = dict(DEFAULTS)
    p = Path(path) if path else FILE
    if p.exists():
        try:
            given = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"instance file {p} is not valid JSON: {exc}") from exc
        unknown = sorted(k for k in given if k not in DEFAULTS and not k.startswith("_"))
        if unknown:
            raise ValueError(f"instance file {p}: unknown keys {unknown}")
        values.update({k: v for k, v in given.items() if k in DEFAULTS})
    return _derive(values)


def wsl(path: str) -> str:
    """The /mnt/<drive>/... form of a Windows path."""
    p = str(path).replace("\\", "/")
    return f"/mnt/{p[0].lower()}{p[2:]}" if re.match(r"^[A-Za-z]:", p) else p


V = load()  # read once per process, as the constants that use it are


def get(key: str) -> str:
    return V[key]
