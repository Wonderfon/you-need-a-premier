import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import review_stats as rs
import tick

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
project_of = tick.project_of


def run(kind, item, hours_ago, role="work", section="Arbitros 线", cost=0.0, **extra):
    return {"kind": kind, "item": item, "role": role, "section": section, "cost_usd": cost, "model": extra.pop("model", "sonnet"),
            "_at": NOW - timedelta(hours=hours_ago), **extra}


def codex(item, hours_ago, **extra):
    return run("executor", item, hours_ago, engine="codex", model="gpt-6-luna", project=project_of(item), **extra)


def review(item, hours_ago, verdict, **extra):
    return run("review", item, hours_ago, cost=1.5, model="opus", verdict=verdict, **extra)


def section(text, title):
    return text.split(f"## {title}")[1].split("\n## ")[0]


class Stats(unittest.TestCase):
    def render(self, runs, backlog="", tick_log=(), usage=(), **kw):
        with mock.patch.object(tick, "note_malformed"):  # parse_backlog would write the real tick-log
            return rs.render(sorted(runs, key=lambda r: r["_at"]), list(tick_log), list(usage), backlog, tick, NOW, **kw)

    def test_codex_counts_match_a_hand_count(self):
        runs = []
        for i, verdict in enumerate(["pass"] * 6 + ["rework"] * 3 + ["blocked"] * 2 + [None] * 9):
            runs.append(codex(f"Uvox：t{i}", 10 - i * 0.1))
            if verdict:
                runs.append(review(f"Uvox：t{i}", 5, verdict))
        text = self.render(runs)
        self.assertIn("| codex gpt-6-luna | 20 | $0.00 | 20 | 6 | 3 | 2 | 9 | 55% |", text)  # the 10-05 hand count: 6 / 3 / 2 / 9
        self.assertEqual(section(text, "Codex 每次执行（窗口内 20 次）").count("| 未审 |"), 9)
        self.assertIn("| claude opus | 11 | $16.50 | 0 | 0 | 0 | 0 | 0 | - |", text)

    def test_as_of_ignores_later_reviews_and_window_cuts_old_runs(self):
        runs = [codex("Uvox：a", 30), review("Uvox：a", 20, "pass"), codex("Uvox：b", 2)]
        self.assertIn("Codex 每次执行（窗口内 1 次）", self.render(runs, window_h=24))
        asof = rs.render(runs[:1], [], [], "", tick, NOW - timedelta(hours=25))  # replay before the review
        self.assertIn("| codex gpt-6-luna | 1 | $0.00 | 1 | 0 | 0 | 0 | 1 | - |", asof)

    def test_per_line_cost_and_first_pass(self):
        runs = [run("executor", "Uvox：a", 3, cost=1.0), review("Uvox：a", 2, "rework"),
                run("executor", "Q：b", 3, role="quant", section="量化线", cost=2.0), review("Q：b", 2, "pass", role="quant", section="量化线")]
        body = section(self.render(runs), "各线")
        self.assertIn("| Arbitros 线 | 2 | $2.50 | 1 | 1 | $1.50 | 0 | 1 | 0 | 0 | 0% |", body)
        self.assertIn("| 量化线 | 2 | $3.50 | 1 | 1 | $1.50 | 1 | 0 | 0 | 0 | 100% |", body)

    def test_every_queued_project_is_listed_with_hours_since_its_last_run(self):
        runs = [run("executor", "Uvox：a", 80, project="uvox"), run("executor", "Delta：a", 10, project="delta"),
                run("executor", "量化：q", 2, role="quant", section="量化线")]
        backlog = ("## Arbitros 线\n- [todo] Uvox：x\n- [review] Uvox：y\n- [todo] Delta：x\n- [todo] Umba：x\n- [done] Other：z\n"
                   "## 量化线\n- [todo] 量化：q2\n")
        body = section(self.render(runs, backlog), "有待办或待审的项目，离上次执行（4 个，久的在前）")
        rows = [l for l in body.splitlines() if l.startswith("| Arbitros 线") or l.startswith("| 量化线")]
        self.assertEqual(len(rows), 4)
        self.assertIn("| Arbitros 线 | umba | todo 1 / review 0 | - | 从没跑过 |", rows[0])  # never run first
        self.assertIn("| Arbitros 线 | uvox | todo 1 / review 1 | 80.0 小时 |", rows[1])
        self.assertIn("| Arbitros 线 | delta | todo 1 / review 0 | 10.0 小时 |", rows[2])  # not cut off at 3 days
        self.assertIn("| 量化线 | quant | todo 1 / review 0 | 2.0 小时 |", rows[3])
        last = tick.last_executor_runs([r for r in runs if r["role"] == "quant"], "quant")[tick.starve_key("量化：q2")]
        self.assertEqual((NOW - last).total_seconds() / 3600, 2.0)  # the scheduler's own number

    def test_paused_projects_are_separate_from_projects_waiting_for_execution(self):
        state = "blocked: owner\x2011-02 15:5x 暂停本项目（原话出处）；不派、不补行、不催，等 owner 说恢复时改回 todo"
        backlog = ("## Arbitros 线\n暂停项目（owner 原话）：FOS、Arbitros 官网、Umzy。保持暂停。\n"
                   f"- [{state}] Umzy：暂停\n- [review] FOS：审完\n- [todo] Arbitros 官网：漏改状态也不催\n"
                   "- [todo] Umba：继续\n## 生活线\n暂停项目（owner 原话）：Thinking Engines（写作模型不停，继续）。\n"
                   "- [todo] 写作模型：继续\n- [review] Thinking Engines：审完\n")
        runs = [run("executor", "FOS：审完", 80), run("executor", "Umzy：暂停", 80)]
        text = self.render(runs, backlog)
        waiting = section(text, "有待办或待审的项目，离上次执行（2 个，久的在前）")
        self.assertIn("umba", waiting)
        self.assertIn("writing", waiting)
        for project in ("fos", "umzy", "Thinking Engines", "Arbitros 官网"):
            self.assertNotIn(project, waiting)
        paused = section(text, "暂停中")
        for name in ("FOS", "Arbitros 官网", "Umzy", "Thinking Engines"):
            self.assertIn(name, paused)
        stuck = section(text, "blocked 或 review 超过 24 小时的行（1 行）")
        self.assertIn("FOS：审完", stuck)  # ongoing reviews still finish
        self.assertNotIn("Umzy：暂停", stuck)

    def test_share_table_uses_choose_lanes_score(self):
        runs = [run("executor", "Uvox：a", 3, cost=6.0), run("review", "Uvox：a", 2, cost=1.0),
                run("coordinate", "x", 1, role="coordinator", section="组织线", cost=2.0),  # COST_KINDS: counts
                run("executor", "Uvox：old", 24 * 8, cost=50.0),  # older than 7 days: not counted
                run("executor", "Q：b", 3, role="quant", section="量化线", cost=0.0, engine="codex"),
                run("terminal", "x", 3, cost=99.0)]  # not a COST_KINDS kind
        backlog = "## Arbitros 线\n权重：40%\n## 量化线\n权重：20%\n## 组织线\n权重：0%\n"
        body = section(self.render(runs, backlog), "份额表（7 天，只算 review/plan/executor/coordinate/compact/lessons；分数 =（花费 + 1 ）/ 权重，最低的先派）".replace("1 ）", "1）"))
        self.assertEqual([l for l in body.splitlines() if l.startswith("| Arbitros 线") or l.startswith("| 量化线") or l.startswith("| 组织线")],
                         ["| 量化线 | 20% | $0.00 | 0.050 |", "| Arbitros 线 | 40% | $7.00 | 0.200 |"])  # weight 0 never dispatched: not ranked
        cost = tick.cost_7d(sorted(runs, key=lambda r: r["_at"]), NOW)
        self.assertEqual(round((cost["Arbitros 线"] + tick.COST_FLOOR) / 40, 3), 0.2)

    def test_malformed_rows_of_the_last_24_hours(self):
        log = [{"_at": NOW - timedelta(hours=h), "decision": "malformed", "row": f"- [todo]（r{h} | x", "reason": "no status"} for h in (2, 30)]
        body = section(self.render([], tick_log=log), "最近 24 小时的 malformed 行（1 行）")
        self.assertIn("| 10-05 10:00Z | - [todo]（r2 / x | no status |", body)
        self.assertNotIn("r30", body)
        self.assertIn("（0 行）\n\n无", self.render([]).split("## 最近 24 小时的 malformed 行")[1])

    def test_running_rows_without_a_live_run_are_listed(self):
        backlog = ("## Arbitros 线\n- [running 11:00] Uvox：a\n- [running 12:00] Umba：b\n## 杂项\n- [running 14:00] 认不出的\n")
        with mock.patch.object(tick, "pid_alive", side_effect=lambda pid: pid == 4242), \
                mock.patch.object(tick, "lane_lock", side_effect=lambda r, p=None: Path(tempfile.gettempdir()) / f"nolock-{r}-{p}") as lock:
            live = Path(tempfile.gettempdir()) / "nolock-work-umba"
            live.write_text("4242", encoding="utf-8")
            self.addCleanup(live.unlink)
            body = section(self.render([], backlog), "没有在跑的 running 行（2 行")
        self.assertIn("| Arbitros 线 | 11:00 | 是（下次调度器启动） | Uvox：a |", body)
        self.assertIn("| 杂项 | 14:00 | 否，认不出是哪种运行 | 认不出的 |", body)
        self.assertNotIn("Umba：b", body)  # its lane lock is live
        self.assertIn("（0 行", self.render([], "## Arbitros 线\n- [todo] x\n").split("## 没有在跑的 running 行")[1])

    def test_stuck_rows_use_the_ledger_then_first_sighting(self):
        runs = [run("executor", "Uvox：old", 30), run("executor", "Uvox：new", 5), run("review", "Delta：b", 40, verdict="blocked")]
        backlog = ("## Arbitros 线\n- [review] Uvox：old\n- [review] Uvox：new\n- [blocked: 等你决定] Delta：b\n"
                   "- [blocked: 等你] Umba：c\n- [blocked: 等你] Umba：d\n- [todo] Uvox：t\n")
        seen = {rs.row_key("blocked", "Umba：c"): (NOW - timedelta(hours=50)).isoformat()}
        text = self.render(runs, backlog, seen=seen)
        body = section(text, "blocked 或 review 超过 24 小时的行（3 行）")
        self.assertIn("| Arbitros 线 | blocked | 50 小时 | 首次见到 | Umba：c |", body)
        self.assertIn("| Arbitros 线 | blocked | 40 小时 | 账本 | Delta：b |", body)
        self.assertIn("| Arbitros 线 | review | 30 小时 | 账本 | Uvox：old |", body)
        self.assertNotIn("Uvox：new", body)
        self.assertIn("另有 1 行还没有时间记录", body)  # Umba：d

    def test_scheduler_decisions_and_usage(self):
        runs = [run("executor", "Uvox：a", 3)]
        log = [{"_at": NOW - timedelta(hours=1), "decision": d, "lane": "work:uvox"} for d in ("spawn", "spawn", "codex_spawn")]
        log += [{"_at": NOW - timedelta(hours=1), "decision": "hard_ceiling"}, {"_at": NOW - timedelta(days=9), "decision": "spawn", "lane": "work"}]
        usage = [{"_at": NOW - timedelta(hours=h), "utilization": u} for h, u in ((5, 0.9), (1, 0.12))]
        text = self.render(runs, tick_log=log, usage=usage)
        self.assertIn("| Arbitros 线 | 2 | 1 | 0 | 0 | 0 | 0 | 0 |", text)
        self.assertIn("| 全局 | 0 | 0 | 0 | 0 | 0 | 1 | 0 |", text)
        self.assertIn("最近一次 12%", text)
        self.assertIn("窗口内最高 90%", text)

    def test_idle_section_counts_hours_without_a_run_under_the_pacing_line_and_cross_starved_spawns(self):
        # window 6 h, hours end at 07:00..12:00Z. Work: one 2 h run ending 10:00 (covers hours ending 09..10:00 and 08:00 partly);
        # life: nothing at all and an empty queue. Usage: resets 5 h after the window start so the pacing line is far above 10 %.
        reset = int((NOW - timedelta(hours=6) + timedelta(hours=5)).timestamp())
        usage = [{"_at": NOW - timedelta(hours=7), "utilization": 0.10, "resets_at": reset}]
        runs = [run("executor", "Uvox：a", 2, elapsed_s=3600)]  # ended 10:00, ran 09:00-10:00: only the hour ending 10:00
        log = [{"_at": NOW - timedelta(hours=h), "decision": d, "lane": "work", "cross_starved": s}
               for h, d, s in ((3, "spawn", "Arbitros 线"), (2, "codex_spawn", "Arbitros 线"), (1, "spawn", "生活线"), (80, "spawn", "Arbitros 线"))]
        log.append({"_at": NOW - timedelta(hours=1), "decision": "ran", "cross_starved": "Arbitros 线"})  # not a start
        backlog = "## Arbitros 线\n权重：40%\n- [todo] Uvox：x\n## 生活线\n权重：10%\n"
        body = section(self.render(runs, backlog, tick_log=log, usage=usage, window_h=6), "空转（最近 6 小时")
        rows = {l.split("|")[1].strip(): [c.strip() for c in l.split("|")[2:-1]] for l in body.splitlines() if l.startswith("| Arbitros 线") or l.startswith("| 生活线")}
        # 线: todo, 待审, 在跑, 没在跑的小时, 低于节奏线, 没活时已空转, 跨线防饿
        self.assertEqual(rows["Arbitros 线"], ["1", "0", "否", "5", "5", "有活或在跑", "2"])
        self.assertEqual(rows["生活线"], ["0", "0", "否", "6", "6", "≥6 小时", "1"])
        late = self.render(runs, backlog, usage=[{**usage[0], "utilization": 0.9}], window_h=6)  # far above the pacing line
        self.assertIn("| Arbitros 线 | 1 | 0 | 否 | 5 | 0 | 有活或在跑 | 0 |", late)

    def test_empty_ledger_still_renders(self):
        self.assertIn("## 额度", self.render([]))

    def test_render_includes_blocked_ready_even_without_a_seen_timestamp(self):
        backlog = "## 组织线\n- [blocked: 等日期 10-04] 组织：可以复查\n"
        body = section(self.render([], backlog), "等待条件可能已满足的 blocked 行（1 行）")
        self.assertIn("组织：可以复查", body)
        self.assertIn("只报不改", body)
        self.assertIn("backlog.md:2", body)


