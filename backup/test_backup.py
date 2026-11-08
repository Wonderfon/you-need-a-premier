"""Offline checks: live WAL, immutable versions, exclusions, missing Drive target."""
import json
import os
from contextlib import closing
from contextlib import redirect_stdout
import io
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch
import zipfile

import backup
from backup import ANCHOR, main, restore_zip_chain, sha256, snapshot_once, validate_target, verify_zip

backup.ARBITROS_STEP = None  # these tests must never reach the server (test_arbitros_step_is_part_of_the_real_root_snapshot sets its own)

EASTERN_SUMMER = timezone(timedelta(hours=-4))


def at(day, hour):
    return datetime(2026, 9, day, hour, tzinfo=EASTERN_SUMMER)


def test_backup():
    with tempfile.TemporaryDirectory(prefix="incore-backup-test-") as temp:
        base = Path(temp)
        root = base / "incore"
        records = root / "records"
        records.mkdir(parents=True)
        original = records / "original.txt"
        original.write_text("原始指令 / original input", encoding="utf-8")
        # A setup conversation mentioning a token is data, not the credential.
        (records / "2026-token-saved.json").write_text('{"reply":"已保存"}', encoding="utf-8")
        for relative in (".secrets/bot-token", "input/config.json", "discord/config.json",
                         ".runtime/models/model.bin", "logs/debug.log", ".git/config"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("DO_NOT_BACK_UP_PLACEHOLDER", encoding="utf-8")
        session = root / "input/data/sessions/test-session"
        (session / "chunks").mkdir(parents=True)
        (session / "original.wav").write_bytes(b"immutable-audio-original")
        (session / "chunks/00000000.part").write_bytes(b"immutable-audio-original")
        db_path = root / "input/data/input.sqlite3"
        writer = sqlite3.connect(db_path)
        try:
            assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE revisions(id INTEGER PRIMARY KEY, text TEXT)")
            writer.execute("INSERT INTO revisions(text) VALUES('committed in WAL')")
            writer.commit()
            assert db_path.with_name(db_path.name + "-wal").stat().st_size > 0
            first = snapshot_once(root=root)
            manifest = json.loads(Path(first["manifest_path"]).read_text(encoding="utf-8"))
            paths = {entry["path"] for entry in manifest["files"]}
            assert paths == {"records/original.txt", "records\x2f2026-token-saved.json",
                             "input/data/input.sqlite3", "input/data/sessions/test-session/original.wav",
                             "input/data/sessions/test-session/chunks/00000000.part"}
            db_entry = next(entry for entry in manifest["files"] if entry["kind"] == "sqlite_snapshot")
            object_path = root / "data/backups/objects" / db_entry["sha256"][:2] / db_entry["sha256"]
            with closing(sqlite3.connect(object_path.as_uri() + "?immutable=1", uri=True)) as restored:
                assert restored.execute("SELECT text FROM revisions").fetchall() == [("committed in WAL",)]
            assert all(b"DO_NOT_BACK_UP_PLACEHOLDER" not in
                       (root / "data/backups/objects" / e["sha256"][:2] / e["sha256"]).read_bytes()
                       for e in manifest["files"])
            assert snapshot_once(root=root)["unchanged"] is True
            writer.execute("INSERT INTO revisions(text) VALUES('later correction')")
            writer.commit()
            second = snapshot_once(root=root)
            assert second["snapshot_id"] != first["snapshot_id"]
            assert Path(first["manifest_path"]).exists()
            # Restore the new SQLite object, proving later WAL commits survived.
            changed = json.loads(Path(second["manifest_path"]).read_text(encoding="utf-8"))
            db_entry2 = next(e for e in changed["files"] if e["kind"] == "sqlite_snapshot")
            db_object2 = root / "data/backups/objects" / db_entry2["sha256"][:2] / db_entry2["sha256"]
            with closing(sqlite3.connect(db_object2.as_uri() + "?immutable=1", uri=True)) as restored:
                assert restored.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 2
            missing = base / "disconnected-drive"
            failed_stage = snapshot_once(missing, root=root)
            assert failed_stage["stage"] == "local_snapshot_verified" and "staging_error" in failed_stage
            assert not missing.exists()
            drive = base / "verified-private-drive-folder"
            drive.mkdir()
            try:
                validate_target(drive, root)
            except ValueError:
                pass
            else:
                raise AssertionError("unverified target accepted")
            (root / "data/backups" / ANCHOR).write_bytes(b"known private archive")
            (drive / ANCHOR).write_bytes(b"known private archive")
            staged = snapshot_once(drive, root=root, archive_now=True, archive_time=at(28, 10))
            assert staged["stage"] == "staged_for_drive_sync"
            assert staged["cloud_received"] is False and staged["cloud_file_id"] is None
            package = Path(staged["package_path"])
            assert package.is_file() and staged["prior_package_name"] is None
            package_mtime = package.stat().st_mtime_ns
            duplicate = snapshot_once(drive, root=root, archive_now=True, archive_time=at(28, 10))
            assert duplicate["unchanged"] and duplicate["package_unchanged"]
            assert package.stat().st_mtime_ns == package_mtime
            restored_root = base / "restored"
            restore_zip_chain(package, restored_root)
            for entry in changed["files"]:
                assert sha256(restored_root / entry["path"]) == entry["sha256"]
            assert {p.name for p in drive.iterdir()} == {ANCHOR, package.name}
            (drive / ANCHOR).write_bytes(b"wrong account/folder")
            assert "staging_error" in snapshot_once(drive, root=root)
        finally:
            writer.close()
    print("Backup offline checks passed: WAL revisions, deduplication, exclusions, target rejection, staged receipts.")


def test_watch_lifecycle():
    with tempfile.TemporaryDirectory(prefix="incore-watch-test-") as temp, redirect_stdout(io.StringIO()):
        stop = Path(temp) / "STOP"
        args = ["--watch", "--stop-file", str(stop)]
        success = {"stage": "local_snapshot_verified", "unchanged": True}
        with patch("backup.snapshot_once", return_value=success) as snapshot, \
             patch("backup._wait_or_stop", side_effect=[False] * 120 + [True]):
            assert main(args) == 0
            assert snapshot.call_count == 121  # No implicit old 120-run cap.
        failure = {**success, "staging_error": "disconnected"}
        with patch("backup.snapshot_once", return_value=failure) as snapshot, \
             patch("backup._wait_or_stop", return_value=False):
            assert main(args) == 2
            assert snapshot.call_count == 3
        with patch("backup.snapshot_once", return_value=success) as snapshot, \
             patch("backup._wait_or_stop", return_value=False):
            assert main(args + ["--max-runs", "2"]) == 0
            assert snapshot.call_count == 2
        stop.write_text("user requested stop", encoding="utf-8")
        with patch("backup.snapshot_once") as snapshot:
            assert main(args) == 0
            snapshot.assert_not_called()
    print("Watch lifecycle checks passed: continuous success, three-failure exit, optional cap, stop flag.")


def test_chat_capture_integration():
    with tempfile.TemporaryDirectory(prefix="incore-capture-integration-") as temp:
        root = Path(temp).resolve() / "incore"
        records = root / "records"
        records.mkdir(parents=True)
        (records / "prior.txt").write_text("already saved", encoding="utf-8")
        with patch("backup._capture_current_chat", side_effect=AssertionError("real chat must not be read")) as capture:
            isolated = snapshot_once(root=root)
            capture.assert_not_called()
            assert "chat_capture" not in isolated

        def capture_new_message():
            (records / "new-chat.jsonl").write_text('{"original":"new input"}\n', encoding="utf-8")
            return {"status": "ok", "appended": 1}

        # Pretend only this temporary fixture is production; the exporter itself
        # is always mocked, so this test cannot read native chat history.
        with patch("backup.ROOT", root), patch("backup._capture_current_chat", side_effect=capture_new_message):
            success = snapshot_once(root=root)
        manifest = json.loads(Path(success["manifest_path"]).read_text(encoding="utf-8"))
        assert success["chat_capture"] == {"status": "ok", "appended": 1}
        assert "records/new-chat.jsonl" in {entry["path"] for entry in manifest["files"]}

        with patch("backup.ROOT", root), patch("backup._capture_current_chat", side_effect=OSError("private detail")):
            failed_capture = snapshot_once(root=root)
        assert failed_capture["stage"] == "local_snapshot_verified"
        assert failed_capture["chat_capture"] == {"status": "failed", "reason": "OSError"}
        assert Path(failed_capture["manifest_path"]).is_file()
        for capture_status in ("blocked", "partial"):
            with patch("backup.ROOT", root), patch("backup._capture_current_chat", return_value={"status": capture_status}):
                receipt = snapshot_once(root=root)
            assert receipt["chat_capture"] == {"status": "failed", "capture_status": capture_status}

        output = io.StringIO()
        with redirect_stdout(output), patch("backup.snapshot_once", return_value=failed_capture) as snapshot, \
                patch("backup._wait_or_stop", return_value=False):
            assert main(["--watch", "--stop-file", str(root / "STOP")]) == 2
        logs = [json.loads(line) for line in output.getvalue().splitlines()]
        assert snapshot.call_count == 3 and len(logs) == 3
        assert all(line["chat_capture"]["status"] == "failed" for line in logs)
    print("Chat capture integration passed: production-only capture, capture-before-snapshot, preserved backups on capture failure, visible failure logs.")


def test_daily_zip_audio_and_restore():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from incore.input.store import InputStore
    with tempfile.TemporaryDirectory(prefix="incore-daily-zip-") as temp:
        base = Path(temp).resolve()
        root, drive = base / "incore", base / "drive"
        drive.mkdir()
        (root / "data/backups").mkdir(parents=True)
        (root / "data/backups" / ANCHOR).write_bytes(b"verified folder anchor")
        (drive / ANCHOR).write_bytes(b"verified folder anchor")
        store = InputStore(root / "input/data")
        session = store.create_audio_session("audio_capture", "paired audio", "audio/webm")
        sid = session["id"]
        chunks = [b"original segment one", b"original segment two"]
        for i, chunk in enumerate(chunks):
            store.put_chunk(sid, i, chunk, hashlib.sha256(chunk).hexdigest())
        completed = store.finalize_audio(sid, len(chunks), sum(map(len, chunks)))
        asr = store.append_revision(sid, "asr", "raw recognition", source="asr", data={"model": "offline-fixture"})
        correction = store.append_revision(sid, "correction", "corrected words", asr["id"], data={"correction_type": "verbatim"})
        store.append_revision(sid, "instruction", "an instruction is not the transcript", correction["id"])
        # Fixture supports both the older live schema and staged verification migration.
        with closing(sqlite3.connect(store.db_path)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS verifications (id TEXT PRIMARY KEY, session_id TEXT, revision_id TEXT, revision_sha256 TEXT, original_sha256 TEXT, scope TEXT, verified_at TEXT, source TEXT)")
            db.execute("INSERT INTO verifications VALUES(?,?,?,?,?,?,?,?)", (
                "offline-human-check", sid, correction["id"], correction["sha256"], completed["original_sha256"],
                "full_recording", "2026-09-28T10:00:00Z", "human_review"))
            db.commit()
        unfinished = store.create_audio_session("audio_capture", "still recording", "audio/webm")
        store.put_chunk(unfinished["id"], 0, b"pending", hashlib.sha256(b"pending").hexdigest())
        before = snapshot_once(drive, root=root, archive_time=at(28, 19))
        assert before["daily_archive_waiting"] and "package_name" not in before
        assert len(list(drive.glob("*.zip"))) == 1  # Existing identity anchor only.
        baseline = snapshot_once(drive, root=root, archive_now=True, archive_time=at(28, 19))
        first = Path(baseline["package_path"])
        first_manifest = verify_zip(first)
        assert first_manifest["previous_package"] is None
        assert set(first_manifest["included_paths"]) == {entry["path"] for entry in first_manifest["files"]}
        with zipfile.ZipFile(first) as archive:
            names = set(archive.namelist())
            assert f"audio/{sid}.webm" in names and f"audio/{sid}.txt" in names and f"audio/{sid}.json" in names
            assert not any(sid in name and "chunks" in name for name in names)
            assert any(unfinished["id"] in name and "chunks" in name for name in names)
            assert archive.read(f"audio/{sid}.txt").decode() == "corrected words"
            details = json.loads(archive.read(f"audio/{sid}.json"))
            assert len(details["revisions"]) == 3
            assert details["revisions"][1]["parent_id"] == asr["id"]
            assert details["original"]["sha256"] == completed["original_sha256"]
            check = details["human_verifications"][0]
            assert check["revision_id"] == correction["id"] and check["revision_sha256"] == correction["sha256"]
            assert check["original_sha256"] == completed["original_sha256"] and check["scope"] == "full_recording"
            assert "待转写" in archive.read(f"audio/{unfinished['id']}.txt").decode()
        # Local CAS still includes the completed session's original chunks.
        local_manifest = json.loads(Path(baseline["manifest_path"]).read_text())
        assert any(sid in e["path"] and "/chunks/" in e["path"] for e in local_manifest["files"])
        newer = store.append_revision(sid, "correction", "new phrasing", correction["id"], data={"correction_type": "rewrite"})
        waiting = snapshot_once(drive, root=root, archive_time=at(28, 19))
        assert waiting["daily_archive_waiting"] and "package_sha256" not in waiting
        day_one = snapshot_once(drive, root=root, archive_time=at(28, 20))
        assert day_one["prior_package_name"] == first.name
        second_manifest = verify_zip(Path(day_one["package_path"]))
        with zipfile.ZipFile(day_one["package_path"]) as archive:
            assert f"audio/{sid}.webm" not in archive.namelist()
            assert archive.read(f"audio/{sid}.txt").decode() == "new phrasing"
            assert json.loads(archive.read(f"audio/{sid}.json"))["revisions"][-1]["parent_id"] == correction["id"]
        count = len(list(drive.glob("*.zip")))
        latest = store.append_revision(sid, "correction", "late same-day revision", newer["id"])
        assert snapshot_once(drive, root=root, archive_time=at(28, 21))["daily_archive_waiting"]
        assert len(list(drive.glob("*.zip"))) == count
        # Missed next evening: one catch-up package on the next startup.
        catchup = snapshot_once(drive, root=root, archive_time=at(30, 9))
        assert catchup["successful_archive_cutoff_date"] == "2026-09-29"
        assert len(list(drive.glob("*.zip"))) == count + 1
        no_change = snapshot_once(drive, root=root, archive_time=at(30, 20))
        assert no_change["package_unchanged"]
        assert len(list(drive.glob("*.zip"))) == count + 1
        already_staged = snapshot_once(drive, root=root, archive_time=at(30, 21))
        assert not already_staged["daily_archive_waiting"] and already_staged["package_unchanged"]
        assert already_staged["package_sha256"] == catchup["package_sha256"]
        restored = base / "restored-new-root"
        result = restore_zip_chain(Path(catchup["package_path"]), restored)
        assert result["packages"] == 3
        audio = restored / "input/data/sessions" / sid / "original.webm"
        assert audio.read_bytes() == b"".join(chunks)
        assert not (audio.parent / "chunks").exists()
        assert (audio.parent / "transcript.txt").read_text() == "late same-day revision"
        details = json.loads((audio.parent / "transcript.json").read_text())
        assert details["revisions"][-1]["id"] == latest["id"]
        with closing(sqlite3.connect(restored / "input/data/input.sqlite3")) as db:
            assert db.execute("SELECT COUNT(*) FROM revisions WHERE session_id=?", (sid,)).fetchone()[0] == 5
            assert db.execute("SELECT revision_id FROM verifications WHERE session_id=?", (sid,)).fetchone()[0] == correction["id"]
            assert db.execute("SELECT parent_id FROM revisions WHERE id=?", (latest["id"],)).fetchone()[0] == newer["id"]
        # A failed publish/readback cannot consume the day's cutoff or advance.
        old_state = next((root / "data/backups").glob("zip-chain-*.json")).read_bytes()
        store.append_revision(sid, "correction", "after successful cutoff", latest["id"])
        real_verify = verify_zip
        def reject_new_remote(path, expected_sha256=None):
            if Path(path).parent == drive and Path(path).name not in {first.name, day_one["package_name"], catchup["package_name"]}:
                raise ValueError("simulated DriveFS verification failure")
            return real_verify(path, expected_sha256)
        with patch("backup.verify_zip", side_effect=reject_new_remote):
            failure = snapshot_once(drive, root=root, archive_now=True, archive_time=at(30, 21))
        assert "staging_error" in failure
        assert next((root / "data/backups").glob("zip-chain-*.json")).read_bytes() == old_state
        recovered = snapshot_once(drive, root=root, archive_now=True, archive_time=at(30, 21))
        assert "staging_error" not in recovered
        # Missing dependencies and hostile paths are rejected before restoration.
        missing_chain = base / "missing-chain"
        missing_chain.mkdir()
        import shutil
        shutil.copyfile(Path(recovered["package_path"]), missing_chain / recovered["package_name"])
        try:
            restore_zip_chain(missing_chain / recovered["package_name"], base / "must-not-restore")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("missing dependency was accepted")
        assert not (base / "must-not-restore").exists()
        hostile = base / "hostile.zip"
        with zipfile.ZipFile(hostile, "w") as archive:
            archive.writestr("../escape.txt", "must not escape")
        try:
            restore_zip_chain(hostile, base / "hostile-restore")
        except ValueError:
            pass
        else:
            raise AssertionError("ZIP traversal was accepted")
        assert not (base / "escape.txt").exists()
    print("Daily ZIP checks passed: baseline, flat pairing, complete revisions, pending chunks, daily cutoff/catch-up/dedupe, failure retry, full restore and traversal rejection.")


def test_empty_day_creates_no_zip():
    with tempfile.TemporaryDirectory(prefix="incore-empty-day-") as temp:
        root, drive = Path(temp) / "incore", Path(temp) / "drive"
        (root / "data/backups").mkdir(parents=True)
        drive.mkdir()
        (root / "data/backups" / ANCHOR).write_bytes(b"identity")
        (drive / ANCHOR).write_bytes(b"identity")
        result = snapshot_once(drive, root=root, archive_time=at(28, 20))
        assert result["daily_archive_empty"] and not list(drive.glob("2026-*.zip"))
        assert snapshot_once(drive, root=root, archive_time=at(28, 21))["daily_archive_waiting"]
    print("Empty daily snapshot creates no package.")


def test_org_state_zip_bundle_exclusions_and_restore():
    """Owner 10-06 A: projects/*.md, audits/, config-sync, a memory folder and a git bundle ride the daily ZIP."""
    import subprocess
    from backup import GIT
    secret = b"DO_NOT_BACK_UP_PLACEHOLDER"
    with tempfile.TemporaryDirectory(prefix="incore-org-state-") as temp:
        base = Path(temp).resolve()
        root, drive, memory = base / "incore", base / "drive", base / "memory"
        drive.mkdir()
        files = {  # kept
            "projects/backlog.md": "- [todo] 待办\n", "projects/lessons/uvox.md": "lesson", "audits/2026-10-06-a.md": "审计",
            "audits/shot/x.png": "png-bytes", "audits/keyboard.png": "a word that merely contains key",
            "data/config-sync/preferences.md": "prefs", "external/claude-memory/MEMORY.md": "- memory",
        }
        excluded = ["projects/tool.py", "projects/api-key.md", "audits/my_token.txt", "audits/.env", "audits/02_secrets/n.md",
                    "audits/cred.dpapi", "audits/run.log", "data/config-sync/secret-notes.md", "external/claude-memory/a_key.md"]
        sources = {}
        for relative, text in {**files, **dict.fromkeys(excluded, secret.decode())}.items():
            path = (memory / relative[len("external/claude-memory/"):]) if relative.startswith("external/") else root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            sources[relative] = path
        (root / "data/backups").mkdir(parents=True)
        (root / "data/backups" / ANCHOR).write_bytes(b"identity")
        (drive / ANCHOR).write_bytes(b"identity")
        git = lambda *args, cwd=root: subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
                                                     check=True, capture_output=True, text=True).stdout
        git("init", "-q"), git("add", "projects/backlog.md"), git("commit", "-q", "-m", "one")
        external = {"external/claude-memory": memory}

        def package(day):
            receipt = snapshot_once(drive, root=root, archive_time=at(day, 20), external=external)
            assert "staging_error" not in receipt, receipt
            return Path(receipt["package_path"])

        def restore_and_compare(latest, name):
            destination = base / name
            restore_zip_chain(latest, destination)
            for relative in files:
                assert sha256(destination / relative) == sha256(sources[relative]), relative
            return destination

        first = snapshot_once(drive, root=root, archive_now=True, archive_time=at(28, 19), external=external)
        baseline = Path(first["package_path"])
        manifest = verify_zip(baseline)
        assert {e["path"] for e in manifest["files"]} == {*files, "git/incore.bundle"}
        with zipfile.ZipFile(baseline) as archive:
            for member in archive.namelist():
                assert secret not in archive.read(member), member
                assert not any(word in member for word in ("api-key", "my_token", ".env", "02_secrets", ".dpapi", "tool.py", "run.log", "secret-notes", "a_key"))
        restored = restore_and_compare(baseline, "restored-1")
        assert "refs/heads" in git("bundle", "list-heads", str(restored / "git/incore.bundle"))
        bundle_sha = sha256(restored / "git/incore.bundle")
        # day 2: only backlog.md changes, no new commit -> the incremental ZIP carries it alone and reuses the bundle
        sources["projects/backlog.md"].write_text("- [done] 待办\n- [todo] 新的\n", encoding="utf-8")
        files["projects/backlog.md"] = "changed"
        second = package(28 + 0)
        second_manifest = verify_zip(second)
        assert second_manifest["included_paths"] == ["projects/backlog.md"], second_manifest["included_paths"]
        assert {e["path"]: e["sha256"] for e in second_manifest["files"]}["git/incore.bundle"] == bundle_sha
        restored = restore_and_compare(second, "restored-2")
        # day 3: a new commit -> a new bundle, which clones to the new head
        git("add", "projects/backlog.md"), git("commit", "-q", "-m", "two")
        third = package(29)
        third_manifest = verify_zip(third)
        assert third_manifest["included_paths"] == ["git/incore.bundle"], third_manifest["included_paths"]
        restored = restore_and_compare(third, "restored-3")
        git("clone", "-q", str(restored / "git/incore.bundle"), str(base / "clone"), cwd=base)
        assert git("rev-parse", "HEAD", cwd=base / "clone") == git("rev-parse", "HEAD")
    print("Org-state checks passed: kept/excluded names, bundle only on new commits, incremental ZIPs, sha256-identical restore.")


def test_scope_2_files_project_bundles_and_credentials():
    """Owner 10-13: meetings, review-audio, ledger json, three Claude files and one bundle per project repo ride the ZIP."""
    import subprocess
    from backup import GIT
    secret = "DO_NOT_BACK_UP_PLACEHOLDER"
    with tempfile.TemporaryDirectory(prefix="incore-scope2-") as temp:
        base = Path(temp).resolve()
        root, drive, home = base / "incore", base / "drive", base / "claude-home"
        drive.mkdir()
        kept = {"meetings/2026-10-13-x.md": "meeting", "review-audio/a.webm": "audio", "data/intel-pushed.jsonl": "{}\n",
                "projects/deployments.json": "{}", "control/data/runs.jsonl": "{}\n", "control/data/kpi.json": "{}",
                "control/data/kpi.md": "kpi", "control/data/backlog-history/daily/b.md": "daily",
                "data/owner-replies.json": '{"records": []}', "data/owner-ratings.json": '{"records": []}',
                "data/arbitros-backup/db/arbitros-20261021T073513Z.sqlite3": "db", "data/arbitros-backup/state.json": "{}",
                "data/poetry-collection/poems.json": "[]", "data/poetry-collection/changes.jsonl": "{}\n"}
        dropped = {"control/data/codex.lock": "1", "control/data/ticks/t.json": "{}", "control/data/runs/r.json": "{}",
                   "control/data/backlog-history/backlog-1.md": "every version", "data/other.jsonl": "{}"}
        for relative, text in {**kept, **dropped}.items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(text, encoding="utf-8")
        home.mkdir()
        (home / "settings.json").write_text('{"permissions": {}}', encoding="utf-8")
        (home / "CLAUDE.md").write_text("claude md", encoding="utf-8")
        (base / "AGENTS.md").write_text("agents", encoding="utf-8")
        for relative in (".credentials.json", "auth.json", ".secrets/t.dpapi", ".env", ".ssh/id_ed25519"):
            (home / relative).parent.mkdir(parents=True, exist_ok=True)
            (home / relative).write_text(secret, encoding="utf-8")
        external_files = {"external/claude-home/claude-settings.json.txt": home / "settings.json",
                          "external/claude-home/CLAUDE.md": home / "CLAUDE.md", "external/claude-home/AGENTS.md": base / "AGENTS.md",
                          "external/claude-home/.credentials.json": home / ".credentials.json",  # even if someone maps them,
                          "external/claude-home/auth.json": home / "auth.json"}                   # they are refused
        (root / "data/backups").mkdir(parents=True)
        (root / "data/backups" / ANCHOR).write_bytes(b"identity")
        (drive / ANCHOR).write_bytes(b"identity")

        def repo(path, files, message="c"):
            path.mkdir(parents=True, exist_ok=True)
            git = lambda *args: subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(path), *args],
                                               check=True, capture_output=True, text=True).stdout
            if not (path / ".git").exists():
                git("init", "-q")
            for name, text in files.items():
                (path / name).write_text(text, encoding="utf-8")
                git("add", "-f", name)
            git("commit", "-q", "-m", message)
            return git

        repo(root, {"README.md": "root"})
        good = repo(base / "projects/good", {"a.py": "print(1)"})
        leaky = repo(base / "projects/leaky", {".env": secret, "b.py": "x"})
        leaky("rm", "-q", ".env"), leaky("commit", "-q", "-m", "removed")  # gone from the tree, still in history
        repo(base / "ClaudeMigration/project files/Big One", {"c.py": "x" * 100})
        (base / "projects/not-a-repo").mkdir()
        (base / "projects/not-a-repo/f.txt").write_text("x")

        def run(day, **extra):
            receipt = snapshot_once(drive, root=root, archive_time=at(day, 20), external={}, external_files=external_files, **extra)
            assert "staging_error" not in receipt, receipt
            return receipt

        first = run(24)
        report = first["project_bundles"]
        manifest = verify_zip(Path(first["package_path"]))
        paths = {e["path"] for e in manifest["files"]}
        assert set(kept) <= paths, set(kept) - paths
        assert not paths & set(dropped), paths & set(dropped)
        assert {"external/claude-home/claude-settings.json.txt", "external/claude-home/CLAUDE.md", "external/claude-home/AGENTS.md"} <= paths
        assert not any(word in p for p in paths for word in ("credentials", "auth.json", "dpapi", ".env", ".ssh", "secret")), paths
        assert report["blocked"] == {"projects__leaky": [".env"]}, report
        assert "git/projects/projects__leaky.bundle" not in paths and "git/projects/projects__not-a-repo.bundle" not in paths
        bundled = {p for p in paths if p.startswith("git/projects/")}
        assert bundled == {"git/projects/projects__good.bundle", "git/projects/ClaudeMigration__project files__Big One.bundle"}, (bundled, report)
        assert report["bundled"] == 2 and not report["too_large"] and not report["failed"], report
        with zipfile.ZipFile(first["package_path"]) as archive:
            for member in archive.namelist():
                assert secret.encode() not in archive.read(member), member
        # restore: settings come back under their own name, bundles verify and clone
        restored = base / "restored"
        restore_zip_chain(Path(first["package_path"]), restored)
        assert (restored / "external/claude-home/settings.json").read_text(encoding="utf-8") == '{"permissions": {}}'
        assert not (restored / "external/claude-home/claude-settings.json.txt").exists()
        subprocess.run([GIT, "clone", "-q", str(restored / "git/projects/projects__good.bundle"), str(base / "clone")], check=True, capture_output=True)
        assert (base / "clone/a.py").read_text() == "print(1)"
        # unchanged refs: the bundle is reused (same bytes), a new commit makes a new one, a fixed history is still blocked
        good("commit", "-q", "--allow-empty", "-m", "again")
        second = run(25)
        assert verify_zip(Path(second["package_path"]))["included_paths"] == ["git/projects/projects__good.bundle"], second
        assert second["project_bundles"]["blocked"] == {"projects__leaky": [".env"]}
        with patch("backup.BUNDLE_MAX", 1):
            good("commit", "-q", "--allow-empty", "-m", "third")
            third = run(26)
        assert "projects__good" in third["project_bundles"]["too_large"], third["project_bundles"]
        # the append-only ledgers are stored at most hourly: a new line right after a snapshot is not stored yet
        before = {e["path"]: e["sha256"] for e in verify_zip(Path(third["package_path"]))["files"]}["control/data/runs.jsonl"]
        (root / "control/data/runs.jsonl").write_text("{}\n{}\n", encoding="utf-8")
        fourth = run(27)
        assert "staging_error" not in fourth and not fourth.get("package_path", "").endswith("x"), fourth
        assert {e["path"]: e["sha256"] for e in verify_zip(Path(fourth["package_path"]))["files"]}["control/data/runs.jsonl"] == before
    print("Scope-2 checks passed: new sources kept, lock/ticks/runs dropped, credentials refused, one bundle per repo, leaky and too-large repos reported, restore under original name.")


