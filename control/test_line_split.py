"""Work-line split (owner\x2010-26/10-27): ledger folding, routing, backlog and deputies migration with its reverse, the one-time
fork of the Beacon Listener deputy, reports and the idle-gate. Plan: audits/2026-10-26-work-line-split-plan.md 5, 6, 8.2."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import line_map  # noqa: E402
import line_split  # noqa: E402
import tick  # noqa: E402

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
SRC = "af5b1df5-c3e4-476d-bb84-8f01ebe66c0e"
CHILD = "11111111-2222-4333-8444-555555555555"
CR = "\r"

BACKLOG = "\n".join([
    "# 总理待办", "", "## 你的指令（最优先）", "- [done 16:02] 【Discord·工作副总理】报一下 〔msg:1 role:work〕", "",
    "## 工作线",
    "权重：36%（【owner\x2010-26 15:3x】总理 10-25 调：34→36；原 34%）（总理 10-22 旧说明）",
    "冲刺：FactoryAlign，版本目标：（owner\x2010-20）一个版本",
    "- [done 10-27 04:2x：审查通过] FOS：已完成的一行",
    "- [todo] FOS：要做的 FOS 行",
    "- [todo] FactoryAlign：要做的 CPBench 行",
    "- [done 10-20 09:00：审查通过] Uvox：已完成的 Uvox 行留在原处",
    "- [todo] Uvox：要搬的 Uvox 行",
    "  缩进的续行跟着 Uvox 行走",
    "- [blocked: 等你决定] Beacon Listener：官网等你定",
    "- [review] （返工）Kiki：参考系补充",
    "- [blocked: 暂停到 FOS 视频交付] Field Observer：跨语言收尾",
    "- [blocked: 等你决定] 输入台：语音微调要不要继续",
    "- [todo] 语音：另一条输入台待办",
    "- [done-ref] Kiki：旧的 Kiki 参考行",
    "【给工作副总理】（总理 10-04）这段说明归 Arbitros",
    "Beacon Listener 官网（owner\x2010-02 新项目，优先级中下）这段项目说明跟着搬",
    "- [todo] Umba：存量项目留在 Arbitros",
    "- [todo] 求职：留在 Arbitros", "",
    "## 量化线", "权重：22%", "- [todo] 量化：x", "",
    "## 组织线", "权重：16%（说明）", "- [done 10-26] 组织：旧", "- [todo] 组织：新的一行", "",
])


def crlf(text):
    return text.replace("\n", "\r\n")


def deputies():
    ex = lambda p, sid, runs: {"project": p, "session_id": sid, "runs": runs, "finished_at": "2026-10-27T05:50:15+00:00", "current": None}
    return {
        "work": {"session_id": SRC, "created_at": "x", "status": "idle", "current": None, "last": None, "runs": 641, "context_tokens": 144370,
                 "executors": {"uvox": ex("uvox", "u-1", 82), "beaconlistener": ex("beaconlistener", "b-1", 7), "fieldlistener": ex("fieldlistener", "f-1", 7),
                               "kiki": ex("kiki", "k-1", 1), "input": ex("input", "in-work", 17), "fos": ex("fos", "fos-1", 96), "general": ex("general", "g-1", 8)}},
        "coordinator": {"session_id": "c", "status": "idle", "current": None, "runs": 5,
                        "executors": {"input": ex("input", "in-coord", 2), "incore": ex("incore", "inc-1", 128)}},
        "life": {"session_id": "l", "status": "idle", "current": None, "runs": 1, "executors": {}},
    }


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.d = Path(self.tmp.name)
        (d / "rules.md").write_text("RULES", encoding="utf-8")
        for name, value in {"DATA": d, "PAUSE_FILE": d / "tick.PAUSE", "LOCK_FILE": d / "tick.lock", "OWNER_LOCK": d / "owner.lock",
                            "ASK_LOCK": d / "ask.lock", "REPORT_LOCK": d / "report.lock", "REPORT_FILE": d / "reports.json",
                            "BACKLOG": d / "backlog.md", "BACKLOG_HISTORY": d / "backlog-history", "RULES_FILE": d / "rules.md", "DEPUTIES_FILE": d / "deputies.json",
                            "RUNS_FILE": d / "runs.jsonl", "LOG_FILE": d / "tick-log.jsonl", "TICKS_DIR": d / "ticks",
                            "OWNER_DATA": d / "owner-data", "LESSONS_DIR": d / "lessons", "USAGE_FILE": d / "usage.jsonl"}.items():
            p = mock.patch.object(tick, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in {"MANIFEST": d / "line-split.json", "tick": tick}.items():
            p = mock.patch.object(line_split, name, value)
            p.start()
            self.addCleanup(p.stop)

    def put_deputies(self, data=None):
        (self.d / "deputies.json").write_text(json.dumps(data or deputies(), ensure_ascii=False), encoding="utf-8")

    def get_deputies(self):
        return json.loads((self.d / "deputies.json").read_text(encoding="utf-8"))


class LedgerFold(Fixture):
    def row(self, **kw):
        return {"at": (NOW - timedelta(hours=1)).isoformat(), "kind": "review", "section": "工作线", "role": "work", "cost_usd": 3.0,
                "project": None, "item": "", "ok": True, **kw}

    def test_fold_follows_project_or_item_prefix_and_leaves_others_alone(self):
        f = line_map.fold
        self.assertEqual(f(self.row(kind="executor", project="uvox", item="Uvox：a"))["section"], "Beacon Listener 线")
        self.assertEqual(f(self.row(kind="executor", project="uvox", item="Uvox：a"))["role"], "uvox")  # its session moved
        self.assertEqual(f(self.row(kind="review", item="（返工）Kiki：b"))["section"], "Beacon Listener 线")
        self.assertEqual(f(self.row(kind="review", item="（返工）Kiki：b"))["role"], "work")  # a review ran in the work deputy's session
        umzy = f(self.row(kind="executor", project="umzy", item="Umzy：u"))  # owner\x2011-07: Umzy is a Beacon Listener project
        self.assertEqual((umzy["section"], umzy["role"]), ("Beacon Listener 线", "uvox"))
        self.assertEqual(f(self.row(kind="review", item="Umzy：u2"))["section"], "Beacon Listener 线")
        self.assertEqual(line_map.project_of_item("（返工）Umzy：u3"), "umzy")
        inp = f(self.row(kind="executor", project="input", item="输入台：c"))
        self.assertEqual((inp["section"], inp["role"]), ("组织线", "coordinator"))
        fos = f(self.row(kind="executor", project="fos", item="FOS：d"))
        self.assertEqual((fos["section"], fos["role"], "legacy_split" in fos), ("Arbitros 线", "work", False))
        general = f(self.row(kind="executor", project="general", item="杂项"))
        self.assertEqual((general["section"], "legacy_split" in general), ("Arbitros 线", False))
        self.assertTrue(f(self.row(kind="plan", item=""))["legacy_split"])
        other = {"section": "量化线", "role": "quant"}
        self.assertIs(f(other), other)
        raw = self.row(kind="executor", project="uvox")
        f(raw)
        self.assertEqual(raw["section"], "工作线")  # a copy: the row (and runs.jsonl) is never rewritten

    def test_cost_7d_keeps_the_total_and_does_not_hand_all_of_it_to_arbitros(self):
        runs = [self.row(kind="executor", project="uvox", item="Uvox：a", cost_usd=6.0), self.row(kind="executor", project="fos", cost_usd=2.0),
                self.row(kind="plan", cost_usd=3.0), self.row(kind="review", item="Kiki：x", cost_usd=1.0), self.row(kind="coordinate", cost_usd=0.0)]
        for r in runs:
            r["_at"] = NOW - timedelta(hours=1)
        cost = tick.cost_7d([line_map.fold(r) for r in runs], NOW)
        self.assertAlmostEqual(sum(cost.values()), 12.0)
        self.assertAlmostEqual(cost["Beacon Listener 线"], 6.0 + 1.0 + 1.0)  # uvox executor + Kiki review + one third of the unattributed plan
        self.assertAlmostEqual(cost["Arbitros 线"], 2.0 + 2.0)  # fos + two thirds of the plan
        self.assertNotIn("工作线", cost)

    def test_load_runs_folds_and_starvation_and_cache_heat_do_not_start_from_zero(self):
        recent = {"at": (NOW - timedelta(minutes=10)).isoformat()}
        rows = [self.row(kind="executor", project="uvox", item="Uvox：a", session_id="u-1"),
                self.row(kind="executor", project="fos", item="FOS：a"),
                self.row(kind="review", item="Uvox：a", **recent)]
        (self.d / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        runs = tick.load_runs()
        self.assertEqual(sorted(tick.last_executor_runs(runs, "uvox")), ["uvox"])  # the moved project's history is the new line's
        self.assertEqual(sorted(tick.last_executor_runs(runs, "work")), ["fos"])
        self.assertFalse(tick.cache_hot(runs, "uvox", NOW))  # the work deputy's session is not the new deputy's: not cache-hot
        self.assertTrue(tick.cache_hot(runs, "work", NOW))
        self.assertEqual({r["section"] for r in runs}, {"Beacon Listener 线", "Arbitros 线"})
        self.assertEqual({json.loads(l)["section"] for l in (self.d / "runs.jsonl").read_text(encoding="utf-8").splitlines()}, {"工作线"})  # the file is as it was


class MeetingTurn(Fixture):
    def test_a_meeting_turn_of_the_unforked_role_does_not_start_blank_and_waits_for_its_source(self):
        import meeting
        (self.d / "meetings").mkdir()
        (self.d / "meetings" / "2026-10-27-t1.md").write_text("# 会议\n\n- 议题：x\n- 与会人：Beacon Listener 副总理 · uvox\n", encoding="utf-8")
        with mock.patch.object(meeting, "MEETINGS", self.d / "meetings"), mock.patch.object(tick, "run_resident") as run:
            self.put_deputies({"work": deputies()["work"]})  # not migrated yet
            self.assertEqual(meeting.turn("uvox"), 5)
            d = deputies()
            d["uvox"] = {"session_id": None, "fork_from": SRC, "fork_state": "pending"}
            self.put_deputies(d)
            (self.d / "deputy-work.lock").write_text("4242")
            with mock.patch.object(tick, "pid_alive", return_value=True):
                self.assertEqual(meeting.turn("uvox"), 3)
        run.assert_not_called()
        self.assertIn("uvox", meeting.attendees(self.d / "meetings" / "2026-10-27-t1.md"))


class StatsReaders(Fixture):
    """kpi.py and review_stats.py read the old ledger too: the same fold, the same numbers as choose_lane scores."""

    def rows(self):
        at = (NOW - timedelta(hours=2)).isoformat()
        base = {"at": at, "section": "工作线", "ok": True, "model": "opus", "role": "work", "session_id": "s"}
        return [{**base, "kind": "executor", "project": "uvox", "item": "Uvox：a", "cost_usd": 6.0, "role": "work"},
                {**base, "kind": "review", "project": None, "item": "Uvox：a", "cost_usd": 1.0, "verdict": "pass"},
                {**base, "kind": "plan", "project": None, "item": "", "cost_usd": 3.0}]

    def test_kpi_load_folds_the_old_rows(self):
        import kpi
        (self.d / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in self.rows()) + "\n", encoding="utf-8")
        with mock.patch.object(kpi, "RUNS_FILE", self.d / "runs.jsonl"):
            loaded = kpi.load(NOW)
        self.assertEqual([(r["section"], r["role"]) for r in loaded], [("Beacon Listener 线", "uvox"), ("Beacon Listener 线", "work"), ("Arbitros 线", "work")])
        self.assertEqual(kpi.review_join(loaded), {0: "pass"})  # the executor row and its review still meet: both are on the Beacon Listener line now

    def test_review_stats_shares_table_gives_the_new_lines_their_part_of_the_old_cost(self):
        import review_stats
        new, _ = line_split.convert_backlog(BACKLOG.split("\n"))
        text = review_stats.render(line_map.fold_all([{**r, "_at": datetime.fromisoformat(r["at"])} for r in self.rows()]), [], [], "\n".join(new), tick, NOW)
        self.assertIn("| Beacon Listener 线 | 12% | $8.00 |", text)  # executor 6 + review 1 + a third of the unattributed plan (3)
        self.assertIn("| Arbitros 线 | 24% | $2.00 |", text)


class Routing(Fixture):
    def parse(self, text=BACKLOG):
        return tick.parse_backlog(text, NOW)

    def test_sections_and_roles_are_one_to_one_and_the_old_title_still_dispatches_work(self):
        self.assertEqual(len(set(tick.LINE_SECTIONS.values())), len(tick.LINE_SECTIONS))
        for role, section in tick.LINE_SECTIONS.items():
            self.assertEqual((tick.SECTION_ROLES[section], tick.role_section(role)), (role, section))
        self.assertEqual(tick.SECTION_ROLES["工作线"], "work")  # an unconverted backlog keeps dispatching
        self.assertEqual(tick.role_section("work"), "Arbitros 线")
        self.assertEqual((tick.ROLE_NAMES["work"], tick.ROLE_NAMES["uvox"]), ("Arbitros 副总理", "Beacon Listener 副总理"))
        self.assertEqual(tick.REPORT_ROLES, ("coordinator", "work", "uvox", "quant", "intelligence", "life"))
        self.assertEqual((tick.deputy_model("uvox", "review"), tick.deputy_effort("uvox", "review")), ("opus", "medium"))

    def test_converted_backlog_routes_each_row_to_its_line_and_project(self):
        new, _ = line_split.convert_backlog(BACKLOG.split("\n"))
        secs = self.parse("\n".join(new))
        self.assertEqual(list(secs), ["Arbitros 线", "Beacon Listener 线", "量化线", "组织线"])
        self.assertEqual((secs["Arbitros 线"]["weight"], secs["Beacon Listener 线"]["weight"]), (24.0, 12.0))
        self.assertEqual(secs["Arbitros 线"]["sprint"], "factoryalign")
        todo = lambda s: [tick.project_of(i) for _, i in secs[s]["todo"]]
        self.assertEqual(todo("Beacon Listener 线"), ["uvox"])
        self.assertEqual([tick.project_of(i) for _, i in secs["Beacon Listener 线"]["review_rows"]], ["kiki"])
        self.assertEqual(sorted(todo("Arbitros 线")), ["factoryalign", "fos", "jobs", "umba"])
        self.assertEqual(todo("组织线"), ["incore", "input"])
        self.assertEqual(tick.project_cwd("kiki"), tick.PROJECTS["kiki"][0])
        self.assertEqual(tick.project_cwd("kiki").parts[-2:], ("projects", "kiki-research"))

    def test_choose_lane_gives_each_line_its_own_executor_and_the_old_work_unit_is_not_reused(self):
        new, _ = line_split.convert_backlog(BACKLOG.split("\n"))
        text = "\n".join(new)
        only = text.replace("权重：24%", "权重：0%").replace("权重：22%", "权重：0%").replace("权重：16%", "权重：0%")  # only the Beacon Listener line dispatches
        quiet = {"coordinator"}
        self.assertEqual(tick.choose_lane(only, [], NOW, busy=quiet, deputy_busy=quiet), ("uvox", None))  # its review row first (the Kiki row)
        self.assertEqual(tick.choose_lane(only, [], NOW, busy=quiet, deputy_busy=quiet | {"uvox"}), ("uvox", "uvox"))  # then the Uvox executor, not work:uvox
        self.assertIsNone(tick.choose_lane(only, [], NOW, busy=quiet | {"uvox:uvox"}, deputy_busy=quiet | {"uvox"}))

    def test_umzy_row_in_the_beacon_listener_section_dispatches_there_not_on_arbitros(self):  # owner\x2011-07
        text = ("## Arbitros 线\n权重：0%\n- [todo] FOS：甲\n## Beacon Listener 线\n权重：12%\n- [todo] Umzy：乙\n"
                "## 量化线\n权重：0%\n## 组织线\n权重：0%\n")
        secs = self.parse(text)
        self.assertEqual([tick.project_of(i) for _, i in secs["Beacon Listener 线"]["todo"]], ["umzy"])
        self.assertEqual([tick.project_of(i) for _, i in secs["Arbitros 线"]["todo"]], ["fos"])
        quiet = {"coordinator"}
        self.assertEqual(tick.choose_lane(text, [], NOW, busy=quiet, deputy_busy=quiet | {"uvox"}), ("uvox", "umzy"))  # the Umzy executor runs under the uvox line
        self.assertEqual(line_map.project_of_item("Umzy：乙"), "umzy")

    def test_a_forked_role_not_yet_forked_has_no_deputy_unit_but_its_executor_runs(self):
        self.put_deputies({"work": deputies()["work"]})  # no uvox entry: line_split.py not applied
        self.assertFalse(tick.deputy_ready("uvox"))
        self.assertTrue(tick.deputy_ready("work") and tick.deputy_ready("quant"))
        new, _ = line_split.convert_backlog(BACKLOG.split("\n"))
        text = "\n".join(new)
        only = text.replace("权重：24%", "权重：0%").replace("权重：22%", "权重：0%").replace("权重：16%", "权重：0%")
        unit = tick.choose_lane(only, [], NOW, busy={"coordinator"}, deputy_busy={"coordinator", "uvox"})
        self.assertEqual(unit, ("uvox", "uvox"))  # the review row of the line waits (deputy not free), the executor does not
        with self.assertRaises(tick.DeputyNotReady):
            tick.run_resident("uvox", "plan", "p", {}, {})

    def test_report_slot_of_an_unforked_role_is_missed_and_the_email_has_six_parts(self):
        self.put_deputies({"work": deputies()["work"]})
        self.assertFalse(tick.deputy_ready("uvox"))
        sent = []
        body = {}
        with mock.patch.object(tick, "send_email", side_effect=lambda s, b: sent.append((s, b)) or True):
            day = {f"text:{r}@09:00": f"报告 {r}" for r in tick.REPORT_ROLES if r != "uvox"}
            tick.send_slot_email(datetime(2026, 10, 28, 9, 5), "09:00", day, None)
        text = sent[0][1]
        self.assertEqual(text.count("【"), 6)
        for name in tick.ROLE_NAMES.values():
            self.assertIn(f"【{name}】", text)
        self.assertIn("【Beacon Listener 副总理】\n未出", text)
        self.assertEqual(len(sent), 1)


class Fork(Fixture):
    """tick.run_resident for role uvox: one fork of the work session, the child's real id from the stream, no blank session."""

    def setUp(self):
        super().setUp()
        d = deputies()
        d["uvox"] = {"session_id": None, "created_at": None, "status": "idle", "current": None, "last": None, "runs": 0,
                     "fork_from": SRC, "fork_state": "pending", "fork_tries": 0, "executors": {}}
        self.put_deputies(d)
        self.calls = []
        self.script = []  # per call: (exit_code, [stream events])

    def fake(self, kind, prompt, run_file, extra=(), effort=None, model=None, compact_at=None, cwd=None, add_dirs=()):
        self.calls.append({"kind": kind, "prompt": prompt, "extra": list(extra)})
        code, events = self.script.pop(0) if self.script else (0, self.ok_stream(CHILD))
        run_file.parent.mkdir(parents=True, exist_ok=True)
        run_file.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        return code

    @staticmethod
    def init(sid):
        return {"type": "system", "subtype": "init", "session_id": sid}

    @staticmethod
    def result(sid, cost=12.5, is_error=False):
        return {"type": "result", "is_error": is_error, "result": "好的", "session_id": sid, "total_cost_usd": cost, "usage": {}}

    @staticmethod
    def assistant():
        return {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "hi"}],
                                                  "usage": {"input_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}

    def reset(self):
        dep = self.get_deputies()
        dep["uvox"] = {"session_id": None, "created_at": None, "status": "idle", "current": None, "last": None, "runs": 0,
                       "fork_from": SRC, "fork_state": "pending", "fork_tries": 0, "executors": {}}
        self.put_deputies(dep)

    def ok_stream(self, sid, cost=12.5):
        return [self.init(sid), self.assistant(), self.result(sid, cost)]

    def go(self, kind="plan", **kw):
        with mock.patch.object(tick, "run_claude", side_effect=self.fake):
            return tick.run_resident("uvox", kind, "任务", {"source": "lane"}, {"kind": kind}, **kw)

    def ledger(self):
        return [json.loads(x) for x in (self.d / "runs.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_first_run_forks_once_then_only_resumes_the_child_and_the_source_is_untouched(self):
        code, _, ok, _, _ = self.go()
        self.assertTrue(ok)
        self.assertEqual(self.calls[0]["extra"], ["--resume", SRC, "--fork-session"])
        self.assertTrue(self.calls[0]["prompt"].startswith("【首次启动】"))
        self.assertIn("只管 Beacon Listener 线", self.calls[0]["prompt"])
        dep = self.get_deputies()
        self.assertEqual((dep["uvox"]["session_id"], dep["uvox"]["forked_from"], dep["uvox"]["fork_state"], dep["uvox"]["fork_from"]),
                         (CHILD, SRC, "forked", None))
        self.assertEqual(dep["work"]["session_id"], SRC)  # the source session is not written back
        self.go("review")
        self.go("review")
        self.assertEqual([c["extra"] for c in self.calls[1:]], [["--resume", CHILD], ["--resume", CHILD]])  # never forked twice
        self.assertFalse(any(c["prompt"].startswith("【首次启动】") for c in self.calls[1:]))
        rows = self.ledger()
        self.assertEqual([r["session"] for r in rows], ["fork", "resume", "resume"])
        self.assertEqual({r["session_id"] for r in rows}, {CHILD})

    def test_the_fork_is_priced_from_its_own_stream_never_negative_never_the_parents_history(self):
        self.go()
        row = self.ledger()[0]
        self.assertTrue(row["cost_estimated"])
        self.assertEqual(row["fork_from"], SRC)
        self.assertGreaterEqual(row["cost_usd"], 0.0)
        self.assertLess(row["cost_usd"], 0.05)  # 12.5 in the result would be the parent's cumulative total: the 1000-token stream is the cost
        self.script = [(0, [self.init(CHILD), self.assistant()])]
        (self.d / "runs.jsonl").unlink()
        self.reset()
        self.go()  # killed before its result event: no total at all, the estimate stands
        self.assertGreaterEqual(self.ledger()[0]["cost_usd"], 0.0)

    def test_result_id_differing_from_init_or_equal_to_the_source_stops_the_fork_for_review(self):
        for events in ([self.init(CHILD), self.result("99999999-2222-4333-8444-555555555555")], [self.init(SRC), self.result(SRC)],
                       [self.init("not-a-uuid"), self.result("not-a-uuid")]):
            self.reset()
            self.script = [(0, events)]
            _, _, ok, _, _ = self.go()
            dep = self.get_deputies()["uvox"]
            self.assertFalse(ok)
            self.assertEqual((dep["session_id"], dep["fork_state"]), (None, "blocked"))
            self.assertFalse(tick.deputy_ready("uvox"))  # no blank session, no second fork
            with self.assertRaises(tick.DeputyNotReady):
                self.go()

    def test_no_id_at_all_retries_after_a_transient_stop_and_blocks_on_the_second_miss(self):
        self.script = [(tick.CEILING_STOP, [])]
        self.go()
        dep = self.get_deputies()["uvox"]
        self.assertEqual((dep["fork_state"], dep["fork_tries"], tick.deputy_ready("uvox")), ("pending", 1, True))  # nothing was created: retry is safe
        self.script = [(1, [{"type": "result", "is_error": True, "result": "boom"}])]
        self.go()
        dep = self.get_deputies()["uvox"]
        self.assertEqual((dep["fork_state"], dep["session_id"], dep["fork_tries"]), ("blocked", None, 2))  # a real miss on the second try stops it
        self.assertFalse(tick.deputy_ready("uvox"))

    def test_a_fork_cut_off_after_its_init_event_is_adopted_not_repeated(self):
        self.script = [(-1, [self.init(CHILD), self.assistant()])]  # killed on timeout, no result
        self.go()
        dep = self.get_deputies()["uvox"]
        self.assertEqual(dep["session_id"], CHILD)  # the id was in the stream: kept at once
        self.go()
        self.assertEqual(self.calls[-1]["extra"], ["--resume", CHILD])
        self.assertEqual(sum("--fork-session" in c["extra"] for c in self.calls), 1)

    def test_crash_between_launch_and_save_is_recovered_from_the_recorded_stream(self):
        run_file = self.d / "ticks" / "crashed.jsonl"
        run_file.parent.mkdir(parents=True)
        run_file.write_text(json.dumps(self.init(CHILD)), encoding="utf-8")
        dep = self.get_deputies()
        dep["uvox"]["fork_run_file"] = str(run_file)  # the supervisor died before it could save the child id
        self.put_deputies(dep)
        self.go("review")
        self.assertEqual(self.calls[0]["extra"], ["--resume", CHILD])
        self.assertEqual(self.get_deputies()["uvox"]["forked_from"], SRC)

    def test_the_fork_waits_while_the_source_deputy_session_is_in_use_and_holds_its_lock_while_forking(self):
        (self.d / "deputy-work.lock").write_text("4242")
        with mock.patch.object(tick, "pid_alive", return_value=True):
            self.assertFalse(tick.fork_source_free("uvox"))
            self.assertTrue(tick.fork_source_free("work") and tick.fork_source_free("quant"))
            with self.assertRaises(tick.DeputyNotReady):
                self.go()
            self.assertEqual(self.calls, [])
            self.assertEqual(self.get_deputies()["uvox"]["status"], "idle")
            new, _ = line_split.convert_backlog(BACKLOG.split("\n"))
            only = "\n".join(new).replace("权重：24%", "权重：0%").replace("权重：22%", "权重：0%").replace("权重：16%", "权重：0%")
            busy = {"coordinator"}
            unit = tick.choose_lane(only, [], NOW, busy=busy, deputy_busy=busy | {r for r in tick.ROLE_NAMES if not tick.fork_source_free(r)})
            self.assertEqual(unit, ("uvox", "uvox"))  # the executor goes on; the review/plan (deputy unit) waits for the source
        (self.d / "deputy-work.lock").unlink()
        seen = {}
        real = self.fake

        def fake(*a, **kw):
            seen["source_locked"] = (self.d / "deputy-work.lock").exists()
            return real(*a, **kw)
        with mock.patch.object(tick, "run_claude", side_effect=fake):
            tick.run_resident("uvox", "plan", "任务", {}, {})
        self.assertTrue(seen["source_locked"])  # nobody else writes the source session while it is copied
        self.assertFalse((self.d / "deputy-work.lock").exists())  # and the lock is given back

    def test_a_fork_is_never_retried_blank_even_if_it_dies_before_any_work(self):
        self.script = [(1, [self.init(CHILD), {"type": "result", "is_error": True, "result": "x", "session_id": CHILD}])]
        self.go()
        self.assertEqual(len(self.calls), 1)  # a resume that dies gets one blank retry elsewhere; a fork does not
        self.assertNotIn("--session-id", self.calls[0]["extra"])


class Migration(Fixture):
    def lines(self, text=BACKLOG):
        return text.split("\n")

    def test_convert_moves_only_open_rows_of_the_moving_projects_and_keeps_every_other_line(self):
        new, rec = line_split.convert_backlog(self.lines())
        text = "\n".join(new)
        heads = [l for l in new if l.startswith("## ")]
        self.assertEqual(heads, ["## 你的指令（最优先）", "## Arbitros 线", "## Beacon Listener 线", "## 量化线", "## 组织线"])
        sec = lambda name: text.split(f"## {name}\n")[1].split("\n## ")[0]
        listener, arith, org = sec("Beacon Listener 线"), sec("Arbitros 线"), sec("组织线")
        for must in ("- [todo] Uvox：要搬的 Uvox 行\n  缩进的续行跟着 Uvox 行走", "- [blocked: 等你决定] Beacon Listener：官网等你定", "- [review] （返工）Kiki：参考系补充",
                     "- [blocked: 暂停到 FOS 视频交付] Field Observer：跨语言收尾", "Beacon Listener 官网（owner\x2010-02 新项目，优先级中下）这段项目说明跟着搬"):
            self.assertIn(must, listener)
        for stay in ("- [done 10-20 09:00：审查通过] Uvox：已完成的 Uvox 行留在原处", "- [done-ref] Kiki：旧的 Kiki 参考行", "【给工作副总理】（总理 10-04）这段说明归 Arbitros",
                     "- [todo] Umba：存量项目留在 Arbitros", "- [todo] 求职：留在 Arbitros", "- [todo] FOS：要做的 FOS 行"):
            self.assertIn(stay, arith)
            self.assertNotIn(stay, listener)
        self.assertIn("- [blocked: 等你决定] 输入台：语音微调要不要继续\n- [todo] 语音：另一条输入台待办", org)
        self.assertTrue(org.rstrip().endswith("语音：另一条输入台待办") and org.index("组织：新的一行") < org.index("输入台"))
        self.assertNotIn("输入台", arith)
        self.assertEqual(rec["rows_moved"], {"Beacon Listener 线": 4, "组织线": 2})
        self.assertEqual(rec["weights"], [36.0, 24.0, 12.0])
        self.assertTrue(arith.startswith("权重：24%（【owner\x2010-26 Discord「拆线：照建议」"))
        self.assertTrue(listener.startswith("权重：12%（"))
        self.assertIn("34→36；原 34%）（总理 10-22 旧说明）", arith)  # the old note text is kept after ours
        old_rows = sorted(l for l in self.lines() if l.startswith("- ["))
        self.assertEqual(sorted(l for l in new if l.startswith("- [")), old_rows)  # no row lost, none added, none edited
        self.assertEqual(sorted(l for l in new if l.strip() and not l.startswith("## ") and not l.startswith("权重")),
                         sorted(l for l in self.lines() if l.strip() and not l.startswith("## ") and not l.startswith("权重")))

    def test_crlf_files_stay_crlf(self):
        new, _ = line_split.convert_backlog(self.lines(crlf(BACKLOG)))
        for l in new[:-1]:
            self.assertTrue(l.endswith(CR), repr(l))
        self.assertEqual(sum(l.startswith("## Beacon Listener 线") for l in new), 1)

    def test_refuses_a_second_run_and_a_file_without_the_old_sections(self):
        new, _ = line_split.convert_backlog(self.lines())
        with self.assertRaises(line_split.SplitError):
            line_split.convert_backlog(new)
        with self.assertRaises(line_split.SplitError):
            line_split.convert_backlog(["## 量化线", "- [todo] 量化：x"])

    def test_other_old_weight_is_split_two_to_one(self):
        _, rec = line_split.convert_backlog(self.lines(BACKLOG.replace("权重：36%", "权重：30%")))
        self.assertEqual(rec["weights"], [30.0, 20.0, 10.0])

    def test_reverse_gives_the_original_back_and_keeps_rows_added_since(self):
        new, rec = line_split.convert_backlog(self.lines())
        back0 = line_split.reverse_backlog(new, rec)
        self.assertEqual(sorted(back0), sorted(self.lines()))  # every line back, weight line included; moved rows sit at the end of the section
        self.assertEqual([l for l in back0 if l.startswith("## ")], [l for l in self.lines() if l.startswith("## ")])
        added = list(new)
        i = added.index("## 量化线")
        added[i - 1:i - 1] = ["- [todo] Uvox：转换之后新写的一行"]  # a row the new deputy wrote under Beacon Listener 线
        back = line_split.reverse_backlog(added, rec)
        self.assertIn("- [todo] Uvox：转换之后新写的一行", back)
        self.assertEqual(sorted(l for l in back if l.startswith("- [")), sorted([*(l for l in self.lines() if l.startswith("- [")), "- [todo] Uvox：转换之后新写的一行"]))
        self.assertEqual(back.count("## 工作线"), 1)
        self.assertNotIn("## Beacon Listener 线", back)

    def test_migrate_deputies_moves_records_whole_forks_from_the_work_session_and_keeps_one_input_executor(self):
        d, rec = line_split.migrate_deputies(deputies(), "t")
        self.assertEqual(sorted(d["uvox"]["executors"]), ["beaconlistener", "fieldlistener", "kiki", "uvox"])
        self.assertEqual(d["uvox"]["executors"]["uvox"], deputies()["work"]["executors"]["uvox"])  # the whole record: runs, ids, history
        self.assertEqual(sorted(d["work"]["executors"]), ["fos", "general"])
        self.assertEqual((d["uvox"]["session_id"], d["uvox"]["fork_from"], d["uvox"]["fork_state"]), (None, SRC, "pending"))
        self.assertEqual(d["work"]["session_id"], SRC)
        self.assertEqual(d["coordinator"]["executors"]["input"]["session_id"], "in-work")  # the active one (17 runs)
        self.assertEqual(d["coordinator"]["migrated"]["input_set_aside"]["executor"]["session_id"], "in-coord")  # never dispatched
        self.assertNotIn("input", d["work"]["executors"])
        self.assertEqual(sorted(d["coordinator"]["executors"]), ["incore", "input"])
        ids = [e["session_id"] for r in d.values() for e in (r.get("executors") or {}).values()]
        self.assertEqual(len(ids), len(set(ids)))  # every project session id lives under exactly one role
        self.assertEqual(rec["source_session"], SRC)

    def test_migrate_refuses_when_not_idle_or_already_done_and_reverse_restores_the_records(self):
        busy = deputies()
        busy["work"]["executors"]["uvox"].update(finished_at=None, started_at="2026-10-27T05:00:00+00:00")
        with self.assertRaises(line_split.SplitError):
            line_split.migrate_deputies(busy, "t")
        running = deputies()
        running["work"]["status"] = "running"
        with self.assertRaises(line_split.SplitError):
            line_split.migrate_deputies(running, "t")
        with self.assertRaises(line_split.SplitError):
            line_split.migrate_deputies({"work": {"session_id": None}}, "t")
        d, rec = line_split.migrate_deputies(deputies(), "t")
        with self.assertRaises(line_split.SplitError):
            line_split.migrate_deputies(d, "t")
        back = line_split.reverse_deputies(d, rec)
        want = deputies()
        for role in ("work", "coordinator", "life"):
            self.assertEqual(back[role], want[role], role)
        self.assertEqual((back["uvox"]["fork_from"], back["uvox"]["fork_state"]), (None, "rolled_back"))  # kept, but nothing can fork or dispatch it
        self.assertFalse(back["uvox"]["executors"])

    def test_apply_and_rollback_on_files_refuse_while_busy_and_back_up_first(self):
        (self.d / "backlog.md").write_text(BACKLOG, encoding="utf-8")
        self.put_deputies()
        with self.assertRaisesRegex(line_split.SplitError, "tick.PAUSE"):
            line_split.apply()
        (self.d / "tick.PAUSE").write_text("")
        with mock.patch.object(tick, "lock_live", side_effect=lambda p: p.name == "lane-work-fos.lock"):
            (self.d / "lane-work-fos.lock").write_text("1")
            with self.assertRaisesRegex(line_split.SplitError, "lane-work-fos"):
                line_split.apply()
        self.assertEqual((self.d / "backlog.md").read_text(encoding="utf-8"), BACKLOG)  # nothing touched
        (self.d / "lane-work-fos.lock").unlink()
        with mock.patch.object(tick, "lock_live", return_value=False):
            rec = line_split.apply()
            self.assertIn("## Beacon Listener 线", (self.d / "backlog.md").read_text(encoding="utf-8"))
            self.assertEqual(self.get_deputies()["uvox"]["fork_from"], SRC)
            self.assertEqual((Path(rec["backup"]) / "backlog.md").read_text(encoding="utf-8"), BACKLOG)
            self.assertTrue((self.d / "line-split.json").exists())
            with self.assertRaises(line_split.SplitError):
                line_split.apply()  # a second run refuses
            line_split.rollback()
        self.assertEqual(sorted((self.d / "backlog.md").read_text(encoding="utf-8").split("\n")), sorted(BACKLOG.split("\n")))
        back = self.get_deputies()
        self.assertEqual(back["work"], deputies()["work"])
        self.assertFalse((self.d / "line-split.json").exists())


class SharedRules(unittest.TestCase):
    def test_role_templates_tell_both_lines_their_scope_and_to_read_the_shared_taste_file(self):
        roles = Path(__file__).resolve().parent / "roles"
        for name in ("work", "uvox"):
            text = (roles / f"{name}.md").read_text(encoding="utf-8")
            self.assertIn("Scope:", text)
            self.assertIn("do not write task rows for them", text)
        self.assertIn("projects/taste/shared.md", (roles / "uvox.md").read_text(encoding="utf-8"))
        self.assertIn("taste/shared.md", tick.FORK_INTRO)

    def test_every_role_with_a_section_has_a_name_a_role_file_and_a_talk_choice(self):
        here = Path(__file__).resolve().parent
        talk = (here / "talk.ps1").read_text(encoding="utf-8-sig")
        for role in tick.LINE_SECTIONS:
            self.assertIn(role, tick.ROLE_NAMES)
            self.assertIn(f"'{role}'", talk)
        self.assertTrue((here / "roles" / "uvox.md").exists())
        self.assertIn("coordinator:input", (here / "roles" / "coordinator.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
