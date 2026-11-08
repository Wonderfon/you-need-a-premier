from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from . import dashboard, sprint
from .server import create_app

NOW = datetime.now(timezone.utc).replace(microsecond=0)
LOCAL = "127.0.0.1:8765"
LONG_GOAL = "语音端改错字给 owner 一屏结论：" + "润色能不能改对同音近音错字，" * 30


def backlog(extra_org: str = "") -> str:
    return "\n".join([
        "# backlog",
        "## 你的指令（最优先）",
        "- [todo] 输入台：owner 指令区的行不算任何一条线 〔msg:1 role:coordinator〕",
        "## 组织线",
        "权重：10%",
        f"冲刺：输入台，版本目标：（owner\x2010-31 Discord，records\x2f2026-10-31-x.json）{LONG_GOAL}",
        "- [running 11-03 10:3x 在做] 输入台：跑着的那一行，写得很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长",
        "- [review] 输入台：待审的一行",
        "- [todo] 输入台：待做一，账本里从没出现过",
        "- [todo] （返工）输入台：待做二，上次审查判返工",
        "- [blocked: 等你决定] 输入台：要你定的事。「A 做」「B 不做」建议 A。",
        "- [blocked: 等 owner 回复] 输入台：要你回的事 `Start-Process 'C:\\x\\a.html'`",
        "- [blocked: 等你] 输入台：要你做的第三件",
        "- [blocked: 定时，2026-12-01起跑] 输入台：定时行，不是卡住",
        "- [todo] 组织：别的项目的行，不属于冲刺项目",
        "- [todo]没有空格的坏行",
        "- [done 11-01：审查通过；audits/a.md] 输入台：最旧的完成",
        "- [done 11-02：审查通过；audits/b.md、audits/missing.md] 输入台：第二新的完成",
        "- [done 11-03 02:5x：审查通过；audits/c.md] 输入台：最新的完成",
        "- [done 10-20：审查通过] 输入台：太老的完成，第四条",
        "- [done 11-03：审查通过] 组织：别的项目的完成",
        extra_org,
        "## 量化线",
        "冲刺：量化，版本目标：照旧",
        "- [todo] 量化：只有一行待做",
        "## 情报线",
        "- [todo] 情报：这条线没有冲刺行",
        "",
    ])


def run(kind: str, item: str, at: datetime, **extra) -> str:
    return json.dumps({"at": at.isoformat(), "role": "coordinator", "section": "组织线", "kind": kind, "item": item, **extra}, ensure_ascii=False)


class SprintCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "control" / "data"
        self.history = self.data / "backlog-history"
        (self.root / "projects").mkdir()
        (self.root / "audits").mkdir()
        self.data.mkdir(parents=True)
        self.write(backlog())

    def write(self, text: str) -> None:
        (self.root / "projects" / "backlog.md").write_bytes(text.replace("\n", "\r\n").encode("utf-8"))

    def ledger(self, *rows: str) -> None:
        (self.data / "runs.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")

    def copy(self, text: str, when: datetime) -> None:
        self.history.mkdir(parents=True, exist_ok=True)
        (self.history / f"backlog-{when:%Y%m%dT%H%M%S}.{when.microsecond:06d}Z.md").write_text(text, encoding="utf-8")

    def daily(self, text: str, when: datetime) -> None:
        folder = self.history / "daily"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"backlog-{when:%Y%m%d}.md"
        path.write_text(text, encoding="utf-8")
        os.utime(path, (when.timestamp(), when.timestamp()))

    def snap(self) -> dict:
        return sprint.snapshot(self.root, NOW)

    def line(self, section: str = "组织线") -> dict:
        return next(x for x in self.snap()["lines"] if x["section"] == section)

    def row(self, needle: str) -> dict:
        s = self.snap()
        rows = [r for x in s["lines"] for r in x["owner"] + [r for g in x["groups"].values() for r in g] if needle in r["full"]]
        self.assertEqual(len(rows), 1, needle)
        return rows[0]


