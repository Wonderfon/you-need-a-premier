import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import machine_watch as mw  # noqa: E402

T0 = datetime(2026, 11, 1, 1, 7).astimezone()
LLAMA = {"name": "llama-server.exe", "pid": 31000, "cmd": "llama-server -m qwen-9b.gguf -ngl 99 --port 8795"}


def row(n, top=(), dwm=8.6, gpu=3.0, idle=0.5, procs=(), mem=33.9, disk=2800.0, interval=600):
    return {"at": (T0 + timedelta(minutes=10 * n)).isoformat(timespec="seconds"), "interval_s": interval,
            "top": [{"name": a, "pid": b, "cpu": c} for a, b, c in top], "dwm": dwm, "system": 23.0, "wmiprvse": 16.9,
            "gpu": {"util": gpu, "mem_used_mib": 1900, "mem_total_mib": 8151, "temp": 46, "pstate": "P8"},
            "mem_free_gb": mem, "c_free_gb": disk, "idle_min": idle, "model_procs": list(procs)}


# 11-01 01:37 as measured in audits/2026-11-01-pc-visual-lag.md: iGoSwServer 98.6%, dwm 97%, backup watcher 35.9%
LAG = [row(n, top=[("iGoSwServer.exe", 10888, 98.6), ("python.exe", 67568, 35.9)], dwm=97.0) for n in range(3)]
# 01:50-01:57 local generation: GPU 92 % while the owner works, dwm 58.7 %
GPU_RUN = [row(n, gpu=92.0, dwm=58.7, procs=[LLAMA]) for n in range(3)]


# iGoSwServer.exe (pid 11692) CPU % per row from data/machine-health.jsonl, 11-07 22:33 to 11-08 04:16 (None = not in that row's top list).
# It wobbles between 40 % and 99 %, so "3 in a row >= 90 %" breaks again and again; the sustained rule must still report it.
REAL_IGO = [
    ("11-07 22:33", 98.3), ("11-07 22:39", None), ("11-07 22:40", None), ("11-07 22:43", 79.7), ("11-07 22:45", None),
    ("11-07 22:46", None), ("11-07 22:52", None), ("11-07 22:53", None), ("11-07 22:54", 93.8), ("11-07 22:57", None),
    ("11-07 22:58", None), ("11-07 23:04", 90.9), ("11-07 23:14", 88.3), ("11-07 23:25", 91.0), ("11-07 23:35", 92.9),
    ("11-07 23:45", 97.3), ("11-07 23:56", 98.3), ("11-07 23:57", None), ("11-07 23:58", None), ("11-08 00:06", 79.0),
    ("11-08 00:17", 90.9), ("11-08 00:17", None), ("11-08 00:18", None), ("11-08 00:22", None), ("11-08 00:23", None),
    ("11-08 00:27", 76.2), ("11-08 00:29", None), ("11-08 00:29", None), ("11-08 00:35", None), ("11-08 00:36", None),
    ("11-08 00:37", 98.6), ("11-08 00:41", None), ("11-08 00:42", None), ("11-08 00:43", None), ("11-08 00:44", None),
    ("11-08 00:48", 91.6), ("11-08 00:49", None), ("11-08 00:50", None), ("11-08 00:50", None), ("11-08 00:51", None),
    ("11-08 00:58", 39.3), ("11-08 00:59", None), ("11-08 01:00", None), ("11-08 01:05", None), ("11-08 01:06", None),
    ("11-08 01:09", 51.8), ("11-08 01:10", None), ("11-08 01:11", None), ("11-08 01:19", 68.4), ("11-08 01:20", None),
    ("11-08 01:21", None), ("11-08 01:23", None), ("11-08 01:24", None), ("11-08 01:24", None), ("11-08 01:25", None),
    ("11-08 01:29", 55.0), ("11-08 01:40", 85.9), ("11-08 01:44", None), ("11-08 01:45", None), ("11-08 01:50", 79.5),
    ("11-08 02:00", None), ("11-08 02:00", 80.1), ("11-08 02:01", None), ("11-08 02:09", None), ("11-08 02:10", None),
    ("11-08 02:11", 99.0), ("11-08 02:21", 98.6), ("11-08 02:31", 76.8), ("11-08 02:42", 97.4), ("11-08 02:42", None),
    ("11-08 02:43", None), ("11-08 02:52", 86.7), ("11-08 03:03", 98.6), ("11-08 03:13", 98.6), ("11-08 03:25", None),
    ("11-08 03:35", 88.3), ("11-08 03:45", 84.4), ("11-08 03:56", 78.5), ("11-08 04:06", 88.2), ("11-08 04:16", 98.5),
]