def _scope3_root(base):
    root, drive = base / "incore", base / "drive"
    drive.mkdir()
    (root / "records").mkdir(parents=True)
    (root / "records/r.json").write_text("{}", encoding="utf-8")
    (root / "backup").mkdir()
    (root / "backup/RESTORE.md").write_text("restore notes", encoding="utf-8")
    shutil_copy(Path(backup.__file__), root / "backup/backup.py")
    (root / "data/backups").mkdir(parents=True)
    (root / "data/backups" / ANCHOR).write_bytes(b"identity")
    (drive / ANCHOR).write_bytes(b"identity")
    return root, drive


def shutil_copy(source, destination):
    destination.write_bytes(source.read_bytes())


def test_scope_3_skills_wsl_files_and_restore_kit():
    """Owner 10-13: the skills folder and three WSL files ride the ZIP; every ZIP carries restore/README.md and restore/backup.py."""
    import subprocess
    secret = "DO_NOT_BACK_UP_PLACEHOLDER"
    with tempfile.TemporaryDirectory(prefix="incore-scope3-") as temp:
        base = Path(temp).resolve()
        root, drive = _scope3_root(base)
        skills, wsl = base / "skills", base / "wsl"
        for relative, text in {"ponytail/SKILL.md": "skill", "ponytail/refs/a.md": "ref", "iso-figure/SKILL.md": "skill2",
                               "ponytail/token.txt": secret, "ponytail/secrets/s.md": secret, "ponytail/.hidden": secret,
                               "ponytail/node_modules/x.js": secret, "api-key/SKILL.md": secret}.items():
            (skills / relative).parent.mkdir(parents=True, exist_ok=True)
            (skills / relative).write_text(text, encoding="utf-8")
        for relative, text in {".codex/config.toml": "model = 'x'", ".claude/settings.json": "{}", ".gitconfig": "[user]",
                               ".codex/auth.json": secret, ".claude/.credentials.json": secret}.items():
            (wsl / relative).parent.mkdir(parents=True, exist_ok=True)
            (wsl / relative).write_text(text, encoding="utf-8")
        files = {"external/wsl-home/codex-config.toml": wsl / ".codex/config.toml",
                 "external/wsl-home/claude-settings.json.txt": wsl / ".claude/settings.json",
                 "external/wsl-home/gitconfig": wsl / ".gitconfig",
                 "external/wsl-home/auth.json": wsl / ".codex/auth.json",  # mapped by mistake: refused
                 "external/wsl-home/credentials.json": wsl / ".claude/.credentials.json"}
        run = lambda day: snapshot_once(drive, root=root, archive_time=at(day, 20), external={backup.SKILLS_PREFIX: skills},
                                        external_files=files)
        first = run(24)
        assert "staging_error" not in first, first
        manifest = verify_zip(Path(first["package_path"]))
        paths = {e["path"] for e in manifest["files"]}
        assert {"external/claude-skills/ponytail/SKILL.md", "external/claude-skills/ponytail/refs/a.md",
                "external/claude-skills/iso-figure/SKILL.md"} <= paths, paths
        assert {"external/wsl-home/codex-config.toml", "external/wsl-home/claude-settings.json.txt", "external/wsl-home/gitconfig"} <= paths
        assert not any(word in p for p in paths for word in ("token", "secrets", "hidden", "node_modules", "api-key", "auth.json", "credentials")), paths
        assert {"restore/README.md", "restore/backup.py"} <= paths
        with zipfile.ZipFile(first["package_path"]) as archive:
            assert archive.read("restore/README.md") == b"restore notes"
            for member in archive.namelist():
                assert secret.encode() not in archive.read(member), member
        # a later ZIP that changes nothing about the kit still carries it
        (root / "records/r2.json").write_text("{}", encoding="utf-8")
        second = run(25)
        included = verify_zip(Path(second["package_path"]))["included_paths"]
        assert {"restore/README.md", "restore/backup.py", "records/r2.json"} <= set(included) and "external/wsl-home/gitconfig" not in included, included
        # the kit works with no code: unzip only restore/, run it on the newest ZIP
        kit = base / "kit"
        with zipfile.ZipFile(second["package_path"]) as archive:
            archive.extract("restore/backup.py", kit)
        out = base / "out"
        done = subprocess.run([sys.executable, str(kit / "restore/backup.py"), "--restore-zip", second["package_path"], "--restore-to", str(out)],
                              capture_output=True, text=True)
        assert done.returncode == 0 and "zip_chain_files_sha256_verified" in done.stdout, done
        assert (out / "external/wsl-home/settings.json").read_text(encoding="utf-8") == "{}"
        assert (out / "external/wsl-home/gitconfig").read_text(encoding="utf-8") == "[user]"
        assert (out / "external/claude-skills/ponytail/SKILL.md").read_text(encoding="utf-8") == "skill"
    print("Scope-3 checks passed: skills and WSL files kept, credential names refused, restore kit in every ZIP and usable alone.")