class GroupingTests(SprintCase):
    def test_rows_of_the_sprint_project_grouped_by_state_owner_rows_split_off(self) -> None:
        line = self.line()
        heads = {s: [r["head"] for r in line["groups"][s]] for s in sprint.STATES}
        self.assertEqual(len(heads["running"]), 1)
        self.assertEqual(heads["review"], ["待审的一行"])
        self.assertEqual(heads["todo"], ["待做一，账本里从没出现过", "（返工）待做二，上次审查判返工"])
        self.assertEqual(heads["blocked"], ["定时行，不是卡住"])  # 等你 rows are not in the group
        self.assertEqual([r["head"].split("。")[0] for r in line["owner"]][:1], ["要你定的事"])
        self.assertEqual(len(line["owner"]), 3)
        everything = json.dumps(line, ensure_ascii=False)
        for stranger in ("别的项目的行", "owner 指令区", "没有空格的坏行", "别的项目的完成"):
            self.assertNotIn(stranger, everything)
        self.assertLessEqual(len(heads["running"][0]), sprint.HEAD_CHARS)

    def test_owner_rows_collected_on_top_with_their_line(self) -> None:
        s = self.snap()
        self.assertEqual([r["section"] for r in s["owner"]], ["组织线"] * 3)
        self.assertTrue(all(r["owner"] and r["status"] == "blocked" for r in s["owner"]))

    def test_goal_is_cut_at_200_with_full_text_kept_and_provenance_note_dropped(self) -> None:
        goal = self.line()["goal"]
        self.assertEqual(goal["project"], "输入台")
        self.assertEqual(len(goal["short"]), sprint.GOAL_CHARS)
        self.assertTrue(goal["cut"])
        self.assertTrue(goal["short"].startswith("语音端改错字"))
        self.assertIn("owner\x2010-31 Discord", goal["full"])
        short = self.line("量化线")["goal"]
        self.assertEqual((short["short"], short["cut"]), ("照旧", False))

    def test_line_without_sprint_has_no_rows_and_every_section_is_listed(self) -> None:
        s = self.snap()
        self.assertEqual([x["section"] for x in s["lines"]], ["组织线", "量化线", "情报线"])
        intel = s["lines"][2]
        self.assertEqual((intel["sprint"], intel["goal"], intel["next"]), (None, None, None))
        self.assertTrue(all(not g for g in intel["groups"].values()))

    def test_sprint_line_only_counts_near_the_section_top(self) -> None:
        self.write(backlog().replace("权重：10%", "权重：10%\n- [todo] 别的\n- [todo] 别的二\n- [todo] 别的三\n- [todo] 别的四"))
        self.assertIsNone(self.line()["sprint"])  # tick.parse_backlog reads it within 4 lines of the heading only

    def test_next_row_is_the_review_row_then_the_first_todo(self) -> None:
        self.assertEqual(self.line()["next"]["head"], "待审的一行")
        self.write(backlog().replace("- [review] 输入台：待审的一行\n", ""))
        self.assertEqual(self.line()["next"]["head"], "待做一，账本里从没出现过")
        self.assertEqual(self.line("量化线")["next"]["head"], "只有一行待做")

    def test_done_keeps_latest_three_with_result_files_and_links_only_for_real_files(self) -> None:
        (self.root / "audits" / "b.md").write_text("# b", encoding="utf-8")
        done = self.line()["done"]
        self.assertEqual([x["head"] for x in done], ["输入台：最新的完成", "输入台：第二新的完成", "输入台：最旧的完成"])
        self.assertEqual([x["day"] for x in done], ["11-03", "11-02", "11-01"])
        self.assertEqual([f["name"] for f in done[1]["files"]], ["audits/b.md", "audits/missing.md"])
        self.assertEqual([f["href"] for f in done[1]["files"]], ["/review/a/b.md", None])
        self.assertEqual(done[0]["files"][0]["href"], None)  # c.md does not exist here

    def test_missing_backlog_is_a_state_not_a_crash(self) -> None:
        (self.root / "projects" / "backlog.md").unlink()
        s = self.snap()
        self.assertEqual((s["state"], s["lines"], s["owner"]), ("missing", [], []))


