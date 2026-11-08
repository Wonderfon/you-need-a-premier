"""Synthetic checks for session_map: mapping, export, restore under a new cwd. Run: cd control; python -m unittest test_session_map"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_map as sm  # noqa: E402

CID, XID, MID = "11111111-1111-1111-1111-111111111111", "01a1-aaaa", "22222222-2222-2222-2222-222222222222"
OLD = r"C:\old user\proj one"


def build(root: Path):
    claude, codex, data = root / "claude", root / "codex", root / "data"
    d = claude / sm.enc(OLD)
    d.mkdir(parents=True)
    (d / f"{CID}.jsonl").write_text(
        '{"type":"queue-operation"}\n' + json.dumps({"type": "user", "cwd": OLD, "version": "2.1.9", "message": "SECRET TEXT"}) + "\n",
        encoding="utf-8")
    (d / CID).mkdir()
    (d / CID / "sub.txt").write_text("side", encoding="utf-8")
    f = codex / "sessions" / "2026" / "10" / "04" / f"rollout-2026-10-04T07-00-00-{XID}.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": "/mnt\x2fc/old", "cli_version": "0.9.0"}}) + "\n", encoding="utf-8")
    (codex / "session_index.jsonl").write_text(json.dumps({"id": XID, "thread_name": "t"}) + "\n" + json.dumps({"id": "other"}) + "\n", encoding="utf-8")
    data.mkdir()
    (data / "deputies.json").write_text(json.dumps({"work": {"session_id": MID, "executors": {"p": {"session_id": CID}}, "last": {"session_id": "x"}}}), encoding="utf-8")
    (data / "runs.jsonl").write_text(json.dumps({"model": "gpt-6", "session_id": XID, "role": "work", "kind": "executor"}) + "\n" + "not json gpt\n", encoding="utf-8")
    return claude, codex, data


class SessionMap(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.claude, self.codex, self.data = build(self.root)
        self.rows = sm.build_map(self.claude, self.codex, self.data / "deputies.json", self.data / "runs.jsonl")
        self.dest = self.root / "export"

    def test_mapping_is_metadata_only(self):
        self.assertEqual(sm.enc(OLD), "C--old-user-proj-one")
        by = {r["session_id"]: r for r in self.rows}
        self.assertEqual(set(by), {MID, CID, XID})  # `last` pointers are not mapped
        self.assertTrue(by[MID]["missing"] and not by[CID].get("missing"))
        self.assertTrue(by[CID]["cwd"] == OLD and by[CID]["cli_version"] == "2.1.9" and by[CID]["sidecar_dir"])
        self.assertTrue(by[XID]["cwd"] == "/mnt\x2fc/old" and by[XID]["cli_version"] == "0.9.0")
        self.assertNotIn("SECRET TEXT", json.dumps(self.rows))

    def test_export_manifest_and_no_overwrite(self):
        man = sm.export(self.rows, self.dest, codex_home=self.codex)
        self.assertEqual(len(man), 2)
        self.assertEqual((self.dest / "codex" / "session_index.jsonl").read_text().count("\n"), 1)
        with self.assertRaises(SystemExit):
            sm.export(self.rows, self.dest, codex_home=self.codex)

    def test_restore_under_new_cwd_and_no_overwrite(self):
        sm.export(self.rows, self.dest, codex_home=self.codex)
        new = r"D:\new\place"
        claude2, codex2 = self.root / "claude2", self.root / "codex2"
        out = sm.restore(self.dest, new, claude_root=claude2, codex_home=codex2)
        self.assertTrue((claude2 / "D--new-place" / f"{CID}.jsonl").exists() and (claude2 / "D--new-place" / CID / "sub.txt").exists())
        self.assertTrue((codex2 / "sessions" / "2026" / "10" / "04" / f"rollout-2026-10-04T07-00-00-{XID}.jsonl").exists())
        self.assertEqual(len(out), 2)
        with self.assertRaises(SystemExit):
            sm.restore(self.dest, new, claude_root=claude2, codex_home=codex2)

    def test_hash_mismatch_stops_restore(self):
        man = sm.export(self.rows, self.dest, codex_home=self.codex)
        (self.dest / man[0]["path"]).write_text("tampered", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            sm.restore(self.dest, r"D:\new\place", claude_root=self.root / "c3", codex_home=self.root / "x3")
        self.assertIn("hash mismatch", str(cm.exception))


if __name__ == "__main__":
    unittest.main()