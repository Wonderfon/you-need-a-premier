"""Archive safety and lossless movement checks; stdlib only, portable paths."""
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backlog_archive as a


class BacklogArchive(unittest.TestCase):
    def setUp(self):
        cache = a.CONFIG["cache_root"]
        cache.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="test-backlog-archive-", dir=cache)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "projects" / "backlog.md"
        self.path.parent.mkdir()
        self.archive_dir = self.root / a.CONFIG["archive_dir"]
        self.today = date(2026, 11, 2)

    def run_archive(self):
        return a.archive(self.root, today=self.today)

    def test_lossless_months_sections_boundary_and_untouched_states(self):
        old = ["- [done 2026-09-30] 中文 / slash ", "- [done 10-26 23:59：完成] same", "- [done 10-26 23:59：完成] same"]
        untouched = ["- [done 10-27] boundary", "- [done 11-02] today", "- [done 12-01] future",
                     "- [done 13-01] invalid", "- [done 02-30] invalid day", "- [done] no date",
                     "- [done 18:32；给 owner\x2010-01：查看] no completion date",
                     "- [done-ref 10-01] reference", "- [todo] run", "- [review] judge",
                     "- [blocked: 等你决定] choose", "- [running 10-01] live"]
        header = "# 待办\r\n## 组织线\r\n权重：22\r\n"
        data = (header + "\r\n".join(old + untouched) + "\r\n## 生活线\n- [done 10-01] other\n").encode("utf-8")
        self.path.write_bytes(data)
        result = self.run_archive()
        expected = (header + "\r\n".join(untouched) + "\r\n## 生活线\n").encode("utf-8")
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertEqual(Path(result["backup"]).read_bytes(), data)
        september = (self.archive_dir / "2026-09.md").read_bytes()
        october = (self.archive_dir / "2026-10.md").read_bytes()
        self.assertTrue(september.endswith((old[0] + "\r\n").encode("utf-8")))
        self.assertEqual(october.count((old[1] + "\r\n").encode("utf-8")), 2)
        self.assertIn(b"## " + "生活线".encode("utf-8") + b"\n- [done 10-01] other\n", october)
        self.assertEqual(result["moved_rows"], 4)
        self.assertEqual(result["before"]["bytes"] - result["after"]["bytes"], result["moved_bytes"])
        saved = {p.name: p.read_bytes() for p in self.archive_dir.glob("*.md")}
        self.assertEqual(self.run_archive()["moved_rows"], 0)
        self.assertEqual(saved, {p.name: p.read_bytes() for p in self.archive_dir.glob("*.md")})

    def test_interrupted_write_retries_without_duplicate_or_loss(self):
        data = "## 组织线\n- [done 09-01] first month\n- [done 10-01] twice\n- [done 10-01] twice\n- [todo] stay\n".encode("utf-8")
        self.path.write_bytes(data)
        real_write = a._atomic_write

        def interrupted(path, content):
            self.assertTrue(list(self.path.with_name("backlog-history").glob("backlog-*.md")))
            self.assertTrue(self.path.with_name("backlog.md.lock").exists())
            if path == self.path.resolve():
                raise OSError("simulated failure before backlog replace")
            real_write(path, content)

        with patch.object(a, "_atomic_write", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.run_archive()
        self.assertEqual(self.path.read_bytes(), data)
        saved = {p.name: p.read_bytes() for p in self.archive_dir.glob("*.md")}
        with self.assertRaises(ValueError):
            a.archive(self.root, today=self.today, keep_days=365)
        self.path.write_bytes(data + b"- [todo] intervening edit\n")
        with self.assertRaises(ValueError):
            self.run_archive()
        self.path.write_bytes(data)
        self.assertEqual(self.run_archive()["moved_rows"], 3)
        self.assertEqual(saved, {p.name: p.read_bytes() for p in self.archive_dir.glob("*.md")})
        self.assertTrue(self.path.read_bytes().endswith(b"- [todo] stay\n"))

    def test_backup_failure_and_busy_lock_leave_everything_untouched(self):
        data = b"## org\n- [done 10-01] old\n"
        self.path.write_bytes(data)
        with patch.object(a.backlog_io, "_snapshot", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_archive()
        self.assertFalse(self.archive_dir.exists())
        self.assertEqual(self.path.read_bytes(), data)
        real_lock = a.backlog_io.lock
        with real_lock(self.path):
            with patch.object(a.backlog_io, "lock", side_effect=lambda p: real_lock(p, timeout=0.01)):
                with self.assertRaises(TimeoutError):
                    self.run_archive()
        self.assertEqual(self.path.read_bytes(), data)
        self.assertFalse(self.archive_dir.exists())

    def test_identical_row_in_a_later_successful_batch_is_still_preserved(self):
        raw = b"- [done 10-01] identical\r\n"
        self.path.write_bytes(b"## org\n" + raw + raw)
        self.run_archive()
        self.path.write_bytes(self.path.read_bytes() + raw)
        self.assertEqual(self.run_archive()["moved_rows"], 1)
        self.assertEqual((self.archive_dir / "2026-10.md").read_bytes().count(raw), 3)

    def test_hardlink_is_rejected_before_any_write(self):
        source = self.root / "source.md"
        source.write_bytes(b"## org\n- [done 10-01] old\n")
        os.link(source, self.path)
        with self.assertRaises(ValueError):
            self.run_archive()
        self.assertEqual(source.read_bytes(), self.path.read_bytes())
        self.assertFalse(self.path.with_name("backlog.md.lock").exists())

    def test_symlink_to_external_file_or_history_is_rejected(self):
        source = self.root / "source.md"
        source.write_bytes(b"## org\n- [done 10-01] old\n")
        try:
            self.path.symlink_to(source)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(ValueError):
            self.run_archive()
        self.path.unlink()
        self.path.write_bytes(source.read_bytes())
        self.path.with_name("backlog-history").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_archive()
        self.assertFalse(self.archive_dir.exists())

    def test_unterminated_row_and_year_boundary_are_conservative(self):
        data = b"## org\n- [done 2025-12-20] explicit year\n- [done 12-20] no guessed year\n- [done 2025-12-21] no newline"
        self.path.write_bytes(data)
        result = a.archive(self.root, today=date(2026, 1, 2))
        self.assertEqual(result["moved_rows"], 2)
        self.assertEqual(self.path.read_bytes(), b"## org\n- [done 12-20] no guessed year\n")
        archived = (self.archive_dir / "2025-12.md").read_bytes()
        self.assertTrue(archived.endswith(b"- [done 2025-12-21] no newline"))
        # Appending here would change the unterminated row's bytes: fail closed.
        self.path.write_bytes(self.path.read_bytes() + b"- [done 2025-12-22] another\n")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            a.archive(self.root, today=date(2026, 1, 2))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((self.archive_dir / "2025-12.md").read_bytes(), archived)


if __name__ == "__main__":
    unittest.main()