def test_wsl_files_are_skipped_not_failed_when_wsl_is_down():
    with tempfile.TemporaryDirectory(prefix="incore-scope3-wsl-") as temp:
        base = Path(temp).resolve()
        root, _ = _scope3_root(base)
        (base / "wsl").mkdir()
        (base / "wsl/config.toml").write_text("x", encoding="utf-8")
        wsl_files = {"external/wsl-home/codex-config.toml": base / "wsl/config.toml",
                     "external/wsl-home/gitconfig": base / "wsl/.gitconfig"}  # this one does not exist
        patches = (patch("backup.ROOT", root), patch("backup.WSL_FILES", wsl_files), patch("backup.EXTERNAL_FILES", {}),
                   patch("backup.MEMORY_DIR", base / "none"), patch("backup.SKILLS_DIR", base / "none"),
                   patch("backup._capture_current_chat", return_value={"status": "ok"}), patch("backup.ARBITROS_STEP", None))
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            with patch("backup._wsl_running", return_value=False) as running:
                down = snapshot_once(root=root)
                assert running.call_count == 1 and down["wsl_files"] == "not_running" and down["stage"] == "local_snapshot_verified", down
                snap = json.loads(Path(down["manifest_path"]).read_text(encoding="utf-8"))
                assert not [e for e in snap["files"] if e["path"].startswith("external/wsl-home/")]
            with patch("backup._wsl_running", return_value=True):
                up = snapshot_once(root=root)
                assert up["wsl_files"] == "missing: external/wsl-home/gitconfig", up
                snap = json.loads(Path(up["manifest_path"]).read_text(encoding="utf-8"))
                assert [e["path"] for e in snap["files"] if e["path"].startswith("external/wsl-home/")] == ["external/wsl-home/codex-config.toml"]
    print("WSL checks passed: not running -> skipped and noted, missing file noted, snapshot unaffected.")


