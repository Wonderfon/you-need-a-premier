import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backlog_io
import pause_projects as pp
import tick


class PauseProjects(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "backlog.md"
        for name in ("note_malformed", "note_window_skipped"):
            patch = mock.patch.object(tick, name)
            patch.start()
            self.addCleanup(patch.stop)

    def test_roundtrip_preserves_bytes_and_other_states_and_sections(self):
        for newline in ("\n", "\r\n"):
            for final_newline in (True, False):
                with self.subTest(newline=newline, final_newline=final_newline):
                    text = ("# 待办\n## 你的指令（最优先）\n- [todo] FOS：owner 原话\n"
                            "## Arbitros 线\n权重：24%\n冲刺：FactoryAlign\n\n"
                            "- [todo] FOS：做甲\n- [blocked: 等你决定] FOS：等选择\n"
                            "- [blocked: 等上一步] Arbitros 官网：等通过\n- [review] FOS：审完\n"
                            "- [running 10:00] FOS：在跑\n- [done] Arbitros 官网：已完成\n"
                            "- [todo] Arbitros 官网：做乙\n- [todo] FactoryAlign：照常\n"
                            "## 生活线\n- [todo] FOS：另一节\n- [todo] 写作模型：继续")
                    before = (text + ("\n" if final_newline else "")).replace("\n", newline).encode("utf-8")
                    self.path.write_bytes(before)
                    pp.apply("pause", "Arbitros 线", path=self.path)
                    paused = self.path.read_bytes()
                    self.assertEqual(paused.decode("utf-8").count("暂停本项目"), 2)
                    parsed = tick.parse_backlog(paused.decode("utf-8"))
                    self.assertEqual(parsed["Arbitros 线"]["weight"], 24)
                    self.assertEqual([item for _, item in parsed["Arbitros 线"]["todo"]], ["FactoryAlign：照常"])
                    pp.apply("pause", "Arbitros 线", path=self.path)
                    self.assertEqual(self.path.read_bytes(), paused)
                    pp.apply("resume", "Arbitros 线", path=self.path)
                    self.assertEqual(self.path.read_bytes(), before)

    def test_umzy_is_on_the_beacon_listener_pause_list_not_the_arbitros_one(self):  # owner\x2011-07: Umzy moved lines
        lists = pp.CONFIG["sections"]
        self.assertNotIn("Umzy", lists["Arbitros 线"])
        self.assertIn("Umzy", lists["Beacon Listener 线"])
        self.path.write_bytes(("## Arbitros 线\n- [todo] Umzy：不归这里\n## Beacon Listener 线\n- [todo] Umzy：做甲\n- [todo] Uvox：做乙\n").encode("utf-8"))
        pp.apply("pause", "Arbitros 线", path=self.path)
        self.assertNotIn("暂停本项目", self.path.read_text(encoding="utf-8"))
        pp.apply("pause", "Beacon Listener 线", path=self.path)
        parsed = tick.parse_backlog(self.path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["Beacon Listener 线"]["todo"], [])
        self.assertEqual([i for _, i in parsed["Arbitros 线"]["todo"]], ["Umzy：不归这里"])

    def test_updates_existing_summary_and_resumes_only_selected_markers(self):
        source = "owner\x2011-02 15:5x 终端，records/example.json"
        note = "Thinking Engines（写作模型不停，owner 说继续）"
        original = (f"## 生活线\n权重：24%\n暂停项目（{source}）：{note}。不催。\n"
                    "- [todo] 驾校模拟器：检查\n- [blocked: 等你] 驾校模拟器：原有\n"
                    "- [todo] 写作模型：继续\n")
        self.path.write_bytes(original.encode("utf-8"))
        pp.apply("pause", "生活线", ("驾校模拟器",), self.path)
        self.assertEqual(pp.paused_projects(self.path.read_text(encoding="utf-8")),
                         {"生活线": ["Thinking Engines", "驾校模拟器"]})
        pp.apply("resume", "生活线", ("驾校模拟器",), self.path)
        self.assertEqual(self.path.read_bytes(), original.encode("utf-8"))

    def test_resumes_existing_owner_pause_format_and_removes_empty_summary(self):
        state = f"blocked: owner\x2011-02 15:5x 暂停本项目（原话出处）{pp.PAUSE_END}"
        self.path.write_bytes((f"## 生活线\n暂停项目（原话）：项目七\n- [{state}] 项目七：任务\n"
                               "- [blocked: 手写暂停本项目] 项目七：其他\n").encode("utf-8"))
        pp.apply("resume", "生活线", ("项目七",), self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"),
                         "## 生活线\n- [todo] 项目七：任务\n- [blocked: 手写暂停本项目] 项目七：其他\n")

    def test_lock_backup_and_parser_and_refuses_write_if_backup_fails(self):
        before = "## 生活线\n- [todo] 项目七：任务\n".encode("utf-8")
        self.path.write_bytes(before)
        snapshot = backlog_io._snapshot

        def checked(path, **kwargs):
            self.assertTrue(path.with_name(path.name + ".lock").exists())
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(kwargs["required"])
            return snapshot(path, **kwargs)

        with mock.patch.object(backlog_io, "_snapshot", side_effect=checked), \
                mock.patch.object(tick, "parse_backlog", wraps=tick.parse_backlog) as parse:
            pp.apply("pause", "生活线", path=self.path)
        self.assertEqual(parse.call_count, 2)
        copies = list((self.path.parent / "backlog-history").glob("backlog-*.md"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), before)
        paused = self.path.read_bytes()
        with mock.patch.object(backlog_io, "_snapshot", side_effect=OSError("backup failed")):
            with self.assertRaisesRegex(OSError, "backup failed"):
                pp.apply("resume", "生活线", path=self.path)
        self.assertEqual(self.path.read_bytes(), paused)
        self.assertFalse(self.path.with_name(self.path.name + ".lock").exists())

    def test_invalid_scope_or_duplicate_summary_changes_nothing(self):
        self.path.write_bytes("## 生活线\n暂停项目：项目七\n暂停项目：驾校模拟器\n- [todo] 项目七：甲\n".encode("utf-8"))
        before = self.path.read_bytes()
        for action, section, projects in (("pause", "缺失", None), ("pause", "生活线", ("写作模型",)),
                                          ("oops", "生活线", None), ("pause", "生活线", None)):
            with self.assertRaises(ValueError):
                pp.apply(action, section, projects, self.path)
            self.assertEqual(self.path.read_bytes(), before)

    def test_both_planning_prompts_exclude_paused_projects(self):
        for kind in ("plan", "codex_plan"):
            prompt = tick.PROMPTS[kind].format(name="副总理", section="生活线")
            self.assertIn("本小节「暂停项目」行里的项目不补行、不从长期池取。", prompt)


if __name__ == "__main__":
    unittest.main()
