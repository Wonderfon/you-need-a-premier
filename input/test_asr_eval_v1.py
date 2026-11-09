from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from . import asr_eval_v1
from .store import InputStore


class ConfirmationSurvivesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = InputStore(Path(self.temp.name))
        session = self.store.create_audio_session("audio_capture", "录音", "audio/webm")
        data = b"original"
        self.store.put_chunk(session["id"], 0, data, hashlib.sha256(data).hexdigest())
        self.sid = session["id"]
        digest = self.store.finalize_audio(self.sid, 1, len(data))["original_sha256"]
        asr = self.store.append_revision(self.sid, "asr", "明天下午三点开会", source="asr", data={"text": "x"})
        self.confirmed = self.store.append_revision(self.sid, "correction", "明天下午四点开会", asr["id"],
                                                    data={"correction_type": "recognition_correction"})
        self.store.verify_revision(self.sid, self.confirmed["id"], digest)

    def tearDown(self):
        self.temp.cleanup()

    def _add(self, label, text, kind="correction"):
        data = {"correction_type": label} if label else {"text": text}
        self.store.append_revision(self.sid, kind, text, self.confirmed["id"], source="asr" if kind == "asr" else "user_correction", data=data)

    def _survives(self):
        db = sqlite3.connect(f"file:{self.store.db_path.as_posix()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            return asr_eval_v1.confirmation_survives(db, self.sid, self.confirmed["id"])
        finally:
            db.close()

    def test_nothing_after_confirmation_survives(self):
        self.assertTrue(self._survives())

    def test_drafts_then_different_rewrite_survives(self):   # owner 00:13
        self._add("draft", "明天下午四点开会吧")
        self._add("user_rewrite", "下午四点开会")
        self.assertTrue(self._survives())

    def test_trailing_unconfirmed_draft_is_superseded(self):  # e9e99b6f
        self._add("draft", "明天下午四点开会吧")
        self._add("draft", "明天下午四点开会吧。")
        self.assertFalse(self._survives())

    def test_later_dictation_correction_or_new_asr_is_superseded(self):
        self._add("recognition_correction", "明天下午五点开会")
        self.assertFalse(self._survives())

    def test_new_asr_is_superseded(self):
        self._add(None, "重新转写", kind="asr")
        self.assertFalse(self._survives())

    def test_rewrite_with_same_text_is_superseded(self):
        self._add("user_rewrite", "明天下午四点开会")
        self.assertFalse(self._survives())


if __name__ == "__main__":
    unittest.main()
