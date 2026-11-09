# For Claude: where this repository must live

The code imports itself by its folder name, so it only runs when the layout is `<Root>\workspace\incore`: this folder is named `incore` and its parent is named `workspace`.

**First, check the layout.** If the current folder is named `incore` and its parent is named `workspace`, say nothing about layout and carry on with what the owner asked (a new owner: follow `docs/start-session.md`).

**Otherwise** the owner has cloned this repository into the wrong place. Tell them in two or three plain sentences: the code needs to sit at `<some folder>\workspace\incore`; offer to build that next to this clone: a new folder `premier-home` beside this one, `premier-home\workspace`, and a second clone of this repository in `premier-home\workspace\incore` (from the local path of this clone, then point its `origin` at the same address as this clone's `origin`). The original clone is not deleted, moved or changed. Wait for their yes before you create anything.

After the yes, build it, then carry on from the new place: `premier-home\workspace` is the workspace and `premier-home\workspace\incore` the code. With a separate yes for each, do steps 2 and 3 of `docs/start.md` for them (virtual environment in `premier-home\venv`, `instance.json`; `user_home` is a new empty folder `premier-home\orghome`), then follow `docs/start-session.md` and write `start-progress.md` in `premier-home\workspace`. Show every file before you write it. Never ask for a password, token or key; never log in, send a message, register a scheduled task or spend money.