def _fake_drive_db(path, rows, operations=(), with_item_column=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE items (stable_id INTEGER, id TEXT, local_title TEXT, file_size INTEGER, trashed INTEGER, is_tombstone INTEGER)")
        db.execute("CREATE TABLE operations (" + ("item_stable_id INTEGER, " if with_item_column else "") + "kind TEXT)")
        db.executemany("INSERT INTO items VALUES (?,?,?,?,?,?)", rows)
        db.executemany("INSERT INTO operations VALUES (" + ("?, ?" if with_item_column else "?") + ")", operations)
        db.commit()


def test_drive_local_evidence_from_a_fake_metadata_db():
    ev = backup._drive_evidence
    with tempfile.TemporaryDirectory(prefix="incore-drive-evidence-") as temp:
        base = Path(temp).resolve()
        ok = (1, "cloudid", "a.zip", 100, 0, 0)
        cases = {"confirmed": [ok], "wrong size": [(1, "cloudid", "a.zip", 99, 0, 0)], "no cloud id": [(1, "", "a.zip", 100, 0, 0)],
                 "trashed": [(1, "cloudid", "a.zip", 100, 1, 0)], "tombstone": [(1, "cloudid", "a.zip", 100, 0, 1)],
                 "not synced yet": [(2, "other", "b.zip", 100, 0, 0)]}
        got = {}
        for name, rows in cases.items():
            db = base / name / "metadata_sqlite_db"
            _fake_drive_db(db, rows)
            got[name] = ev("a.zip", 100, [db])
        assert got == {"confirmed": "confirmed", "wrong size": "pending", "no cloud id": "pending", "trashed": "pending",
                       "tombstone": "pending", "not synced yet": "pending"}, got
        queued = base / "queued/metadata_sqlite_db"
        _fake_drive_db(queued, [ok, (2, "o", "b.zip", 5, 0, 0)], operations=[(2, "upload")])
        assert ev("a.zip", 100, [queued]) == "confirmed"  # another file's queued upload does not hold this one back
        _fake_drive_db(base / "q2/metadata_sqlite_db", [ok], operations=[(1, "upload")])
        assert ev("a.zip", 100, [base / "q2/metadata_sqlite_db"]) == "pending"
        _fake_drive_db(base / "q3/metadata_sqlite_db", [ok], operations=[("upload",)], with_item_column=False)
        assert ev("a.zip", 100, [base / "q3/metadata_sqlite_db"]) == "pending"  # no per-item column: any queue counts
        # second account folder without the ZIP, first one with it
        assert ev("a.zip", 100, [base / "not synced yet/metadata_sqlite_db", base / "confirmed/metadata_sqlite_db"]) == "confirmed"
        # anything unreadable or of another shape is "unavailable", never an exception
        (base / "garbage").mkdir()
        (base / "garbage/metadata_sqlite_db").write_bytes(b"not a database" * 50)
        with closing(sqlite3.connect(base / "other.db")) as db:
            db.execute("CREATE TABLE unrelated (x)")
        assert ev("a.zip", 100, [base / "garbage/metadata_sqlite_db"]) == "unavailable"
        assert ev("a.zip", 100, [base / "other.db"]) == "unavailable"
        assert ev("a.zip", 100, [base / "missing.db"]) == "unavailable"
        assert ev("a.zip", 100, []) == "unavailable"
        with patch("backup.DRIVE_DB_GLOB", base / "no-such-drivefs"):
            assert ev("a.zip", 100) == "unavailable"
    print("Drive evidence checks passed: confirmed / pending / unavailable on fake metadata databases.")


def test_drive_local_evidence_in_status_json():
    secret_name = "UNRELATED_FILE_NAME_IN_DRIVE.zip"
    with tempfile.TemporaryDirectory(prefix="incore-scope3-drive-") as temp:
        base = Path(temp).resolve()
        root, drive = _scope3_root(base)
        glob_root = base / "DriveFS"
        db = glob_root / "acct123/metadata_sqlite_db"
        _fake_drive_db(db, [(9, "x", secret_name, 7, 0, 0)])
        with patch("backup.DRIVE_DB_GLOB", glob_root), patch("backup.DRIVE_RECHECK_S", 0):
            run = lambda day, hour: snapshot_once(drive, root=root, archive_time=at(day, hour), external={}, external_files={})
            first = run(24, 20)
            assert first["drive_local_evidence"] == "pending" and first["cloud_received"] is False, first
            name, size = first["package_name"], first["package_bytes"]
            _fake_drive_db(db.with_name("new.db"), [(9, "x", secret_name, 7, 0, 0), (10, "cloudid", name, size, 0, 0)])
            db.unlink()
            new = db.with_name("new.db")
            new.rename(db)
            # not due (before 20:00 the next day), records changed: the waiting receipt still reports the evidence
            (root / "records/r2.json").write_text("{}", encoding="utf-8")
            second = run(25, 10)
            assert "package_name" not in second and second["last_staged_package"]["package_bytes"] == size, second
            assert second["drive_local_evidence"] == "confirmed" and second["cloud_received"] is False, second
            assert json.loads((root / "data/backups/status.json").read_text(encoding="utf-8"))["drive_local_evidence"] == "confirmed"
            assert secret_name not in json.dumps(second)
            db.write_bytes(b"broken")  # confirmed is sticky; and a broken DB on a new ZIP is unavailable, not a failure
            assert run(25, 11)["drive_local_evidence"] == "confirmed"
            third = run(25, 21)
            assert third["drive_local_evidence"] == "unavailable" and "staging_error" not in third, third
        # no DriveFS at all
        with patch("backup.DRIVE_DB_GLOB", base / "absent"):
            (root / "records/r3.json").write_text("{}", encoding="utf-8")
            fourth = snapshot_once(drive, root=root, archive_time=at(26, 21), external={}, external_files={})
            assert fourth["drive_local_evidence"] == "unavailable" and "staging_error" not in fourth
    print("Drive evidence in status.json passed: confirmed/pending/unavailable, cloud_received untouched, other file names absent.")


def test_credential_allow_list_is_exact_and_rechecked():
    """Owner-reviewed false positives pass only as exact "repo:path"; the same path in another repo, or a new `api key.txt`,
    is still refused; changing the allow list re-checks a repo whose verdict was cached."""
    import subprocess
    from backup import GIT
    with tempfile.TemporaryDirectory(prefix="incore-allow-") as temp:
        base = Path(temp).resolve()
        root, drive = base / "incore", base / "drive"
        drive.mkdir()
        (root / "data/backups").mkdir(parents=True)
        (root / "data/backups" / ANCHOR).write_bytes(b"identity")
        (drive / ANCHOR).write_bytes(b"identity")

        def repo(path, files):
            path.mkdir(parents=True, exist_ok=True)
            git = lambda *args: subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(path), *args],
                                               check=True, capture_output=True, text=True).stdout
            if not (path / ".git").exists():
                git("init", "-q")
            for name, text in files.items():
                (path / name).parent.mkdir(parents=True, exist_ok=True)
                (path / name).write_text(text, encoding="utf-8")
                git("add", "-f", name)
            git("commit", "-q", "-m", "c")
            return git

        repo(root, {"README.md": "root"})
        site = repo(base / "projects/arbitrosai-website", {"server/templates/tpl/key_points.html": "points"})
        repo(base / "projects/other", {"key_points.html": "same name, other repo"})

        def run(day):
            receipt = snapshot_once(drive, root=root, archive_time=at(day, 20), external={}, external_files={})
            assert "staging_error" not in receipt, receipt
            return receipt["project_bundles"], {e["path"] for e in verify_zip(Path(receipt["package_path"]))["files"]}

        report, paths = run(24)  # the reviewed pair passes; the same file name in another repo does not
        assert "git/projects/projects__arbitrosai-website.bundle" in paths, report
        assert report["blocked"] == {"projects__other": ["key_points.html"]}, report
        with patch("backup.CREDENTIAL_ALLOW", set()):  # list changed: the cached "passes" verdict is re-checked and refused
            report, paths = run(25)
        assert report["blocked"] == {"projects__arbitrosai-website": ["server/templates/tpl/key_points.html"],
                                     "projects__other": ["key_points.html"]}, report
        assert not any(p.startswith("git/projects/projects__arbitrosai") for p in paths), paths
        report, paths = run(26)  # back to the list: re-checked again
        assert "git/projects/projects__arbitrosai-website.bundle" in paths and "projects__arbitrosai-website" not in report["blocked"], report
        (base / "projects/arbitrosai-website/api key.txt").write_text("not a real key", encoding="utf-8")
        site("add", "-f", "api key.txt")
        site("commit", "-q", "-m", "a credential-type name")
        report, paths = run(27)  # a new credential-type file in an allowed repo is still refused
        assert report["blocked"]["projects__arbitrosai-website"] == ["api key.txt"], report
        assert not any(p.startswith("git/projects/projects__arbitrosai") for p in paths), paths
    print("Allow-list checks passed: exact pair only, cache re-checked on list change, new credential-type names still refused.")