class AgeTests(SprintCase):
    def test_seven_hour_dots_block_and_ordinary_todo_have_separate_counts(self) -> None:
        text = "\n".join(["## 组织线", "冲刺：输入台，版本目标：区分两种计数",
                          "- [blocked: dotz T-1 阻塞：缺全文] 输入台：dotz 阻塞行",
                          "- [todo] 输入台：普通等待行", ""])
        self.write(text)
        self.copy(text, NOW - timedelta(hours=7))
        s = self.snap()
        line = s["lines"][0]
        self.assertEqual((s["stuck_total"], s["alarm_total"]), (1, 1))
        self.assertEqual((line["stuck"], len(line["alarm_rows"])), (1, 1))
        self.assertEqual(line["alarm_rows"][0]["head"], "dotz 阻塞行")
        self.assertEqual(line["alarm_rows"][0]["alarm"], {"kind": "dotz 阻塞", "age_text": "至少 7 小时"})
        self.assertFalse(line["alarm_rows"][0]["stuck"])  # ordinary blocked rows were never in the waiting counter
        self.assertIsNone(line["groups"]["todo"][0]["alarm"])
        self.assertTrue(line["groups"]["todo"][0]["stuck"])

    def test_ages_from_the_ledger_review_since_executor_end_todo_since_rework(self) -> None:
        self.ledger(
            run("executor", "输入台：待审的一行", NOW - timedelta(hours=9), ok=True, exit_code=0),
            run("executor", "输入台：待审的一行", NOW - timedelta(hours=8), ok=True, exit_code=0),
            run("review", "输入台：待审的一行", NOW - timedelta(hours=1), verdict="pass", audit_file="audits/r.md"),
            run("review", "（返工）输入台：待做二，上次审查判返工", NOW - timedelta(hours=3), verdict="rework"),
            run("executor", "输入台：待做一，账本里从没出现过", NOW - timedelta(hours=2), ok=False, exit_code=1),
        )
        review = self.row("待审的一行")
        self.assertAlmostEqual(review["age_seconds"], 8 * 3600, delta=2)
        self.assertEqual((review["age_exact"], review["stuck"], review["age_text"]), (True, True, "8 小时"))
        self.assertEqual(review["last_exec"]["ok"], True)
        self.assertEqual(review["last_review"]["verdict"], "pass")
        self.assertEqual(review["files"][0]["name"], "audits/r.md")
        rework = self.row("待做二")
        self.assertAlmostEqual(rework["age_seconds"], 3 * 3600, delta=2)
        self.assertFalse(rework["stuck"])
        failed = self.row("待做一")  # a failed executor run does not move a todo row
        self.assertEqual((failed["last_exec"]["ok"], failed["last_exec"]["code"]), (False, 1))
        self.assertIsNone(failed["age_seconds"])

    def test_key_ignores_the_rework_prefix_and_text_after_80_characters(self) -> None:
        long_item = "输入台：" + "很长的待做" * 30
        self.write(backlog(f"- [review] {long_item}"))
        self.ledger(run("executor", long_item[:80] + "后面改过了", NOW - timedelta(hours=7), ok=True, exit_code=0))
        self.assertAlmostEqual(self.row("很长的待做")["age_seconds"], 7 * 3600, delta=2)

    def test_running_age_from_the_time_written_in_the_status_bracket(self) -> None:
        stamp = (NOW - timedelta(hours=3)).astimezone()
        self.write(backlog().replace("running 11-03 10:3x", f"running {stamp:%m-%d %H}:{stamp.minute // 10}x"))
        row = self.row("跑着的那一行")
        self.assertEqual(row["age_source"], "行里写的时间")
        self.assertFalse(row["age_exact"])
        self.assertTrue(3 * 3600 <= row["age_seconds"] <= 3 * 3600 + 600, row["age_seconds"])
        self.assertTrue(row["age_text"].startswith("至少 3 小时"))

    def test_age_from_history_copies_exact_inside_the_recent_set_and_lower_bound_beyond(self) -> None:
        without = backlog().replace("- [todo] 输入台：待做一，账本里从没出现过\n", "")
        with_row = backlog()
        owner = "- [blocked: 等 owner 回复] 输入台：要你回的事"
        earlier = with_row.replace(owner, "- [todo] 输入台：要你回的事")  # was todo before
        self.copy(without, NOW - timedelta(minutes=50))          # before the write that added 待做一
        self.copy(with_row, NOW - timedelta(minutes=20))
        self.copy(with_row, NOW - timedelta(minutes=5))
        self.daily(earlier, NOW - timedelta(hours=30))           # a gap: daily copies only
        first = self.row("待做一")
        self.assertEqual((first["age_seconds"], first["age_exact"]), (50 * 60, True))  # the write at -50 min added it
        self.assertEqual(first["age_text"], "50 分钟")
        owner_row = self.row("要你回的事")
        # in every recent copy, absent as a blocked row only in the daily copy 30 h ago: at least as long as the oldest recent copy
        self.assertFalse(owner_row["age_exact"])
        self.assertEqual(owner_row["age_seconds"], 50 * 60)
        self.assertTrue(owner_row["age_text"].startswith("至少 "))
        self.assertTrue(owner_row["age_source"].startswith("任务表副本"))

    def test_unknown_is_not_zero_and_is_counted_apart_from_stuck(self) -> None:
        self.write(backlog().replace("running 11-03 10:3x", f"running {(NOW - timedelta(hours=1)).astimezone():%m-%d %H:%M}"))
        line = self.line()  # no ledger, no history at all
        row = self.row("待做一")
        self.assertEqual((row["age_seconds"], row["age_text"], row["stuck"]), (None, "不知道", False))
        self.assertIsNone(row["last_exec"])
        self.assertFalse(row["ledger_known"])
        self.assertEqual(line["stuck"], 0)
        self.assertGreaterEqual(line["unknown"], 4)
        scheduled = line["groups"]["blocked"][0]
        self.assertEqual(scheduled["age_text"], "不知道")  # a 定时 row is neither stuck nor counted as unknown
        self.assertEqual(line["unknown"], 6)  # review 1, todo 2, owner 3; the running row has its own time, the 定时 row is parked

    def test_stuck_means_over_six_hours_and_skips_scheduled_and_waiting_on_another_row(self) -> None:
        self.ledger(
            run("executor", "输入台：待审的一行", NOW - timedelta(hours=6, minutes=1), ok=True, exit_code=0),
            run("review", "（返工）输入台：待做二，上次审查判返工", NOW - timedelta(hours=5, minutes=59), verdict="rework"),
        )
        recent = (NOW - timedelta(hours=1)).astimezone()
        self.write(backlog().replace("running 11-03 10:3x", f"running {recent:%m-%d %H}:{recent.minute // 10}0"))
        self.daily(backlog(), NOW - timedelta(days=3))  # rows with no ledger entry are "at least 3 days" in the state they are in
        line = self.line()
        self.assertEqual(self.row("待审的一行")["stuck"], True)
        self.assertEqual(self.row("待做二")["stuck"], False)
        sched = line["groups"]["blocked"][0]
        self.assertFalse(sched["stuck"])
        self.assertTrue(all(r["stuck"] for r in line["owner"]))  # at least 3 days, waiting on him
        self.assertEqual(self.snap()["stuck_total"], line["stuck"] + self.line("量化线")["stuck"])  # the quant row is also at least 3 days old in that copy
        self.assertFalse(self.row("跑着的那一行")["stuck"])
        self.assertEqual(line["stuck"], 5)  # the review row, 待做一 (at least 3 days), the three owner rows

    def test_ledger_is_read_incrementally_and_a_half_written_line_waits(self) -> None:
        path = self.data / "runs.jsonl"
        first = run("executor", "输入台：待审的一行", NOW - timedelta(hours=9), ok=True, exit_code=0)
        path.write_text(first + "\n", encoding="utf-8")
        self.assertAlmostEqual(self.row("待审的一行")["age_seconds"], 9 * 3600, delta=2)
        newer = run("executor", "输入台：待审的一行", NOW - timedelta(hours=2), ok=True, exit_code=0)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(newer[:40])  # half a line
        self.assertAlmostEqual(self.row("待审的一行")["age_seconds"], 9 * 3600, delta=2)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(newer[40:] + "\n")
        self.assertAlmostEqual(self.row("待审的一行")["age_seconds"], 2 * 3600, delta=2)
        path.write_text("garbage\n{not json\n", encoding="utf-8")  # rotated and unreadable rows: skipped, no crash
        self.assertIsNone(self.row("待审的一行")["age_seconds"])


