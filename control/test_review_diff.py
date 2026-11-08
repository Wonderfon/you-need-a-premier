import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import records_index as ri
import review_packet as rp
import review_stats as rs
import tick

NOW = datetime(2026, 10, 30, 16, tzinfo=timezone.utc)


class _SteppingClock(datetime):
    """datetime.now() that moves forward on every call: Windows' clock can repeat a value between two writes."""
    _t = datetime.now(timezone.utc)

    @classmethod
    def now(cls, tz=None):
        cls._t += timedelta(seconds=1)
        return cls._t if tz is None else cls._t.astimezone(tz)


class RecordsCandidates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def put(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def test_nested_messages_numbered_fields_and_original_line_locations(self):
        data = {"owner_message_verbatim_1": "先测", "owner_message_verbatim_2": "不要上线",
                "messages": [{"owner_verbatim": "OK，同意"}], "meaning": ["仅是解释"]}
        key, text = ri.extract_text(data)
        self.assertEqual(key, "owner_message_verbatim_1")
        self.assertIn("先测\n不要上线", text)
        self.assertIn("OK，同意", text)
        self.assertNotIn("仅是解释", text)
        raw = json.dumps(data, ensure_ascii=False, indent=2)
        for block in ri.located_text(raw):
            self.assertIn('"' + block["field"] + '"', raw.splitlines()[block["line"] - 1])
        self.assertEqual(len(ri.located_text('{"messages":[{"owner_verbatim":"A"},{"owner_verbatim":"B"}]}')), 2)

    def test_old_unchanged_index_is_reextracted_without_changing_original_or_invalidating_labels(self):
        path = self.put("2026-10-30-a.json", {"owner_verbatim": "变成脚本变成函数"})
        original = path.read_bytes()
        index, labels = self.root / "index.jsonl", self.root / "labels.json"
        index.write_text(json.dumps({"file": path.stem, "file_sha256": hashlib.sha256(original).hexdigest(),
                                     "chars": 0, "labels": {"project": "incore"}}) + "\n", encoding="utf-8")
        ri.update(self.root, index, labels, NOW)
        self.assertEqual(path.read_bytes(), original)
        row = ri.latest(ri.read_rows(index))[path.stem]
        self.assertEqual(row["chars"], len("变成脚本变成函数"))
        self.assertFalse(row["labels_stale"])

    def test_project_full_path_and_dates_keep_old_and_new_words_without_deciding(self):
        for day, text, path in [("28", "可以", "audits/a/same.html"), ("29", "等等", "audits/a/same.html"),
                                ("30", "A", "audits/b/same.html"), ("31", "未来的话", "audits/a/same.html")]:
            self.put(f"2026-10-{day}-a.json", {"project": "incore", "context": path, "owner_verbatim": text})
        data = ri.catalog(self.root)
        result = ri.candidates(data, ("incore",), ("audits\\a\\same.html",), since="2026-10-28", until="2026-10-30")
        self.assertEqual([r["date"] for r in result["hits"]], ["2026-10-29", "2026-10-28"])
        self.assertEqual(result["status"], "候选")
        self.assertEqual(ri.candidates(data, ("wrong-project",))["status"], "未找到")
        self.assertTrue(result["gaps"])  # no index: originals still found
        self.assertNotIn("已答", json.dumps(result, ensure_ascii=False))

    def test_bad_json_and_unrecognized_words_are_explicit_gaps(self):
        (self.root / "2026-10-30-bad.json").write_text("{", encoding="utf-8")
        self.put("2026-10-30-unknown.json", {"meaning": "derived only"})
        result = ri.candidates(ri.catalog(self.root))
        self.assertEqual(result["status"], "未找到")
        self.assertEqual(len(result["gaps"]), 3)  # bad JSON; missing index and text field


class ReviewDiff(unittest.TestCase):
    def setUp(self):  # review_snapshot reads the archive next to rs.BACKLOG: never the real workspace archive
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(rs, "BACKLOG", Path(tmp.name) / "projects" / "backlog.md")
        patch.start()
        self.addCleanup(patch.stop)

    def snap(self, text, now=NOW, log=(), branches=None):
        return rs.review_snapshot(text, list(log), tick, now, branches)

    def test_removed_rows_are_unknown_and_same_named_tasks_in_other_sections_stay_separate(self):
        before = self.snap("## 组织线\n- [blocked: 等你决定] 组织：保留\n- [blocked] 组织：办结\n- [blocked] 组织：消失\n"
                           "## 生活线\n- [blocked] 组织：保留\n", NOW - timedelta(hours=8))
        after = self.snap("## 组织线\n- [blocked: 已回A，等审] 组织：保留\n- [done] 组织：办结\n- [blocked] 组织：新增\n"
                          "## 生活线\n- [blocked] 组织：保留\n")
        text = rs.diff_section(after, before)
        self.assertIn("新增 blocked（1）", text)
        self.assertIn("已解除 blocked（移至其他状态；不等于事项办完）（1）", text)
        self.assertIn("原 blocked 行消失或改文，需核对（不算已解除）（1）", text)
        self.assertIn("blocked 条件变化（1）", text)

    def test_missing_equal_or_future_baseline_does_not_claim_no_change(self):
        for baseline in (None, self.snap(""), self.snap("", NOW + timedelta(hours=1))):
            text = rs.diff_section(self.snap("## 组织线\n- [blocked] 组织：a\n"), baseline)
            self.assertIn("不能判断新增／解除", text)
            self.assertNotIn("新增 blocked（0）", text)

    def test_due_rechecks_exclude_fact_check_dates_and_done_rows(self):
        data = self.snap("## 组织线\n- [blocked] 组织：a，复查 10-29；〔事实：x；核对 10-01〕\n"
                         "- [todo] 组织：b，2026-10-30 复查\n- [blocked] 组织：c，复查 11-01\n"
                         "- [done] 组织：d，复查 10-20\n- [blocked] 组织：e，复查 13-20\n")
        text = rs.diff_section(data, None)
        self.assertIn("已到复查日（只识别明确的复查日期，不把事实核对日当复查日）（2）", text)

    def test_dispatch_is_from_start_log_not_run_end_and_is_bounded_by_as_of(self):
        log = [{"_at": NOW - timedelta(hours=h), "lane": lane, "decision": decision, "_source": f"tick-log.jsonl:{n}"}
               for n, (h, lane, decision) in enumerate([(10, "work:fos", "spawn"), (1, "work:fos", "codex_spawn"),
                    (-1, "life", "spawn"), (0, "life", "ran")], 1)]
        previous = self.snap("", NOW - timedelta(hours=8), log)
        current = self.snap("", NOW, log)
        self.assertEqual(list(current["dispatch"]), ["Arbitros 线"])
        self.assertIn("有变化 | tick-log.jsonl:2", rs.diff_section(current, previous))
        self.assertIn("历史分支快照不可用", rs.diff_section(current, previous))

    def test_snapshot_schema_and_replay_cannot_advance_live_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            for bad in ("{", json.dumps({"version": 1, "as_of": NOW.isoformat(), "rows": [None], "dispatch": {}})):
                path.write_text(bad, encoding="utf-8")
                self.assertIsNone(rs.load_snapshot(path))
            with mock.patch.object(rs, "AS_OF", NOW.isoformat()):
                with self.assertRaises(ValueError):
                    rs.write(tick)

    def test_write_uses_one_backlog_and_advances_only_derived_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            backlog, out = root / "backlog.md", root / "stats.md"
            original = "## 组织线\n- [blocked] 组织：a\n"
            backlog.write_text(original, encoding="utf-8")
            with mock.patch.multiple(rs, BACKLOG=backlog, DATA=root, OUT=out, SEEN=root / "seen.json"), \
                    mock.patch.object(rs, "branch_facts", return_value=[]), mock.patch.object(tick, "note_malformed"), \
                    mock.patch.object(rs.codex_burn, "section", return_value=""), \
                    mock.patch.object(rs, "datetime", _SteppingClock):
                first = rs.write(tick)
                snapshot = rs.load_snapshot(out.with_suffix(".json"))
                self.assertIn("不能判断新增／解除", first)
                self.assertEqual(snapshot["rows"][0]["status"], "blocked")
                second = rs.write(tick)
                self.assertIn("新增 blocked（0）", second)
                self.assertEqual(backlog.read_text(encoding="utf-8"), original)

    def test_build_replay_uses_explicit_snapshot_and_records_without_advancing_either(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            records = root / "records"
            records.mkdir()
            record = records / "2026-10-29-reply.json"
            record.write_text(json.dumps({"context": "组织 audits/test.html", "owner_verbatim": "先试，别上线"},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
            backlog = "## 组织线\n- [blocked: 等你决定] 组织：a，结果 audits/test.html\n"
            previous = root / "previous.json"
            previous.write_text(json.dumps(self.snap("", NOW - timedelta(hours=8))), encoding="utf-8")
            before = {p: p.read_bytes() for p in (previous, record)}
            with mock.patch.multiple(rs, DATA=root, BACKLOG=root / "archived.md", OUT=root / "stats.md"), \
                    mock.patch.object(rs, "render", return_value="HEADER"), \
                    mock.patch.object(rs, "branch_facts", side_effect=AssertionError("live refs during replay")), \
                    mock.patch.object(rs.codex_burn, "section", side_effect=AssertionError("live quota during replay")):
                text = rs.build(tick, now=NOW, backlog_text=backlog, records_dir=records, previous_path=previous)
            self.assertIn("新增 blocked（1）", text)
            self.assertIn("路径命中", text)
            self.assertIn("先试，别上线", text)
            self.assertIn("records\x2f2026-10-29-reply.json:3", text)
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            self.assertFalse((root / "stats.json").exists())

    def test_unavailable_current_backlog_cannot_report_zero_changes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch.multiple(rs, DATA=root, BACKLOG=root / "missing.md", OUT=root / "stats.md"), \
                    mock.patch.object(rs, "render", return_value="HEADER"):
                text = rs.build(tick, now=NOW)
            self.assertIn("来源不可用", text)
            self.assertNotIn("新增 blocked（0）", text)

    def test_real_git_uses_local_default_branch_for_unpushed_merges(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)

            def git(*args):
                subprocess.run([rp.GIT, "-c", "user.name=codex", "-c", "user.email=codex@local", "-C", str(repo), *args],
                               check=True, capture_output=True, text=True, encoding="utf-8")

            git("init", "-b", "master")
            git("commit", "--allow-empty", "-m", "base")
            git("update-ref", "refs/remotes/origin/master", "HEAD")
            git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
            git("checkout", "-b", "claude/a")
            git("commit", "--allow-empty", "-m", "a")
            git("checkout", "master")
            git("merge", "--no-ff", "--no-edit", "claude/a")
            git("checkout", "-b", "claude/b")
            git("commit", "--allow-empty", "-m", "b")
            with mock.patch.dict(tick.PROJECTS, {"incore": (repo, ("组织：",))}):
                rows = self.snap("## 组织线\n- [review] 组织：a，claude/a claude/missing\n"
                                 "- [done：过审，等 owner 合] 组织：b，claude/b\n")["rows"]
                facts = rs.branch_facts(rows, tick)
            self.assertEqual({r["branch"]: r["state"] for r in facts},
                             {"claude/a": "已合并", "claude/missing": "分支未找到"})
            text = rs.diff_section(self.snap("## 组织线\n- [done：等 owner 合] 组织：claude/b\n", branches=facts), None)
            self.assertIn("旧等合并引用：1 行", text)
            self.assertNotIn("incore · claude/b", text)

class ReviewDiffBounds(unittest.TestCase):
    def snap(self, text, branches=None):
        return rs.review_snapshot(text, [], tick, NOW, branches)

    def test_sections_are_bounded_and_overflow_goes_to_detail_json(self):
        many = "## 组织线\n" + "".join(f"- [blocked: 等你决定] 组织：事项{i:03d}长长长，复查 10-0{i % 9 + 1}\n" for i in range(60))
        detail = {}
        text = rs.diff_section(self.snap(many), self.snap("", None) | {"as_of": (NOW - timedelta(hours=8)).isoformat()}, detail)
        self.assertLessEqual(len(text.splitlines()), 56)
        self.assertIn("另有 54 条，明细见", text)
        self.assertEqual(len(detail["新增 blocked"]), 60)  # nothing lost, only moved out of the table
        data = {"rows": [], "gaps": []}
        cand = rs.owner_candidates(self.snap(many), tick, data, detail)
        self.assertIn("未找到", cand)
        self.assertLessEqual(len(rs.clip(cand, 24, 2400).splitlines()), 24)
        self.assertLessEqual(len(rs.clip("x\n" * 500, 24, 2400).splitlines()), 24)
        self.assertLessEqual(len(rs.clip("字" * 5000, 24, 2400)), 2400)

    def test_only_row_citations_and_its_branch_or_result_name_match(self):
        def rec(name, fields_text, search):
            return {"file": name, "date": "2026-10-29", "search_text": search, "issues": [],
                    "fields": [{"field": "owner_verbatim", "line": 3, "text": fields_text}]}
        rows = [rec("records/cited.json", "行内引用", "仅直接引用"),
                rec("records/branch.json", "分支原话", "claude/pool-refill"),
                rec("records/result.json", "结果原话", "audits/pool-result.md"),
                rec("records/factoryalign.json", "无关原话", "组织 补池"),
                rec("records/source-audit.json", "来源审计", "audits/factoryalign-review.md"),
                rec("records/date-only.json", "只按日期", "组织 无关内容")]
        backlog = ("## 组织线\n- [blocked: 等你决定] 组织：直接引用 records/cited.json\n"
                   "- [blocked: 等你决定] 组织：分支 claude/pool-refill\n"
                   "- [blocked: 等你决定] 组织：结果 audits/pool-result.md\n"
                   "- [blocked: 等你决定] 组织：补池任务，来源 audits/factoryalign-review.md\n"
                   "- [blocked: 等你决定] 组织：补池任务\n")
        text = rs.owner_candidates(self.snap(backlog), tick, {"rows": rows, "gaps": []})
        for name in ("cited", "branch", "result"):
            self.assertIn(f"records/{name}.json:3", text)
        self.assertNotIn("records/factoryalign.json", text)
        self.assertNotIn("records/source-audit.json", text)
        self.assertNotIn("records/date-only.json", text)
        self.assertIn("未找到：组织：补池任务", text)

    def test_only_blocked_waiting_for_owner_are_looked_up(self):
        text = rs.owner_candidates(self.snap("## 组织线\n- [blocked: 等总理] 组织：a\n- [todo] 组织：b\n"), tick,
                                   {"rows": [], "gaps": []})
        self.assertIn("0 行，0 行有候选", text)

    def test_directly_cited_record_is_eligible(self):
        rows = [{"file": f"records/{name}.json", "date": "2026-10-29", "issues": [],
                 "search_text": "unrelated text",
                 "fields": [{"field": "owner_verbatim", "line": 3, "text": name}]}
                for name in ("old", "new")]
        backlog = "## 组织线\n- [blocked: 等你决定] 组织：复盘事实包 records\\old.json\n"
        text = rs.owner_candidates(self.snap(backlog), tick, {"rows": rows, "gaps": []})
        self.assertIn("records/old.json:3", text)
        self.assertNotIn("records/new.json", text)

    def test_unreadable_repos_merge_into_one_line_per_project_and_only_waiting_unmerged_are_listed(self):
        branches = [{"project": "driving-course-sim", "branch": f"claude/u{i}", "default": None, "state": "未知（仓库、默认分支或 git 不可读）",
                     "waiting": False} for i in range(5)]
        branches += [{"project": "incore", "branch": "claude/w", "default": "master", "state": "仍未合并", "waiting": True},
                     {"project": "incore", "branch": "claude/open", "default": "master", "state": "仍未合并", "waiting": False},
                     {"project": "incore", "branch": "claude/m", "default": "master", "state": "已合并", "waiting": True}]
        text = rs.diff_section(self.snap("", branches), None)
        self.assertEqual(text.count("仓库、默认分支或 git 不可读"), 1)
        self.assertIn("5 条分支未判断", text)
        self.assertIn("incore · claude/w", text)
        self.assertNotIn("incore · claude/open", text)
        self.assertNotIn("claude/u0", text)
