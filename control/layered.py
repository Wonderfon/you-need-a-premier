"""Public template + private layer loading (Premier public edition, block 1).

`control/roles/<name>.md` in git is the public template. The private layer (instance_config `private_layer_dir`, by default
`incore/data/config-sync/private-layer/`: git-ignored, already inside the backup's `data/config-sync` scope) may hold a
same-named file; when it does, that whole file is used, otherwise the template. No merging, no line-level patching.
Standard library only. Nothing here touches quota, pacing, dispatch caps or budget gates."""
import os
from pathlib import Path

import instance_config

CONTROL = Path(__file__).resolve().parent


def private_dir() -> Path:
    raw = instance_config.get("private_layer_dir")
    return Path(raw if os.name == "nt" else instance_config.wsl(raw))  # WSL sees the Windows folder under /mnt/<drive>


def private_path(template: Path, directory: Path | None = None) -> Path | None:
    """Where the private copy of a control/ file would be (control/roles/x.md -> <layer>/roles/x.md); None when the file is not under control/."""
    p = Path(template)
    if not (p.parent.name == "roles" and p.parent.parent.name == "control"):  # judged by name: tick.CONTROL may be the main folder while this file is a worktree's
        return None
    return (directory or private_dir()) / "roles" / p.name


def resolve(template: Path, directory: Path | None = None) -> Path:
    p = private_path(template, directory)
    return p if p is not None and p.is_file() else Path(template)


def read_text(template: Path, directory: Path | None = None) -> str:
    return resolve(template, directory).read_text(encoding="utf-8")


def role_text(name: str, directory: Path | None = None) -> str:
    return read_text(CONTROL / "roles" / f"{name}.md", directory)