def test_unreadable_wsl_links_are_skipped_and_counted_but_real_read_errors_still_fail():
    """A link Windows cannot follow (WinError 1920) is skipped in records/ and in the organization state and shows in the receipt;
    any other OSError still fails the round, so a real read problem is not hidden."""
    import backup
    real = backup.sha256

    def reading(bad_name, winerror):
        def sha(path):
            if Path(path).name == bad_name:
                error = OSError(22, "The file cannot be accessed by the system")
                error.winerror = winerror
                raise error
            return real(path)
        return sha

    with tempfile.TemporaryDirectory(prefix="incore-wsl-link-") as temp:
        root = Path(temp).resolve() / "incore"
        for name in ("audits/ok.md", "audits/fixtures/pytest-cases/link.md", "records/ok.json", "records/link.json"):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text("x", encoding="utf-8")
        for bad in ("link.md", "link.json"):
            with patch("backup.sha256", reading(bad, 1920)):
                receipt = snapshot_once(root=root, external={})
            assert receipt["unreadable_links_skipped"]["count"] == 1, receipt
            assert receipt["unreadable_links_skipped"]["first"][0].endswith(bad), receipt
            manifest = json.loads(Path(receipt["manifest_path"]).read_text(encoding="utf-8"))
            paths = {entry["path"] for entry in manifest["files"]}
            assert not any(p.endswith(bad) for p in paths) and ("audits/ok.md" in paths) and ("records/ok.json" in paths), paths
        clean = snapshot_once(root=root, external={})
        assert "unreadable_links_skipped" not in clean  # the list is per round
        for bad in ("broken.md", "link.json"):  # broken.md is new: an unchanged org file is trusted by size and mtime, not read
            (root / "audits" / "broken.md").write_text("y", encoding="utf-8")
            with patch("backup.sha256", reading(bad, 5)):  # another read error: still a failure
                try:
                    snapshot_once(root=root, external={})
                except OSError:
                    pass
                else:
                    raise AssertionError(f"a real read error on {bad} was swallowed")
    print("WSL link checks passed: unreadable links skipped and counted in records and org state; other read errors still fail.")