def real_rows():
    rows = []
    for stamp, cpu in REAL_IGO:
        at = datetime.fromisoformat(f"2026-{stamp.replace(' ', 'T')}:00-05:00")
        base = row(0, top=[("iGoSwServer.exe", 11692, cpu)] if cpu is not None else [])
        rows.append({**base, "at": at.isoformat(), "interval_s": 600})
    return rows
IDLE = [row(n, top=[("msedgewebview2.exe", 42776, 4.1)], dwm=8.6, gpu=3.0) for n in range(6)]


class Judge(unittest.TestCase):
    def kinds(self, rows, i=None):
        return {(a["kind"], a["key"]) for a in mw.anomalies_at(rows, len(rows) - 1 if i is None else i)}

    def test_replay_of_the_real_lag_morning_is_found(self):
        self.assertEqual(self.kinds(LAG), {("cpu", "iGoSwServer.exe#10888"), ("dwm", "dwm")})  # the 35.9 % backup watcher is not one
        found = self.kinds(GPU_RUN)
        self.assertIn(("gpu_input", "gpu"), found)
        self.assertIn(("dwm", "dwm"), found)
        detail = next(a["detail"] for a in mw.anomalies_at(GPU_RUN, 2) if a["kind"] == "gpu_input")
        self.assertIn("92%", detail)
        self.assertIn("llama-server.exe（pid 31000）", detail)
        self.assertIn("--port 8795", detail)

    def test_wobbling_76_to_99_percent_for_hours_is_reported_as_sustained(self):
        rows = real_rows()
        found = mw.episodes(rows, datetime.fromisoformat("2026-11-07T22:00:00-05:00"))
        igo = [e for e in found if e["key"] == "iGoSwServer.exe#11692"]
        self.assertEqual(len(igo), 1)  # one episode, not a string of short ones
        self.assertLessEqual(igo[0]["first"].strftime("%m-%d %H:%M"), "11-08 01:00")
        self.assertGreaterEqual(igo[0]["last"].strftime("%m-%d %H:%M"), "11-08 03:45")  # the rows after it are not in the fixture
        self.assertIn("平均", igo[0]["detail"])

    def test_average_rule_needs_a_full_hour_and_a_high_average(self):
        top = lambda cpu: [("iGoSwServer.exe", 1, cpu)]
        self.assertEqual(self.kinds([row(n, top=top(80.0)) for n in range(5)]), set())  # 5 samples: not an hour yet
        self.assertEqual(self.kinds([row(n, top=top(74.0)) for n in range(6)]), set())  # under the line
        self.assertEqual(self.kinds([row(n, top=top(76.0)) for n in range(6)]), {("cpu", "iGoSwServer.exe#1")})
        self.assertEqual(self.kinds([row(n, top=top(76.0 if n % 2 else 80.0), interval=600) for n in range(6)][:5] + [row(9, top=top(80.0))]), set())  # gap breaks the chain

    def test_normal_idle_is_not_reported(self):
        for i in range(len(IDLE)):
            self.assertEqual(mw.anomalies_at(IDLE, i), [])
        self.assertEqual(mw.report_part(self._file(IDLE), T0 + timedelta(hours=2)), "近 12 小时取样 6 次，无持续异常")

    def test_needs_three_consecutive_samples(self):
        self.assertEqual(self.kinds(LAG, 1), set())  # only two rows so far
        mixed = [LAG[0], row(1, top=[("iGoSwServer.exe", 10888, 12.0)], dwm=8.0), LAG[2]]
        self.assertEqual(self.kinds(mixed), set())
        gap = [row(0, top=[("iGoSwServer.exe", 10888, 98.0)]), row(1, top=[("iGoSwServer.exe", 10888, 98.0)]),
               row(5, top=[("iGoSwServer.exe", 10888, 98.0)])]  # 40 minutes after the second: not consecutive
        self.assertEqual(self.kinds(gap), set())

    def test_a_restarted_process_with_another_pid_does_not_chain(self):
        rows = [row(n, top=[("iGoSwServer.exe", 10888 + n, 98.0)]) for n in range(3)]
        self.assertEqual(self.kinds(rows), set())

    def test_high_gpu_only_counts_with_owner_input_and_a_local_model_process(self):
        self.assertNotIn(("gpu_input", "gpu"), self.kinds([row(0, gpu=92.0, idle=45.0, procs=[LLAMA])]))  # owner away
        self.assertNotIn(("gpu_input", "gpu"), self.kinds([row(0, gpu=92.0, idle=1.0)]))  # a game / browser, no model process
        self.assertNotIn(("gpu_input", "gpu"), self.kinds([row(0, gpu=60.0, idle=1.0, procs=[LLAMA])]))

    def test_low_memory_and_disk_need_two_samples(self):
        rows = [row(0, mem=3.0, disk=80.0)]
        self.assertEqual(self.kinds(rows), set())
        rows.append(row(1, mem=3.0, disk=80.0))
        self.assertEqual(self.kinds(rows), {("memory", "memory"), ("disk", "disk")})

    def test_first_sample_after_a_start_has_no_cpu_figures_and_never_alerts(self):
        first = row(0, dwm=None)
        first["interval_s"], first["top"] = None, []
        self.assertEqual(self.kinds([first, LAG[1], LAG[2]]), set())

    def test_an_empty_row_between_normal_rows_does_not_break_the_chain(self):
        empty = {"at": LAG[1]["at"], "interval_s": None, "top": []}  # what a test run once appended to the real file
        self.assertEqual(self.kinds([LAG[0], empty, LAG[1], LAG[2]]), self.kinds(LAG))
        self.assertIn(("cpu", "iGoSwServer.exe#10888"), self.kinds([LAG[0], LAG[1], empty, LAG[2]]))
        self.assertEqual(self.kinds([LAG[0], LAG[1], LAG[2], empty]), set())  # the empty row itself reports nothing

    def test_report_part_merges_episodes_and_says_when_there_is_no_data(self):
        rows = LAG + [row(3, top=[("iGoSwServer.exe", 10888, 98.6)], dwm=97.0)]
        text = mw.report_part(self._file(rows), T0 + timedelta(hours=1))
        self.assertTrue(text.startswith("近 12 小时持续异常："), text)
        self.assertIn("iGoSwServer.exe（pid 10888）", text)
        self.assertIn("dwm", text)
        self.assertIn("2 次取样", text)  # rows 2 and 3 satisfy the 3-in-a-row rule
        self.assertIn("没有机器取样记录", mw.report_part(self._file([]), T0))
        self.assertIn("没有机器取样记录", mw.report_part(self._file(IDLE), T0 + timedelta(hours=20)))  # all older than 12 h

    def test_report_part_names_the_end_date_when_the_episode_crosses_midnight(self):
        rows = [row(n, top=[("iGoSwServer.exe", 10888, 98.6)], dwm=97.0) for n in range(136, 151)]  # 11-01 23:47 to 11-02 01:17
        text = mw.report_part(self._file(rows), T0 + timedelta(hours=16), hours=24)
        self.assertIn("–11-02 01:07（13 次取样）", text)
        self.assertNotIn("约", text)  # under a day: no day count

    def test_report_part_adds_a_day_count_for_an_episode_longer_than_24_hours(self):
        rows = LAG + [row(n, top=[("iGoSwServer.exe", 10888, 98.6)], dwm=97.0) for n in range(432, 435)]  # 3 days later
        text = mw.report_part(self._file(rows), T0 + timedelta(days=4), hours=24 * 5)
        self.assertIn("11-01 01:27–11-04 ", text)  # end date is named
        self.assertIn("（约 3 天，", text)

    def test_dashboard_view_is_stale_when_the_sampler_stopped(self):
        path = self._file(LAG)
        self.assertEqual({a["kind"] for a in mw.current(path, T0 + timedelta(minutes=30))["anomalies"]}, {"cpu", "dwm"})
        self.assertEqual(mw.current(path, T0 + timedelta(hours=3))["state"], "stale")
        self.assertEqual(mw.current(self._file([]), T0)["state"], "missing")

    def test_dashboard_view_still_shows_the_anomaly_when_an_empty_row_follows(self):
        empty = {"at": (T0 + timedelta(minutes=21)).isoformat(timespec="seconds"), "interval_s": None, "top": []}  # the 10:28:39 case
        view = mw.current(self._file(LAG + [empty]), T0 + timedelta(minutes=25))
        self.assertEqual(view["state"], "ok")
        self.assertTrue(any(a["kind"] == "cpu" and "iGoSwServer.exe（pid 10888）" in a["detail"] for a in view["anomalies"]), view)

    def test_dashboard_view_drops_an_anomaly_whose_last_reading_is_older_than_the_limit(self):
        empty = {"at": (T0 + timedelta(minutes=50)).isoformat(timespec="seconds"), "interval_s": None, "top": []}
        view = mw.current(self._file(LAG + [empty]), T0 + timedelta(minutes=51))
        self.assertEqual((view["state"], view["anomalies"]), ("ok", []))

    def test_discord_alert_is_a_function_only_and_does_not_repeat(self):
        path, state = self._file(LAG), Path(self.tmp) / "alerts.json"
        sent = []
        self.assertTrue(mw.alert_if_new(lambda text: sent.append(text) or True, path, state, T0 + timedelta(minutes=30)))
        self.assertIn("iGoSwServer.exe", sent[0])
        self.assertFalse(mw.alert_if_new(lambda text: sent.append(text) or True, path, state, T0 + timedelta(hours=1)))
        self.assertTrue(mw.alert_if_new(lambda text: sent.append(text) or True, path, state, T0 + timedelta(hours=7)))
        self.assertEqual(len(sent), 2)
        self.assertFalse(mw.alert_if_new(lambda text: False, path, Path(self.tmp) / "fresh.json", T0))  # failed post: nothing recorded
        self.assertFalse((Path(self.tmp) / "fresh.json").exists())

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self.tmpdir.name
        self.addCleanup(self.tmpdir.cleanup)

    def _file(self, rows):
        path = Path(self.tmp) / "machine-health.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows) + ("not json\n" if rows else ""), encoding="utf-8")
        return path


