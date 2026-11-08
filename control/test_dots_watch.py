import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dots_watch  # noqa: E402
import tick  # noqa: E402

NOW = datetime(2026, 10, 25, 23, 30)
TAG = dots_watch.DOTS_TAG
ITEM = f"组织：只读调研 X 的公开文档，每条给 URL；不登录、不注册、不花钱 {TAG}"
DEADLINE_ITEM = f"组织：只读调研 Y {TAG}〔截止：10-26 05:00〕"


class DotsWatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.drive, self.backlog = d / "drive", d / "backlog.md"
        (self.drive / "tasks").mkdir(parents=True)
        (self.drive / "log.md").write_text("# log\n\n- old line\n", encoding="utf-8")
        p = mock.patch.object(dots_watch, "DRIVE", self.drive)
        p.start()
        self.addCleanup(p.stop)
        self.logged = []
        self.set_rows(f"- [todo] {ITEM}")

    def set_rows(self, *rows):
        text = "\r\n".join(["# backlog", "## 组织线", "权重: 20%", *rows, "## 你的指令（最优先）", f"- [todo] 【Discord x】{ITEM}", ""])
        self.backlog.write_bytes(text.encode("utf-8"))

    def step(self, now=NOW):
        dots_watch.step(self.backlog, lambda *a, **k: self.logged.append((a, k)), now)

    def text(self):
        return self.backlog.read_bytes().decode("utf-8")

    def task(self, tid, first, extra=""):
        (self.drive / "tasks" / f"{tid}.md").write_text(first + "\n\n# t\n" + extra, encoding="utf-8")

    def row(self, status, item=ITEM):
        return f"- [{status}] {item}"

    def test_send(self):
        self.task("T-20261025-02", "status: done 2026-10-25T20:05:53+00:00")  # the next number follows the highest existing one
        self.step()
        first, _, body = (self.drive / "tasks" / "T-20261025-03.md").read_text(encoding="utf-8").partition("\n")
        self.assertEqual(first, "status: new")
        self.assertIn(ITEM, body)
        self.assertIn("results/T-20261025-03/", body)
        self.assertIn("2026-10-26 05:30", body)  # default 6 h
        self.assertTrue((self.drive / "log.md").read_text(encoding="utf-8").splitlines()[-1].split(" | ")[1:3] == ["T-20261025-03", "new"])
        text = self.text()
        self.assertIn(self.row("running dotz T-20261025-03 截止 10-26 05:30"), text)
        self.assertIn("\r\n", text)
        self.assertNotIn("\n", text.replace("\r\n", ""))  # CRLF kept on every line
        self.assertIn(f"- [todo] 【Discord x】{ITEM}", text)  # the owner section is not touched
        self.step()  # a second look sends nothing more
        self.assertEqual(len(list((self.drive / "tasks").glob("T-*"))), 2)

    def test_deadline_tag(self):
        self.set_rows(self.row("todo", DEADLINE_ITEM))
        self.step()
        self.assertIn(self.row("running dotz T-20261025-01 截止 10-26 05:00", DEADLINE_ITEM), self.text())

    def test_quoted_tags_do_not_assign_or_set_deadlines(self):
        quoted = f"「另一行{TAG}〔截止：10-25 12:00〕」"
        for item in (f"组织：引用{quoted}", f"组织：引用「{TAG}〔截止：10-25 12:00〕」"):
            with self.subTest(item=item):
                self.assertFalse(dots_watch.tagged(item))
                self.assertFalse(dots_watch.misplaced_tag(item))
                self.assertEqual(dots_watch.deadline_of(item, NOW), datetime(2026, 10, 26, 5, 30))
        item = f"组织：引用{quoted}，公开检索{TAG}〔截止：10-26 05:00〕"
        self.assertTrue(dots_watch.tagged(item))
        self.assertFalse(dots_watch.misplaced_tag(item))
        self.assertEqual(dots_watch.deadline_of(item, NOW), datetime(2026, 10, 26, 5))
        self.assertEqual(dots_watch._plain(item), f"组织：引用{quoted}，公开检索")
        self.set_rows(self.row("todo", item))
        self.step()
        self.assertIn(self.row("running dotz T-20261025-01 截止 10-26 05:00", item), self.text())
        # A trailing quote stays body text, so a preceding dotz tag remains misplaced.
        self.assertFalse(dots_watch.tagged(f"组织：公开检索{TAG}{quoted}"))
        self.assertTrue(dots_watch.misplaced_tag(f"组织：公开检索{TAG}{quoted}"))

    def test_running_row_is_invisible_to_tick(self):
        self.step()
        text = self.text()
        self.assertEqual(tick.running_rows(text), [])  # not a `running HH:MM` row: recover_running leaves it
        sec = tick.parse_backlog(text)["组织线"]
        self.assertEqual((sec["todo"], sec["review"]), ([], None))
        self.set_rows(self.row("todo"))
        sec = tick.parse_backlog(self.text())["组织线"]  # a todo that has not been sent yet is not taken either
        self.assertEqual(sec["todo"], [])
        self.assertIsNone(tick.codex_first_todo(sec, tick.project_of(ITEM)))

    def test_done(self):
        self.step()
        self.task("T-20261025-01", "status: done 2026-10-26T01:00:00+00:00")
        self.step()
        text = self.text()
        self.assertIn("- [review] 组织：只读调研 X", text)
        self.assertIn("results/T-20261025-01/", text)
        self.assertNotIn(TAG, text.split("## 你的指令")[0])

    def test_blocked(self):
        self.step()
        self.task("T-20261025-01", "status: blocked 页面要登录 [403]")
        self.step()
        self.assertIn("- [blocked: dotz T-20261025-01 阻塞：页面要登录  403 ] 组织：只读调研 X", self.text())

    def test_timeout(self):
        self.step()
        self.task("T-20261025-01", "status: claimed 2026-10-26T01:00:00+00:00")
        self.step(datetime(2026, 10, 26, 5, 29))
        self.assertIn("running dotz T-20261025-01", self.text())  # before the deadline: waiting
        self.step(datetime(2026, 10, 26, 5, 31))
        text = self.text()
        self.assertNotIn("running dotz", text)
        self.assertIn("- [todo] 组织：只读调研 X", text)
        self.assertIn("dotz 超时：T-20261025-01 到截止仍是 claimed", text)
        self.assertNotIn(TAG, text.split("## 你的指令")[0])
        self.assertIn("| timeout |", (self.drive / "log.md").read_text(encoding="utf-8"))
        first = (self.drive / "tasks" / "T-20261025-01.md").read_text(encoding="utf-8").split("\n")[0]
        self.assertEqual(first, "status: claimed 2026-10-26T01:00:00+00:00")  # dotz is working on it: left alone
        self.step(datetime(2026, 10, 26, 6, 0))  # not sent again
        self.assertEqual(len(list((self.drive / "tasks").glob("T-*"))), 1)

    def test_timeout_still_new(self):
        self.step()
        self.step(datetime(2026, 10, 27, 0, 0))
        self.assertIn("到截止仍是 new", self.text())
        first = (self.drive / "tasks" / "T-20261025-01.md").read_text(encoding="utf-8").split("\n")[0]
        self.assertEqual(first, "status: blocked 调度器撤回：超时，任务行已退回")  # dotz will not do it for nothing

    def test_step_never_raises(self):
        with mock.patch.object(dots_watch, "refusal", side_effect=ValueError("bad row")):
            self.step()
        self.assertEqual([(e[0][0], e[0][1]) for e in self.logged], [("dots_error", "ValueError: bad row")])
        with mock.patch.object(dots_watch, "refusal", side_effect=ValueError):
            dots_watch.step(self.backlog, mock.Mock(side_effect=RuntimeError), NOW)  # even a failing log call stays inside

    def test_refused(self):
        bad = {"Kiki：查资料": "Kiki", "组织：看 incore/records/x.json 并总结": "records/", "组织：读 .env 里的配置": ".env",
               "组织：整理 discord\\data 里的附件": "discord", "组织：核对 API key 的用量": "key", "组织：查 token 价格": "token",
               "组织：读 02_secrets 文件": "02_secrets", "组织：调研完直接购买": "购买", "组织：把结果发邮件给他": "发邮件",
               "组织：改完部署到生产环境": "部署", "组织：联系作者问一下": "联系"}
        self.set_rows(*(self.row("todo", f"{k} {TAG}") for k in bad))
        self.step()
        text = self.text()
        for k, word in bad.items():
            self.assertRegex(text, rf"- \[blocked: dotz 拒派：[^\]]*{word}[^\]]*\] {k.replace('\\', chr(92) * 2)}")
        self.assertEqual(text.count("拒派"), len(bad))
        self.assertNotIn("running dotz", text)
        self.assertEqual(list((self.drive / "tasks").glob("T-*")), [])
        self.assertEqual(len([e for e in self.logged if e[0][0] == "dots_refused"]), len(bad))
        self.assertNotIn(TAG, text.split("## 你的指令")[0])

    def test_negated_limits_pass_but_past_deadline_is_refused(self):
        self.set_rows(self.row("todo", f"组织：只读调研，不要联系任何人，不得对外联系，不付费 {TAG}"),
                      self.row("todo", f"组织：调研 {TAG}〔截止：10-25 12:00〕"))
        self.step()
        text = self.text()
        self.assertEqual(text.count("running dotz"), 1)
        self.assertIn("拒派：截止时间已过", text)

    def test_a_row_that_only_quotes_the_tag_is_an_ordinary_todo(self):
        quoting = f"- [todo] 组织：`{TAG}` 接进调度（见 records/x.json）〔执行者：Claude〕"
        self.set_rows(quoting)
        before = self.backlog.read_bytes()
        self.step()
        self.assertEqual(self.backlog.read_bytes(), before)
        self.assertEqual(len(tick.parse_backlog(self.text())["组织线"]["todo"]), 1)

    def test_misplaced_tags_are_blocked_once_without_sending(self):
        items = (f"写作模型：公开论文检索（只读）{TAG}：①列出论文；②比较方法",
                 f"情报：公开功能盘点（只读；{TAG}）：逐条给出处",
                 f"组织：公开资料 {TAG}〔截止：10-26 05:00〕 结果写进报告")
        untouched = [self.row(s, items[0]) for s in ("review", "done", "done-ref", "blocked: 等日期", "running 10:00")]
        discord = self.row("todo", f"【Discord x】{items[0]}")
        self.set_rows(*(self.row("todo", i) for i in items), *untouched, discord)
        self.step()
        text = self.text()
        for item in items:
            self.assertIn(self.row("blocked: dotz 标签不在行尾，挪到行尾后改回 todo", item), text)
        for line in (*untouched, discord):
            self.assertIn(line, text)
        self.assertIn("\r\n", text)
        self.assertNotIn("\n", text.replace("\r\n", ""))
        before = self.backlog.read_bytes()
        with mock.patch.object(dots_watch.backlog_io, "rewrite_line") as rewrite:
            self.step()
        rewrite.assert_not_called()
        self.assertEqual(self.backlog.read_bytes(), before)
        self.assertEqual(list((self.drive / "tasks").glob("T-*")), [])
        self.assertEqual([e[0][0] for e in self.logged], ["dots_misplaced_tag"] * len(items))

    def test_misplaced_tag_is_blocked_without_drive_and_can_be_fixed(self):
        item = f"组织：公开检索{TAG}：①列来源"
        self.set_rows(self.row("todo", item))
        with mock.patch.object(dots_watch, "DRIVE", self.drive / "not-mounted"):
            self.step()
        self.assertIn(self.row("blocked: dotz 标签不在行尾，挪到行尾后改回 todo", item), self.text())
        self.set_rows(self.row("todo", item.replace(TAG, "") + TAG))
        self.step()
        self.assertIn("running dotz T-20261025-01", self.text())

    def test_tag_references_remain_ordinary_todos(self):
        items = [f"组织：{q} 接进调度" for q in (f"`{TAG}`", f"``{TAG}``", f"「{TAG}」", f"“{TAG}”", f'"{TAG}"', f"'{TAG}'")]
        items += [f"组织：审查员过审后再另排一行{TAG}派单。", f"组织：说明里先标{TAG}；保持规则",
                  f"组织：把{TAG}标签接进调度"]
        # A real assignment alongside a quoted reference is still misplaced.
        self.assertTrue(dots_watch.misplaced_tag(f"组织：修 `{TAG}` 示例，另派公开检索 {TAG}：①列来源"))
        self.set_rows(*(self.row("todo", i) for i in items))
        before = self.backlog.read_bytes()
        with mock.patch.object(dots_watch, "DRIVE", self.drive / "not-mounted"):
            self.step()
        self.assertEqual(self.backlog.read_bytes(), before)
        self.assertEqual(self.logged, [])
        self.assertEqual([i for _, i in tick.parse_backlog(self.text())["组织线"]["todo"]], items)

    @mock.patch.object(dots_watch, "MAX_LIVE", 3)
    def test_at_most_three_wait_on_dots(self):
        self.set_rows(*(self.row("todo", f"组织：调研 {i} {TAG}") for i in range(5)))
        self.step()
        self.assertEqual(self.text().count("running dotz"), 3)
        self.assertEqual(self.text().count("[todo]"), 2 + 1)  # two left, plus the owner row
        self.task("T-20261025-01", "status: done x")
        self.step()
        self.assertEqual(self.text().count("running dotz"), 3)  # one finished, the next went out

    def test_drive_unreadable_changes_nothing(self):
        with mock.patch.object(dots_watch, "DRIVE", self.drive / "not-mounted"):
            before = self.backlog.read_bytes()
            self.step()
            self.step()
        self.assertEqual(self.backlog.read_bytes(), before)
        self.assertEqual([e[0][0] for e in self.logged], ["dots_drive_unreadable"])  # one warning, not one per look

    def test_no_dots_row_never_touches_drive(self):
        self.set_rows("- [todo] 组织：普通行")
        with mock.patch.object(dots_watch, "DRIVE", self.drive / "not-mounted"):
            self.step()
        self.assertEqual(self.logged, [])

    def test_supervise_step_calls_it(self):
        missing = Path(self.tmp.name) / "no-backlog.md"  # supervise_step returns right after the usage sample: nothing is spawned
        with mock.patch.object(tick.dots_watch, "step") as s, mock.patch.object(tick.dots_review, "step") as review, mock.patch.object(tick, "backup_watch"), \
                mock.patch.object(tick, "website_watch"), mock.patch.object(tick, "fresh_usage", return_value=None), \
                mock.patch.object(tick, "log"), mock.patch.object(tick, "latest_usage", return_value=None), \
                mock.patch.object(tick, "spawn") as spawn, mock.patch.object(tick, "BACKLOG", missing):
            tick.supervise_step({"sample": None, "spawned": {}, "aux": {}}, time.time())
        s.assert_called_once_with(missing, mock.ANY)
        review.assert_called_once()  # replaced: the real one reads the production state file and the real Drive
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