def test_org_state_hashes_only_changed_files():
    """Second round: an organization file with the same size and mtime is not read; a changed byte or a touched mtime is."""
    import backup
    with tempfile.TemporaryDirectory(prefix="incore-org-hash-") as temp:
        root = Path(temp).resolve() / "incore"
        for name, text in {"audits/a.md": "aaaa", "audits/b.md": "bbbb", "projects/backlog.md": "- [todo] x\n"}.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, encoding="utf-8")
        read = []
        real = backup.sha256

        def counting(path):
            read.append(Path(path).name)
            return real(path)

        def round_():
            read.clear()
            with patch("backup.sha256", counting):
                return snapshot_once(root=root, external={})

        first = round_()
        assert {"a.md", "b.md", "backlog.md"} <= set(read), read
        second = round_()
        assert not {"a.md", "b.md", "backlog.md"} & set(read), read  # unchanged: no content read (receipt checks hash the manifest only)
        assert second["unchanged"] and second["file_count"] == first["file_count"]
        a = root / "audits/a.md"
        mtime = a.stat().st_mtime_ns
        a.write_text("aaab", encoding="utf-8")  # one byte, and the mtime moves on
        os.utime(a, ns=(mtime + 2_000_000_000,) * 2)
        third = round_()
        assert "a.md" in read and "b.md" not in read, read
        assert not third["unchanged"]
        b = root / "audits/b.md"
        os.utime(b, ns=(b.stat().st_mtime_ns + 2_000_000_000,) * 2)  # only the mtime
        round_()
        assert "b.md" in read, read
        # same size, same mtime, other bytes is the one case this trusts the cache for: the files are not edited in place
        print("Org-state hash cache checks passed: unchanged files are not re-read; size or mtime change re-hashes.")