class Sampling(unittest.TestCase):
    def test_parse_gpu_takes_the_busiest_card(self):
        got = mw.parse_gpu("3, 1938, 8151, 46, P8\n92, 7400, 8151, 71, P2\nbad line\n")
        self.assertEqual((got["util"], got["pstate"], got["mem_used_mib"]), (92.0, "P2", 7400.0))
        self.assertIsNone(mw.parse_gpu("N/A\n"))

    def test_step_samples_once_per_interval_and_appends_a_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, state, calls = Path(tmp) / "data" / "machine-health.jsonl", {}, []

            def fake(prev, now=None):
                calls.append(prev)
                return {"at": "2026-11-01T01:00:00-04:00", "n": len(calls)}, {"wall": 1}

            real, mw.sample = mw.sample, fake
            try:
                self.assertIsNotNone(mw.step(state, 1000.0, path))
                self.assertIsNone(mw.step(state, 1000.0 + mw.SAMPLE_EVERY_S - 1, path))
                self.assertIsNotNone(mw.step(state, 1000.0 + mw.SAMPLE_EVERY_S, path))
            finally:
                mw.sample = real
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)
            self.assertEqual(calls, [None, {"wall": 1}])  # the second call gets the first call's process table

    def test_trim_keeps_the_newest_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.jsonl"
            old = (mw.MAX_BYTES, mw.KEEP_ROWS)
            mw.MAX_BYTES, mw.KEEP_ROWS = 300, 2
            try:
                for n in range(8):
                    mw.append_row(path, {"at": f"2026-11-01T01:0{n}:00-04:00", "pad": "x" * 60})
            finally:
                mw.MAX_BYTES, mw.KEEP_ROWS = old
            self.assertLessEqual(len(mw.read_rows(path)), 5)
            self.assertEqual(mw.read_rows(path)[-1]["at"], "2026-11-01T01:07:00-04:00")

    @unittest.skipUnless(sys.platform == "win32", "Windows process table")
    def test_real_two_sample_probe_is_fast_and_has_the_fields(self):
        first, state = mw.sample(None)
        time.sleep(1)
        second, _ = mw.sample(state)
        self.assertLess(second["sample_ms"], 1000)
        self.assertIsNone(first["interval_s"])
        self.assertGreaterEqual(len(second["top"]), 1)
        self.assertIsNotNone(second["dwm"])  # dwm.exe is only readable through the performance counters, not GetProcessTimes
        self.assertIsNotNone(second["system"])
        for key in ("dwm", "gpu", "mem_free_gb", "c_free_gb", "idle_min", "model_procs"):
            self.assertIn(key, second)


if __name__ == "__main__":
    unittest.main()
