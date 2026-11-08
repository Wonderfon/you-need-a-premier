import hashlib
import tempfile
import unittest
from pathlib import Path
from .postprocess import VERSION, apply
from .store import InputStore

# Raw ASR from real sessions b3c6df86-... and 6ecc992f-... (excerpt), where Claude was heard as cloud.
SESSION_B3C6 = "所以现在Cloud Code才能准确识别为Anthropic Cloud，而不是云吗？"
SESSION_B3C6_FIXED = "所以现在Claude Code才能准确识别为Anthropic Claude，而不是云吗？"
SESSION_6ECC = "your own usage limit ... different from cloud code which has five hours ... usage limit monitoring ... vice prime minister."

class TermRuleTests(unittest.TestCase):
    def test_new_terms_and_no_false_positives(self):
        for raw, expected in [("我是跟 code x 去提的", "我是跟 Codex 去提的"), ("cloud code code x", "Claude Code Codex"), ("这个f o s我们", "这个FOS我们"), ("F O S的项目", "FOS的项目"),
                              ("fiber mind 是产品", "FabGrid 是产品"), ("Cyber mind agent", "FabGrid agent"), ("谷歌COLLAB上面跑", "谷歌Colab上面跑"), ("Google Colan，它", "Google Colab，它"),
                              ("上传到我的get up", "上传到我的GitHub"), ("这个woozy agent", "这个Uvox agent"), ("阿瑞斯猫是公司", "Arbitros是公司"), ("ARISMO的官网", "Arbitros的官网")]:
            with self.subTest(raw=raw):
                out, hits = apply(raw)
                self.assertEqual(out, expected)
                self.assertEqual(apply(out), (out, []))
        for keep in ["the graph we are collaborating here", "get up early", "five minds", "code xml", "乌贼和阿瑞斯", "scode x", "额度和额外"]:
            self.assertEqual(apply(keep), (keep, []))

    def test_optional_filler_rules_are_off_by_default_and_conservative(self):
        from .postprocess import OPTIONAL_RULES
        raw = "我们呃，那个嗯方案。嗯，对。你你你看这个这个这个。额度、呃逆"
        self.assertEqual(apply(raw), (raw, []))
        out, hits = apply(raw, OPTIONAL_RULES)
        self.assertEqual(out, "我们那个方案。嗯，对。你看这个。额度、呃逆")
        self.assertEqual({h["rule_id"] for h in hits}, {"filler-e", "filler-en-mid", "repeat-3x"})


