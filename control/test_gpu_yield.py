import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gpu_yield as g  # noqa: E402

SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source TEXT NOT NULL, title TEXT NOT NULL,
  status TEXT NOT NULL, mime_type TEXT, source_filename TEXT, audio_extension TEXT, original_sha256 TEXT, total_bytes INTEGER,
  chunk_count INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE chunks (session_id TEXT NOT NULL, seq INTEGER NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, stored_at TEXT NOT NULL,
  PRIMARY KEY (session_id, seq));
CREATE TABLE revisions (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL, parent_id TEXT, content TEXT NOT NULL,
  sha256 TEXT NOT NULL, created_at TEXT NOT NULL, source TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE verifications (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, revision_id TEXT NOT NULL, revision_sha256 TEXT NOT NULL,
  original_sha256 TEXT NOT NULL, scope TEXT NOT NULL, verified_at TEXT NOT NULL, source TEXT NOT NULL);
"""
NOW = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)      # 14:00 US Eastern, outside the night window
NIGHT = datetime(2026, 10, 24, 5, 0, tzinfo=timezone.utc)     # 01:00 US Eastern, inside it
ROOMY = lambda: 4000  # noqa: E731


def stamp(minutes_ago, now=NOW):
    return (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class GpuYield(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = Path(self.dir.name) / "input.sqlite3"
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.commit()
        con.close()

    def session(self, status, minutes_ago, now=NOW, sid="s1"):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO sessions (id, created_at, updated_at, source, title, status) VALUES (?,?,?,?,?,?)",
                    (sid, stamp(minutes_ago + 5, now), stamp(minutes_ago, now), "mic", "t", status))
        con.commit()
        con.close()

    def ask(self, now=NOW, free=ROOMY):
        return g.can_run(now, self.db, free)

    def test_idle_long_enough_is_allowed_at_any_hour(self):
        self.session("transcribed", 25)
        d = self.ask()
        self.assertTrue(d.allowed, d.reason)
        self.assertEqual(d.basis, "input")
        self.assertIn("quiet for 25 min", d.reason)

    def test_empty_station_counts_as_idle(self):
        self.assertTrue(self.ask().allowed)

    def test_just_used_is_denied_and_the_boundary_is_the_configured_minutes(self):
        self.session("transcribed", 5)
        d = self.ask()
        self.assertFalse(d.allowed)
        self.assertEqual(d.basis, "input")
        self.assertTrue(g.can_run(NOW, self.db, ROOMY, idle_minutes=5).allowed)                                    # quiet for exactly 5 min
        self.assertFalse(g.can_run(NOW - timedelta(seconds=1), self.db, ROOMY, idle_minutes=5).allowed)           # 4 min 59 s

    def test_a_chunk_a_revision_or_a_verification_counts_as_activity(self):
        self.session("transcribed", 120)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO chunks VALUES ('s1',0,'x',1,?)", (stamp(3),))
        con.commit()
        con.close()
        self.assertFalse(self.ask().allowed)
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM chunks")
        con.execute("INSERT INTO revisions (id,session_id,kind,content,sha256,created_at,source) VALUES ('r1','s1','edit','c','x',?, 'owner')", (stamp(2),))
        con.commit()
        con.close()
        self.assertFalse(self.ask().allowed)
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM revisions")
        con.execute("INSERT INTO verifications VALUES ('v1','s1','r1','x','x','all',?, 'owner')", (stamp(1),))
        con.commit()
        con.close()
        self.assertFalse(self.ask().allowed)

    def test_a_long_transcription_blocks_even_when_its_last_update_is_old(self):
        self.session("transcribing", 45)     # started 45 min ago, still running: updated_at is stale but the job is live
        d = self.ask()
        self.assertFalse(d.allowed)
        self.assertIn("transcribing", d.reason)

    def test_a_crash_leftover_does_not_block_forever(self):
        self.session("transcribing", 60 * 5)
        self.assertTrue(self.ask().allowed)

    def test_unreadable_input_falls_back_to_the_night_window(self):
        missing = Path(self.dir.name) / "nope.sqlite3"
        day = g.can_run(NOW, missing, ROOMY)
        night = g.can_run(NIGHT, missing, ROOMY)
        self.assertEqual((day.allowed, day.basis), (False, "night-window"))
        self.assertEqual((night.allowed, night.basis), (True, "night-window"))
        self.assertIn("unreadable", night.reason)

    def test_a_corrupt_file_is_unreadable_not_a_crash(self):
        self.db.write_bytes(b"this is not a database" * 100)
        self.assertFalse(self.ask().allowed)
        self.assertTrue(self.ask(NIGHT).allowed)

    def test_a_locked_database_is_still_read(self):
        self.session("transcribed", 60)
        writer = sqlite3.connect(self.db)
        writer.execute("BEGIN IMMEDIATE")
        try:
            self.assertTrue(self.ask().allowed)
        finally:
            writer.rollback()
            writer.close()

    def test_the_read_does_not_change_the_file(self):
        self.session("transcribed", 60)
        before = self.db.read_bytes()
        self.ask()
        self.assertEqual(self.db.read_bytes(), before)

    def test_low_vram_wins_over_an_idle_station(self):
        self.session("transcribed", 120)
        d = self.ask(free=lambda: 1000)
        self.assertEqual((d.allowed, d.basis), (False, "vram"))
        self.assertFalse(self.ask(free=lambda: g.MIN_FREE_MIB - 1).allowed)
        self.assertTrue(self.ask(free=lambda: g.MIN_FREE_MIB).allowed)

    def test_unreadable_vram_is_treated_as_low(self):
        def broken():
            raise FileNotFoundError("nvidia-smi")
        d = self.ask(free=broken)
        self.assertEqual((d.allowed, d.basis), (False, "vram"))

    def test_night_window_edges_use_us_eastern_with_daylight_saving(self):
        edt = lambda h: datetime(2026, 10, 23, tzinfo=timezone.utc) + timedelta(hours=h + 4)  # noqa: E731  (October is EDT, UTC-4)
        self.assertTrue(g.in_night_window(edt(23)))
        self.assertFalse(g.in_night_window(edt(22)))
        self.assertTrue(g.in_night_window(datetime(2026, 10, 24, 10, 59, tzinfo=timezone.utc)))   # 06:59 EDT
        self.assertFalse(g.in_night_window(datetime(2026, 10, 24, 11, 0, tzinfo=timezone.utc)))   # 07:00 EDT
        self.assertTrue(g.in_night_window(datetime(2026, 12, 1, 4, 0, tzinfo=timezone.utc)))      # 23:00 EST


if __name__ == "__main__":
    unittest.main()
