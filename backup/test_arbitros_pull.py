"""Offline tests of arbitros_pull: the server is a fake `_ssh`; the SQLite bytes are real. Run: python test_arbitros_pull.py"""
import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import arbitros_pull as ap

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


class FakeServer:
    """Stands in for ssh: `docker exec` returns sha256 + a real database, `find` the uploads listing, `cat` a file."""
    def __init__(self, temp: Path):
        self.db = temp / "server.sqlite3"
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("CREATE TABLE subscribers (email TEXT)")
            db.execute("CREATE TABLE articles (title TEXT)")
            db.commit()
        self.files = {}  # rel -> (bytes, mtime)
        self.workspace = {}  # the same for workspace-files
        self.down = None
        self.cats = []

    def add_subscriber(self, email):
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("INSERT INTO subscribers VALUES (?)", (email,))
            db.commit()

    def __call__(self, command, *, stdin=None):
        if self.down:
            raise ap.PullError(self.down)
        if command.startswith("docker exec"):
            assert stdin and b"backup(" in stdin  # the SQLite backup API runs in the container
            data = self.db.read_bytes()
            return hashlib.sha256(data).hexdigest().encode() + b"\n" + data
        if " find " in command:
            files = self.workspace if "/workspace-files" in command else self.files
            return b"".join(f"{r}\0{len(d)}\0{m}\0".encode() for r, (d, m) in files.items())  # a missing folder lists nothing
        if command.startswith("cat"):
            if "/workspace-files/" in command:
                rel = command.split("/workspace-files/", 1)[1].rstrip("'")
                self.cats.append("ws:" + rel)
                return self.workspace[rel][0]
            rel = command.split("/uploads/", 1)[1].rstrip("'")
            self.cats.append(rel)
            return self.files[rel][0]
        raise AssertionError(command)


def run(server, root, minutes):
    with patch.object(ap, "_ssh", server):
        return ap.step(root, T0 + timedelta(minutes=minutes))


def test_pull_keeps_copies_and_mirrors_uploads_incrementally():
    with tempfile.TemporaryDirectory(prefix="arbitros-pull-") as temp:
        root, server = Path(temp) / "root", FakeServer(Path(temp))
        root.mkdir()
        server.files = {"a.png": (b"image-a", 1000.0), "sub/b.pdf": (b"pdf-b", 2000.0)}
        out = run(server, root, 0)
        assert "arbitros_error" not in out and out["arbitros"]["database"] == "saved", out
        assert out["arbitros"]["files"] == 2 and out["arbitros"]["pulled"] == 2
        folder = root / "data/arbitros-backup"
        assert (folder / "uploads/sub/b.pdf").read_bytes() == b"pdf-b"
        copy = folder / "db" / out["arbitros"]["last_copy"]
        with closing(sqlite3.connect(copy)) as db:
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        # not due yet: no ssh at all
        with patch.object(ap, "_ssh", side_effect=AssertionError("must not run")):
            assert "arbitros_error" not in ap.step(root, T0 + timedelta(minutes=30))
        # due, nothing changed: no new copy, no upload re-read
        server.cats.clear()
        assert run(server, root, 61)["arbitros"]["database"] == "unchanged" and server.cats == []
        assert len(list((folder / "db").glob("arbitros-*.sqlite3"))) == 1
        # a new upload and a changed one: only those two are pulled; a new subscriber makes a new copy
        server.files["c.png"] = (b"image-c", 3000.0)
        server.files["a.png"] = (b"image-a-edited", 4000.0)
        server.add_subscriber("x@example.com")
        out = run(server, root, 122)
        assert out["arbitros"]["pulled"] == 2 and sorted(server.cats) == ["a.png", "c.png"], server.cats
        assert out["arbitros"]["database"] == "saved"
        assert (folder / "uploads/a.png").read_bytes() == b"image-a-edited"
        # removed on the server: kept locally
        del server.files["c.png"]
        run(server, root, 183)
        assert (folder / "uploads/c.png").read_bytes() == b"image-c"
        # unchanged database still gets a copy once a day
        out = run(server, root, 122 + 24 * 60)
        assert out["arbitros"]["database"] == "saved"


def test_workspace_files_are_mirrored_incrementally_and_never_deleted_locally():
    with tempfile.TemporaryDirectory(prefix="arbitros-ws-") as temp:
        root, server = Path(temp) / "root", FakeServer(Path(temp))
        root.mkdir()
        mirror = root / "data/arbitros-backup/workspace-files"
        # the folder does not exist on the server yet: no error, nothing written
        out = run(server, root, 0)
        assert "arbitros_error" not in out and out["arbitros"]["workspace_files"] == 0, out
        server.workspace = {"r1/reply.pdf": (b"pdf-1", 100.0), "n.txt": (b"note", 200.0)}
        out = run(server, root, 61)
        assert out["arbitros"]["workspace_files"] == 2 and out["arbitros"]["workspace_pulled"] == 2, out
        assert (mirror / "r1/reply.pdf").read_bytes() == b"pdf-1" and not (root / "data/arbitros-backup/uploads/n.txt").exists()
        # nothing changed: no file is read again; one changed and one new: exactly those two
        server.cats.clear()
        assert run(server, root, 122)["arbitros"]["workspace_pulled"] == 0 and server.cats == []
        server.workspace["n.txt"] = (b"note-2", 300.0)
        server.workspace["new.png"] = (b"png", 300.0)
        out = run(server, root, 183)
        assert out["arbitros"]["workspace_pulled"] == 2 and sorted(server.cats) == ["ws:n.txt", "ws:new.png"], server.cats
        assert (mirror / "n.txt").read_bytes() == b"note-2"
        # removed on the server: kept locally
        del server.workspace["r1/reply.pdf"]
        out = run(server, root, 244)
        assert "arbitros_error" not in out and (mirror / "r1/reply.pdf").read_bytes() == b"pdf-1"
        # an unsafe name in uploads does not keep the workspace files from being pulled
        server.files = {"../x": (b"x", 1.0)}
        server.workspace["late.txt"] = (b"late", 400.0)
        out = run(server, root, 305)
        assert "uploads: 1 not pulled" in out["arbitros_error"] and (mirror / "late.txt").read_bytes() == b"late"


