from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from . import pending
from .server import CSRF_COOKIE, create_app

NOW = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)
LOCAL = "127.0.0.1:8765"


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz)


def _freeze_clock(test: unittest.TestCase) -> None:
    """Rating and the page read results with the real clock, and 10-16 is 15 days old by 10-31; freeze them at NOW."""
    patch = mock.patch.object(pending, "datetime", _FrozenDatetime)
    patch.start()
    test.addCleanup(patch.stop)
BACKLOG = "\r\n".join([
    "# backlog",
    "## 组织线",
    "- [todo] 组织：别的活",
    "- [blocked: 等你决定（不急，今年不投博后）] 求职：材料还差几件。选项：A 现在补，B 明年再说。建议B。",
    "- [blocked: 等你决定] 组织：是否改成每天 09:00 汇总。①改 ②不改。建议改。`Start-Process 'C:\\x\\a.html'` 这件事挡着别的工作。",
    "## 工作线",
    "- [blocked: 等你决定] Uvox：首页主张选哪句。A 现在的；B「Don't do research alone.」；C 别的。建议 B。 〔事实：Stripe 价格 $20；核对 10-17〕",
    "- [blocked: 等你决定] FOS：片尾那一句要不要改，10-20 前定。建议改。〔事实：导师在 10-25 前有空；核对 10-05〕",
    "- [blocked: 等你决定] Uvox：没有选项的一件事。",
    "- [blocked: 等你决议] 不是这个状态",
    "- [blocked: 等你决定] 写作：读样例（审查员 10-12 加）再定（没有截止日，不急）。①好 ②不好。",
    "## 量化线",
    "- [blocked: 等你决定] 量化：措辞怎么写。",
    "- [done 10-16 03:48：审查通过，a3458db] BeaconListener 文案〔执行者：Claude〕 `Start-Process 'C:\\x\\b.html'`",
    "- [done 10-01：审查通过] 太旧的成果 `Start-Process 'C:\\x\\old.html'`",
    "- [done 10-17：审查通过] 没有打开命令的成果",
    "- [done 10-17：没过审查] 未写通过 `Start-Process 'C:\\x\\c.html'`",
    "",
])


class PendingParserRegressionTests(unittest.TestCase):
    """P1–P5/F1/F2, verbatim from audits/2026-11-03-pending-buttons-check.md."""

    def parse(self, line: str, options: list[dict[str, str]]) -> dict:
        found = pending.extract_text(line, datetime.fromisoformat("2026-11-03T12:00:00-05:00"))
        self.assertEqual(found["total"], 1)
        item = found["items"][0]
        self.assertEqual(item["options"], options)
        return item

    def test_p1_letter_options_take_priority_over_numbered_background(self) -> None:
        self.parse("- [blocked: 等你决定] ①背景一；②背景二。选项：A 批准；B 暂缓。",
                   [{"label": "A", "text": "批准"}, {"label": "B", "text": "暂缓"}])

    def test_p2_parenthesized_letter_does_not_replace_option(self) -> None:
        self.parse("- [blocked: 等你决定] A 建 main（你选 A 后再给命令）；B 先不建。",
                   [{"label": "A", "text": "建 main（你选 A 后再给命令）"}, {"label": "B", "text": "先不建"}])

    def test_p3_quoted_reply_example_does_not_replace_option(self) -> None:
        self.parse("- [blocked: 等你决定] A 照页面勾选（如「R02 A、R04 B…」）贴回；B 暂不定。",
                   [{"label": "A", "text": "照页面勾选（如「R02 A、R04 B…」）贴回"}, {"label": "B", "text": "暂不定"}])

    def test_p4_two_questions_preserve_the_complete_first_question(self) -> None:
        # Keep the already shipped numbering; this task does not choose a new page layout.
        self.parse("- [blocked: 等你决定] 问题1：A 可以；B 太细；C 太粗。问题2：A 要；B 不用。",
                   [{"label": "1A", "text": "可以"}, {"label": "1B", "text": "太细"}, {"label": "1C", "text": "太粗"},
                    {"label": "2A", "text": "要"}, {"label": "2B", "text": "不用"}])

    def test_p5_option_text_stops_before_the_next_letter(self) -> None:
        self.parse("- [blocked: 等你决定] A 做；B 停。",
                   [{"label": "A", "text": "做"}, {"label": "B", "text": "停"}])

    def test_f1_facts_allow_semicolon_comment_after_checked_date(self) -> None:
        item = self.parse("- [blocked: 等你决定] A 认可；B 暂缓。〔事实：已读样例；核对 10-26；第二轮结果 audits/example.md〕",
                          [{"label": "A", "text": "认可"}, {"label": "B", "text": "暂缓"}])
        self.assertEqual(item["facts"], [{"text": "已读样例", "checked": "10-26", "age_days": 8, "stale": True}])
        self.assertFalse(item["facts_missing"])
        self.assertNotIn("〔事实：", item["full"])

    def test_f2_facts_allow_comma_comment_after_checked_date(self) -> None:
        item = self.parse("- [blocked: 等你决定] A 发布；B 暂缓。〔事实：尚未部署；核对 11-02，审查员重读仍无此项〕",
                          [{"label": "A", "text": "发布"}, {"label": "B", "text": "暂缓"}])
        self.assertEqual(item["facts"], [{"text": "尚未部署", "checked": "11-02", "age_days": 1, "stale": False}])
        self.assertFalse(item["facts_missing"])
        self.assertNotIn("〔事实：", item["full"])


class PendingTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "projects").mkdir()
        (self.root / "projects" / "backlog.md").write_bytes(BACKLOG.encode("utf-8"))
        self.data = self.root / "data"

    def items(self) -> dict[str, dict]:
        return {it["head"].split("：")[0].split("] ")[-1] + "/" + str(it["line"]): it
                for it in pending.extract_pending(self.root, NOW)["items"]}

    def test_extracts_every_waiting_row_with_counts(self) -> None:
        found = pending.extract_pending(self.root, NOW)
        self.assertEqual(found["total"], 7)
        self.assertEqual(found["by_section"], {"组织线": 2, "Arbitros 线": 0, "Beacon Listener 线": 0, "工作线": 4, "量化线": 1, "情报线": 0, "生活线": 0})
        self.assertFalse(any("不是这个状态" in it["head"] for it in found["items"]))

    def test_sorted_by_deadline_then_blocking_then_file_order_with_quiet_last(self) -> None:
        lines = [it["line"] for it in pending.extract_pending(self.root, NOW)["items"]]
        self.assertEqual(lines, [8, 5, 7, 9, 13, 4, 11])  # FOS(deadline), 组织(blocks), Uvox x2, 量化, then 不急: 求职, 写作

    def test_first_seen_comes_from_review_stats_sightings(self) -> None:
        (self.root / "control" / "data").mkdir(parents=True)
        (self.root / "control" / "data" / "review-stats-seen.json").write_text(
            json.dumps({"blocked|Uvox：没有选项的一件事。": "2026-10-17T01:00:00+00:00"}, ensure_ascii=False), encoding="utf-8")
        by_line = {it["line"]: it for it in pending.extract_pending(self.root, NOW)["items"]}
        self.assertEqual(by_line[9]["first_seen"], "2026-10-17T01:00:00+00:00")
        self.assertIsNone(by_line[7]["first_seen"])

    def test_options_recommendation_deadline_blocking_and_commands(self) -> None:
        by_line = {it["line"]: it for it in pending.extract_pending(self.root, NOW)["items"]}
        job, org, uvox, fos = by_line[4], by_line[5], by_line[7], by_line[8]
        self.assertEqual([o["label"] for o in job["options"]], ["A", "B"])
        self.assertTrue(job["quiet"])
        self.assertEqual([o["label"] for o in org["options"]], ["①", "②"])
        self.assertTrue(org["blocks"])
        self.assertEqual(org["commands"], ["Start-Process 'C:\\x\\a.html'"])
        self.assertEqual([o["label"] for o in uvox["options"]], ["A", "B", "C"])
        self.assertEqual(uvox["recommendation"], "建议 B")
        self.assertIn("10-20 前", fos["deadline"])
        quiet = by_line[11]  # "没有截止日" is no deadline; 不急 anywhere early counts; who-added note is dropped
        self.assertEqual((quiet["deadline"], quiet["quiet"]), (None, True))
        self.assertNotIn("审查员", quiet["summary"])
        self.assertEqual(by_line[9]["options"], [])
        self.assertFalse(by_line[9]["blocks"])

    def test_text_cleanup_for_the_page(self) -> None:
        text = "情报：三件事（稿子见 audits/a.txt，约 $0.08）。已带出①先看，再定。①是不是第一次 ②要不要写 ③署名。选 C:\\orghome 或 B 以后。`Start-Process wsl` `Start-Process http://127.0.0.1:<端口>`"
        (self.root / "projects" / "backlog.md").write_text("- [blocked: 等你决定] " + text, encoding="utf-8")
        item = pending.extract_pending(self.root, NOW)["items"][0]
        self.assertEqual(item["summary"], "情报：三件事")  # cut at the first （
        self.assertEqual([o["text"][:4] for o in item["options"]], ["是不是第", "要不要写", "署名。选"])  # the real ①, not the earlier mention
        self.assertEqual(item["commands"], [])  # cut-off and placeholder commands are not shown
        self.assertIsNone(item["deadline"])  # "2026-10-12 前"-style paths and prices are not dates

    def test_letter_options_skip_drive_paths(self) -> None:
        body = "选哪个。A 现在做；B 以后做；C:\\orghome\\y 是路径。"
        (self.root / "projects" / "backlog.md").write_text("- [blocked: 等你决定] " + body, encoding="utf-8")
        self.assertEqual([o["label"] for o in pending.extract_pending(self.root, NOW)["items"][0]["options"]], ["A", "B"])

    def test_facts_are_extracted_and_marked_stale_after_seven_days(self) -> None:
        by_line = {it["line"]: it for it in pending.extract_pending(self.root, NOW)["items"]}
        fresh, old, none = by_line[7], by_line[8], by_line[9]
        self.assertEqual((fresh["facts"][0]["checked"], fresh["facts"][0]["stale"]), ("10-17", False))
        self.assertEqual((old["facts"][0]["age_days"], old["facts"][0]["stale"]), (13, True))
        self.assertTrue(none["facts_missing"])
        self.assertNotIn("事实", fresh["full"])
        # exactly 7 days is still fine, 8 is stale
        for day, stale in (("10-11", False), ("10-10", True)):
            line = f"- [blocked: 等你决定] X：a 〔事实：p；核对 {day}〕"
            (self.root / "projects" / "backlog.md").write_text(line, encoding="utf-8")
            self.assertEqual(pending.extract_pending(self.root, NOW)["items"][0]["facts"][0]["stale"], stale, day)

    def test_results_need_review_pass_command_and_to_be_recent(self) -> None:
        results = pending.recent_results(self.root, NOW)
        self.assertEqual([r["done"] for r in results], ["10-16"])
        self.assertEqual(results[0]["commands"], ["Start-Process 'C:\\x\\b.html'"])
        self.assertEqual(results[0]["title"], "BeaconListener 文案")  # 〔执行者…〕 tag dropped

    def test_results_older_than_five_days_are_counted_but_not_listed(self) -> None:
        today = NOW.astimezone().date()
        four = (today - timedelta(days=4)).strftime("%m-%d")
        six = (today - timedelta(days=6)).strftime("%m-%d")
        (self.root / "projects" / "backlog.md").write_text("\n".join([
            f"- [done {four}：审查通过] 四天前 `Start-Process 'C:\\x\\four.html'`",
            f"- [done {six}：审查通过] 六天前 `Start-Process 'C:\\x\\six.html'`",
        ]), encoding="utf-8")
        results = pending.recent_results(self.root, NOW)
        self.assertEqual([item["title"] for item in results], ["四天前"])
        self.assertEqual(pending.older_results_count(self.root, NOW), 1)
        self.assertEqual(pending.snapshot(self.root, self.data, NOW)["results_omitted"], 1)
        page = (Path(__file__).parent / "static" / "todo.html").read_text(encoding="utf-8")
        self.assertIn("更早的 ' + (data.recent_omitted || 0) + ' 条没列", page)

    def test_reply_is_written_with_time_and_backlog_is_untouched(self) -> None:
        before = (self.root / "projects" / "backlog.md").read_bytes()
        item = pending.extract_pending(self.root, NOW)["items"][0]
        record = pending.save_reply(self.root, self.data, item["id"], "照建议办", "就这样")
        pending.save_reply(self.root, self.data, item["id"], "再想想", "")
        saved = json.loads((self.data / pending.REPLIES_FILE).read_text(encoding="utf-8"))["records"]
        self.assertEqual([r["option"] for r in saved], ["照建议办", "再想想"])
        self.assertTrue(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", record["at"]))
        self.assertEqual((saved[0]["id"], saved[0]["note"], saved[0]["status"]), (item["id"], "就这样", "new"))
        self.assertEqual(saved[0]["head"], item["head"])
        self.assertEqual((self.root / "projects" / "backlog.md").read_bytes(), before)
        shown = pending.snapshot(self.root, self.data)["pending"]["items"][0]
        self.assertEqual(shown["reply"]["option"], "再想想")  # the latest reply is the one shown

    def test_reply_rejects_empty_and_unknown_rows(self) -> None:
        item = pending.extract_pending(self.root, NOW)["items"][0]
        with self.assertRaises(ValueError):
            pending.save_reply(self.root, self.data, item["id"], " ", "")
        with self.assertRaises(LookupError):
            pending.save_reply(self.root, self.data, "0000000000", "A", "")
        self.assertFalse((self.data / pending.REPLIES_FILE).exists())

    def test_rating_is_written_and_only_three_verdicts_work(self) -> None:
        _freeze_clock(self)
        item = pending.recent_results(self.root, NOW)[0]
        pending.save_rating(self.root, self.data, item["id"], "返工", "标题看不懂", NOW)
        saved = json.loads((self.data / pending.RATINGS_FILE).read_text(encoding="utf-8"))["records"]
        self.assertEqual((saved[0]["verdict"], saved[0]["note"], saved[0]["id"]), ("返工", "标题看不懂", item["id"]))
        for bad in ("好", ""):
            with self.assertRaises(ValueError):
                pending.save_rating(self.root, self.data, item["id"], bad, "", NOW)
        with self.assertRaises(LookupError):
            pending.save_rating(self.root, self.data, "0000000000", "用", "", NOW)
        self.assertEqual(len(json.loads((self.data / pending.RATINGS_FILE).read_text(encoding="utf-8"))["records"]), 1)

    def test_new_replies_of_one_line_are_carried_and_marked_done(self) -> None:
        by_line = {it["line"]: it for it in pending.extract_pending(self.root, NOW)["items"]}
        pending.save_reply(self.root, self.data, by_line[5]["id"], "照建议办", "就改")   # 组织线
        pending.save_reply(self.root, self.data, by_line[7]["id"], "B", "")             # 工作线
        block = pending.replies_block(self.data, "组织线", "C:\\x\\pending.py")
        self.assertIn("选的选项：照建议办", block)
        self.assertIn("说明：就改", block)
        self.assertIn(by_line[5]["head"], block)
        self.assertNotIn(by_line[7]["head"], block)  # the other line's reply stays out
        self.assertIn(f"python C:\\x\\pending.py done {by_line[5]['id']}", block)
        self.assertEqual(pending.replies_block(self.data, "量化线", "p"), "")
        self.assertEqual(pending.mark_done(self.data, by_line[5]["id"]), 1)
        self.assertEqual(pending.replies_block(self.data, "组织线", "p"), "")  # handled: not carried again
        self.assertIn("选的选项：B", pending.replies_block(self.data, "Arbitros 线", "p"))  # a reply saved under the old title goes to the old work deputy
        self.assertEqual(pending.replies_block(self.data, "工作线", "p"), "")
        saved = json.loads((self.data / pending.REPLIES_FILE).read_text(encoding="utf-8"))["records"]
        self.assertEqual([r["status"] for r in saved], ["done", "new"])
        self.assertTrue(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", saved[0]["done_at"]))
        self.assertEqual(pending.mark_done(self.data, by_line[5]["id"]), 0)

    def test_replies_saved_under_the_old_work_line_go_to_the_line_their_row_is_in_now(self) -> None:
        """Work-line split (owner\x2010-26): three unhandled old replies (one per destination), one handled, one whose head is in two
        lines, one whose row is gone: each reaches exactly one deputy, once; the conflict reaches nobody and stays `new`."""
        row = lambda head: f"- [blocked: 等你决定] {head}。选项：A 好，B 不好。建议 A。"
        after = "\n".join([
            "## 组织线", row("输入台：语音微调要不要继续"), row("撞头：同一句开头"),
            "## Arbitros 线", row("FOS：片尾要不要改"), row("撞头：同一句开头"),
            "## Beacon Listener 线", row("Kiki：参考系怎么选"), "",
        ])
        old = lambda head, status="new": {"at": "2026-10-25T01:00:00Z", "kind": "reply", "id": pending._id("pending", head), "section": "工作线",
                                          "head": head, "option": "A", "note": "", "status": status}
        heads = {k: next(it["head"] for it in pending.extract_text(after, NOW)["items"] if it["head"].startswith(k))
                 for k in ("输入台", "FOS", "Kiki", "撞头")}
        records = [old(heads["输入台"]), old(heads["FOS"]), old(heads["Kiki"]), old(heads["撞头"]),
                   old("Umba：已经不在清单里的那一行。选项：A", "new"), old(heads["FOS"] + "（旧回复，已处理）", "done")]
        records[5]["id"] = "handled001"
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / pending.REPLIES_FILE).write_text(json.dumps({"records": records}, ensure_ascii=False), encoding="utf-8")
        seen = {s: [r["head"][:6] for r in pending.new_replies(self.data, s, after)]
                for s in ("组织线", "Arbitros 线", "Beacon Listener 线", "量化线")}
        self.assertEqual(seen["组织线"], ["输入台：语音"])
        self.assertEqual(seen["Beacon Listener 线"], ["Kiki：参"])
        self.assertEqual(seen["Arbitros 线"], ["FOS：片尾", "Umba：已"])  # its own, and the one whose row is gone
        self.assertEqual(seen["量化线"], [])
        self.assertEqual(sum(len(v) for v in seen.values()), 4)  # six saved: one handled, one in two lines: neither delivered
        self.assertNotIn("handled001", pending.replies_block(self.data, "Arbitros 线", "p", after))
        self.assertNotIn("撞头", pending.replies_block(self.data, "组织线", "p", after) + pending.replies_block(self.data, "Arbitros 线", "p", after))
        pending.mark_done(self.data, records[2]["id"])
        saved = json.loads((self.data / pending.REPLIES_FILE).read_text(encoding="utf-8"))["records"]
        self.assertEqual([r["status"] for r in saved], ["new", "new", "done", "new", "new", "done"])  # the conflict stays `new`
        self.assertEqual(pending.new_replies(self.data, "Beacon Listener 线", after), [])  # handled: not delivered again

    def test_the_total_is_one_count_for_the_page_and_the_daily_report(self) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "control"))
        import review_stats
        text = review_stats.daily([], BACKLOG, NOW.astimezone().date(), None)
        total = pending.extract_text(BACKLOG, NOW)["total"]
        self.assertIn(f"现在等你决定的行（和「等你决定」页同一个数）（{total}）", text)
        self.assertEqual(total, pending.extract_pending(self.root, NOW)["total"])

    def test_results_also_take_the_open_command_from_the_named_result_file(self) -> None:
        (self.root / "audits").mkdir()
        (self.root / "audits" / "2026-10-18-x.md").write_text("打开命令：`Start-Process 'C:\\x\\result.html'`\n\n# t\n", encoding="utf-8")
        (self.root / "audits" / "2026-10-18-late.md").write_text("\n\n\n\n\n\n`Start-Process 'C:\\x\\seventh-line.html'`\n", encoding="utf-8")
        (self.root / "projects" / "backlog.md").write_text(
            "## 工作线\n- [done 10-17：审查通过；audits/2026-10-18-x.md] FactoryAlign：v1 结果页\n"
            "- [done 10-17：审查通过；audits/2026-10-18-late.md] 第七行才有命令\n- [done 10-17：审查通过；audits/none.md] 文件不存在\n", encoding="utf-8")
        results = pending.recent_results(self.root, NOW)
        self.assertEqual([r["commands"] for r in results], [["Start-Process 'C:\\x\\result.html'"]])

    def test_replied_rows_that_left_the_backlog_are_listed_as_handled(self) -> None:
        by_line = {it["line"]: it for it in pending.extract_pending(self.root, NOW)["items"]}
        pending.save_reply(self.root, self.data, by_line[5]["id"], "照建议办", "")
        pending.save_reply(self.root, self.data, by_line[7]["id"], "B", "")
        shown = pending.snapshot(self.root, self.data)
        self.assertEqual(shown["handled"], [])  # both rows still wait: they stay in the list, marked replied
        path = self.root / "projects" / "backlog.md"
        path.write_bytes(path.read_bytes().replace("[blocked: 等你决定] 组织：是否改成".encode(), "[done 10-18] 组织：是否改成".encode()))
        shown = pending.snapshot(self.root, self.data)
        self.assertEqual([r["head"] for r in shown["handled"]], [by_line[5]["head"]])
        self.assertNotIn(by_line[5]["id"], [it["id"] for it in shown["pending"]["items"]])

    def test_missing_backlog_gives_an_empty_page(self) -> None:
        (self.root / "projects" / "backlog.md").unlink()
        shown = pending.snapshot(self.root, self.data)
        self.assertEqual((shown["pending"]["total"], shown["results"]), (0, []))


class PendingRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "projects").mkdir()
        (self.root / "projects" / "backlog.md").write_text(BACKLOG, encoding="utf-8")
        _freeze_clock(self)
        app = create_app(data_root=self.root / "input-data", expected_port=8765, dashboard_root=self.root)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_page_api_and_writes_go_through_the_csrf_check(self) -> None:
        page = await self.client.get("/pending", headers={"Host": LOCAL})
        html = await page.text()
        self.assertEqual(page.status, 200)
        self.assertNotIn("__CSP_NONCE__", html)
        self.assertNotIn("innerHTML", html)
        self.assertIn("'nonce-", page.headers["Content-Security-Policy"])
        token = page.cookies[CSRF_COOKIE].value
        self.assertIn(f"const CSRF = '{token}';", html)  # the cookie is httponly: the page gets the token inline
        self.assertNotIn("__CSRF_TOKEN__", html)
        data = await (await self.client.get("/api/pending", headers={"Host": LOCAL})).json()
        self.assertEqual(data["pending"]["total"], 7)
        org = next(it for it in data["pending"]["items"] if it["commands"])
        self.assertEqual(org["links"], [None])  # C:\x\a.html is outside audits/: command only, no link
        item_id = data["pending"]["items"][0]["id"]
        origin = {"Host": LOCAL, "Origin": f"http://{LOCAL}"}
        body = {"id": item_id, "option": "照建议办", "note": ""}
        refused = await self.client.post("/api/pending/reply", json=body, headers=origin)
        self.assertEqual(refused.status, 403)  # no CSRF header
        sent = await self.client.post("/api/pending/reply", json=body, headers={**origin, "X-CSRF-Token": token})
        self.assertEqual(sent.status, 200)
        self.assertTrue((self.root / "data" / pending.REPLIES_FILE).exists())
        again = (await (await self.client.get("/api/pending", headers={"Host": LOCAL})).json())["pending"]["items"][0]
        self.assertEqual(again["reply"]["option"], "照建议办")
        gone = await self.client.post("/api/pending/reply", json={**body, "id": "0000000000"},
                                      headers={**origin, "X-CSRF-Token": token})
        self.assertEqual(gone.status, 409)
        result_id = data["results"][0]["id"]
        rated = await self.client.post("/api/pending/rating", json={"id": result_id, "verdict": "用", "note": ""},
                                       headers={**origin, "X-CSRF-Token": token})
        self.assertEqual(rated.status, 200)
        bad = await self.client.post("/api/pending/rating", json={"id": result_id, "verdict": "好", "note": ""},
                                     headers={**origin, "X-CSRF-Token": token})
        self.assertEqual(bad.status, 400)


if __name__ == "__main__":
    unittest.main()
