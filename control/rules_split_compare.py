"""Premier public edition block 1: per role, sha256 of the assembled prompt before (git BASE_REF, the monolithic files) and after
(template + private layer through control/layered.py). Every row must be identical; exit code 1 otherwise.

Assembled prompt of a role = tick-rules.md text + "\\n\\n" + that role's description file text (the two things the role is told to
follow; tick.py appends the rules itself and the roles read their description file). Run: python control/rules_split_compare.py"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG = {
    "BASE_REF": "master",  # the commit/branch with the unsplit files (before this change)
    "ROLES": ["coordinator", "work", "uvox", "life", "quant", "intelligence", "codex-secretary", "codex-sandbox-limits", "asr-execution"],
    "PRIVATE_DIR": None,  # None = instance_config private_layer_dir
}

sys.path.insert(0, str(Path(__file__).resolve().parent))
import instance_config as ic  # noqa: E402
import layered  # noqa: E402

REPO = layered.CONTROL.parent


def _git(*args) -> bytes:
    for cmd in ([shutil.which("git") or ic.get("git_exe"), "-c", "safe.directory=*"], ["wsl", "-e", "git"]):
        where = str(REPO) if cmd[0] != "wsl" else ic.wsl(str(REPO))
        done = subprocess.run(cmd + ["-C", where, *args], capture_output=True)
        if done.returncode == 0:
            return done.stdout
    raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.decode(errors='replace')}")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def before(role: str) -> str:
    blob = lambda name: _git("show", f"{CONFIG['BASE_REF']}:control/roles/{name}.md").decode("utf-8")
    return blob("tick-rules") + "\n\n" + blob(role)


def after(role: str, directory=None) -> str:
    return layered.role_text("tick-rules", directory) + "\n\n" + layered.role_text(role, directory)


def compare(directory=None) -> list[tuple[str, str, str]]:
    return [(r, sha(before(r)), sha(after(r, directory))) for r in CONFIG["ROLES"]]


if __name__ == "__main__":
    d = Path(CONFIG["PRIVATE_DIR"]) if CONFIG["PRIVATE_DIR"] else None
    rows = compare(d)
    for role, b, a in rows:
        print(f"{'same' if a == b else 'DIFF'}  {role:22s} before {b}  after {a}")
    print(f"layer: {d or layered.private_dir()}  base: {CONFIG['BASE_REF']}")
    sys.exit(0 if all(b == a for _, b, a in rows) else 1)