class BlockedReady(unittest.TestCase):
    NOW = datetime(2026, 11, 2, 14, 30, tzinfo=timezone(timedelta(hours=-5)))

    def report(self, backlog, now=None):
        clock = now or self.NOW
        rows = rs.review_snapshot(backlog, [], tick, clock)["rows"]
        before = json.dumps(rows, ensure_ascii=False)
        text = rs.blocked_ready_section(rows, clock)
        self.assertEqual(json.dumps(rows, ensure_ascii=False), before)  # reporting must not release or edit rows
        return text

    def test_wait_date_time_deadline_and_scheduled_date(self):
        text = self.report("## 组织线\n"
                           "- [blocked: 等日期 11-01] 组织：日期\n"
                           "- [blocked: 等 11-02 14:29] 组织：时分\n"
                           "- [blocked: 截止美东 11-02 14:30] 组织：截止\n"
                           "- [blocked: 定时，2026-10-29起跑] 组织：定时\n"
                           "- [blocked: 等到 10-29] 组织：等到\n"
                           "- [blocked: 等 11-02 14:31] 组织：未到时间\n"
                           "- [blocked: 等日期 11-09] 组织：未来日期\n"
                           "- [blocked: 定时，2027-01-04起跑] 组织：明年\n")
        self.assertIn("（5 行）", text)
        for item in ("日期", "时分", "截止", "定时", "等到"):
            self.assertIn(f"组织：{item}", text)
        for item in ("未到时间", "未来日期", "明年"):
            self.assertNotIn(f"组织：{item}", text)
        self.assertIn("2026-11-02 14:29（本地时间）（无年按本次年份）", text)

    def test_same_section_done_evidence_and_completion_wording(self):
        state = "done 10-30 23:26：sol 审查通过，audits/" + "证据" * 30
        item = "组织：" + "长标题" * 25 + "|尾部"
        text = self.report(f"## 组织线\n- [{state}] 组织：Colab CLI 实测核实\n"
                           "- [blocked: 等上面「Colab CLI 实测核实」那行过审] " + item + "\n"
                           "- [blocked: 等本线「Colab CLI 实测核实」完成] 组织：完成后做\n"
                           "- [blocked: 等上面「Colab CLI 实测核实」返工过审] 组织：返工后做\n")
        self.assertIn("（3 行）", text)
        self.assertIn(f"backlog.md:2 [{state[:40]}]", text)
        self.assertIn(item[:60], text)
        self.assertNotIn("尾部", text)
        self.assertIn("同小节前置已 done", text)

    def test_dependency_must_be_done_in_the_same_section_item(self):
        text = self.report("## 别线\n- [done] 组织：别线前置\n"
                           "## 组织线\n- [review] 组织：待审前置\n"
                           "- [done：状态里提及缺失前置] 组织：其他事情\n"
                           "- [blocked: 等「别线前置」那行过审] 组织：跨线\n"
                           "- [blocked: 等「待审前置」完成] 组织：未审完\n"
                           "- [blocked: 等「缺失前置」完成] 组织：没找到\n")
        self.assertIn("（0 行）", text)

    def test_dots_deadline_passed_and_future_or_missing_deadlines(self):
        text = self.report("## 组织线\n"
                           "- [blocked: 等 dotz 交 T-20261030-04（截止美东 10-31 12:00）] 组织：dotz 旧任务\n"
                           "- [blocked: 等 dotz 交 T-20261102-01（截止美东 11-03 12:00）] 组织：dotz 未来\n"
                           "- [blocked: dotz T-20261030-05 阻塞：缺文件] 组织：dotz 没截止\n")
        self.assertIn("（1 行）", text)
        self.assertIn("dotz 截止已过", text)
        self.assertIn("T-20261030-04：2026-10-31 12:00", text)
        self.assertNotIn("等待日期已到 |", text)
        self.assertNotIn("组织：dotz 未来", text)
        self.assertNotIn("组织：dotz 没截止", text)

    def test_three_human_wait_exclusions_override_all_hints(self):
        for exclusion in ("等你决定", "暂停本项目", "等总理"):
            with self.subTest(exclusion=exclusion):
                text = self.report("## 组织线\n- [done] 组织：前置\n"
                                   f"- [blocked: 等日期 10-01；等「前置」过审；dotz T-20261030-04 截止美东 10-31 12:00；{exclusion}] 组织：排除\n")
                self.assertIn("（0 行）", text)

    def test_body_dates_citations_and_nonblocked_rows_do_not_count(self):
        text = self.report("## 组织线\n"
                           "- [blocked: 等更多数据；核对 10-01，records\x2f2026-10-01-a.json] 组织：正文等日期 10-01\n"
                           "- [blocked: 定时，分支合入后第 8 天；原定 10-01 作废] 组织：旧日期作废\n"
                           "- [review: 等日期 10-01] 组织：待审\n"
                           "- [done: 等日期 10-01] 组织：完成\n")
        self.assertIn("（0 行）", text)

    def test_invalid_dates_and_imprecise_clocks_are_skipped(self):
        text = self.report("## 组织线\n"
                           "- [blocked: 等日期 02-30] 组织：无效日期\n"
                           "- [blocked: 等 10-30 25:00] 组织：无效时间\n"
                           "- [blocked: 等 10-30 22:1x] 组织：模糊时间\n")
        self.assertIn("（0 行）", text)

    def test_utc_suffix_compares_the_fixed_clock_in_utc(self):
        # 14:30 EST = 19:30Z; a Z cutoff must not be compared with 14:30.
        text = self.report("## 组织线\n"
                           "- [blocked: 等 11-02 19:29Z] 组织：UTC 已过\n"
                           "- [blocked: 等 11-02 19:31Z] 组织：UTC 未到\n")
        self.assertIn("（1 行）", text)
        self.assertIn("2026-11-02 19:29Z", text)
        self.assertNotIn("组织：UTC 未到", text)


