import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import backlog_io
import backlog_ops
import tick


class BacklogOpsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "backlog.md"
        self.log = self.root / "coordinator-log.md"
        self.before = ("# 待办\r\n## 组织线\r\n权重：22%\r\n"
                       "- [review] 组织：样例 待补说明\r\n\r\n备注留原处\r\n\r\n"
                       "## 生活线\r\n- [todo] 组织：样例 别的小节\r\n").encode("utf-8")
        self.path.write_bytes(self.before)
        self.log.write_bytes(b"existing\r\n")
        # parse_backlog can emit scheduler diagnostics; copies must stay isolated.
        patcher = mock.patch.object(tick, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def apply(self, **kwargs):
        return backlog_ops.apply("组织线", path=self.path, log_path=self.log, log="- 一条记录\n", **kwargs)

    def reject(self, **kwargs):
        before, log = self.path.read_bytes(), self.log.read_bytes()
        with self.assertRaises(ValueError):
            self.apply(**kwargs)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.log.read_bytes(), log)
        self.assertFalse(self.path.with_name("backlog.md.lock").exists())
        self.assertFalse(self.path.with_name("backlog-history").exists())

    def test_combined_edit_backup_crlf_insertion_and_log(self):
        backup = self.apply(
            states=[("- [review] 组织：样例", "done 11-02：通过")],
            replacements=[("- [done 11-02：通过] 组织：样例", "待补说明", "已补说明")],
            additions=["- [todo] 组织：后续一", "- [blocked: 等审查] 组织：后续二"],
        )
        expected = self.before.replace("- [review] 组织：样例 待补说明".encode("utf-8"),
            ("- [done 11-02：通过] 组织：样例 已补说明\r\n- [todo] 组织：后续一\r\n"
             "- [blocked: 等审查] 组织：后续二").encode("utf-8"))
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertEqual(backup.read_bytes(), self.before)
        self.assertEqual(self.log.read_bytes(), b"existing\r\n" + "- 一条记录\n".encode("utf-8"))

    def test_zero_or_multiple_row_matches_do_not_write_even_after_valid_edit(self):
        self.reject(replacements=[("- [review] 组织：样例", "待补说明", "first"), ("missing", "x", "y")])
        self.path.write_bytes(self.before.replace(b"\r\n\r\n", "\r\n- [review] 组织：样例 第二条\r\n\r\n".encode("utf-8"), 1))
        self.reject(states=[("- [review] 组织：样例", "done")])

    def test_missing_or_duplicate_heading_does_not_write(self):
        for data in (self.before.replace("## 组织线".encode("utf-8"), b"## other"),
                     self.before + "## 组织线\r\n".encode("utf-8")):
            self.path.write_bytes(data)
            self.reject(additions=["- [todo] 组织：新行"])

    def test_literal_replacement_requires_exactly_one_occurrence(self):
        self.reject(replacements=[("- [review] 组织：样例", "absent", "new")])
        self.path.write_bytes(self.before.replace("待补说明".encode("utf-8"), "待补说明 待补说明".encode("utf-8")))
        self.reject(replacements=[("- [review] 组织：样例", "待补说明", "new")])

    def test_duplicates_detected_across_sections_and_statuses_and_within_additions(self):
        self.reject(additions=["- [todo] 组织：样例 待补说明"])
        self.reject(additions=["- [review] 组织：样例 别的小节"])
        self.reject(additions=["- [todo] 组织：新行", "- [done] 组织：新行"])

    def test_stable_duplicate_key_and_replay_are_rejected(self):
        self.reject(additions=["- [todo] 组织：样例 新要求"], duplicate_keys=["组织：样例"])
        self.apply(additions=["- [todo] 组织：新行"])
        before, log = self.path.read_bytes(), self.log.read_bytes()
        with self.assertRaises(ValueError):
            self.apply(additions=["- [todo] 组织：新行"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.log.read_bytes(), log)

    def test_lf_mixed_and_unterminated_line_endings(self):
        for ending, terminal in (("\n", "\n"), ("\r\n", "\r\n"), ("\n", ""), ("\r\n", "")):
            with self.subTest(ending=ending, terminal=terminal):
                self.path.write_bytes((f"## 组织线{ending}- [todo] 组织：旧行{terminal}").encode("utf-8"))
                self.apply(additions=["- [todo] 组织：新行"])
                expected = (f"## 组织线{ending}- [todo] 组织：旧行{ending}- [todo] 组织：新行{terminal}"
                            if terminal else f"## 组织线{ending}- [todo] 组织：旧行\n- [todo] 组织：新行{ending[:-1]}")
                self.assertEqual(self.path.read_bytes(), expected.encode("utf-8"))
        self.path.write_bytes("## 组织线\r\n- [review] 组织：旧行\n\n## 其他\r\n".encode("utf-8"))
        self.apply(states=[("- [review]", "done")], additions=["- [todo] 组织：新行"])
        self.assertEqual(self.path.read_bytes(),
            "## 组织线\r\n- [done] 组织：旧行\n- [todo] 组织：新行\r\n\n## 其他\r\n".encode("utf-8"))

    def test_append_to_empty_section_keeps_metadata_and_next_section(self):
        self.path.write_bytes("## 组织线\r\n权重：22%\r\n\r\n## 其他\r\n".encode("utf-8"))
        self.apply(additions=["- [todo] 组织：新行"])
        self.assertEqual(self.path.read_bytes(),
            "## 组织线\r\n权重：22%\r\n- [todo] 组织：新行\r\n\r\n## 其他\r\n".encode("utf-8"))

    def test_bad_newlines_states_and_non_task_lines_are_rejected(self):
        for kwargs in ({"states": [("- [review]", "done] swallowed")]},
                       {"states": [("权重", "done")]}, {"additions": ["## injected"]},
                       {"additions": ["- [todo] 组织：x\nnext"]},
                       {"replacements": [("- [review]", "待补说明", "x\r\ny")]},
                       {"replacements": [("- [review]", "待补说明", "")] , "duplicate_keys": ["missing"]}):
            self.reject(**kwargs)

    def test_required_backup_failure_does_not_write_or_log(self):
        with mock.patch.object(backlog_io, "_snapshot", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.apply(states=[("- [review]", "done")])
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.log.read_bytes(), b"existing\r\n")

    def test_edit_owns_lock_during_parse_and_backup(self):
        real_parse = tick.parse_backlog
        def parse(text):
            self.assertTrue(self.path.with_name("backlog.md.lock").exists())
            return real_parse(text)
        with mock.patch.object(backlog_io, "edit", wraps=backlog_io.edit) as edit, \
                mock.patch.object(tick, "parse_backlog", side_effect=parse) as parser:
            self.apply(states=[("- [review]", "done")])
        edit.assert_called_once()
        parser.assert_called_once()

    def test_parser_failure_leaves_backlog_and_log_unchanged(self):
        with mock.patch.object(tick, "parse_backlog", side_effect=ValueError("bad backlog")):
            self.reject(states=[("- [review]", "done")])

    def test_concurrent_writers_preserve_all_rows_and_log_lines(self):
        errors = []
        def add(n):
            try:
                self.apply(additions=[f"- [todo] 组织：并发 {n}"])
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=add, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        data = self.path.read_bytes().decode("utf-8")
        for n in range(6):
            self.assertEqual(data.count(f"- [todo] 组织：并发 {n}\r\n"), 1)
        self.assertEqual(self.log.read_text(encoding="utf-8").count("- 一条记录\n"), 6)


if __name__ == "__main__":
    unittest.main()
