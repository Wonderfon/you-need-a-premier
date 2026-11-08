import re
import socket
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from . import rules, suggest
from .postprocess import VERSION
from .server import CSRF_COOKIE, STORE_KEY, create_app
from .store import InputStore

# Synthetic wording only; never the owner's text.
HEARD, FIXED = "阿帕奇", "Apache"


def correct(store, machine, final, kind="recognition_correction"):
    sid = store.create_audio_session("audio_capture", "t", "audio/webm")["id"]
    raw = store.append_revision(sid, "asr", machine, source="asr")
    store.append_revision(sid, "correction", final, raw["id"], "user_correction", {"correction_type": kind})
    return sid


class RuleSuggestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = InputStore(Path(self.temp.name))
        self.path = self.store.rules_path

    def tearDown(self):
        self.temp.cleanup()

    def two_sessions(self):
        return [correct(self.store, "今天我们用阿帕奇做网站。", "今天我们用Apache做网站。"),
                correct(self.store, "这个阿帕奇服务要重启", "这个Apache服务要重启")]

    def test_mining_needs_two_sessions_and_gives_evidence(self):
        correct(self.store, "今天我们用阿帕奇做网站。", "今天我们用Apache做网站。")
        self.assertEqual(suggest.suggestions(self.store, self.path), [])
        sids = [correct(self.store, "这个阿帕奇服务要重启", "这个Apache服务要重启")]
        (found,) = suggest.suggestions(self.store, self.path)
        self.assertEqual((found["from"], found["to"], found["count"], found["false_hits"]), (HEARD, FIXED, 2, 0))
        self.assertEqual(len(found["evidence"]), 2)
        self.assertIn(HEARD, found["evidence"][0]["original"])
        self.assertIn(FIXED, found["evidence"][0]["edited"])
        self.assertIn(sids[0], found["evidence_sessions"])

    def test_punctuation_fillers_digits_rewrites_and_covered_pairs_are_not_suggested(self):
        for _ in range(2):
            correct(self.store, "我们嗯开会，然后走", "我们开会。然后走")                       # filler + punctuation
            correct(self.store, "一共三个人来了", "一共3个人来了")                             # digits
            correct(self.store, "我觉得这个方案挺好", "整体来看这个方案可行", "user_rewrite")    # rewrite label
            correct(self.store, "用cloud code写", "用Claude Code写")                        # a built-in already does it
        self.assertEqual(suggest.suggestions(self.store, self.path), [])

    def test_false_hit_threshold(self):
        self.two_sessions()
        self.assertEqual(len(suggest.suggestions(self.store, self.path)), 1)
        # The owner kept the heard wording here: the rule would have broken this confirmed text.
        correct(self.store, "那个阿帕奇直升机很大", "那个阿帕奇直升机很大")
        self.assertEqual(suggest.suggestions(self.store, self.path), [])
        hits, false, checked = suggest.check(suggest._texts(self.store)[0], HEARD, FIXED)
        self.assertEqual((hits, false), (3, 1))

    def test_accept_applies_to_new_transcripts_then_delete_stops(self):
        self.two_sessions()
        rule = suggest.accept(self.store, self.path, HEARD, FIXED)
        self.assertEqual((rule["source"], len(rule["evidence_sessions"])), ("suggested", 2))
        self.assertEqual(suggest.suggestions(self.store, self.path), [])          # accepted: no longer suggested
        sid = self.store.create_audio_session("audio_capture", "t", "audio/webm")["id"]
        derived = self.store.append_asr_result(sid, {"text": "明天部署阿帕奇服务"})
        self.assertEqual(derived["content"], "明天部署Apache服务")
        self.assertTrue(derived["data"]["processor_version"].startswith(VERSION + "+rules-"))
        self.assertEqual(derived["data"]["applied_rules"][0]["rule_id"], "owner:" + rule["id"])
        self.assertEqual(self.store.session_detail(sid)["revisions"][0]["content"], "明天部署阿帕奇服务")  # machine original kept
        rules.delete_rule(self.path, rule["id"])
        sid2 = self.store.create_audio_session("audio_capture", "t", "audio/webm")["id"]
        self.assertEqual(self.store.append_asr_result(sid2, {"text": "明天部署阿帕奇服务"})["kind"], "asr")
        self.assertEqual(rules.active_rules(self.path)[1], VERSION)
        self.assertEqual(self.store.session_detail(sid)["revisions"][1]["content"], "明天部署Apache服务")  # old record untouched

    def test_near_has_both_examples_and_stays_out_of_formal_suggestions(self):
        sids = self.two_sessions()
        bad = correct(self.store, "那个阿帕奇直升机很大", "那个阿帕奇直升机很大")
        view = suggest.recommendations(self.store, self.path)
        self.assertEqual(view["suggestions"], [])
        (near,) = view["near"]
        self.assertEqual((near["count"], near["hits"], near["false_hits"]), (2, 3, 1))
        self.assertEqual(near["evidence_sessions"], sids)
        self.assertIn(FIXED, near["evidence"][0]["edited"])
        self.assertEqual(near["false_evidence"], [{"session_id": bad, "original": "那个阿帕奇直升机很大"}])
        with self.assertRaises(rules.RuleError):
            suggest.accept(self.store, self.path, HEARD, FIXED)
        rule = suggest.accept(self.store, self.path, HEARD, FIXED, near=True)
        self.assertEqual((rule["source"], rule["evidence_sessions"]), ("suggested", sids))
        self.assertEqual(suggest.recommendations(self.store, self.path), {"suggestions": [], "near": []})

    def test_near_includes_half_but_rejects_more_and_rechecks_stale_choice(self):
        self.two_sessions()
        for _ in range(2):
            correct(self.store, "那个阿帕奇直升机很大", "那个阿帕奇直升机很大")
        (near,) = suggest.recommendations(self.store, self.path)["near"]
        self.assertEqual((near["hits"], near["false_hits"]), (4, 2))
        correct(self.store, "那个阿帕奇直升机很大", "那个阿帕奇直升机很大")
        self.assertEqual(suggest.recommendations(self.store, self.path)["near"], [])
        with self.assertRaises(rules.RuleError):
            suggest.accept(self.store, self.path, HEARD, FIXED, near=True)

    def test_near_english_examples_are_single_sentences(self):
        correct(self.store, "Start here. This Apatchy service is ready. Then stop.", "Start here. This Apache service is ready. Then stop.")
        correct(self.store, "Use Apatchy for the website.", "Use Apache for the website.")
        correct(self.store, "Wait here. The Apatchy helicopter stays here. Keep it.", "Wait here. The Apatchy helicopter stays here. Keep it.")
        (near,) = suggest.recommendations(self.store, self.path)["near"]
        self.assertEqual((near["count"], near["hits"], near["false_hits"]), (2, 3, 1))
        self.assertEqual(near["evidence"][0]["original"], "This Apatchy service is ready.")
        self.assertEqual(near["evidence"][0]["edited"], "This Apache service is ready.")
        self.assertEqual(near["false_evidence"][0]["original"], "The Apatchy helicopter stays here.")

    def test_near_needs_two_distinct_sessions_and_excludes_covered_rules(self):
        correct(self.store, "用阿帕奇部署整个系统，然后给阿帕奇升级。", "用Apache部署整个系统，然后给Apache升级。")
        correct(self.store, "那个阿帕奇直升机很大", "那个阿帕奇直升机很大")
        self.assertEqual(suggest.recommendations(self.store, self.path)["near"], [])
        correct(self.store, "这个阿帕奇服务要重启", "这个Apache服务要重启")
        self.assertEqual(len(suggest.recommendations(self.store, self.path)["near"]), 1)
        rules.add_rule(self.path, HEARD, FIXED, "manual")
        self.assertEqual(suggest.recommendations(self.store, self.path)["near"], [])

    def test_stale_suggestion_cannot_be_accepted(self):
        with self.assertRaises(rules.RuleError):
            suggest.accept(self.store, self.path, HEARD, FIXED)

    def test_ignored_is_not_suggested_again_until_undone(self):
        self.two_sessions()
        rules.ignore(self.path, HEARD, FIXED)
        self.assertEqual(suggest.suggestions(self.store, self.path), [])
        self.two_sessions()                                                         # more evidence does not bring it back
        self.assertEqual(suggest.suggestions(self.store, self.path), [])
        rules.undo(self.path, rules.load(self.path)["log"][-1]["id"])
        self.assertEqual(len(suggest.suggestions(self.store, self.path)), 1)

    def test_builtin_can_be_disabled_not_deleted_and_undo_restores(self):
        self.assertEqual(rules.process("这个code x", self.path)[0], "这个Codex")
        rules.set_builtin(self.path, "codex-split", False)
        out, hits, version = rules.process("这个code x", self.path)
        self.assertEqual((out, hits), ("这个code x", []))
        self.assertNotEqual(version, VERSION)
        rules.undo(self.path, rules.load(self.path)["log"][-1]["id"])
        self.assertEqual(rules.process("这个code x", self.path)[0], "这个Codex")
        with self.assertRaises(rules.RuleError):
            rules.set_builtin(self.path, "no-such-rule", False)
        with self.assertRaises(rules.RuleError):
            rules.delete_rule(self.path, "codex-split")

    def test_manual_rule_order_undo_and_validation(self):
        rule = rules.add_rule(self.path, "阿帕奇", "Apache", "manual")
        longer = rules.add_rule(self.path, "阿帕奇服务器", "Apache HTTP Server", "manual")
        self.assertEqual(rules.process("阿帕奇服务器和阿帕奇", self.path)[0], "Apache HTTP Server和Apache")   # longest wording first
        rules.delete_rule(self.path, rule["id"])
        entry = next(e for e in rules.load(self.path)["log"] if e["action"] == "delete")
        rules.undo(self.path, entry["id"])
        self.assertEqual(len(rules.load(self.path)["rules"]), 2)
        with self.assertRaises(rules.RuleError):
            rules.undo(self.path, entry["id"])                                      # already undone
        for bad in [("", "x"), ("abc", "abc"), ("阿", "a"), ("x" * 61, "y"), ("阿帕奇", "")]:
            with self.assertRaises(rules.RuleError):
                rules.add_rule(self.path, *bad, "manual")
        self.assertEqual(rules.process("用\\1做", self.path)[0], "用\\1做")
        rules.add_rule(self.path, "反斜杠", "a\\1b", "manual")
        self.assertEqual(rules.process("反斜杠", self.path)[0], "a\\1b")           # replacement is literal, not a regex template
        self.assertTrue(longer["added_at"].endswith("Z"))

    def test_corrupt_rule_file_degrades_to_builtins_and_is_not_overwritten(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(rules.process("code x", self.path)[0], "Codex")
        with self.assertRaises(rules.RuleError):
            rules.add_rule(self.path, "阿帕奇", "Apache", "manual")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")


class RuleRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.app = create_app(Path(self.temp.name), expected_port=port, dashboard_root=Path(self.temp.name))
        self.client = TestClient(TestServer(self.app, host="127.0.0.1", port=port))
        await self.client.start_server()
        html = await (await self.client.get("/")).text()
        token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        self.headers = {"Origin": f"http://127.0.0.1:{port}", "Cookie": f"{CSRF_COOKIE}={token}", "X-CSRF-Token": token}
        self.store = self.app[STORE_KEY]

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_owner_path_accept_delete_undo(self):
        for machine, final in [("今天我们用阿帕奇做网站。", "今天我们用Apache做网站。"), ("这个阿帕奇服务要重启", "这个Apache服务要重启")]:
            correct(self.store, machine, final)
        view = await (await self.client.get("/api/rules")).json()
        (suggestion,) = view["suggestions"]
        self.assertEqual(len(view["builtin"]), 11)
        accepted = await self.client.post("/api/rules/accept", json={"from": suggestion["from"], "to": suggestion["to"]}, headers=self.headers)
        view = await accepted.json()
        self.assertEqual((view["suggestions"], len(view["owner"])), ([], 1))
        stale = await self.client.post("/api/rules/accept", json={"from": HEARD, "to": FIXED}, headers=self.headers)
        self.assertEqual(stale.status, 409)
        off = await self.client.post("/api/rules/builtin/codex-split/enabled", json={"enabled": False}, headers=self.headers)
        self.assertFalse(next(r for r in (await off.json())["builtin"] if r["id"] == "codex-split")["enabled"])
        deleted = await self.client.delete("/api/rules/owner/" + view["owner"][0]["id"], headers=self.headers)
        view = await deleted.json()
        self.assertEqual(view["owner"], [])
        undo = await self.client.post(f"/api/rules/log/{[e for e in view['log'] if e['action'] == 'delete'][0]['id']}/undo", json={}, headers=self.headers)
        self.assertEqual(len((await undo.json())["owner"]), 1)
        no_csrf = await self.client.delete("/api/rules/owner/x", headers={"Origin": self.headers["Origin"]})
        self.assertEqual(no_csrf.status, 403)
        added = await self.client.post("/api/rules/add", json={"from": "x", "to": "y"}, headers=self.headers)
        self.assertEqual(added.status, 400)

    async def test_near_ignore_undo_then_explicit_accept_keeps_provenance(self):
        sids = [correct(self.store, "今天我们用阿帕奇做网站。", "今天我们用Apache做网站。"),
                correct(self.store, "这个阿帕奇服务要重启", "这个Apache服务要重启")]
        correct(self.store, "那个阿帕奇直升机很大", "那个阿帕奇直升机很大")
        view = await (await self.client.get("/api/rules")).json()
        self.assertEqual(view["suggestions"], [])
        self.assertEqual(len(view["near"]), 1)
        body = {"from": HEARD, "to": FIXED}
        formal = await self.client.post("/api/rules/accept", json=body, headers=self.headers)
        self.assertEqual(formal.status, 409)
        ignored = await self.client.post("/api/rules/ignore", json=body, headers=self.headers)
        self.assertEqual(ignored.status, 200)
        view = await ignored.json()
        self.assertEqual(view["near"], [])
        restored = await self.client.post(f"/api/rules/log/{view['log'][0]['id']}/undo", json={}, headers=self.headers)
        self.assertEqual(len((await restored.json())["near"]), 1)
        accepted = await self.client.post("/api/rules/accept", json={**body, "near": True}, headers=self.headers)
        self.assertEqual(accepted.status, 200)
        view = await accepted.json()
        self.assertEqual((view["suggestions"], view["near"]), ([], []))
        self.assertEqual((view["owner"][0]["source"], view["owner"][0]["evidence_sessions"]), ("suggested", sids))
        self.assertEqual(view["log"][0]["action"], "accept")
        stale = await self.client.post("/api/rules/accept", json={**body, "near": True}, headers=self.headers)
        self.assertEqual(stale.status, 409)


if __name__ == "__main__":
    unittest.main()