class Daily(unittest.TestCase):
    DAY = NOW.astimezone().date()

    def setUp(self):  # daily() derives its workspace root from rs.BACKLOG: use an empty one, not the real records/archive
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(rs, "BACKLOG", Path(tmp.name) / "projects" / "backlog.md")
        patch.start()
        self.addCleanup(patch.stop)

    def test_one_line_report_lists_todays_verdicts_waiting_rows_files_and_cost(self):
        wrote = Path(tempfile.mkdtemp()) / "run.jsonl"
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "C:\\w\\incore\\audits\\2026-10-05-new.md"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "C:\\w\\incore\\audits\\2026-10-03-read-only.md"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "cat > /mnt\x2fc/w/incore/audits/2026-10-05-codex.md <<EOF"}}]}}]
        wrote.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        self.addCleanup(wrote.unlink)
        runs = [run("executor", "Uvox：a", 1, cost=1.0, run_file=str(wrote)), review("Uvox：a", 0.5, "pass"), review("Uvox：b", 0.4, "rework"),
                review("Uvox：c", 0.3, "blocked"), review("Uvox：old", 30, "pass"),
                run("executor", "Q：x", 1, role="quant", section="量化线", cost=2.0), review("Q：x", 0.5, "pass", role="quant", section="量化线")]
        backlog = "## Arbitros 线\n冲刺：Uvox，版本目标：v1\n- [blocked: 等你决定] Uvox：d\n- [todo] Uvox：t\n## 量化线\n- [blocked: 等你] Q：y\n"
        text = rs.daily(sorted(runs, key=lambda r: r["_at"]), backlog, self.DAY, "Arbitros 线")
        self.assertIn("审查通过（1）：\n- Uvox：a", text)  # not the 30-hour-old pass, not the quant line
        self.assertIn("判返工（1）：\n- Uvox：b", text)
        self.assertIn("判受阻（1）：\n- Uvox：c", text)
        self.assertIn("冲刺行（明天冲刺那一行照它写）：冲刺：Uvox，版本目标：v1", text)
        self.assertIn("现在等你决定的行（和「等你决定」页同一个数）（1）：\n- Uvox：d", text)
        self.assertIn("结果文件 audits/（2；路径按事项择一展开）", text)  # written, not merely read
        self.assertIn("| Arbitros 线 | 1 | 0 | 3 | $5.50 |", text)  # review cost 1.5 x 3 + executor 1.0
        self.assertNotIn("量化线", text.split("今天的运行和花费")[1])

    def test_only_rows_waiting_on_the_owner_are_listed_the_rest_are_counted(self):
        backlog = ("## Arbitros 线\n- [blocked: 等你决定] Uvox：a\n- [blocked: 等 owner 回 x] Uvox：b\n- [blocked: 定时，10-14 起跑] Uvox：c\n"
                   "- [blocked: 等上一步通过] Uvox：d\n- [blocked: 等同事] Uvox：e\n")
        text = rs.daily([], backlog, self.DAY, "Arbitros 线")
        self.assertIn("现在等你决定的行（和「等你决定」页同一个数）（1）：\n- Uvox：a\n", text)
        self.assertIn("等你操作的行（别的写法，如「等你粘命令」「等 owner 回…」，也在「要你的」页里，方括号里是卡片编号）（1）：\n- Uvox：b\n", text)
        self.assertNotIn("Uvox：c", text)
        self.assertIn("另有 3 行在等别的（定时、上一步、同事等），不用你动手。", text)

    def test_report_facts_keep_the_card_numbers_of_the_owner_page(self):
        backlog = "## Arbitros 线\n- [blocked: 等你决定] Uvox：a\n- [blocked: 等 owner 回 x] Uvox：b\n"
        text = rs.daily([], backlog, self.DAY, "Arbitros 线", codes={("D", "Uvox：a"): "D12", ("T", "Uvox：b"): "T13"})
        self.assertIn("- [D12] Uvox：a\n", text)
        self.assertIn("- [T13] Uvox：b\n", text)
        self.assertIn("待行动：待 owner 决定（backlog:2）", text)
        self.assertIn("待行动：待 owner 操作（backlog:3）", text)
        self.assertNotIn("[", rs.daily([], backlog, self.DAY, "Arbitros 线").split("等你决定的行")[1].split("另有")[0])  # no numbers: as before

    def test_coordinator_sees_every_line_and_usage(self):
        runs = [review("Uvox：a", 0.5, "pass"), review("Q：x", 0.5, "rework", section="量化线")]
        usage = [{"_at": NOW - timedelta(hours=h), "utilization": u} for h, u in ((3, 0.4), (1, 0.2))]
        text = rs.daily(runs, "## 量化线\n- [blocked: 等你] Q：y\n", self.DAY, None, usage)
        self.assertIn("- Arbitros 线：Uvox：a", text)
        self.assertIn("- 量化线：Q：x", text)
        self.assertIn("- 量化线：Q：y", text)
        self.assertIn("今天最高 40%", text)
        self.assertIn("最近一次 20%", text)

    def test_long_lists_are_capped(self):
        runs = [review(f"Uvox：{i}", 0.5, "pass") for i in range(12)]
        text = rs.daily(runs, "", self.DAY, "Arbitros 线")
        self.assertIn("今天审查通过（12）", text)
        self.assertIn("…另有 10 条", text)
        self.assertEqual(text.count("最新回复出处："), rs.LIST_MAX)
        self.assertLess(len(text), 2500)  # includes per-item path/status diagnostics

    def test_since_replaces_the_calendar_day_for_the_twice_daily_reports(self):
        since = (NOW - timedelta(hours=10)).astimezone().replace(tzinfo=None)
        runs = [review("Uvox：old", 11, "pass"), review("Uvox：in", 9, "pass"), review("Uvox：late", 2, "rework")]
        usage = [{"_at": NOW - timedelta(hours=h), "utilization": u} for h, u in ((12, 0.9), (3, 0.3))]
        text = rs.daily(runs, "", self.DAY, None, usage, since)
        self.assertTrue(text.startswith(f"【今日数据（{self.DAY:%m-%d}，自 {since:%m-%d %H:%M} 起，"))
        self.assertIn("本时段审查通过（1）：\n- Arbitros 线：Uvox：in", text)
        self.assertIn("本时段判返工（1）：\n- Arbitros 线：Uvox：late", text)
        self.assertNotIn("Uvox：old", text)
        self.assertIn("本时段最高 30%", text)  # the 90 % at hour 12 is before the window

    def test_quiet_day(self):
        text = rs.daily([], "", self.DAY, "Arbitros 线")
        self.assertIn("今天审查通过（0）：无", text)
        self.assertIn("无运行", text)


