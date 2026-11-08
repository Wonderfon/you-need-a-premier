from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from .dashboard import _limits, _process_created, build_snapshot
from .server import IDENTITY_HEADER, create_app

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
SECRET = "sk-test-SECRETVALUE0123456789"
LOCAL = "127.0.0.1:8765"
PRIVATE_HOST = "owner-box.tail1234.ts.net"
OWNER = "owner@example.com"
FORBIDDEN_KEYS = {"prompt", "payload", "receipt", "access_token", "session_id", "exact_text", "response"}


def iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def unix(minutes_ago: float) -> int:
    return int((NOW - timedelta(minutes=minutes_ago)).timestamp())


def write_json(root: Path, rel: str, value: object) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def usage_path(root: Path) -> Path:
    return root / "control" / "data" / "usage.jsonl"


def db_path(root: Path) -> Path:
    return root / "discord" / "data" / "discord.sqlite3"


def write_usage(root: Path, lines: list[object]) -> None:
    path = usage_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n",
                    encoding="utf-8")


def obs(minutes_ago: float, utilization: float, resets_minutes_ago: float, source: str = "sampler") -> dict:
    return {"observed_at": iso(minutes_ago), "utilization": utilization, "resets_at": unix(resets_minutes_ago),
            "source": source}


def make_usage(root: Path) -> None:
    write_usage(root, [
        obs(400, 0.30, 60), obs(300, 0.55, 60), obs(200, 0.52, 60),   # previous window, reset 1 h ago
        obs(120, 0.10, -120), obs(30, 0.30, -120, "deputy:work"),     # current window, resets in 2 h
        "{not json",
        obs(10, -0.5, -120),                                          # impossible value is skipped
        obs(5, 0.35, -120),
    ])


def make_deputies(root: Path) -> None:
    write_json(root, "control/data/deputies.json", {
        "coordinator": {"session_id": "s-1", "created_at": iso(600), "status": "idle", "current": None,
                        "last": {"message_id": "m1", "finished_at": iso(20), "ok": True, "elapsed_s": 95.5,
                                 "summary": "审阅完毕"}, "runs": 4},
        "work": {"session_id": "s-2", "created_at": iso(600), "status": "running",
                 "current": {"message_id": "m2", "source": "coordinator:dispatch",
                             "text_preview": f"修复看板 {SECRET}", "started_at": iso(3)},
                 "last": {"message_id": "m0", "finished_at": iso(60), "ok": False, "elapsed_s": 30,
                          "summary": r"失败于 C:\orghome\secret\file.txt"}, "runs": 2},
        "life": {"session_id": "s-3", "created_at": iso(600), "status": "idle", "current": None, "last": None,
                 "runs": 0},
        "intelligence": "not an object",
    })


def make_db(root: Path) -> None:
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE inbound (message_id TEXT PRIMARY KEY, source TEXT NOT NULL, received_at TEXT NOT NULL,
            guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, author_id TEXT NOT NULL, role TEXT NOT NULL,
            exact_text TEXT NOT NULL);
        CREATE TABLE direct_requests (message_id TEXT PRIMARY KEY, status TEXT NOT NULL, queued_only INTEGER NOT NULL,
            updated_at TEXT NOT NULL, next_attempt REAL NOT NULL DEFAULT 0, response TEXT, detail TEXT, ack_id TEXT,
            reply_ids TEXT);
    """)
    rows = [("p1", "direct:message", "work", "pending", 0, 0, None),
            ("b1", "slash:ask", "coordinator", "blocked", 0, unix(-10), rf"限流 {SECRET} C:\orghome\x.log"),
            ("n1", "coordinator:dispatch", "life", "needs_attention", 0, 0, "需要确认"),
            ("q1", "input:voice", "work", "queued_only", 1, 0, None)]
    rows += [(f"a{i:02d}", "direct:message", "work", "answered", 0, unix(-10), "old") for i in range(12)]
    for i, (mid, source, role, status, only, retry, detail) in enumerate(rows):
        conn.execute("INSERT INTO inbound VALUES (?,?,?,?,?,?,?,?)",
                     (mid, source, iso(100 - i), "22", "33", "11", role, f"请求 {mid} " + "长" * 300))
        conn.execute("INSERT INTO direct_requests VALUES (?,?,?,?,?,?,?,?,?)",
                     (mid, status, only, iso(50 - i), retry, f"reply {SECRET}", detail, "ack", "[]"))
    conn.commit()
    conn.close()


BACKLOG = f"""# 总理待办

规则：……

## 你的指令（最优先）
- [done 16:02] 【Discord·工作副总理】报一下 〔msg:1 role:work〕
- [todo] 【Discord·量化副总理】看看开户 〔msg:2 role:quant〕

## 工作线
权重：60%（Uvox 优先于 Umba）

- [running 23:12] Uvox T5 修复：示例项目被锁
- [review] Umba 贴图：见 C:\\orghome\\wt\\umba.md
- [todo] 第一条待办
- [todo] 第二条待办 {SECRET}
- [blocked: 等你决定] 养老院同意书
- [done 21:30] 已完成的
- [done-ref] 参考
- [each tick] 每轮

