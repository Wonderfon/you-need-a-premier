"""records_capture: a fully archived thread whose source history is gone is not a failure (10-14: Codex compressed it)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import records_capture as rc


class MissingHistory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "records").mkdir()
        (root / "sessions").mkdir()
        self.dest = root / "records" / "current-chat"
        self.patches = [mock.patch.object(rc, "ROOT", root), mock.patch.object(rc, "SESSION_ROOT", root / "sessions"),
                        mock.patch.object(rc, "DESTINATION", self.dest)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def test_archived_thread_without_history_is_ok(self):
        self.dest.mkdir(parents=True)
        raw = '{"type":"x"}'
        entry = {"thread_id": rc.THREAD_ID, "record_key": "k1", "source_jsonl_record": raw,
                 "source_record_sha256": rc.digest(raw.encode("utf-8"))}
        (self.dest / "messages.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
        (Path(self._tmp.name) / "sessions" / f"rollout-{rc.THREAD_ID}.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd")
        result = rc.capture_once()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["archived_record_versions"], 1)
        self.assertEqual((self.dest / "messages.jsonl").read_text(encoding="utf-8").count("\n"), 1)  # archive untouched

    def test_no_archive_and_no_history_still_fails(self):
        self.assertEqual(rc.capture_once()["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