class Files(unittest.TestCase):
    def test_write_regenerates_the_table_and_remembers_first_sightings(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "runs.jsonl").write_text(json.dumps({"at": NOW.isoformat(), "kind": "executor", "item": "Uvox：a", "section": "Arbitros 线",
                                                      "cost_usd": 1.0, "model": "sonnet", "role": "work"}) + "\n", encoding="utf-8")
            (d / "backlog.md").write_text("## Arbitros 线\n- [blocked: 等你] Uvox：b\n", encoding="utf-8")
            with mock.patch.multiple(rs, DATA=d, BACKLOG=d / "backlog.md", OUT=d / "review-stats.md", SEEN=d / "seen.json"), \
                    mock.patch.object(tick, "note_malformed"):
                text = rs.write(tick)
                self.assertEqual((d / "review-stats.md").read_text(encoding="utf-8"), text)
                first = json.loads((d / "seen.json").read_text(encoding="utf-8"))
                self.assertEqual(list(first), ["blocked|Uvox：b"])
                rs.write(tick)
                self.assertEqual(json.loads((d / "seen.json").read_text(encoding="utf-8")), first)  # first sighting is kept
                (d / "backlog.md").write_text("## Arbitros 线\n- [done] Uvox：b\n", encoding="utf-8")
                rs.write(tick)
                self.assertEqual(json.loads((d / "seen.json").read_text(encoding="utf-8")), {})  # gone rows are forgotten


if __name__ == "__main__":
    unittest.main()