## 组织线
权重：10%
- [blocked: 定时，10-07 起跑] 周复盘
"""


def make_backlog(root: Path) -> None:
    path = root / "projects" / "backlog.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(BACKLOG.replace("\n", "\r\n").encode("utf-8"))


def all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in all_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in all_keys(item)}
    return set()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SnapshotTests(unittest.TestCase):
    def test_missed_timed_row_appears_then_clears_without_writing_any_data(self) -> None:
        day = (NOW.astimezone() - timedelta(days=1)).strftime("%Y-%m-%d")
        row = f"- [todo] 组织：到点恢复〔时段：10:50–13:00〕〔窗口日期：{day}〕"
        path = self.root / "projects" / "backlog.md"
        path.parent.mkdir(parents=True)
        path.write_text("## 组织线\n" + row + "\n", encoding="utf-8")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        missed = build_snapshot(self.root, now=NOW)["timed_rows"]
        self.assertEqual(missed["state"], "ok")
        self.assertEqual(len(missed["missed"]), 1)
        self.assertEqual(missed["missed"][0]["message"], "到点行错过：原定 10:50、窗口已过、还没执行")
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        path.write_text(path.read_text(encoding="utf-8").replace("[todo]", "[done 13:01]"), encoding="utf-8")
        self.assertEqual(build_snapshot(self.root, now=NOW)["timed_rows"]["missed"], [])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def full(self) -> dict:
        make_usage(self.root)
        make_deputies(self.root)
        make_db(self.root)
        make_backlog(self.root)
        return build_snapshot(self.root, now=NOW)

    def test_project_delivery_cache_alerts_unknown_and_freshness(self) -> None:
        self.assertEqual(build_snapshot(self.root, now=NOW)["project_delivery"]["state"], "missing")
        path = "control/data/project-delivery.json"
        data = {"schema_version": 1, "generated_at_utc": NOW.isoformat(), "projects": [
            {"key": "uvox", "name": "Uvox", "days_since_delivery": 8, "approved_unmerged": 6,
             "pause_new_drafts": True, "issues": []},
            {"key": "new", "name": "New", "days_since_delivery": None, "approved_unmerged": None,
             "delivery_state": "unknown", "issues": ["仓库不可读"]}]}
        write_json(self.root, path, data)
        snapshot = build_snapshot(self.root, now=NOW)["project_delivery"]
        self.assertFalse(snapshot["stale"])
        self.assertTrue(snapshot["projects"][0]["age_exceeded"])
        self.assertTrue(snapshot["projects"][0]["pause_new_drafts"])
        self.assertIsNone(snapshot["projects"][1]["approved_unmerged"])
        self.assertTrue(build_snapshot(self.root, now=NOW + timedelta(hours=10))["project_delivery"]["stale"])
        write_json(self.root, path, {"schema_version": 1, "projects": "bad"})
        self.assertEqual(build_snapshot(self.root, now=NOW)["project_delivery"]["state"], "invalid")

    def test_machine_row_shows_sustained_anomalies_and_a_stopped_sampler(self) -> None:
        self.assertEqual(build_snapshot(self.root, now=NOW)["machine"], {"state": "missing", "anomalies": []})
        path = self.root / "data" / "machine-health.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)

        def rows(top, dwm):  # the 11-01 01:37 shape: three 10-minute samples ending at NOW - 1 minute
            return "".join(json.dumps({"at": (NOW - timedelta(minutes=1 + 10 * (2 - n))).isoformat(), "interval_s": 600,
                                       "top": top, "dwm": dwm, "gpu": {"util": 3.0}, "idle_min": 1.0, "model_procs": []}) + "\n"
                           for n in range(3))

        path.write_text(rows([{"name": "iGoSwServer.exe", "pid": 10888, "cpu": 98.6}], 97.0), encoding="utf-8")
        machine = build_snapshot(self.root, now=NOW)["machine"]
        self.assertEqual((machine["state"], sorted(a["kind"] for a in machine["anomalies"])), ("ok", ["cpu", "dwm"]))
        path.write_text(rows([{"name": "msedgewebview2.exe", "pid": 1, "cpu": 4.1}], 8.6), encoding="utf-8")
        self.assertEqual(build_snapshot(self.root, now=NOW)["machine"]["anomalies"], [])
        self.assertEqual(build_snapshot(self.root, now=NOW + timedelta(hours=2))["machine"]["state"], "stale")

    def test_usage_window_baseline_and_history(self) -> None:
        usage = self.full()["usage"]
        self.assertEqual(usage["state"], "ok")
        from .dashboard import _tick
        tick = _tick()  # the chart's limits are the scheduler's
        self.assertEqual((usage["pause_pct"], usage["ceiling_pct"]), (tick.BASELINE_MAX, tick.HARD_CEILING))
        self.assertEqual((usage["pause_pct"], usage["ceiling_pct"]), (90, 95))
        with mock.patch(f"{_limits.__module__}._tick", side_effect=ImportError):
            self.assertEqual(_limits(), (80, 90))  # tick.py unavailable: fallbacks
        from . import dashboard
        self.assertIs(_tick(), tick)  # cached while tick.py is unchanged
        dashboard._tick_mtime = 0.0  # as if tick.py changed on disk (a merge): reloaded
        self.assertIsNot(_tick(), tick)
        window = usage["window"]
        self.assertEqual((window["start_utc"], window["end_utc"], window["ended"]), (iso(180), iso(-120), False))
        self.assertEqual([(p["pct"], p["source"]) for p in window["points"]],
                         [(10.0, "sampler"), (30.0, "deputy:work"), (35.0, "sampler")])
        latest = usage["latest"]
        self.assertEqual((latest["pct"], latest["age_seconds"], latest["stale"]), (35.0, 300, False))
        self.assertEqual(latest["baseline_pct"], round(tick.BASELINE_MAX * 175 / 300, 1))
        self.assertEqual(usage["history"], [{"end_utc": iso(60), "final_pct": 52.0, "peak_pct": 55.0, "samples": 3}])

    def test_backup_secretary_command_reads_the_private_layer_file_then_the_template(self) -> None:
        # control/layered.py: the private-layer file replaces the template when it exists
        page = (Path(__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")
        private = "incore/data/config-sync/private-layer/roles/codex-secretary.md in full if it exists; otherwise read "
        self.assertIn(private, page)
        self.assertLess(page.index("private-layer/roles/codex-secretary.md"), page.index("incore/control/roles/codex-secretary.md in full"))

    def test_project_names_cover_tick_projects(self) -> None:
        from .dashboard import _tick

        page = (Path(__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")
        project_names = re.search(r"const PROJECT_NAMES = \{(.*?)\};", page, re.S)
        self.assertIsNotNone(project_names)
        page_keys = set(re.findall(r"\b([a-z][a-z0-9]*):", project_names.group(1)))
        for key in _tick().PROJECTS:
            with self.subTest(project=key):
                self.assertIn(key, page_keys)

    def test_usage_missing_empty_and_ended(self) -> None:
        usage = build_snapshot(self.root, now=NOW)["usage"]
        self.assertEqual((usage["state"], usage["latest"], usage["window"], usage["history"]), ("missing", None, None, []))
        write_usage(self.root, ["", "garbage"])
        self.assertEqual(build_snapshot(self.root, now=NOW)["usage"]["state"], "empty")
        write_usage(self.root, [obs(400, 0.30, 60), obs(200, 0.52, 60)])
        usage = build_snapshot(self.root, now=NOW)["usage"]
        self.assertTrue(usage["window"]["ended"])
        self.assertTrue(usage["latest"]["stale"])
        self.assertEqual(usage["history"][0]["final_pct"], 52.0)
        self.assertFalse((self.root / "control" / "data" / "deputies.json").exists())

    def test_deputies_missing_means_not_enabled(self) -> None:
        deputies = build_snapshot(self.root, now=NOW)["deputies"]
        self.assertEqual(deputies["state"], "missing")
        self.assertEqual([(r["key"], r["enabled"]) for r in deputies["roles"]],
                         [(key, False) for key in ("coordinator", "work", "uvox", "life", "quant", "intelligence")])

    def test_deputies_fields(self) -> None:
        roles = {r["key"]: r for r in self.full()["deputies"]["roles"]}
        self.assertEqual(roles["coordinator"]["last"],
                         {"finished_utc": iso(20), "ok": True, "elapsed_seconds": 95.5, "summary": "审阅完毕"})
        self.assertIsNone(roles["coordinator"]["current"])
        self.assertEqual(roles["coordinator"]["runs"], 4)
        work = roles["work"]
        self.assertEqual((work["status"], work["current"]["origin"], work["current"]["elapsed_seconds"]),
                         ("running", "coordinator", 180))
        self.assertEqual(work["last"]["summary"], "失败于 file.txt")
        self.assertFalse(roles["quant"]["enabled"])
        self.assertFalse(roles["intelligence"]["enabled"])
        self.assertTrue(roles["life"]["enabled"])

    def test_plans_per_line_in_dispatch_order(self) -> None:
        plans = self.full()["plans"]
        self.assertEqual(plans["state"], "ok")
        roles = {p["key"]: p for p in plans["roles"]}
        self.assertEqual([p["key"] for p in plans["roles"]], ["coordinator", "work", "uvox", "quant", "intelligence", "life"])
        work = roles["work"]
        self.assertEqual((work["section"], work["weight_pct"], work["weight_note"]), ("Arbitros 线", 60.0, "Uvox 优先于 Umba"))  # the old title still reads, as the Arbitros line
        self.assertEqual(work["running"], [{"text": "Uvox T5 修复：示例项目被锁", "owner": False, "detail": "23:12"}])
        self.assertEqual(work["review"], [{"text": "Umba 贴图：见 umba.md", "owner": False}])
        self.assertEqual([t["text"] for t in work["todo"]], ["第一条待办", "第二条待办 [已隐藏]"])
        self.assertEqual(work["blocked"], [{"text": "养老院同意书", "owner": False, "detail": "等你决定"}])
        self.assertEqual(work["done_count"], 1)  # done-ref, "each tick" and the answered owner item are not counted
        # An open owner instruction is listed under the role it was sent to, without its message trailer.
        self.assertEqual(roles["quant"]["todo"], [{"text": "【Discord·量化副总理】看看开户", "owner": True}])
        self.assertEqual((roles["coordinator"]["weight_pct"], roles["coordinator"]["weight_note"]), (10.0, None))
        self.assertEqual(roles["coordinator"]["blocked"][0]["detail"], "定时，10-07 起跑")
        self.assertEqual(roles["life"]["todo"], [])

    def test_plan_todos_carry_tick_project(self) -> None:
        from .dashboard import _tick
        tick = _tick()
        todos = ["论文 文献更新", "项目七 战斗手感", "（返工）情报 每日扫描", "Uvox 阶段二", "随便一件杂事"]
        path = self.root / "projects" / "backlog.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("## 工作线\n" + "".join(f"- [todo] {t}\n" for t in todos)
                        + "## 你的指令（最优先）\n- [todo] 【Discord】Uvox 指令 〔msg:9 role:work〕\n", encoding="utf-8")

        def work_todos() -> list[dict]:
            plans = build_snapshot(self.root, now=NOW)["plans"]
            self.assertEqual(plans["state"], "ok")
            return next(p for p in plans["roles"] if p["key"] == "work")["todo"]

        rows = [r for r in work_todos() if not r["owner"]]
        self.assertEqual([r["project"] for r in rows], [tick.project_of(t) for t in todos])
        self.assertEqual([r["project"] for r in rows], ["thesis", "jianzhixin", "intel", "uvox", "general"])
        self.assertTrue(all("project" not in r for r in work_todos() if r["owner"]))  # the deputy's, not a project's
        with mock.patch(f"{_limits.__module__}._tick", side_effect=ImportError):  # tick.py unreadable: no labels, plans ok
            rows = [r for r in work_todos() if not r["owner"]]
        self.assertEqual([r["text"] for r in rows], todos)
        self.assertTrue(all("project" not in r for r in rows))

    def agents_fixture(self) -> None:
        def ex(sid, started=None, finished=None, **extra):
            return {"session_id": sid, "runs": 3, "context_tokens": 5000, "last_item": f"上次 {sid}",
                    "started_at": started, "finished_at": finished, "exit_code": 0, **extra}
        write_json(self.root, "control/data/deputies.json", {
            "work": {"session_id": "wwwwwwww00", "status": "running",
                     "current": {"kind": "review", "text_preview": "验收 Uvox", "started_at": iso(2)},
                     "executors": {
                         "uvox": ex("aaaaaaaa11", iso(30), iso(20)),                 # review row below
                         "umba": ex("bbbbbbbb22", iso(30), iso(20)),                 # （返工） todo routes to umba
                         "input": ex("cccccccc33", iso(30), iso(20), current={"kind": "terminal"}),  # live lock, owner's window
                         "jobs": ex("dddddddd44", iso(30), iso(20)),                 # only blocked
                         "thesis": ex("eeeeeeee55", iso(30), iso(20)),               # nothing
                         "writing": ex("ffffffff66", iso(10), None),                 # unfinished and recent
                         "driving": ex("gggggggg77", iso(600), None),                # unfinished but long dead
                         "general": ex("hhhhhhhh88", iso(30), iso(20)),              # todo without a project prefix
                         "jianzhixin": ex("iiiiiiii99", iso(30), iso(20))}},         # lock file with a dead pid
            "quant": {"status": "idle", "current": None, "executors": {}},
        })
        write_json(self.root, "control/data/kpi.json", {"executor_sessions": [
            {"role": "work", "project": "uvox", "session": "aaaaaaaa", "first_pass_rate": 0.857, "reviewed_n": 7}]})
        lock = self.root / "control" / "data"
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        (lock / "lane-work-input.lock").write_text(str(os.getpid()), encoding="utf-8")
        (lock / "lane-work-jianzhixin.lock").write_text(str(child.pid), encoding="utf-8")
        path = self.root / "projects" / "backlog.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("## 工作线\n- [review] Uvox 阶段一\n- [todo] （返工）Umba 贴图\n- [todo] 情报 每日扫描\n"
                        "- [todo] 随便一件杂事\n- [blocked: 等你决定] 求职 投递\n- [todo] 输入台 修一个字\n"
                        "## 你的指令（最优先）\n- [todo] 【Discord】Uvox 指令 〔msg:9 role:work〕\n", encoding="utf-8")

    @unittest.skipUnless(os.name == "nt", "lane locks use the Windows process API")
    def test_agent_status_derivation(self) -> None:
        self.agents_fixture()
        agents = build_snapshot(self.root, now=NOW)["agents"]
        self.assertEqual(agents["state"], "ok")
        work = next(r for r in agents["roles"] if r["key"] == "work")
        status = {a["project"]: a["status"] for a in work["agents"]}
        self.assertEqual(status, {"input": "running", "writing": "running", "uvox": "review", "umba": "todo",
                                  "intel": "unstarted", "general": "todo", "jobs": "blocked", "thesis": "idle",
                                  "driving": "idle", "jianzhixin": "idle"})
        # A live lock beats the todo row of the same project; a stale unfinished entry and a dead lock do not run.
        self.assertEqual([a["status"] for a in work["agents"]][:2], ["running", "running"])
        self.assertEqual(work["counts"], {"running": 2, "review": 1, "todo": 2, "blocked": 1, "idle": 3, "unstarted": 1})
        uvox = next(a for a in work["agents"] if a["project"] == "uvox")
        self.assertEqual((uvox["session"], uvox["runs"], uvox["review"], uvox["first_pass_rate"], uvox["reviewed"]),
                         ("aaaaaaaa", 3, 1, 0.857, 7))
        self.assertEqual(uvox["folder"], r"ClaudeMigration\project files\Uvox")
        # The owner instruction "Uvox ..." is the deputy's, not a project row.
        self.assertEqual(uvox["todo"], 0)
        self.assertEqual((work["running"], work["kind"], work["preview"]), (True, "验收", "验收 Uvox"))
        quant = next(r for r in agents["roles"] if r["key"] == "quant")
        self.assertEqual((quant["running"], quant["kind"], quant["agents"]), (False, None, []))

    @unittest.skipUnless(os.name == "nt", "lane locks use the Windows process API")
    def test_agent_talk_command_and_terminal_state(self) -> None:
        self.agents_fixture()
        roles = {r["key"]: r for r in build_snapshot(self.root, now=NOW)["agents"]["roles"]}
        talk = "pwsh -File " + str(Path(r"C:\orghome\orgcore_workspace") / "incore" / "control" / "talk.ps1")
        agents = {a["project"]: a for a in roles["work"]["agents"]}
        self.assertEqual((agents["uvox"]["command"], agents["uvox"]["terminal"]), (f"{talk} work uvox", False))
        self.assertEqual((agents["input"]["terminal"], agents["input"]["status"]), (True, "running"))
        self.assertEqual(agents["intel"]["command"], None)  # no session yet: the page says so instead
        self.assertEqual((roles["work"]["command"], roles["quant"]["command"]), (f"{talk} work", None))

    def test_after_the_work_line_split_the_board_shows_both_lines_with_their_own_agents_and_plans(self) -> None:
        """Owner 10-27 ("组织面板 still shows the old 工作线 agent and plan"): what the board reads (deputies.json executors, the
        backlog sections and their weights) after control/line_split.py: Arbitros and Beacon Listener side by side."""
        control = str(Path(__file__).resolve().parent.parent / "control")
        sys.path.insert(0, control)
        import line_split
        old = "\n".join([
            "## 你的指令（最优先）", "- [todo] 【Discord·Arbitros 副总理】x 〔msg:9 role:work〕", "",
            "## 工作线", "权重：36%（旧说明）", "- [todo] FOS：做事", "- [todo] Uvox：搬走的 Uvox 行", "- [review] Kiki：参考系",
            "- [blocked: 等你决定] Beacon Listener：官网", "- [todo] 输入台：修一个字", "- [done 10-20：审查通过] Uvox：留下的完成行", "",
            "## 量化线", "权重：22%", "", "## 组织线", "权重：16%", "- [todo] 组织：新的一行", "",
        ])
        ex = lambda p, sid: {"project": p, "session_id": sid, "runs": 3, "started_at": iso(30), "finished_at": iso(20), "current": None}
        deputies = {"work": {"session_id": "wwwwwwww00", "status": "idle", "current": None, "runs": 9,
                             "executors": {"fos": ex("fos", "ffffffff11"), "uvox": ex("uvox", "uuuuuuuu22"), "kiki": ex("kiki", "kkkkkkkk33"),
                                           "beaconlistener": ex("beaconlistener", "bbbbbbbb44"), "input": ex("input", "iiiiiiii55")}},
                    "coordinator": {"session_id": "cccccccc00", "status": "idle", "current": None, "executors": {}}}
        new_lines, _ = line_split.convert_backlog(old.split("\n"))
        new_deputies, _ = line_split.migrate_deputies(deputies, iso(5))
        new_deputies["uvox"].update(session_id="llllllll66", fork_from=None, fork_state="forked")  # as after its first run
        path = self.root / "projects" / "backlog.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(new_lines), encoding="utf-8")
        write_json(self.root, "control/data/deputies.json", new_deputies)
        snap = build_snapshot(self.root, now=NOW)
        plans = {p["key"]: p for p in snap["plans"]["roles"]}
        self.assertEqual([p["key"] for p in snap["plans"]["roles"]], ["coordinator", "work", "uvox", "quant", "intelligence", "life"])
        self.assertEqual((plans["work"]["section"], plans["work"]["weight_pct"]), ("Arbitros 线", 24.0))
        self.assertEqual((plans["uvox"]["section"], plans["uvox"]["weight_pct"]), ("Beacon Listener 线", 12.0))
        self.assertEqual([t["text"] for t in plans["work"]["todo"] if not t["owner"]], ["FOS：做事"])
        self.assertEqual([t["text"] for t in plans["work"]["todo"] if t["owner"]], ["【Discord·Arbitros 副总理】x"])  # the instruction stays with the role key it was sent to
        self.assertEqual([t["text"] for t in plans["uvox"]["todo"] if not t["owner"]], ["Uvox：搬走的 Uvox 行"])
        self.assertEqual([t["text"] for t in plans["uvox"]["review"]], ["Kiki：参考系"])
        self.assertEqual([t["text"] for t in plans["uvox"]["blocked"]], ["Beacon Listener：官网"])
        self.assertEqual(plans["uvox"]["done_count"], 0)  # the finished Uvox row stays in the Arbitros section as history
        self.assertEqual(plans["work"]["done_count"], 1)
        self.assertEqual([t["text"] for t in plans["coordinator"]["todo"]], ["组织：新的一行", "输入台：修一个字"])
        roles = {r["key"]: r for r in snap["deputies"]["roles"]}
        self.assertEqual((roles["work"]["enabled"], roles["uvox"]["enabled"]), (True, True))
        agents = {r["key"]: r for r in snap["agents"]["roles"]}
        self.assertEqual(sorted(a["project"] for a in agents["work"]["agents"]), ["fos"])
        self.assertEqual(sorted(a["project"] for a in agents["uvox"]["agents"]), ["beaconlistener", "kiki", "uvox"])
        self.assertEqual({a["project"]: a["status"] for a in agents["uvox"]["agents"]}, {"uvox": "todo", "kiki": "review", "beaconlistener": "blocked"})
        self.assertEqual(sorted(a["project"] for a in agents["coordinator"]["agents"]), ["incore", "input"])
        talk = "pwsh -File " + str(Path(r"C:\orghome\orgcore_workspace") / "incore" / "control" / "talk.ps1")
        self.assertEqual(agents["uvox"]["command"], f"{talk} uvox")
        self.assertEqual({a["project"]: a["command"] for a in agents["uvox"]["agents"]}["kiki"], f"{talk} uvox kiki")
        self.assertEqual({a["project"]: a["command"] for a in agents["coordinator"]["agents"]}["input"], f"{talk} coordinator input")
        self.assertNotIn("工作线", [p["section"] for p in snap["plans"]["roles"]])
        from . import server
        page = server.dashboard_labels((Path(__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8"))
        for key in ("uvox", "Beacon Listener 副总理", "Arbitros 副总理"):
            self.assertIn(key, page)
        self.assertNotIn("工作学习副总理", page)

    def test_agent_routing_is_ticks(self) -> None:
        from .dashboard import _tick
        tick = _tick()
        self.assertEqual((tick.project_of("（返工）输入台 x"), tick.project_of("语音 y"), tick.project_of("其他")),
                         ("input", "input", "general"))

    def test_agents_without_state_files(self) -> None:
        agents = build_snapshot(self.root, now=NOW)["agents"]
        self.assertEqual(agents["state"], "missing")
        self.assertEqual([r["key"] for r in agents["roles"]], ["coordinator", "work", "uvox", "life", "quant", "intelligence"])
        self.assertEqual({len(r["agents"]) for r in agents["roles"]}, {0})

    def test_plans_missing_backlog(self) -> None:
        plans = build_snapshot(self.root, now=NOW)["plans"]
        self.assertEqual(plans["state"], "missing")
        self.assertEqual({len(p["todo"]) for p in plans["roles"]}, {0})

    def test_backlog_over_json_limit_still_reads(self) -> None:
        from .dashboard import JSON_LIMIT, _plans, _read_json
        path = self.root / "projects" / "backlog.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("## 工作线\n" + "- [done] 旧条目\n" * (JSON_LIMIT // 10) + "- [todo] 新待办\n", encoding="utf-8")
        self.assertGreater(path.stat().st_size, JSON_LIMIT)
        plans = _plans(self.root)
        self.assertEqual(plans["state"], "ok")
        work = next(p for p in plans["roles"] if p["key"] == "work")
        self.assertEqual(([t["text"] for t in work["todo"]], work["done_count"] > 0), (["新待办"], True))
        write_json(self.root, "control/data/big.json", {"pad": "x" * JSON_LIMIT})  # other files keep the 2 MB cap
        self.assertEqual(_read_json(self.root, "control", "data", "big.json")[0], "unreadable")

    def test_queue_open_and_answered(self) -> None:
        queue = self.full()["queue"]
        self.assertEqual(queue["state"], "ok")
        self.assertEqual(queue["counts"], {"pending": 1, "blocked": 1, "needs_attention": 1, "queued_only": 1,
                                           "answered": 12})
        self.assertEqual([r["status"] for r in queue["open"]], ["queued_only", "needs_attention", "blocked", "pending"])
        self.assertEqual([r["origin"] for r in queue["open"]], ["input", "coordinator", "discord", "discord"])
        blocked = queue["open"][2]
        self.assertEqual((blocked["role"], blocked["next_attempt_utc"]), ("coordinator", iso(-10)))
        self.assertEqual(blocked["detail"], "限流 [已隐藏] x.log")
        self.assertTrue(queue["open"][0]["queued_only"])
        self.assertEqual(len(queue["answered"]), 10)
        self.assertEqual({(r["next_attempt_utc"], r["detail"]) for r in queue["answered"]}, {(None, None)})
        self.assertLessEqual(max(len(r["preview"]) for r in queue["open"] + queue["answered"]), 100)

    def test_missing_db_is_not_created(self) -> None:
        self.assertEqual(build_snapshot(self.root, now=NOW)["queue"]["state"], "missing")
        self.assertFalse(db_path(self.root).exists())
        self.assertFalse((self.root / "discord").exists())

    @unittest.skipUnless(os.name == "nt", "process check uses the Windows API")
    def test_services_pid_checks(self) -> None:
        mine = _process_created(os.getpid())
        self.assertIsNotNone(mine)
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        write_json(self.root, "input/data/server-process.json",
                   {"ProcessId": os.getpid(), "StartedAtUtc": mine.isoformat().replace("+00:00", "Z")})
        write_json(self.root, "data/backups/watcher-process.json",
                   {"ProcessId": child.pid, "StartedAtUtc": iso(1)})
        # Same live pid but a different recorded start: a reused pid is not the service.
        write_json(self.root, "discord/data/gateway-process.json",
                   {"ProcessId": os.getpid(), "StartedAtUtc": (mine - timedelta(days=1)).isoformat()})
        services = {s["key"]: s for s in build_snapshot(self.root, now=NOW)["services"]}
        self.assertEqual({key: s["state"] for key, s in services.items()},
                         {"input": "running", "backup": "stopped", "discord": "stopped"})
        self.assertEqual(services["input"]["pid"], os.getpid())
        for rel in ("input/data/server-process.json", "data/backups/watcher-process.json",
                    "discord/data/gateway-process.json"):
            (self.root / rel).unlink()
        self.assertEqual({s["state"] for s in build_snapshot(self.root, now=NOW)["services"]}, {"missing"})

    def test_secrets_and_paths_not_exposed(self) -> None:
        text = json.dumps(self.full(), ensure_ascii=False)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("Users\\\\owner", text)
        self.assertNotIn("reply ", text)
        self.assertEqual(all_keys(json.loads(text)) & FORBIDDEN_KEYS, set())

    def test_snapshot_does_not_modify_sources(self) -> None:
        self.full()
        files = sorted(p.relative_to(self.root) for p in self.root.rglob("*"))
        before = {p: digest(self.root / p) for p in files if (self.root / p).is_file()}
        build_snapshot(self.root, now=NOW)
        self.assertEqual(sorted(p.relative_to(self.root) for p in self.root.rglob("*")), files)
        self.assertEqual({p: digest(self.root / p) for p in before}, before)

    def test_scheduler_registered_only_with_a_supervisor_record_or_tick_log(self) -> None:
        self.assertFalse(build_snapshot(self.root, now=NOW)["scheduler_registered"])
        write_json(self.root, "control/data/supervisor-state.json", {"started_at": "2026-11-02T08:00:00-05:00"})
        self.assertTrue(build_snapshot(self.root, now=NOW)["scheduler_registered"])
        (self.root / "control/data/supervisor-state.json").unlink()
        (self.root / "control/data/tick-log.jsonl").write_text("{}\n", encoding="utf-8")
        self.assertTrue(build_snapshot(self.root, now=NOW)["scheduler_registered"])

    def test_supervisor_shows_merge_lag_and_handles_git_failure(self) -> None:
        from . import dashboard

        loaded = "a" * 40
        current = "b" * 40
        write_json(self.root, "control/data/supervisor-state.json", {
            "started_at": "2026-11-02T08:00:00-05:00", "master_commit": loaded, "tick_sha256": "c" * 64,
        })
        cases = (("same", "0\n", 0), ("one", "1\n", 1), ("multiple", "4\n", 4))
        for name, merge_count, expected in cases:
            with self.subTest(name=name):
                def git(_repo, *args):
                    if args == ("rev-parse", "master"):
                        return current + "\n"
                    if args == ("rev-list", "--merges", "--count", f"{loaded}..master"):
                        return merge_count
                    self.fail(f"unexpected git args: {args}")

                with mock.patch.object(dashboard, "_control", return_value=mock.Mock(git=git)):
                    result = build_snapshot(self.root, now=NOW)["supervisor"]
                self.assertEqual((result["state"], result["behind_merges"]), ("ok", expected))
                self.assertEqual((result["loaded_commit"], result["current_commit"]), (loaded, current))

        def git_failure(_repo, *args):
            return None

        with mock.patch.object(dashboard, "_control", return_value=mock.Mock(git=git_failure)):
            result = build_snapshot(self.root, now=NOW)["supervisor"]
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["current_commit"])
        self.assertIsNone(result["behind_merges"])


class InstanceLabelTests(unittest.TestCase):
    PAGE = (Path(__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")

    def test_default_labels_equal_the_owner_display(self) -> None:
        from . import server
        html = server.dashboard_labels(self.PAGE)
        self.assertIn("总理 · Claude Code（Opus 5.5）", html)
        self.assertIn("Arbitros 副总理", html)

    def test_instance_values_replace_names_and_escape(self) -> None:
        from . import server
        values = {"language": "zh", "secretary_card": True, "secretary_model": "m <1>", "coordinator_label": "Chief <b>", "coordinator_note": "", "role_names": {"coordinator": "Chief", "work": "Deputy A </script>"}}
        with mock.patch.object(server.ic, "get", values.__getitem__):
            html = server.dashboard_labels(self.PAGE)
        self.assertIn("Chief &lt;b&gt;", html)
        self.assertIn("-m m &lt;1&gt; -c", html)
        self.assertIn("-m m &lt;1&gt;'", html)  # the stop command names the same model
        self.assertIn("reply in Chinese", html)
        self.assertNotIn("__SECRETARY", html)
        self.assertIn("const ROLE_NAMES = {", html)
        self.assertNotIn("</script>\"", html.split("const ROLE_NAMES = ")[1][:200])
        self.assertNotIn("Arbitros", html.split("const ROLE_NAMES = ")[1][:200])

    def test_secretary_card_and_language_follow_the_instance(self) -> None:
        from . import server
        base = {"language": "zh", "secretary_card": True, "secretary_model": "gpt-6.1-sol", "coordinator_label": "", "coordinator_note": "", "role_names": {}}
        with mock.patch.object(server.ic, "get", base.__getitem__):
            html = server.dashboard_labels(self.PAGE)
        self.assertIn('id="secretary-panel" aria-labelledby="secretary-title" >', html)
        self.assertIn("-m gpt-6.1-sol -c", html)
        off = {**base, "secretary_card": False, "language": "en"}
        with mock.patch.object(server.ic, "get", off.__getitem__):
            html = server.dashboard_labels(self.PAGE)
        self.assertIn('id="secretary-panel" aria-labelledby="secretary-title" hidden>', html)
        self.assertIn("reply in English", html)

    def test_nav_separators_follow_the_links_present(self) -> None:
        from . import server
        with mock.patch.object(server, "poetry_panel", None):
            nav = server.dashboard_labels(self.PAGE).split('<nav class="toplinks">')[1].split("</nav>")[0]
        self.assertEqual((nav.count("<a "), nav.count('class="sep"')), (3, 2))  # one separator per gap, none for the absent poetry link
        with mock.patch.object(server, "poetry_panel", object()):
            nav = server.dashboard_labels(self.PAGE).split('<nav class="toplinks">')[1].split("</nav>")[0]
        self.assertEqual((nav.count("<a "), nav.count('class="sep"')), (4, 3))

    def test_poetry_link_only_when_the_panel_is_installed(self) -> None:
        from . import server
        with mock.patch.object(server, "poetry_panel", None):
            self.assertNotIn("/poetry", server.dashboard_labels(self.PAGE))
        with mock.patch.object(server, "poetry_panel", object()):
            self.assertIn('href="/poetry"', server.dashboard_labels(self.PAGE))


class DeploymentTests(unittest.TestCase):
    """projects/deployments.json: registry read/write, commits behind, url checks that never stall the page."""

    def setUp(self) -> None:
        from . import dashboard
        self.dashboard = dashboard
        self.ctl = dashboard._control("deployments")
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "incore"
        (self.root / "projects").mkdir(parents=True)
        self.repo = Path(self.tmp.name) / "site"
        self.repo.mkdir()
        self.commits = []
        self.git("init", "-b", "main")
        for n in range(3):
            (self.repo / "f.txt").write_text(str(n), encoding="utf-8")
            self.git("add", "f.txt")
            self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", f"commit {n}")
            self.commits.append(self.git("rev-parse", "--short", "HEAD"))
        dashboard._url_cache.clear()
        dashboard._url_pending.clear()

    def git(self, *args: str) -> str:
        out = self.ctl.git(self.repo, *args)
        self.assertIsNotNone(out, args)
        return out

    def entry(self, **over) -> dict:
        base = {"name": "Site", "url": "http://127.0.0.1:9/", "access": "public", "repo": "site", "branch": "main",
                "commit": self.commits[0], "deployed_at": "2026-10-07T19:29:00-04:00"}
        return {**base, **over}

    def write(self, *entries: dict) -> Path:
        path = self.root / "projects" / "deployments.json"
        self.ctl.save(list(entries), path)
        return path

    def products(self) -> list[dict]:
        with mock.patch.object(self.dashboard, "_fetch_status", return_value="online"):
            return build_snapshot(self.root, now=NOW)["deployments"]["products"]

    def test_record_updates_one_entry_and_resolves_commit(self) -> None:
        path = self.write(self.entry(), self.entry(name="Other"))
        done = self.ctl.record("site", self.commits[2], "feature", path, self.root)
        self.assertEqual((done["branch"], done["commit"]), ("feature", self.git("rev-parse", "HEAD")[:12]))
        saved = self.ctl.load(path)
        self.assertEqual(saved[0]["commit"], done["commit"])
        self.assertEqual(saved[1]["commit"], self.commits[0])
        self.assertNotEqual(saved[0]["deployed_at"], "2026-10-07T19:29:00-04:00")
        with self.assertRaises(SystemExit):
            self.ctl.record("site", "deadbeef", None, path, self.root)
        with self.assertRaises(SystemExit):
            self.ctl.record("missing product", self.commits[2], None, path, self.root)

    def test_shipped_registry_loads(self) -> None:
        products = self.ctl.load()
        self.assertIsInstance(products, list)
        required = {"name", "url", "access", "repo", "branch", "commit", "deployed_at", "run", "source"}
        for product in products:
            with self.subTest(name=product.get("name")):
                self.assertTrue(required <= product.keys(), f"missing fields: {required - product.keys()}")

    def test_registry_entry_without_url_is_not_shown(self) -> None:
        entries = [self.entry(name="Site", url="https://example.com/", run="static", source="test"),
                   self.entry(name="No URL", url=None, repo=None, branch=None, commit=None, run="static", source="test")]
        path = self.write(*entries)
        loaded = self.ctl.load(path)
        required = {"name", "url", "access", "repo", "branch", "commit", "deployed_at", "run", "source"}
        self.assertTrue(all(required <= entry.keys() for entry in loaded))
        self.assertEqual([product["name"] for product in self.products()], ["Site"])

    def test_commits_behind_head(self) -> None:
        self.write(self.entry(), self.entry(name="Current", commit=self.commits[2]))
        first, current = self.products()
        self.assertEqual(first["gap"]["behind"], 2)
        self.assertEqual(first["gap"]["head_title"], "commit 2")
        self.assertEqual(first["commit"], self.commits[0])
        self.assertEqual(first["deployed_at"], "2026-10-07T23:29:00Z")
        self.assertEqual(current["gap"]["behind"], 0)
        self.assertNotIn(str(self.repo), json.dumps(first))

    def test_missing_repo_branch_or_commit_is_unknown_not_an_error(self) -> None:
        self.write(self.entry(name="a", repo="nowhere"), self.entry(name="b", branch="gone"),
                   self.entry(name="c", commit="0123456"), self.entry(name="d", repo=None, branch=None, commit=None,
                                                                      deployed_at="2026-10-04"))
        gaps = {p["name"]: p for p in self.products()}
        self.assertEqual([gaps[k]["gap"]["state"] for k in "abc"], ["unknown"] * 3)
        self.assertEqual(gaps["d"]["gap"]["state"], "norepo")
        self.assertEqual(gaps["d"]["deployed_at"], "2026-10-04")

    def test_missing_registry(self) -> None:
        self.assertEqual(build_snapshot(self.root, now=NOW)["deployments"], {"state": "missing", "products": []})

    def test_url_status_200_and_401(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(401 if self.path.startswith("/locked") else 200)
                self.end_headers()

            def log_message(self, *args) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        self.write(self.entry(name="open", url=f"{base}/"), self.entry(name="locked", url=f"{base}/locked"),
                   self.entry(name="dead", url="http://127.0.0.1:9/"))
        import time
        for _ in range(20):  # a refused connection can take a while on Windows; slow ones show "checking" meanwhile
            states = {p["name"]: p["online"] for p in build_snapshot(self.root, now=NOW)["deployments"]["products"]}
            if "checking" not in states.values():
                break
            time.sleep(0.5)
        self.assertEqual(states, {"open": "online", "locked": "password", "dead": "down"})

    def test_slow_url_does_not_hold_up_the_page(self) -> None:
        import threading
        import time
        release = threading.Event()
        self.addCleanup(release.set)
        self.write(self.entry(), self.entry(name="b", url="http://127.0.0.1:9/b"))
        started = time.monotonic()
        with mock.patch.object(self.dashboard, "_fetch_status", lambda url: release.wait(10) and "online"):
            first = build_snapshot(self.root, now=NOW)["deployments"]["products"]
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertEqual({p["online"] for p in first}, {"checking"})
            second_started = time.monotonic()
            build_snapshot(self.root, now=NOW)  # still pending: no new checks, no wait on them beyond the cap
            self.assertLess(time.monotonic() - second_started, 0.5)
            release.set()
            time.sleep(0.5)
            again = build_snapshot(self.root, now=NOW)["deployments"]["products"]
        self.assertEqual({p["online"] for p in again}, {"online"})
        self.assertEqual(len(self.dashboard._url_cache), 2)  # cached: the next read does not re-check


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    async def client(self, **private: str) -> TestClient:
        app = create_app(data_root=self.root / "input-data", expected_port=8765, dashboard_root=self.root, **private)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_local_routes_read_only(self) -> None:
        client = await self.client()
        response = await client.get("/api/organization", headers={"Host": LOCAL})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["schema_version"], 2)
        self.assertEqual((body["queue"]["state"], body["deputies"]["state"]), ("missing", "missing"))
        self.assertFalse(db_path(self.root).exists())
        page = await client.get("/dashboard", headers={"Host": LOCAL})
        self.assertEqual(page.status, 200)
        html = await page.text()
        nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", page.headers["Content-Security-Policy"]).group(1)
        self.assertIn(f'nonce="{nonce}"', html)
        self.assertNotIn("__CSP_NONCE__", html)
        self.assertNotIn("innerHTML", html)
        self.assertIn("const SCHEMA = 2;", html)
        self.assertNotRegex(html, r"__[A-Z_]+__")  # every instance placeholder was filled
        self.assertIn("const ROLE_NAMES = {", html)
        index = await client.get("/", headers={"Host": LOCAL})
        self.assertIn('href="/dashboard"', await index.text())

    async def test_trust_boundary_protects_dashboard(self) -> None:
        client = await self.client(https_host=PRIVATE_HOST, owner_login=OWNER)
        for path in ("/dashboard", "/api/organization"):
            self.assertEqual((await client.get(path, headers={"Host": "evil.example"})).status, 421)
            self.assertEqual((await client.get(path, headers={"Host": PRIVATE_HOST})).status, 403)
            wrong = {"Host": PRIVATE_HOST, IDENTITY_HEADER: "someone@example.com"}
            self.assertEqual((await client.get(path, headers=wrong)).status, 403)
            owner = {"Host": PRIVATE_HOST, IDENTITY_HEADER: OWNER}
            self.assertEqual((await client.get(path, headers=owner)).status, 200)
            cross = {"Host": LOCAL, "Sec-Fetch-Site": "cross-site"}
            self.assertEqual((await client.get(path, headers=cross)).status, 403)
            self.assertEqual((await client.get(path, headers={"Host": LOCAL, "Origin": "http://evil.example"})).status,
                             403)
        self.assertFalse(db_path(self.root).exists())


if __name__ == "__main__":
    unittest.main()
