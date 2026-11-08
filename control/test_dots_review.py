import json
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dots_review as dr  # noqa: E402
import dots_watch  # noqa: E402
import review_packet as rp  # noqa: E402
import tick  # noqa: E402

NOW = datetime(2026, 11, 5, 10, 0)
SEC = "组织线"
VERDICT = "三句理由。\n证据：a.py:3\n命令：python -m unittest，OK\n\nVERDICT: pass\n"


def git(repo, *a):
    return subprocess.run([rp.GIT, "-c", "safe.directory=*", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout


class DotsReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data, self.drive = self.root / "data", self.root / "drive"
        self.audits = self.root / "incore" / "audits"
        self.records = self.root / "incore" / "records"
        for d in (self.data, self.audits, self.records, self.drive / "tasks", self.drive / "materials"):
            d.mkdir(parents=True)
        (self.drive / "log.md").write_text("# log\n", encoding="utf-8")
        rules = self.root / "rules.md"
        rules.write_text("- 审查的目的（owner\x2010-04）：代替 owner 试用。\n", encoding="utf-8")
        self.backlog = self.root / "backlog.md"
        for name, value in {"DATA": self.data, "WORKSPACE": self.root, "BACKLOG": self.backlog, "RULES_FILE": rules,
                            "LOG_FILE": self.data / "log.jsonl", "RUNS_FILE": self.data / "runs.jsonl"}.items():
            p = mock.patch.object(tick, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(dots_watch, "DRIVE", self.drive)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(dr, "SCAN_EVERY_MIN", 0)
        p.start()
        self.addCleanup(p.stop)
        self.runs, self.packets, self.n, self.rows = [], {}, 0, {}

    # -- helpers
    def add(self, item, packet=None, role="coordinator", project="incore", ok=True, name=None):  # role picks the section
        """A [review] row of the organization line with its executor run and review packet."""
        self.n += 1
        name = name or f"res{self.n}.md"
        (self.audits / name).write_text(f"# 结果 {self.n}\n\n内容 {item[:10]}\n", encoding="utf-8")
        self.runs.append({"_at": NOW - timedelta(hours=1), "role": role, "item": item, "kind": "executor", "ok": ok, "project": project})
        self.packets[item] = packet if packet is not None else {"text": "", "hard": None, "facts": {
            "repo": None, "refs": {}, "base": None, "changed": [], "result_files": [name]}}
        self.rows.setdefault(role, []).append(f"- [review] {item}")
        self.backlog.write_text("# b\n" + "".join(f"## {tick.LINE_SECTIONS[r]}\n权重：20%\n" + "\n".join(v) + "\n" for r, v in self.rows.items()), encoding="utf-8")

    def patches(self):
        return mock.patch.multiple(tick, load_runs=lambda: self.runs, review_packet_for=lambda item: self.packets.get(item))

    def step(self, now=NOW):
        with self.patches():
            dr.step(tick, now, self.root)

    def state(self):
        return dr.load_state(self.data / dr.STATE_NAME)

    def code_repo(self, tests=True, extra=()):
        repo = self.root / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "master")
        git(repo, "config", "user.email", "t@example.invalid")
        git(repo, "config", "user.name", "t")
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        git(repo, "add", "a.py")
        git(repo, "commit", "-qm", "base")
        base = git(repo, "rev-parse", "HEAD").strip()
        git(repo, "checkout", "-qb", "claude/topic")
        (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        names = ["a.py"]
        if tests:
            (repo / "tests").mkdir()
            (repo / "tests" / "test_a.py").write_text("import unittest\n", encoding="utf-8")
            names.append("tests/test_a.py")
        (repo / "tool.exe").write_bytes(b"MZ")
        (repo / ".env").write_text("K=1", encoding="utf-8")
        (repo / "notes.md").write_text("hi\n", encoding="utf-8")
        git(repo, "add", "-f", "tool.exe", ".env", "notes.md", *names)
        git(repo, "commit", "-qm", "work")
        return repo, base, git(repo, "rev-parse", "HEAD").strip(), names

    # -- picking
    def test_candidates_rules(self):
        quant = "量化：回测补一项，只读"
        self.add("组织：只读盘点 X")
        self.add(quant, role="quant", project="quant")
        self.add("量化：核对券商下单接口的返回", role="quant", project="quant")
        self.add("量化：账户权限的盘点", role="quant", project="quant")
        self.add("组织：读 records\x2f2026-11-05-x.json 整理", name="r.md")
        self.add("组织：看 input/data/ 下的转写")
        self.add("组织：核对 discord/data/ 和 review-audio/ 目录")
        self.add("组织：检查 .env 的读取方式")
        self.add("Kiki：写一段")
        self.add("组织：画吉祥物〔执行者：Opus〕")
        self.add("组织：这行的执行没成功", ok=False)
        secs = tick.parse_backlog(self.backlog.read_text(encoding="utf-8"))
        got = [c[3] for c in dr.candidates(tick, secs, self.runs, dr.load_state(self.root / "none.json"))]
        self.assertEqual(got, ["组织：只读盘点 X", quant])
        sent = dr.load_state(self.root / "none.json")
        sent["rows"][dr.key_of("coordinator", "组织：只读盘点 X")] = {}  # sent once is enough
        self.assertEqual([c[3] for c in dr.candidates(tick, secs, self.runs, sent)], [quant])

    def test_prepare_checks_the_evidence(self):
        doc = {"text": "", "hard": None, "facts": {"repo": None, "refs": {}, "base": None, "changed": [], "result_files": ["a.md"]}}
        code = lambda **o: {"text": "", "hard": None, "facts": {"repo": "/r", "refs": {"claude/x": "a" * 40}, "base": "b" * 40,
                            "changed": ["a.py", "tests/test_a.py"], "result_files": ["a.md"], **o}}
        for packet, ok in ((doc, True), (code(), True), ({**doc, "hard": "没开工"}, False),
                           (code(changed=["a.py"]), False), (code(risky=["records/x.json"]), False),
                           (code(changed=["tests/test_a.py", "cfg/api_token.txt"]), False),
                           (code(result_files=[]), False), (code(base=None), False)):
            with mock.patch.object(tick, "review_packet_for", return_value=packet):
                self.assertEqual(bool(dr.prepare(tick, "x")[0]), ok, packet)
        with mock.patch.object(tick, "review_packet_for", return_value=None):
            self.assertIsNone(dr.prepare(tick, "x")[0])

    # -- materials
    def test_materials_have_no_claude_verdict_and_no_owner_words(self):
        self.records.joinpath("r.json").write_text(json.dumps({"owner_verbatim": "我希望你更充分地利用这个强力的外包资源缓解压力"}, ensure_ascii=False), encoding="utf-8")
        (self.audits / "2026-11-05-x-review.md").write_text("审查结论 SECRETVERDICT\nVERDICT: pass\n", encoding="utf-8")
        self.n += 1
        (self.audits / "res.md").write_text("# 结果\n\n正常段落。\n\n他说：我希望你更充分地利用这个强力的外包资源缓解压力。\n\n结尾\n", encoding="utf-8")
        repo, base, tip, names = self.code_repo()
        packet = {"text": "", "hard": None, "facts": {"repo": str(repo), "refs": {"claude/topic": tip}, "base": base,
                  "changed": names + ["tool.exe", ".env", "notes.md"], "result_files": ["res.md", "2026-11-05-x-review.md"]}}
        d = self.root / "out"
        with self.assertRaises(ValueError):  # a review file listed as a result is refused, not copied
            dr.build_row(tick, d, "p01", "组织：x", packet, [])
        packet["facts"]["result_files"] = ["res.md"]
        # .env is a credential name: the row itself is refused earlier, by prepare()
        with mock.patch.object(tick, "review_packet_for", return_value=packet):
            self.assertIn("凭据", dr.prepare(tick, "x")[1])
        packet["facts"]["changed"] = names + ["tool.exe", "notes.md"]
        dr.build_row(tick, self.root / "out2", "p01", "组织：x", packet, dr.verbatims(self.records))
        d = self.root / "out2"
        files = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
        self.assertEqual(files, ["README.txt", "diff-claude_topic.patch", "result/res.md", "src-noexe.zip", "task.md", "tests.md"])
        self.assertIn("[此段含 owner 原话，已按规则删去]", (d / "result" / "res.md").read_text(encoding="utf-8"))
        self.assertNotIn("外包资源", "".join(p.read_text(encoding="utf-8") for p in d.rglob("*.md")))
        with zipfile.ZipFile(d / "src-noexe.zip") as z:
            self.assertEqual(sorted(z.namelist()), ["a.py", "notes.md", "tests/test_a.py"])  # .exe and .env left out
        self.assertIn("python -m unittest", (d / "tests.md").read_text(encoding="utf-8"))
        self.assertNotIn("SECRETVERDICT", "".join(p.read_text(encoding="utf-8", errors="ignore") for p in d.rglob("*") if p.is_file() and p.suffix != ".zip"))

    def test_secret_in_a_result_drops_the_row(self):
        with self.assertRaises(ValueError):
            dr.scrub("key sk-" + "a" * 30, [])

    # -- the loop
    def test_waits_for_three_rows_or_two_hours(self):
        self.add("组织：盘点一")
        self.add("组织：盘点二")
        self.step()
        self.assertEqual(list((self.drive / "tasks").glob("T-*.md")), [])
        self.step(NOW + timedelta(hours=1, minutes=59))
        self.assertEqual(list((self.drive / "tasks").glob("T-*.md")), [])
        self.step(NOW + timedelta(hours=2, minutes=1))
        self.assertEqual(len(list((self.drive / "tasks").glob("T-*.md"))), 1)

    def test_batch_of_ten_task_file_and_no_resend(self):
        for i in range(12):
            self.add(f"组织：只读盘点 {i:02d}")
        self.step()
        task = next((self.drive / "tasks").glob("T-20261105-01.md"))
        first, _, body = task.read_text(encoding="utf-8").partition("\n")
        self.assertEqual(first, "status: new")
        self.assertIn("materials/review-20261105-01/", body)
        self.assertIn("VERDICT: pass", body)
        self.assertIn("2026-11-05 22:00", body)  # DEADLINE_H after sending
        folder = self.drive / "materials" / "review-20261105-01"
        self.assertEqual(sorted(p.name for p in folder.iterdir() if p.is_dir()), [f"p{i:02d}" for i in range(1, 11)])
        self.assertTrue((folder / "rules.md").is_file())
        st = self.state()
        self.assertEqual(len(st["rows"]), 10)
        self.assertEqual(self.drive.joinpath("log.md").read_text(encoding="utf-8").splitlines()[-1].split(" | ")[1:3], ["T-20261105-01", "new"])
        self.step(NOW + timedelta(minutes=10))  # 2 rows left, a second batch only after the hold; the first 10 are never resent
        self.step(NOW + timedelta(hours=2, minutes=20))
        self.assertEqual(len(self.state()["rows"]), 12)
        self.assertEqual(self.state()["batches"]["T-20261105-02"]["rows"].keys(), {"p01", "p02"})

    def test_at_most_two_open_batches_and_unsendable_rows_are_remembered(self):
        for i in range(3):
            self.add(f"组织：A{i}")
        self.step()
        for i in range(3):
            self.add(f"组织：B{i}")
        self.step(NOW + timedelta(minutes=1))
        for i in range(3):
            self.add(f"组织：C{i}")
        self.step(NOW + timedelta(minutes=2))
        self.assertEqual(len(self.state()["batches"]), 2)  # MAX_OPEN
        # rows whose evidence fails are not retried every loop
        self.packets.update({f"组织：C{i}": {"text": "", "hard": "没开工", "facts": {}} for i in range(3)})
        b = self.state()["batches"]
        for t in b.values():
            t["status"] = "done"
        dr.save_state(self.data / dr.STATE_NAME, dict(self.state(), batches=b))
        self.step(NOW + timedelta(minutes=3))
        self.assertEqual(len(self.state()["skipped"]), 3)
        with mock.patch.object(dr, "prepare", wraps=dr.prepare) as prep:
            self.step(NOW + timedelta(minutes=4))
            self.assertEqual(prep.call_count, 0)

    def test_follow_done_blocked_timeout(self):
        for i in range(3):
            self.add(f"组织：盘点 {i}")
        self.step()
        tid = next(iter(self.state()["batches"]))
        tf = self.drive / "tasks" / f"{tid}.md"
        tf.write_text(tf.read_text(encoding="utf-8").replace("status: new", "status: done 2026-11-05T15:00:00+00:00", 1), encoding="utf-8")
        self.step(NOW + timedelta(minutes=30))
        self.assertEqual(self.state()["batches"][tid]["status"], "done")
        # timeout: still new after the deadline -> withdrawn, rows stay sent (no resend)
        for i in range(3):
            self.add(f"组织：另一批 {i}")
        self.step(NOW + timedelta(minutes=31))
        tid2 = [t for t in self.state()["batches"] if t != tid][0]
        self.step(NOW + timedelta(hours=13))
        self.assertEqual(self.state()["batches"][tid2]["status"], "timeout")
        self.assertTrue((self.drive / "tasks" / f"{tid2}.md").read_text(encoding="utf-8").startswith("status: blocked 调度器撤回"))
        self.assertIn("| timeout |", self.drive.joinpath("log.md").read_text(encoding="utf-8"))
        self.assertEqual(len(self.state()["rows"]), 6)

    def test_scan_is_rate_limited(self):
        self.add("组织：盘点")
        with mock.patch.object(dr, "SCAN_EVERY_MIN", 5), mock.patch.object(tick, "parse_backlog", wraps=tick.parse_backlog) as pb:
            self.step()
            self.step(NOW + timedelta(minutes=4))
            self.assertEqual(pb.call_count, 1)
            self.step(NOW + timedelta(minutes=6))
            self.assertEqual(pb.call_count, 2)

    def test_drive_unreadable_does_nothing(self):
        self.add("组织：盘点")
        with mock.patch.object(dots_watch, "DRIVE", self.root / "no-drive"):
            self.step()
        self.assertFalse((self.data / dr.STATE_NAME).exists())

    # -- the opinion in the deputy's prompt
    def sent_row(self, item="组织：盘点 0"):
        for i in range(3):
            self.add(f"组织：盘点 {i}")
        self.step()
        return self.state()["rows"][dr.key_of("coordinator", item)]

    def test_opinion_text_only_with_a_verdict(self):
        row = self.sent_row()
        self.assertEqual(dr.opinion_text(tick, "coordinator", "组织：盘点 0"), "")  # nothing back yet
        self.assertEqual(dr.opinion_text(tick, "coordinator", "组织：没送过"), "")
        f = self.drive / "materials" / row["folder"] / row["p"] / "verdict.md"
        f.write_text("还没写完\n", encoding="utf-8")
        self.assertEqual(dr.opinion_text(tick, "coordinator", "组织：盘点 0"), "")  # no VERDICT line yet
        f.write_text(VERDICT, encoding="utf-8")
        text = dr.opinion_text(tick, "coordinator", "组织：盘点 0")
        self.assertIn("只作参考，结论由你下", text)
        self.assertIn(VERDICT.strip(), text)
        self.assertIn("它的 pass 不能替你关行", text)
        (f.parent / "results").mkdir()
        f.rename(f.parent / "results" / "verdict.md")  # results sub-folder is read too
        self.assertIn("VERDICT: pass", dr.opinion_text(tick, "coordinator", "组织：盘点 0"))

    def test_prompt_carries_the_opinion_and_is_unchanged_without_it(self):
        row = self.sent_row()
        item = "组织：盘点 0"
        seen = []

        def fake_resident(role, kind, prompt, *a, **k):
            seen.append(prompt)
            return 0, self.data / "x.jsonl", True, "VERDICT: pass", 1

        def run():
            with self.patches(), mock.patch.object(tick, "run_resident", side_effect=fake_resident), \
                    mock.patch.object(tick.shadow_review, "run", return_value=(None, None)), \
                    mock.patch.object(tick, "review_siblings", return_value=[]), mock.patch.object(tick, "note_verdict"):
                tick.run_lane_work("coordinator", SEC, "review", "- [review] " + item, item, 1, 20)

        run()
        (self.drive / "materials" / row["folder"] / row["p"] / "verdict.md").write_text(VERDICT, encoding="utf-8")
        run()
        self.assertNotIn("dotz 先审意见", seen[0])
        self.assertIn("dotz 先审意见，只作参考，结论由你下", seen[1])
        self.assertIn("命令：python -m unittest，OK", seen[1])
        self.assertEqual(seen[1][:seen[1].index("\n【dotz")], seen[0])  # the rest of the prompt is the same

    def test_batch_review_prompt_carries_the_opinion_per_row(self):
        row = self.sent_row()
        (self.drive / "materials" / row["folder"] / row["p"] / "verdict.md").write_text(VERDICT, encoding="utf-8")
        rows = [{"line": "- [review] 组织：盘点 0", "item": "组织：盘点 0", "packet": None, "shadow": None},
                {"line": "- [review] 组织：盘点 1", "item": "组织：盘点 1", "packet": None, "shadow": None}]
        with mock.patch.object(tick, "run_resident", return_value=(0, self.data / "x.jsonl", True, "VERDICT 1: pass", 1)) as rr, \
                mock.patch.object(tick, "note_verdict"):
            tick.run_review_batch("coordinator", SEC, rows)
        prompt = rr.call_args[0][2]
        self.assertEqual(prompt.count("dotz 先审意见"), 1)
        self.assertLess(prompt.index("【第 1 条】"), prompt.index("dotz 先审意见"))
        self.assertLess(prompt.index("dotz 先审意见"), prompt.index("【第 2 条】"))

    def test_every_test_that_runs_the_supervisor_replaces_dots_review_step(self):
        """supervise_step calls dots_review.step, which reads control/data and the real Drive: a test that runs the loop must replace it."""
        offenders = [p.name for p in sorted(Path(__file__).parent.glob("test_*.py"))
                     if re.search(r"tick\.supervise(_step)?\(|tick\.main\(\s*(\[\s*\])?\s*\)", p.read_text(encoding="utf-8"))
                     and "dots_review" not in p.read_text(encoding="utf-8")]
        self.assertEqual(offenders, [])

    def test_plan_prompts_name_dots_during_the_codex_gate(self):
        for k in ("plan", "codex_plan"):
            self.assertIn("〔执行者：dotz〕", tick.PROMPTS[k])
            self.assertIn("一次可以排多行", tick.PROMPTS[k])
            self.assertIn("CODEX_WEEKLY_MAX", tick.PROMPTS[k])

    def test_replay_lists_and_writes_nothing(self):
        for i in range(4):
            self.add(f"组织：只读盘点 {i}")
        self.add("组织：读 records/x.json")
        with self.patches():
            out = dr.replay(tick, self.backlog)
        self.assertIn("会送 4，分 1 批", out)
        self.assertNotIn("records/x.json", out.split("检查后不送")[0])
        self.assertEqual(list((self.drive / "tasks").iterdir()), [])
        self.assertEqual(list((self.drive / "materials").iterdir()), [])
        self.assertFalse((self.data / dr.STATE_NAME).exists())


if __name__ == "__main__":
    unittest.main()