def test_watch_refuses_a_second_watcher():
    from backup import _running_watcher
    watch = '"python.exe" -u incore/backup/backup.py --watch --drive --interval 60'
    with tempfile.TemporaryDirectory(prefix="incore-single-test-") as temp, redirect_stdout(io.StringIO()) as out:
        stop = Path(temp) / "STOP"
        proc = Path(temp) / "watcher-process.json"
        args = ["--watch", "--stop-file", str(stop), "--max-runs", "1"]
        success = {"stage": "local_snapshot_verified", "unchanged": True}
        # no state file: starts normally
        with patch("backup.snapshot_once", return_value=success) as snapshot:
            assert main(args) == 0 and snapshot.call_count == 1
        proc.write_text(json.dumps({"ProcessId": 999999}), encoding="utf-8")
        for cmd, expect in ((watch, 999999), ("", None), ('"python.exe" other.py', None),  # dead PID, reused PID
                            ('"python.exe" backup.py --once', None)):
            assert _running_watcher(proc, lambda pid: cmd) == expect
        assert _running_watcher(proc, lambda pid: watch if pid == 999999 else "") == 999999
        # this process and its parent (the venv launcher) are not another watcher
        for own in (os.getpid(), os.getppid()):
            proc.write_text(json.dumps({"ProcessId": own}), encoding="utf-8")
            assert _running_watcher(proc, lambda pid: watch) is None
        proc.write_text(json.dumps({"ProcessId": 999999}), encoding="utf-8-sig")  # as PowerShell Set-Content writes it
        assert _running_watcher(proc, lambda pid: watch) == 999999
        proc.write_text("{broken", encoding="utf-8")
        assert _running_watcher(proc, lambda pid: watch) is None
        # a live watcher recorded: main exits 0 with one log line and never snapshots
        proc.write_text(json.dumps({"ProcessId": 999999}), encoding="utf-8")
        with patch("backup._command_line", return_value=watch), patch("backup.snapshot_once") as snapshot:
            assert main(args) == 0
            snapshot.assert_not_called()
        assert '"stage": "watcher_already_running"' in out.getvalue() and '"pid": 999999' in out.getvalue()
    print("Single watcher checks passed: live watcher refuses a second, dead/reused/own/parent PID does not.")


def test_arbitros_step_is_part_of_the_real_root_snapshot():
    with tempfile.TemporaryDirectory(prefix="incore-arbitros-hook-") as temp:
        root, other = Path(temp).resolve() / "incore", Path(temp).resolve() / "other"
        for folder in (root, other):
            (folder / "records").mkdir(parents=True)
            (folder / "records" / "r.json").write_text("{}", encoding="utf-8")
        seen = []
        def step(path):
            seen.append(path)
            return {"arbitros": {"ok_at": "x"}, "arbitros_error": "ssh down"}
        with patch("backup.ARBITROS_STEP", step), patch("backup.ROOT", root), \
                patch("backup._capture_current_chat", return_value={"status": "ok"}):
            real = snapshot_once(root=root)
            assert seen == [root] and real["arbitros_error"] == "ssh down"
            assert json.loads((root / "data/backups/status.json").read_text(encoding="utf-8"))["arbitros_error"] == "ssh down"
            assert real["stage"] == "local_snapshot_verified"  # the Arbitros error does not turn the snapshot into a failure
            assert "arbitros_error" not in snapshot_once(root=other, external={})  # a test/restore root never calls it
            assert seen == [root]
    print("Arbitros hook checks passed: real root only, error visible in status.json, snapshot itself unaffected.")