class IsolationTests(SprintCase):
    def test_reads_only_under_root_and_writes_nothing(self) -> None:
        decoy = self.root / "decoy"
        decoy.mkdir()
        (decoy / "backlog.md").write_text("## 组织线\n冲刺：量化，版本目标：诱饵\n- [todo] 量化：诱饵行\n", encoding="utf-8")
        (decoy / "runs.jsonl").write_text(run("executor", "输入台：待审的一行", NOW - timedelta(minutes=1), ok=True, exit_code=0) + "\n", encoding="utf-8")
        tick = dashboard._tick()
        text = backlog("\n".join([
            "- [blocked: dotz T-1 阻塞] 输入台：dotz 行",
            "- [blocked: 沙箱权限不足] 输入台：沙箱行",
            "- [blocked: 返回重派] 输入台：重派行",
            "- [todo] 输入台：Claude 行〔执行者：Claude〕",
        ]))
        self.write(text)
        self.copy(text, NOW - timedelta(hours=7))
        self.ledger(run("executor", "输入台：待审的一行", NOW - timedelta(hours=9), ok=True, exit_code=0))
        before = sorted((str(p.relative_to(self.root)), p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*") if p.is_file())
        patches = [mock.patch.object(tick, name, value) for name, value in (
            ("WORKSPACE", decoy), ("BACKLOG", decoy / "backlog.md"), ("RUNS_FILE", decoy / "runs.jsonl"),
            ("DATA", decoy / "data"), ("BACKLOG_HISTORY", decoy / "history"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(tick, "parse_backlog", side_effect=AssertionError("page must bypass diagnostic writes")):
            s = self.snap()
        after = sorted((str(p.relative_to(self.root)), p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*") if p.is_file())
        self.assertEqual(before, after)  # nothing created (tick.note_malformed would have written malformed-seen.json) or changed
        self.assertFalse((decoy / "data").exists())
        self.assertNotIn("诱饵", json.dumps(s, ensure_ascii=False))
        self.assertAlmostEqual(self.row("待审的一行")["age_seconds"], 9 * 3600, delta=2)
        self.assertEqual(s["alarm_total"], 4)
        self.assertEqual({r["alarm"]["kind"] for r in s["lines"][0]["alarm_rows"]},
                         {"dotz 阻塞", "沙箱/权限挡住", "返回重派", "Claude 执行者 todo 没派出去"})
        self.ledger(run("executor", "输入台：最近另一行", NOW - timedelta(hours=2), ok=True, exit_code=0))
        self.assertEqual(self.snap()["alarm_total"], 3)  # a recent project run resets the Claude todo's alarm clock


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "projects").mkdir()
        (self.root / "projects" / "backlog.md").write_text(backlog(), encoding="utf-8")

    async def client(self) -> TestClient:
        app = create_app(data_root=self.root / "input-data", expected_port=8765, dashboard_root=self.root)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_page_and_api_are_read_only_and_the_board_links_to_it(self) -> None:
        client = await self.client()
        page = await client.get("/sprint", headers={"Host": LOCAL})
        self.assertEqual(page.status, 200)
        html = await page.text()
        nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", page.headers["Content-Security-Policy"]).group(1)
        self.assertIn(f'nonce="{nonce}"', html)
        self.assertNotIn("__CSP_NONCE__", html)
        self.assertNotIn("innerHTML", html)
        self.assertIn("/api/sprint", html)
        api = await client.get("/api/sprint", headers={"Host": LOCAL})
        body = await api.json()
        self.assertEqual((api.status, body["state"], [x["section"] for x in body["lines"]]), (200, "ok", ["组织线", "量化线", "情报线"]))
        self.assertEqual(len(body["owner"]), 3)
        for method in (client.post, client.put, client.delete):
            self.assertGreaterEqual((await method("/api/sprint", headers={"Host": LOCAL})).status, 400)
        board = await (await client.get("/dashboard", headers={"Host": LOCAL})).text()
        self.assertIn('href="/sprint"', board)
        self.assertFalse((self.root / "control").exists())  # a page read creates nothing


if __name__ == "__main__":
    unittest.main()