class PostprocessTests(unittest.TestCase):
    def test_terms_and_idempotence(self):
        for raw, expected in [("Q五GPU常驻", "Q5GPU常驻"), ("Q五它节省显存", "Q5它节省显存"), ("WSL cloud code配置", "WSL Claude Code配置"), ("cloud code存下来的记忆", "Claude Code存下来的记忆"),
                              ("cloud code", "Claude Code"), ("cloud code配置", "Claude Code配置"), ("cloud code CLI", "Claude Code CLI"), ("CLOUD CODE", "Claude Code"), ("Cloud  Code里", "Claude Code里"), ("用cloud code。", "用Claude Code。"),
                              ("anthropic cloud", "Anthropic Claude"), ("ANTHROPIC  CLOUD。", "Anthropic Claude。"), ("Anthropic Cloud Code", "Anthropic Claude Code"),
                              ("Google Cloud Code和cloud code不同", "Google Cloud Code和Claude Code不同"),
                              (SESSION_B3C6, SESSION_B3C6_FIXED), (SESSION_6ECC, SESSION_6ECC.replace("cloud code", "Claude Code")),
                              # Owner blanket request (单独cloud也应该替换): every other standalone cloud, storage/weather included.
                              ("cloud", "Claude"), ("Cloud", "Claude"), ("CLOUD", "Claude"), ("cLoUd", "Claude"), ("用cloud。", "用Claude。"), ("上传到cloud", "上传到Claude"), ("cloud额度", "Claude额度"),
                              ("cloud storage", "Claude storage"), ("the cloud looks heavy today", "the Claude looks heavy today"), ("cloud coder配置", "Claude coder配置"),
                              ("Google Cloud和cloud", "Google Cloud和Claude"), ("iCloud和cloud", "iCloud和Claude"), ("clouds, cloudflare, cloud", "clouds, cloudflare, Claude")]:
            with self.subTest(raw=raw):
                out, hits = apply(raw)
                self.assertEqual(out, expected)
                self.assertTrue(hits)
                self.assertEqual(apply(out), (out, []))

    def test_real_session_hits(self):
        # 云 is the owner naming the misrecognition, not a product name; it stays.
        self.assertEqual(apply(SESSION_B3C6)[1], [
            {"rule_id": "claude-code-name", "before": "Cloud Code", "after": "Claude Code"},
            {"rule_id": "anthropic-claude-name", "before": "Anthropic Cloud", "after": "Anthropic Claude"}])
        # "vice prime minister" is left alone; this helper only fixes the Claude names.
        self.assertEqual(apply(SESSION_6ECC)[1], [{"rule_id": "claude-code-name", "before": "cloud code", "after": "Claude Code"}])

    def test_multiword_rules_run_before_standalone_cloud(self):
        self.assertEqual(apply("cloud storage和Cloud Code"), ("Claude storage和Claude Code", [
            {"rule_id": "claude-code-name", "before": "Cloud Code", "after": "Claude Code"},
            {"rule_id": "claude-standalone-name", "before": "cloud", "after": "Claude"}]))
        self.assertEqual(apply("anthropic cloud")[1], [{"rule_id": "anthropic-claude-name", "before": "anthropic cloud", "after": "Anthropic Claude"}])

    def test_ambiguous_and_unobserved_unchanged(self):
        for text in ["Google Cloud Code配置", "Google   cloud code CLI", "google cloud code", "谷歌Cloud Code", "GCP Cloud Code", "AWS cloud code部署", "Azure Cloud Code",
                     "Google Cloud Storage", "Google Cloud", "谷歌cloud", "GCP cloud存储", "AWS cloud", "Azure cloud迁移", "今天云很多",
                     "iCloud", "iCloud code", "clouds", "CLOUDS很多", "cloudflare配置", "cloudcode", "club code配置", "Anthropic Cloudflare",
                     "Claude", "Claude Code", "claude code", "Anthropic Claude", "Q五", "AQ五模型", "外部短路"]:
            with self.subTest(text=text):
                self.assertEqual(apply(text), (text, []))

    def test_raw_preservation_and_no_inherited_human_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            store = InputStore(Path(folder))
            sid = store.create_audio_session("audio_capture", "test", "audio/webm")["id"]
            audio = b"synthetic test bytes, never a user recording"
            digest = hashlib.sha256(audio).hexdigest()
            store.put_chunk(sid, 0, audio, digest)
            store.finalize_audio(sid, 1, len(audio))
            result = {"text": "Q五GPU常驻", "metadata": {"engine": "test", "boundary_review_required": True}}
            derived = store.append_asr_result(sid, result)
            revisions = store.session_detail(sid)["revisions"]
            self.assertEqual(len(revisions), 2)
            self.assertEqual(revisions[0]["content"], result["text"])
            self.assertEqual(derived["parent_id"], revisions[0]["id"])
            self.assertEqual(derived["kind"], "postprocess")
            self.assertFalse(derived["data"]["human_verified"])
            self.assertTrue(derived["data"]["metadata"]["boundary_review_required"])
            self.assertIsNone(derived["verification"] if "verification" in derived else None)
            self.assertEqual(store.list_verified_training_rows(), [])
            store.verify_revision(sid, derived["id"], digest)
            self.assertEqual(store.list_verified_training_rows()[0]["revision_id"], derived["id"])
            self.assertEqual(store.get_session_path(sid).read_bytes(), audio)
            self.assertEqual(store.append_asr_result(sid, {"text":"无需替换"})["kind"], "asr")
            self.assertEqual(store.list_verified_training_rows(), [])

    def test_v2_revision_is_separate_unverified_and_keeps_prior_history(self):
        with tempfile.TemporaryDirectory() as folder:
            store = InputStore(Path(folder))
            sid = store.create_audio_session("audio_capture", "test", "audio/webm")["id"]
            audio = b"synthetic test bytes, never a user recording"
            digest = hashlib.sha256(audio).hexdigest()
            store.put_chunk(sid, 0, audio, digest)
            store.finalize_audio(sid, 1, len(audio))
            # An earlier run without term hits, explicitly checked by a human.
            old = store.append_asr_result(sid, {"text": "明天下午三点开会"})
            self.assertEqual(old["kind"], "asr")
            store.verify_revision(sid, old["id"], digest)

            derived = store.append_asr_result(sid, {"text": SESSION_B3C6, "metadata": {"engine": "test"}})
            detail = store.session_detail(sid)
            revisions = detail["revisions"]
            self.assertEqual([rev["kind"] for rev in revisions], ["asr", "asr", "postprocess"])
            prior, raw, fixed = revisions
            self.assertEqual((prior["content"], prior["sha256"]), (old["content"], old["sha256"]))
            self.assertIsNotNone(prior["verification"])  # Kept on record, only superseded.
            self.assertFalse(prior["verification"]["current"])
            self.assertEqual(raw["content"], SESSION_B3C6)
            self.assertEqual(raw["sha256"], hashlib.sha256(SESSION_B3C6.encode("utf-8")).hexdigest())
            self.assertIsNone(raw["parent_id"])
            self.assertEqual(fixed["id"], derived["id"])
            self.assertEqual(fixed["parent_id"], raw["id"])
            self.assertEqual(fixed["content"], SESSION_B3C6_FIXED)
            self.assertEqual(fixed["data"]["processor_version"], VERSION)
            self.assertEqual([hit["rule_id"] for hit in fixed["data"]["applied_rules"]], ["claude-code-name", "anthropic-claude-name"])
            self.assertFalse(fixed["data"]["human_verified"])
            self.assertIsNone(fixed["verification"])
            # Same pick as app.js revisionForEditing: newest non-instruction revision.
            self.assertEqual(next(rev for rev in reversed(revisions) if rev["kind"] != "instruction")["id"], derived["id"])
            self.assertEqual(store.list_verified_training_rows(), [])
            self.assertEqual(detail["original_sha256"], digest)
            self.assertEqual(store.get_session_path(sid).read_bytes(), audio)

    def test_v3_standalone_cloud_keeps_raw_and_derived_unverified(self):
        with tempfile.TemporaryDirectory() as folder:
            store = InputStore(Path(folder))
            sid = store.create_audio_session("audio_capture", "test", "audio/webm")["id"]
            audio = b"synthetic test bytes, never a user recording"
            digest = hashlib.sha256(audio).hexdigest()
            store.put_chunk(sid, 0, audio, digest)
            store.finalize_audio(sid, 1, len(audio))
            raw_text = "上传到cloud，AWS cloud不动"
            derived = store.append_asr_result(sid, {"text": raw_text, "metadata": {"engine": "test"}})
            raw, fixed = store.session_detail(sid)["revisions"]
            self.assertEqual(raw["kind"], "asr")
            self.assertEqual(raw["content"], raw_text)
            self.assertEqual(raw["sha256"], hashlib.sha256(raw_text.encode("utf-8")).hexdigest())
            self.assertIsNone(raw["parent_id"])
            self.assertEqual(fixed["id"], derived["id"])
            self.assertEqual(fixed["kind"], "postprocess")
            self.assertEqual(fixed["parent_id"], raw["id"])
            self.assertEqual(fixed["content"], "上传到Claude，AWS cloud不动")
            self.assertEqual(fixed["data"]["processor_version"], VERSION)
            self.assertEqual([hit["rule_id"] for hit in fixed["data"]["applied_rules"]], ["claude-standalone-name"])
            self.assertFalse(fixed["data"]["human_verified"])
            self.assertIsNone(fixed["verification"])
            self.assertEqual(store.list_verified_training_rows(), [])
            # Vendor-prefixed cloud alone yields no derived revision.
            self.assertEqual(store.append_asr_result(sid, {"text": "AWS cloud迁移"})["kind"], "asr")
            self.assertEqual(store.list_verified_training_rows(), [])
            self.assertEqual(store.get_session_path(sid).read_bytes(), audio)

if __name__ == "__main__": unittest.main()
