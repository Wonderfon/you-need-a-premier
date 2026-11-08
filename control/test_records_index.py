import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import records_index as ri  # noqa: E402

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


class RecordsIndexCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.rec, self.index, self.labels = d / "records", d / "out" / "index.jsonl", d / "out" / "labels.json"
        self.rec.mkdir()
        self.labels.parent.mkdir()

    def put(self, name, data):
        (self.rec / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def update(self):
        return ri.update(self.rec, self.index, self.labels, NOW)

    def test_new_file(self):
        self.put("2026-10-02-a", {"received_local": "2026-10-02T20:15-04:00 (approx)", "source": "s", "exact_text": "你好"})
        self.labels.write_text(json.dumps({"2026-10-02-a": {"tags": ["想法"], "project": "general", "decision": "",
                                                          "one_line": "打招呼"}}), encoding="utf-8")
        self.assertEqual(self.update(), {"new": 1, "labelled": 1})
        (row,) = ri.read_rows(self.index)
        self.assertEqual((row["status"], row["hour_local"], row["date_local"], row["chars"], row["text_key"]),
                         ("new", 20, "2026-10-02", 2, "exact_text"))
        self.assertEqual(row["text_sha256"], hashlib.sha256("你好".encode("utf-8")).hexdigest())
        self.assertEqual(row["labels"]["one_line"], "打招呼")

    def test_unchanged_is_skipped(self):
        self.put("2026-10-02-a", {"exact_text": "x"})
        self.update()
        before = self.index.read_bytes()
        self.assertEqual(self.update(), {"unchanged": 1})
        self.assertEqual(self.index.read_bytes(), before)

    def test_changed_file_keeps_old_row(self):
        self.put("2026-10-02-a", {"exact_text": "x"})
        self.labels.write_text(json.dumps({"2026-10-02-a": {"tags": ["任务"], "project": "input", "decision": "",
                                                          "one_line": "旧标签"}}), encoding="utf-8")
        self.update()
        self.put("2026-10-02-a", {"exact_text": "x 改了"})
        self.assertEqual(self.update(), {"changed": 1})
        old, new = ri.read_rows(self.index)
        self.assertEqual((old["status"], new["status"]), ("new", "changed"))
        self.assertEqual(new["prev_file_sha256"], old["file_sha256"])
        self.assertTrue(new["labels_stale"])
        self.assertEqual(new["labels"], old["labels"])
        self.assertIn("待标注", ri.readme(ri.read_rows(self.index)))
        # relabelling clears the stale mark
        self.labels.write_text(json.dumps({"2026-10-02-a": {"tags": ["任务"], "project": "input", "decision": "",
                                                          "one_line": "新标签"}}), encoding="utf-8")
        self.update()
        self.assertNotIn("labels_stale", ri.read_rows(self.index)[-1])

    def test_text_and_time_variants(self):
        self.assertEqual(ri.extract_text({"verbatim": ["a", "b"]}), ("verbatim", "a\nb"))
        self.assertEqual(ri.extract_text({"verbatim_1": "a", "verbatim_2": "b"}), ("verbatim_1", "a\nb"))
        self.assertEqual(ri.extract_text([{"text": "a"}, {"text": "b"}]), ("text", "a\nb"))
        self.assertEqual(ri.extract_text({"decision": "x"}), (None, ""))
        self.assertEqual(ri.extract_text({"owner_text": "好", "decisions": ["d"]}), ("owner_text", "好"))
        self.assertEqual(ri.extract_text({"answers": [{"question": "q", "exact_answer": "a"}]})[0], "answers")
        self.assertEqual(ri.extract_text({"exact_answer": "少赚不超过 3 个点"}), ("exact_answer", "少赚不超过 3 个点"))
        self.assertEqual(ri.local_time("2026-10-11"), ("2026-10-11", None))  # recorded_on / local_date style: date only
        self.assertEqual(ri.local_time("2026-10-09T04:20:00-04:00 (approx)"), ("2026-10-09", 4))
        est = lambda t: t.astimezone(timezone(timedelta(hours=-5)))  # after 11-01; the real function follows the machine
        edt = lambda t: t.astimezone(timezone(timedelta(hours=-4)))
        with mock.patch.object(ri, "_to_local", edt):
            self.assertEqual(ri.local_time("2026-10-04T21:43:43.103Z"), ("2026-10-04", 17))
            self.assertEqual(ri.local_time("2026-09-29T02:45:46.353375+00:00"), ("2026-09-28", 22))
        with mock.patch.object(ri, "_to_local", est):
            self.assertEqual(ri.local_time("2026-11-02T03:10:00Z"), ("2026-11-01", 22))
        self.assertEqual(ri.local_time("2026-10-03 19:4x (EDT)"), ("2026-10-03", 19))
        self.assertEqual(ri.local_time("2026-10-06 ~00:45 EDT"), ("2026-10-06", 0))
        self.assertEqual(ri.local_time("2026-10-04 (America/New_York, early)"), ("2026-10-04", None))


if __name__ == "__main__":
    unittest.main()