def test_only_the_newest_fourteen_copies_stay():
    with tempfile.TemporaryDirectory(prefix="arbitros-keep-") as temp:
        root, server = Path(temp) / "root", FakeServer(Path(temp))
        root.mkdir()
        for day in range(20):
            server.add_subscriber(f"{day}@example.com")
            run(server, root, day * 120)
        names = sorted(p.name for p in (root / "data/arbitros-backup/db").glob("arbitros-*.sqlite3"))
        assert len(names) == ap.KEEP == 14, names
        with closing(sqlite3.connect(root / "data/arbitros-backup/db" / names[-1])) as db:
            assert db.execute("SELECT count(*) FROM subscribers").fetchone()[0] == 20  # the newest one is the latest


def test_failures_show_as_arbitros_error_and_clear_on_success():
    with tempfile.TemporaryDirectory(prefix="arbitros-fail-") as temp:
        root, server = Path(temp) / "root", FakeServer(Path(temp))
        root.mkdir()
        server.down = "ssh demo01-server exit 255: Connection timed out"
        out = run(server, root, 0)
        assert "255" in out["arbitros_error"], out
        # retried sooner than the hourly cadence
        server.down = None
        out = run(server, root, ap.RETRY_S // 60 + 1)
        assert "arbitros_error" not in out and out["arbitros"]["database"] == "saved"
        # an upload that changes while it is read: reported, the rest still pulled, retried next time
        server.files = {"a.png": (b"abc", 1.0)}
        real = server.__call__
        with patch.object(ap, "_ssh", lambda c, **kw: b"ab" if c.startswith("cat") else real(c, **kw)):
            out = ap.step(root, T0 + timedelta(minutes=90))
        assert "size changed" in out["arbitros_error"], out
        out = run(server, root, 90 + ap.RETRY_S // 60 + 1)
        assert "arbitros_error" not in out and (root / "data/arbitros-backup/uploads/a.png").read_bytes() == b"abc"
        # no success for more than STALE_S is an error even while attempts are not due
        state = json.loads((root / "data/arbitros-backup/state.json").read_text(encoding="utf-8"))
        late = datetime.fromisoformat(state["ok_at"]) + timedelta(seconds=ap.STALE_S + 1)
        state["tried_at"] = late.isoformat()
        (root / "data/arbitros-backup/state.json").write_text(json.dumps(state), encoding="utf-8")
        assert "no successful Arbitros pull" in ap.step(root, late)["arbitros_error"]


def test_bad_streams_are_refused():
    with tempfile.TemporaryDirectory(prefix="arbitros-bad-") as temp:
        root, server = Path(temp) / "root", FakeServer(Path(temp))
        root.mkdir()
        real = server.__call__
        for label, payload, expect in (("truncated", lambda d: hashlib.sha256(d).hexdigest().encode() + b"\n" + d[:100], "sha256"),
                                       ("empty", lambda d: b"", "sha256"),
                                       ("not sqlite", lambda d: hashlib.sha256(b"junk" * 50).hexdigest().encode() + b"\n" + b"junk" * 50, "")):
            with patch.object(ap, "_ssh", lambda c, p=payload, **kw: p(server.db.read_bytes()) if c.startswith("docker") else real(c, **kw)):
                out = ap.step(root, T0 + timedelta(hours=10 * (1 + len(label))), force=True)
            assert out["arbitros_error"], label
            assert expect in out["arbitros_error"], (label, out)
        assert not list((root / "data/arbitros-backup/db").glob("arbitros-*")), "nothing bad may be kept"


def test_unsafe_upload_names_are_not_written():
    with tempfile.TemporaryDirectory(prefix="arbitros-names-") as temp:
        root, server = Path(temp) / "root", FakeServer(Path(temp))
        root.mkdir()
        server.files = {"../escape.txt": (b"x", 1.0), "ok.txt": (b"fine", 1.0)}
        out = run(server, root, 0)
        assert "unsafe name" in out["arbitros_error"]
        assert not (root / "data/escape.txt").exists() and (root / "data/arbitros-backup/uploads/ok.txt").read_bytes() == b"fine"


if __name__ == "__main__":
    test_pull_keeps_copies_and_mirrors_uploads_incrementally()
    test_workspace_files_are_mirrored_incrementally_and_never_deleted_locally()
    test_only_the_newest_fourteen_copies_stay()
    test_failures_show_as_arbitros_error_and_clear_on_success()
    test_bad_streams_are_refused()
    test_unsafe_upload_names_are_not_written()
    print("arbitros_pull checks passed.")