def test_snapshot_survives_a_source_that_is_written_between_every_step():
    """The online-backup API restarts when another connection commits between two steps. Here a second connection commits at
    every progress call, which is what a source written all the time does: copying in several steps never finishes."""
    class Spy:
        def __init__(self, inner, writer):
            self.inner, self.writer, self.calls = inner, writer, 0

        def backup(self, target, **kw):
            real = kw["progress"]

            def progress(*args):
                self.calls += 1
                self.writer.execute("INSERT INTO t(v) VALUES ('w')")
                self.writer.commit()
                real(*args)
            return self.inner.backup(target, **{**kw, "progress": progress})

        def close(self):
            self.inner.close()

    with tempfile.TemporaryDirectory() as folder:
        source, out = Path(folder) / "s.sqlite3", Path(folder) / "o.sqlite3"
        with closing(sqlite3.connect(source)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
            db.executemany("INSERT INTO t(v) VALUES (?)", [("x" * 3000,)] * 2200)
            db.commit()
        real_connect, spies = sqlite3.connect, []
        with closing(real_connect(source)) as writer:
            def connect(target, *args, **kw):
                inner = real_connect(target, *args, **kw)
                if kw.get("uri"):
                    spies.append(Spy(inner, writer))
                    return spies[-1]
                return inner
            with patch.object(backup.sqlite3, "connect", connect):
                backup.snapshot_database(source, out)
        assert spies[0].calls < 5, spies[0].calls
        with closing(sqlite3.connect(out)) as db:
            assert db.execute("SELECT count(*) FROM t").fetchone()[0] >= 2200
    print("Busy-source snapshot passed: a commit at every progress call does not restart the copy; it finishes in one pass.")


def test_premier_gaps_g1_g2_g4_g8_ride_the_zip_and_restore_byte_identical():
    """Premier G1/G2/G4/G8: card numbers, polish settings + its prompt file, Discord attachments, send receipts and the machine-health
    ledger are in the ZIP and come back with the same bytes; private-https.json (owner fills it in) is never taken; originals untouched."""
    with tempfile.TemporaryDirectory(prefix="incore-premier-gaps-") as temp:
        base = Path(temp).resolve()
        root, drive, models = base / "incore", base / "drive", base / "models"
        for d in (drive, models, root / "data/backups"):
            d.mkdir(parents=True)
        (root / "data/backups" / ANCHOR).write_bytes(b"identity")
        (drive / ANCHOR).write_bytes(b"identity")
        prompt = models / "prompt-v1.txt"
        prompt.write_text("把口语整理成书面语。\n", encoding="utf-8")
        sources = {
            "data/owner-todo-ids.json": '{"D1": "row a"}', "data/review-dismissed.json": '{"V3": "seen"}',
            "input/data/llm-postprocess.json": json.dumps({"enabled": True, "prompt_path": str(prompt), "model": "m"}),
            "discord/data/attachments/111/脚本 图.png": "png-bytes", "discord/data/attachments/111/a.txt": "text",
            "notify/data/sent.jsonl": '{"id": 1}\n', "data/machine-health.jsonl": '{"ok": true}\n',
        }
        never = {"input/data/private-https.json": '{"cert": "x"}', "discord/data/attachments/111/my_token.txt": "t",
                 "notify/data/credentials.json": "c"}
        for relative, text in {**sources, **never}.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        sources["external/llm-prompt/prompt-v1.txt"] = prompt
        assert backup._llm_prompt_files(root) == {"external/llm-prompt/prompt-v1.txt": prompt}
        settings = root / "input/data/llm-postprocess.json"
        good = settings.read_text(encoding="utf-8")
        for bad in ("{\"prompt_path\": \"\"}", "not json", json.dumps({"prompt_path": str(models / "nope.txt")})):
            settings.write_text(bad, encoding="utf-8")
            assert backup._llm_prompt_files(root) == {}, bad
        settings.write_text(good, encoding="utf-8")
        assert backup._llm_prompt_files(base / "elsewhere") == {}
        origin = {r: sha256(p if isinstance(p, Path) else root / r) for r, p in sources.items()}
        receipt = snapshot_once(drive, root=root, archive_now=True, archive_time=at(28, 19), external={},
                                external_files=backup._llm_prompt_files(root))
        assert "staging_error" not in receipt, receipt
        package = Path(receipt["package_path"])
        in_zip = {e["path"] for e in verify_zip(package)["files"]}
        for path in sources:
            assert path in in_zip, path
        assert not any(path in in_zip for path in never), in_zip
        destination = base / "restored"
        restore_zip_chain(package, destination)
        for relative, digest in origin.items():
            assert sha256(destination / relative) == digest, relative
        for relative, digest in origin.items():  # the originals were only read
            assert sha256(prompt if relative.startswith("external/") else root / relative) == digest, relative
    print("Premier gap checks passed: G1/G2/G4/G8 sources in the ZIP, restore sha256-identical, private-https.json and credential names left out.")

def test_premier_allow_pairs_reach_the_bundle_and_other_key_names_still_stop_it():
    """Premier 2b: the reviewed tokenizer/source names of factoryalign and the writing repo are bundled; another key-named
    file in the same repo, `api key.txt`, and blind_key.json / key.json are still refused; nothing open-ended was allowed."""
    import subprocess
    from backup import GIT, CREDENTIAL_ALLOW
    answer_keys = {"ClaudeMigration/project files/style_rewriter:" + k for k in (
        "experiments/blind_read_kit/blind_key.json", "experiments/blind_read_kit/two_routes/blind_key.json",
        "experiments/four_step/restyle_v3_blind/copy_acceptance/key.json", "experiments/four_step/restyle_v3_blind/demo/key.json",
        "experiments/four_step/restyle_v3_blind/ready/key.json")}
    named = {p for p in CREDENTIAL_ALLOW if p.rsplit(":", 1)[1].rsplit("/", 1)[-1] in ("api key.txt", "blind_key.json", "key.json")}
    assert named == answer_keys, named  # only the five reviewed answer keys, by exact path; never `api key.txt`
    assert all(":" in p and "*" not in p for p in CREDENTIAL_ALLOW)
    with tempfile.TemporaryDirectory(prefix="incore-allow2-") as temp:
        base = Path(temp).resolve()
        root, drive = base / "incore", base / "drive"
        drive.mkdir()
        (root / "data/backups").mkdir(parents=True)
        (root / "data/backups" / ANCHOR).write_bytes(b"identity")
        (drive / ANCHOR).write_bytes(b"identity")

        def repo(path, files):
            path.mkdir(parents=True, exist_ok=True)
            git = lambda *args: subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(path), *args],
                                               check=True, capture_output=True, text=True).stdout
            if not (path / ".git").exists():
                git("init", "-q")
            for name, text in files.items():
                (path / name).parent.mkdir(parents=True, exist_ok=True)
                (path / name).write_text(text, encoding="utf-8")
                git("add", "-f", name)
            git("commit", "-q", "-m", "c")

        repo(root, {"README.md": "root"})
        fa = {"bench/token_columns.py": "x = 1\n", "decision_data/tokenizer_check.py": "y = 2\n",
              "decision_data/frozen/tokenizer-check.json": "{}"}
        ps = {"experiments/expand_v3_retrain_prep/tokenizer_v2.json": "{}", "scripts/token_harness_report.py": "z = 3\n"}
        repo(base / "projects/factoryalign", fa)
        repo(base / "ClaudeMigration/project files/style_rewriter", ps)

        def run(day):
            receipt = snapshot_once(drive, root=root, archive_time=at(day, 20), external={}, external_files={})
            assert "staging_error" not in receipt, receipt
            return receipt["project_bundles"], {e["path"] for e in verify_zip(Path(receipt["package_path"]))["files"]}

        report, paths = run(24)
        assert report["blocked"] == {}, report
        assert {"git/projects/projects__factoryalign.bundle",
                "git/projects/ClaudeMigration__project files__style_rewriter.bundle"} <= paths, paths
        repo(base / "projects/factoryalign", {"decision_data/api_key_2.txt": "placeholder"})  # same repo, another key name
        repo(base / "ClaudeMigration/project files/style_rewriter", {"experiments/other_kit/blind_key.json": "{}"})
        report, paths = run(25)
        assert report["blocked"] == {"projects__factoryalign": ["decision_data/api_key_2.txt"],
                                     "ClaudeMigration__project files__style_rewriter":
                                     ["experiments/other_kit/blind_key.json"]}, report
    print("Premier allow checks passed: reviewed names bundled, other key-named files in the same repos still blocked.")


def _files_below_per_file_checks(directory, root):
    """The former implementation (a resolve and an _allowed call for every file), kept as the reference."""
    if not directory.exists():
        return
    for parent, directories, filenames in os.walk(directory, followlinks=False):
        directories[:] = [n for n in directories if backup._allowed(Path(parent) / n, root)
                          and not (Path(parent) / n).is_symlink() and not (Path(parent) / n).is_junction()]
        for name in sorted(filenames):
            path = Path(parent) / name
            if backup._allowed(path, root) and not path.is_symlink() and path.resolve().is_relative_to(root):
                yield path


def test_files_below_matches_the_per_file_checks():
    with tempfile.TemporaryDirectory(prefix="incore-below-") as temp:
        root = Path(temp).resolve() / "root"
        root.mkdir()
        names =["a.md", "sub/b.json", "sub/deep/c.txt", ".hidden/d.txt", "sub/.dot.txt", "secrets/e.txt", "sub/token/f.txt",
                 "x.log", "sub/y.tmp", "db.sqlite3-wal", "db.sqlite3-shm", "Upper/Credentials/g.txt", "ok/h.txt", "ok/Token.TXT"]
        for rel in names:
            f = root / "audits" / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x", encoding="utf-8")
        outside = Path(temp).resolve() / "outside"
        outside.mkdir()
        (outside / "o.txt").write_text("x", encoding="utf-8")
        for link, target, is_dir in (("audits/link-dir", outside, True), ("audits/link-file.txt", outside / "o.txt", False)):
            try:
                os.symlink(target, root / link, target_is_directory=is_dir)
            except OSError:
                pass  # no symlink privilege on this machine; the junction below still covers a linked folder
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "audits" / "junction"), str(outside)],
                           capture_output=True, check=False)
        folder = root / "audits"
        want = sorted(_files_below_per_file_checks(folder, root))
        assert sorted(backup._files_below(folder, root)) == want and len(want) >= 4, want
        assert not any("outside" in str(p) for p in want)
        # a folder that is itself a link out of the root yields nothing, as before
        if (root / "audits" / "link-dir").exists() or (root / "audits" / "junction").exists():
            link = root / "audits" / ("link-dir" if (root / "audits" / "link-dir").exists() else "junction")
            assert list(backup._files_below(link, root)) == list(_files_below_per_file_checks(link, root)) == []
        assert list(backup._files_below(root / "missing", root)) == []
    print("files_below checks passed: same files as the per-file checks (hidden, forbidden, log/tmp/wal, links, junction, missing).")


if __name__ == "__main__":
    test_unreadable_wsl_links_are_skipped_and_counted_but_real_read_errors_still_fail()
    test_files_below_matches_the_per_file_checks()
    test_snapshot_survives_a_source_that_is_written_between_every_step()
    test_scope_3_skills_wsl_files_and_restore_kit()
    test_wsl_files_are_skipped_not_failed_when_wsl_is_down()
    test_drive_local_evidence_from_a_fake_metadata_db()
    test_drive_local_evidence_in_status_json()
    test_scope_2_files_project_bundles_and_credentials()
    test_credential_allow_list_is_exact_and_rechecked()
    test_arbitros_step_is_part_of_the_real_root_snapshot()
    test_watch_refuses_a_second_watcher()
    test_backup()
    test_watch_lifecycle()
    test_chat_capture_integration()
    test_daily_zip_audio_and_restore()
    test_empty_day_creates_no_zip()
    test_org_state_zip_bundle_exclusions_and_restore()
    test_org_state_hashes_only_changed_files()
    test_premier_gaps_g1_g2_g4_g8_ride_the_zip_and_restore_byte_identical()
    test_premier_allow_pairs_reach_the_bundle_and_other_key_names_still_stop_it()
