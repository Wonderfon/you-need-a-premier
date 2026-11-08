import http.server
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import health_check as h

BATTERY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<BatteryReport xmlns="http://schemas.microsoft.com/battery/2012"><Batteries><Battery><Id>X</Id>
<DesignCapacity>94618</DesignCapacity><FullChargeCapacity>80000</FullChargeCapacity><CycleCount>7</CycleCount>
</Battery></Batteries></BatteryReport>"""

NOW = datetime(2026, 10, 6, 12, 0)


def snap(resets=0, ports=0, kp=0, free_gb=3000, priv=100 * h.MB):
    ev = {k: {"count": n, "times": [], "by_id": {}} for k, n in
          (("storage_reset", resets), ("port_exhaustion", ports), ("whea", 5), ("kernel_power_41", kp))}
    return {"ts": NOW.isoformat(timespec="seconds"), "events": ev,
            "storage": {"c_free": free_gb * h.GB, "c_total": 4000 * h.GB, "sizes": {"wt": 10 * h.GB}},
            "processes": {"input": {"pids": [1], "priv": priv, "handles": 100}, "asr": None},
            "top_tcp": [{"pid": 1, "name": "a.exe", "n": 3}], "battery": h.parse_battery(BATTERY_XML)}


class HealthCheck(unittest.TestCase):
    def test_battery_parse(self):
        self.assertEqual(h.parse_battery(BATTERY_XML), {"DesignCapacity": 94618, "FullChargeCapacity": 80000, "CycleCount": 7})

    def test_all_quiet_has_no_red(self):
        text, red = h.render(snap(), snap(), NOW)
        self.assertEqual(red, [])
        self.assertNotIn("🔴 ", text.split("\n", 3)[3])  # legend line mentions the marker

    def test_old_headless_browser_main_process_is_reported_once(self):
        table = {
            42: {"pid": 42, "name": "msedge.exe", "command": "msedge.exe --headless=new", "age_s": 7201},
            43: {"pid": 43, "name": "msedge.exe", "command": "msedge.exe --type=gpu-process --headless=new", "age_s": 100000},
            "_debug_listeners": [],
        }
        alerts = h.headless_browser_alerts(table)
        self.assertEqual(len(alerts), 1)
        text, red = h.render({**snap(), "headless_browser_alerts": alerts}, snap(), NOW)
        self.assertEqual(len(red), 1)
        self.assertIn("PID 42", red[0])
        self.assertIn("只报告，不结束进程", text)

    def test_headless_browser_age_must_exceed_two_hours(self):
        table = {42: {"pid": 42, "name": "chrome.exe", "command": "chrome.exe --headless", "age_s": 7200}}
        self.assertEqual(h.headless_browser_alerts(table), [])
        table[42]["age_s"] += 1
        self.assertEqual(len(h.headless_browser_alerts(table)), 1)

    def test_public_debug_listener_is_reported_but_loopback_is_not(self):
        raw = {"procs": [{"pid": 42, "name": "chrome.exe", "command": "chrome.exe --headless --remote-debugging-port=9333", "age_s": 10}],
               "debug_listeners": [{"pid": 42, "address": "0.0.0.0", "port": 9333}]}
        with patch.object(h, "ps_json", return_value=raw):
            table = h.process_table()
        self.assertIn("0.0.0.0", h.headless_browser_alerts(table)[0])
        table["_debug_listeners"][0]["address"] = "127.0.0.1"
        self.assertEqual(h.headless_browser_alerts(table), [])

    @unittest.skipUnless(sys.platform == "win32", "needs Windows PowerShell")
    def test_process_table_with_non_ascii_command_line_parses(self):
        # a real PowerShell prints these in the console code page unless ps_json forces UTF-8 (the first Windows run crashed on 0xf3)
        got = h.ps_json("@{command=([string][char]0x4e2d + [char]0xf3 + [char]0x20ac)} | ConvertTo-Json -Compress")
        self.assertEqual(got, {"command": "中ó€"})

    def test_public_listener_must_match_the_browser_debug_port(self):
        table = {42: {"pid": 42, "name": "msedge.exe", "command": "msedge.exe --headless --remote-debugging-port=9333", "age_s": 1},
                 "_debug_listeners": [{"pid": 43, "address": "0.0.0.0", "port": 9333}]}
        self.assertEqual(h.headless_browser_alerts(table), [])
        table["_debug_listeners"][0] = {"pid": 42, "address": "0.0.0.0", "port": 9444}
        self.assertEqual(h.headless_browser_alerts(table), [])

    def test_each_threshold_goes_red_at_its_limit_only(self):
        for kwargs, hit in [({"resets": 3}, "iaStorVD"), ({"resets": 2}, None), ({"ports": 3}, "端口"), ({"ports": 2}, None),
                            ({"kp": 1}, "Kernel"), ({"free_gb": 199}, "C:"), ({"free_gb": 200}, None)]:
            _, red = h.render(snap(**kwargs), snap(), NOW)
            self.assertEqual(bool(red), hit is not None, kwargs)
            if hit:
                self.assertIn(hit, red[0])

    def test_memory_growth_needs_a_baseline_and_50_percent(self):
        self.assertEqual(h.render(snap(priv=150 * h.MB), snap(), NOW)[1][0][:8], "input 内存")
        self.assertEqual(h.render(snap(priv=149 * h.MB), snap(), NOW)[1], [])
        self.assertEqual(h.render(snap(priv=900 * h.MB), None, NOW)[1], [])

    def test_website_is_red_unless_http_200(self):
        url = h.WEBSITE_HEALTH_URL
        for site, red in [({"url": url, "status": 200}, False), ({"url": url, "status": 503}, True),
                          ({"url": url, "status": 301}, True), ({"url": url, "status": None, "error": "timed out"}, True)]:
            text, found = h.render({**snap(), "website": site}, snap(), NOW)
            self.assertEqual(bool(found), red, site)
            self.assertIn("官网在线", text)
            if red:
                self.assertIn("官网", found[0])
        self.assertNotIn("官网在线", h.render(snap(), snap(), NOW)[0])  # snapshots from before this item still render

    def test_collect_website_reads_the_status_without_following_redirects(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                code = int(self.path.strip("/"))
                self.send_response(code)
                self.send_header("Location", "/200")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_port}/"
            self.assertEqual([h.collect_website(base + c)["status"] for c in ("200", "503", "301")], [200, 503, 301])
        finally:
            server.shutdown()
            server.server_close()
        gone = h.collect_website(base + "200")  # nothing listens any more
        self.assertIsNone(gone["status"])
        self.assertTrue(gone["error"])

    def test_tcp_states_are_counted_and_shown_with_the_total(self):
        table = {"_states": [{"state": "TimeWait", "n": 30}, {"state": "Established", "n": 120}, {"state": "Listen", "n": 2}]}
        states = h.tcp_states(table)
        self.assertEqual(list(states.items()), [("Established", 120), ("TimeWait", 30), ("Listen", 2)])
        text, _ = h.render({**snap(), "tcp_states": states}, {**snap(), "tcp_states": {"TimeWait": 5}}, NOW)
        self.assertIn("按状态：Established 120，TimeWait 30，Listen 2，合计 152", text)
        self.assertIn("上周：TimeWait 5，合计 5", text)
        self.assertNotIn("按状态", h.render(snap(), None, NOW)[0])  # snapshots from before this item still render

    def test_wt_top_dirs_lists_the_biggest_five_with_growth(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, n in [("a", 1), ("b", 6), ("c", 3), ("d", 5), ("e", 2), ("f", 4), ("g", 7)]:
                (Path(tmp) / name).mkdir()
                (Path(tmp) / name / "x").write_bytes(b"x" * n)
            (Path(tmp) / "loose.txt").write_bytes(b"x" * 99)  # files are not subdirectories
            with patch.object(h, "WT", Path(tmp)):
                top = h.wt_top_dirs()
        self.assertEqual([(r["name"], r["size"]) for r in top], [("g", 7), ("b", 6), ("d", 5), ("f", 4), ("c", 3)])
        mk = lambda rows: {**snap(), "storage": {**snap()["storage"], "wt_top": rows}}
        text, _ = h.render(mk(top), mk([{"name": "g", "size": 7 - h.GB}]), NOW)
        self.assertIn("wt 最大的 5 个子目录", text)
        self.assertIn("| g |", text)
        self.assertIn("| b |", text)

    def test_wsl_tmp_goes_red_at_80_percent_only(self):
        df = "Filesystem 1024-blocks Used Available Capacity Mounted on\ntmpfs 16777216 {used} 1 {pct}% /tmp\n"
        self.assertEqual(h.parse_df(df.format(used=13421773, pct=80)), {"size": 16777216 * 1024, "used": 13421773 * 1024, "pct": 80})
        self.assertIsNone(h.parse_df("garbage"))
        for pct, red in [(79, False), (80, True), (100, True)]:
            st = {**snap()["storage"], "wsl_tmp": {"size": 16 * h.GB, "used": pct * h.GB // 100, "pct": pct}}
            text, found = h.render({**snap(), "storage": st}, snap(), NOW)
            self.assertEqual(bool(found), red, pct)
            self.assertIn("WSL /tmp", text)
            if red:
                self.assertIn("WSL /tmp", found[0])

    def test_baseline_picks_snapshot_nearest_seven_days(self):
        old = lambda d: {"ts": (NOW - timedelta(days=d)).isoformat()}
        self.assertEqual(h.find_baseline([old(1), old(6), old(9), old(20)], NOW), old(6))
        self.assertIsNone(h.find_baseline([old(1), old(2), old(20)], NOW))

    def test_roles_use_listener_and_descendants_not_the_stale_pid_file(self):
        def proc(pid, ppid, name="python.exe"):
            return {"pid": pid, "ppid": ppid, "name": name, "priv": h.MB, "handles": 10}
        table = {p["pid"]: p for p in (proc(10, 1), proc(11, 10), proc(12, 11), proc(13, 12, "conhost.exe"), proc(20, 1, "other.exe"))}
        table["_listener"], table["_tcp"] = [11], []
        roles = h.role_pids(table)
        self.assertEqual(roles["input"], [10, 11])
        self.assertEqual(roles["asr"], [12])


if __name__ == "__main__":
    unittest.main()
