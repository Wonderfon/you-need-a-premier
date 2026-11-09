import json
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backlog_io as b  # noqa: E402

HEAD = "# 待办\n\n规则一行\n\n## 工作线\n\n- [todo] a\n"


class BacklogIO(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "backlog.md"
        self.addCleanup(self.tmp.cleanup)

    def text(self):
        return self.path.read_bytes().decode("utf-8")

    def test_creates_section_after_header_block(self):
        self.path.write_bytes(HEAD.encode("utf-8"))
        b.insert_owner_item("- [todo] X", self.path)
        self.assertEqual(self.text(), "# 待办\n\n规则一行\n\n## 你的指令（最优先）\n- [todo] X\n\n## 工作线\n\n- [todo] a\n")

    def test_fifo_within_existing_section(self):
        self.path.write_bytes(HEAD.encode("utf-8"))
        for n in "XYZ":
            b.insert_owner_item(f"- [todo] {n}", self.path)
        t = self.text()
        self.assertEqual(t.count("## 你的指令（最优先）"), 1)
        self.assertLess(t.index("] X"), t.index("] Y"))
        self.assertLess(t.index("] Y"), t.index("] Z"))
        self.assertLess(t.index("] Z"), t.index("## 工作线"))
        self.assertIn("] Z\n\n## 工作线", t)

    def test_crlf_preserved(self):
        self.path.write_bytes(HEAD.replace("\n", "\r\n").encode("utf-8"))
        b.insert_owner_item("- [todo] X", self.path)
        b.insert_owner_item("- [todo] Y", self.path)
        t = self.text()
        self.assertNotRegex(t, r"(?<!\r)\n")
        self.assertIn("## 你的指令（最优先）\r\n- [todo] X\r\n- [todo] Y\r\n\r\n## 工作线", t)

    def test_no_headings_appends_section_at_end(self):
        self.path.write_bytes("# t\n".encode("utf-8"))
        b.insert_owner_item("- [todo] X", self.path)
        self.assertIn("## 你的指令（最优先）\n- [todo] X", self.text())

    def test_rewrite_line_and_crlf(self):
        self.path.write_bytes(b"- [todo] a\r\n- [todo] b\r\n")
        self.assertTrue(b.rewrite_line("- [todo] a", "- [review] a", self.path))
        self.assertEqual(self.path.read_bytes(), b"- [review] a\r\n- [todo] b\r\n")
        self.assertFalse(b.rewrite_line("- [todo] zzz", "x", self.path))

    def test_clear_seen_owner_card_from_recent_record_and_preserve_crlf(self):
        from datetime import datetime
        records = Path(self.tmp.name) / "records"
        records.mkdir()
        (records / "2026-10-29-owner-reply.json").write_text(
            '{"context":"approved audits/sample-page.html"}', encoding="utf-8")
        row = "- [done 10-28；给 owner\x2010-28：看样例，audits/sample-page.html] Writing：样例页"
        self.path.write_bytes((row + "\r\n").encode("utf-8"))

        changed = b.clear_seen_owner_cards(records, self.path, datetime(2026, 10, 30, 12))

        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0][0], 1)
        self.assertEqual(changed[0][2], "- [done 10-28；owner 已看 10-29（records\x2f2026-10-29-owner-reply.json）] Writing：样例页")
        self.assertEqual(self.path.read_bytes(), (changed[0][2] + "\r\n").encode("utf-8"))

    def test_clear_seen_owner_card_without_matching_filename_is_unchanged(self):
        from datetime import datetime
        records = Path(self.tmp.name) / "records"
        records.mkdir()
        (records / "2026-10-29-owner-reply.json").write_text(
            '{"context":"owner reviewed input/output pairs"}', encoding="utf-8")
        row = "- [done 10-28；给 owner\x2010-28：看样例，audits/arbitros-workspace-ui/pairs] Arbitros：对比图"
        self.path.write_bytes((row + "\n").encode("utf-8"))
        before = self.path.read_bytes()

        self.assertEqual(b.clear_seen_owner_cards(records, self.path, datetime(2026, 10, 30, 12)), [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_clear_seen_owner_card_ignores_records_older_than_14_days(self):
        from datetime import datetime
        records = Path(self.tmp.name) / "records"
        records.mkdir()
        (records / "2026-10-16-owner-reply.json").write_text(
            '{"context":"approved audits/sample-page.html"}', encoding="utf-8")
        row = "- [done 10-15；给 owner\x2010-15：看样例，audits/sample-page.html] Writing：样例页"
        self.path.write_bytes((row + "\n").encode("utf-8"))
        before = self.path.read_bytes()

        self.assertEqual(b.clear_seen_owner_cards(records, self.path, datetime(2026, 10, 30, 12)), [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_clear_seen_owner_card_is_idempotent(self):
        from datetime import datetime
        records = Path(self.tmp.name) / "records"
        records.mkdir()
        (records / "2026-10-29-owner-reply.json").write_text(
            '{"context":"approved wt/sample/page.html"}', encoding="utf-8")
        row = "- [done 10-28；给 owner\x2010-28：看样例，C:\\orghome\\wt\\sample\\page.html] Writing：样例页"
        self.path.write_bytes((row + "\n").encode("utf-8"))
        now = datetime(2026, 10, 30, 12)

        self.assertEqual(len(b.clear_seen_owner_cards(records, self.path, now)), 1)
        after = self.path.read_bytes()
        self.assertEqual(b.clear_seen_owner_cards(records, self.path, now), [])
        self.assertEqual(self.path.read_bytes(), after)

    def clear_once(self, record_text, card_path, card_day="10-28", record_name="2026-10-29-owner-reply.json"):
        from datetime import datetime
        records = Path(self.tmp.name) / "records"
        records.mkdir(exist_ok=True)
        (records / record_name).write_text(json.dumps({"context": record_text}), encoding="utf-8")
        self.path.write_bytes(f"- [done {card_day}；给 owner {card_day}：看，{card_path}] X：y\n".encode("utf-8"))
        return b.clear_seen_owner_cards(records, self.path, datetime(2026, 10, 30, 12))

    def test_substring_of_another_file_name_is_not_a_hit(self):
        # the review's 928 case: the record names ...-owner-guide.html, the card cites audits/<dir>/guide.html
        self.assertEqual(self.clear_once("see audits/2026-10-25-org-core-v01-owner-guide.html",
                                         "audits/2026-10-26-bl-blog-real/guide.html"), [])

    def test_generic_file_name_alone_is_not_a_hit(self):
        # the 921 case: the record mentions some other README.html; only a full path counts
        self.assertEqual(self.clear_once("README.html and wt/other-pack/README.html",
                                         "wt\\kiki-reading-pack\\reading-pack\\README.html"), [])
        self.assertEqual(self.clear_once("audits/README.md", "audits/README.md"), [])  # generic, no directory below audits
        self.assertEqual(len(self.clear_once("opened wt/kiki-pack/pack/README.html",
                                             "C:\\orghome\\wt\\kiki-pack\\pack\\README.html")), 1)  # full path, slashes ignored

    def test_path_boundary_is_respected(self):
        self.assertEqual(self.clear_once("audits/sample-page.html.bak", "audits/sample-page.html"), [])
        self.assertEqual(self.clear_once("myaudits/sample-page.html", "audits/sample-page.html"), [])

    def test_record_older_than_the_card_is_not_a_hit(self):
        self.assertEqual(self.clear_once("audits/sample-page.html", "audits/sample-page.html", card_day="10-29",
                                         record_name="2026-10-28-owner-reply.json"), [])
        self.assertEqual(len(self.clear_once("audits/sample-page.html", "audits/sample-page.html", card_day="10-29",
                                             record_name="2026-10-29-owner-reply.json")), 1)  # same day counts

    def test_hourly_throttle(self):
        records = Path(self.tmp.name) / "records"
        records.mkdir()
        (records / "2026-10-29-owner-reply.json").write_text('{"context":"audits/sample-page.html"}', encoding="utf-8")
        self.path.write_bytes("- [done 10-28；给 owner\x2010-28：看，audits/sample-page.html] X\n".encode("utf-8"))
        from datetime import datetime
        kw = dict(records_dir=records, path=self.path, now=datetime(2026, 10, 30, 12))
        with unittest.mock.patch.object(b, "_last_clear", [float("-inf")]):
            first = b.clear_seen_owner_cards_hourly(1000.0, **kw)
            self.path.write_bytes("- [done 10-28；给 owner\x2010-28：看，audits/sample-page.html] X\n".encode("utf-8"))
            self.assertEqual(b.clear_seen_owner_cards_hourly(1000.0 + 3599, **kw), [])
            again = b.clear_seen_owner_cards_hourly(1000.0 + 3600, **kw)
        self.assertEqual((len(first), len(again)), (1, 1))

    def test_scheduler_step_clears_owner_cards_each_round_not_only_at_start(self):
        import tick
        with unittest.mock.patch.object(tick.backlog_io, "clear_seen_owner_cards_hourly") as clear, \
                unittest.mock.patch.object(tick, "spawn"), unittest.mock.patch.object(tick, "backup_watch"),unittest.mock.patch.object(tick, "website_watch"), unittest.mock.patch.object(tick, "machine_watch_step"), \
                unittest.mock.patch.object(tick.dots_watch, "step"), unittest.mock.patch.object(tick.dots_review, "step"), \
                unittest.mock.patch.object(tick, "fresh_usage", return_value=None), \
                unittest.mock.patch.object(tick, "latest_usage", return_value=None), \
                unittest.mock.patch.object(tick, "BACKLOG", self.path):
            self.path.write_bytes(HEAD.encode("utf-8"))
            st = {"sample": None, "spawned": {}, "aux": {}}
            tick.supervise_step(st, 1000.0)
            tick.supervise_step(st, 1030.0)
        self.assertEqual(clear.call_count, 2)  # called every round; the hour limit lives in backlog_io

    def test_lock_is_exclusive_and_released(self):
        self.path.write_bytes(HEAD.encode("utf-8"))
        lock_file = self.path.with_name("backlog.md.lock")
        with b.lock(self.path):
            self.assertTrue(lock_file.exists())
            with self.assertRaises(TimeoutError):
                with b.lock(self.path, timeout=0.2):
                    pass
        self.assertFalse(lock_file.exists())

    def test_stale_lock_is_broken(self):
        lock_file = self.path.with_name("backlog.md.lock")
        lock_file.write_text("")
        old = time.time() - 120
        os.utime(lock_file, (old, old))
        with b.lock(self.path, timeout=1):
            pass
        self.assertFalse(lock_file.exists())

    def test_concurrent_inserts_all_land(self):
        self.path.write_bytes(HEAD.encode("utf-8"))
        threads = [threading.Thread(target=b.insert_owner_item, args=(f"- [todo] n{i}", self.path)) for i in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sum(f"] n{i}\n" in self.text() for i in range(12)), 12)

    def test_history_keeps_the_newest_50_copies_of_the_old_file(self):
        self.path.write_bytes(HEAD.encode("utf-8"))
        for i in range(55):
            b.rewrite_line(f"- [todo] a" if i == 0 else f"- [todo] a{i - 1}", f"- [todo] a{i}", self.path)
        history = sorted((self.path.parent / "backlog-history").glob("backlog-*.md"))
        self.assertEqual(len(history), 50)
        self.assertTrue(history[0].name.endswith("Z.md"))
        self.assertIn("- [todo] a53\n", history[-1].read_text(encoding="utf-8"))  # the file as it was before the last write
        self.assertIn("- [todo] a4\n", history[0].read_text(encoding="utf-8"))  # the 6th write's input; the first five are pruned

    def test_history_keeps_write_order_after_pruning_with_stopped_or_reversed_clock(self):
        from datetime import datetime as D, timedelta as T, timezone as Z
        start = D(2026, 10, 31, 23, 59, 59, tzinfo=Z.utc)
        forward = [start + T(microseconds=i) for i in range(55)]
        for label, times in (
            ("stopped", [start] * 110),
            ("backward", [start - T(seconds=i) for i in range(110)]),
            ("rollback_after_pruning", forward + [start - T(days=1)] * 55),
        ):
            with self.subTest(clock=label):
                folder = self.path.parent / label
                folder.mkdir()
                path = folder / "backlog.md"
                path.write_text("- [todo] a\n", encoding="utf-8")
                with self.clock(*times):
                    for i in range(len(times)):
                        old = "- [todo] a" if i == 0 else f"- [todo] a{i - 1}"
                        self.assertTrue(b.rewrite_line(old, f"- [todo] a{i}", path))
                        if i >= 50:
                            history = sorted(folder.joinpath("backlog-history").glob("backlog-*.md"))
                            self.assertEqual([p.read_text(encoding="utf-8") for p in history],
                                             [f"- [todo] a{n}\n" for n in range(i - 50, i)])
                self.assertEqual(path.read_text(encoding="utf-8"), f"- [todo] a{len(times) - 1}\n")

    def test_history_logical_stamp_crosses_month_but_daily_uses_actual_utc_day(self):
        from datetime import datetime as D, timezone as Z
        self.path.write_text("- [todo] a\n", encoding="utf-8")
        end = D(2026, 10, 31, 23, 59, 59, 999999, tzinfo=Z.utc)
        with self.clock(end, end, D(2026, 11, 1, tzinfo=Z.utc)):
            for old, new in (("a", "b"), ("b", "c"), ("c", "d")):
                self.assertTrue(b.rewrite_line(f"- [todo] {old}", f"- [todo] {new}", self.path))
        history = self.path.parent / "backlog-history"
        copies = sorted(history.glob("backlog-*.md"))
        self.assertEqual([p.name for p in copies], ["backlog-20261031T235959.999999Z.md",
                         "backlog-20261101T000000.000000Z.md", "backlog-20261101T000000.000001Z.md"])
        self.assertEqual([p.read_text(encoding="utf-8") for p in copies],
                         [f"- [todo] {letter}\n" for letter in "abc"])
        daily = history / "daily"
        self.assertEqual(sorted(p.name for p in daily.iterdir()), ["backlog-20261031.md", "backlog-20261101.md"])
        self.assertEqual((daily / "backlog-20261031.md").read_text(encoding="utf-8"), "- [todo] a\n")
        self.assertEqual((daily / "backlog-20261101.md").read_text(encoding="utf-8"), "- [todo] c\n")

    def clock(self, *times):
        """backlog_io.datetime whose now() walks through `times` (the last one repeats)."""
        it = iter(times)
        state = {"last": None}

        class Fake(b.datetime):
            @classmethod
            def now(cls, tz=None):
                state["last"] = next(it, state["last"])
                return state["last"]
        return unittest.mock.patch.object(b, "datetime", Fake)

    def test_history_keeps_each_days_first_copy_for_14_days(self):
        from datetime import datetime as D, timedelta as T, timezone as Z
        self.path.write_bytes(HEAD.encode("utf-8"))
        day = lambda n, h: D(2026, 10, n, h, 0, 0, tzinfo=Z.utc)
        daily = self.path.parent / "backlog-history" / "daily"
        daily.mkdir(parents=True)
        (daily / "backlog-20260920.md").write_text("old", encoding="utf-8")  # 21 days before 10-11
        (daily / "backlog-20260928.md").write_text("edge", encoding="utf-8")  # 13 days: kept
        (daily / "backlog-20260927.md").write_text("cutoff", encoding="utf-8")  # 14 days before 10-11: the cutoff day itself is kept
        times = [day(9, 1), day(9, 5), day(10, 2), day(10, 9), day(10, 20), day(11, 3), day(11, 4)]
        with self.clock(*times):
            for i in range(7):
                b.rewrite_line("- [todo] a" if i == 0 else f"- [todo] a{i - 1}", f"- [todo] a{i}", self.path)
        names = sorted(p.name for p in daily.glob("backlog-*.md"))
        self.assertEqual(names, ["backlog-20260927.md", "backlog-20260928.md", "backlog-20261009.md", "backlog-20261010.md", "backlog-20261011.md"])
        # exactly one per day, and it is the file as it was before that day's first write (i = 0, 2, 5)
        self.assertIn("- [todo] a\n", (daily / "backlog-20261009.md").read_text(encoding="utf-8"))
        self.assertIn("- [todo] a1\n", (daily / "backlog-20261010.md").read_text(encoding="utf-8"))
        self.assertIn("- [todo] a4\n", (daily / "backlog-20261011.md").read_text(encoding="utf-8"))
        self.assertEqual(len(list((self.path.parent / "backlog-history").glob("backlog-*.md"))), 7)  # the recent-50 copies unchanged

    def test_history_names_never_collide_and_keep_write_order(self):
        from datetime import datetime as D, timezone as Z
        self.path.write_bytes(HEAD.encode("utf-8"))
        same = D(2026, 10, 11, 1, 2, 3, 456789, tzinfo=Z.utc)  # a clock too coarse to tell the writes apart
        with self.clock(same):
            for i in range(5):
                b.rewrite_line("- [todo] a" if i == 0 else f"- [todo] a{i - 1}", f"- [todo] a{i}", self.path)
        history = sorted((self.path.parent / "backlog-history").glob("backlog-*.md"))
        self.assertEqual(len(history), 5)
        for i, f in enumerate(history):  # each copy is the file before write i
            self.assertIn("- [todo] a\n" if i == 0 else f"- [todo] a{i - 1}\n", f.read_text(encoding="utf-8"))
    def test_malformed_rows_are_logged_once_a_day_and_not_fixed(self):
        import tick
        bad = "- [todo]（6b 10-05 已通过） Uvox：6c 乌贼与 agent 页视觉"
        late = "- [todo] 写 Uvox：项目名不在行首"
        text = f"## 工作线\n{bad}\n{late}\n- [todo] Uvox：正常\n- [todo] 组织：正常\n## 你的指令（最优先）\n- [x 坏的但在 owner 小节\n"
        with tempfile.TemporaryDirectory() as d, unittest.mock.patch.object(tick, "DATA", Path(d)), \
                unittest.mock.patch.object(tick, "LOG_FILE", Path(d) / "tick-log.jsonl"):
            for _ in range(3):
                got = tick.parse_backlog(text)
            logged = [json.loads(x) for x in (Path(d) / "tick-log.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(x["decision"], x["row"]) for x in logged],
                             [("malformed", bad[:60]), ("malformed", late[:60])])  # once each, 3 parses
            self.assertEqual([i for _, i in got["工作线"]["todo"]], [late[len("- [todo] "):], "Uvox：正常", "组织：正常"])
            (Path(d) / "malformed-seen.json").write_text(json.dumps({bad[:60]: "2020-01-01"}), encoding="utf-8")
            tick.parse_backlog(text)  # the file now holds only yesterday's entry: both rows log again today
            self.assertEqual(len((Path(d) / "tick-log.jsonl").read_text(encoding="utf-8").splitlines()), 4)


if __name__ == "__main__":
    unittest.main()
