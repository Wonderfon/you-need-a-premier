from __future__ import annotations

import json
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_traffic import summarize


# Thirty hand-written Caddy-shaped JSON access records. All IPs use TEST-NET ranges.
LOGS = """\
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/","remote_ip":"198.51.100.1","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"],"Referer":["https://www.google.com/search?q=one"]}},"status":200,"resp_headers":{"Content-Type":["text/html; charset=utf-8"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/","remote_ip":"198.51.100.1","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"],"Referer":["https://www.google.com/search?q=two"]}},"status":200,"resp_headers":{"Content-Type":["text/html; charset=utf-8"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/blog/","remote_ip":"198.51.100.2","headers":{"User-Agent":["Mozilla/5.0 Firefox/120"],"Referer":["https://example.org/posts/demo?utm_source=test"]}},"status":200,"resp_headers":{"Content-Type":["text/html; charset=utf-8"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/api/data","remote_ip":"198.51.100.1","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":200,"resp_headers":{"Content-Type":["application/json"]}}
{"ts":1792598400,"request":{"method":"POST","host":"arbitros.lab.beaclis.com","uri":"/api/contact","remote_ip":"198.51.100.3","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":201,"resp_headers":{"Content-Type":["application/json"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/not-found","remote_ip":"198.51.100.4","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":404,"resp_headers":{"Content-Type":["text/html"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/assets/site.css","remote_ip":"198.51.100.1","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":200,"resp_headers":{"Content-Type":["text/css"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/u/s.js","remote_ip":"198.51.100.1","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":200,"resp_headers":{"Content-Type":["application/javascript"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/","remote_ip":"198.51.100.5","headers":{"User-Agent":["Googlebot/2.1"],"Referer":["https://www.google.com/"]}},"status":200,"resp_headers":{"Content-Type":["text/html"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/","remote_ip":"198.51.100.6","headers":{"User-Agent":["incore-dashboard/1"]}},"status":200,"resp_headers":{"Content-Type":["text/html"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/healthz","remote_ip":"198.51.100.6","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":200,"resp_headers":{"Content-Type":["application/json"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/image.png","remote_ip":"198.51.100.7","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":200,"resp_headers":{"Content-Type":["image/png"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/about/","remote_ip":"198.51.100.1","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"],"Referer":["https://arbitros.lab.beaclis.com/"]}},"status":200,"resp_headers":{"Content-Type":["text/html"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/welcome","remote_ip":"198.51.100.1","headers":{"User-Agent":["Mozilla/5.0 Firefox/120"],"Referer":["https://news.example/launch"]}},"status":200,"resp_headers":{"Content-Type":["text/html"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/error","remote_ip":"198.51.100.8","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":500,"resp_headers":{"Content-Type":["text/html"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/","remote_ip":"198.51.100.9","headers":{"User-Agent":["python-requests/2.31"]}},"status":200,"resp_headers":{"Content-Type":["text/html"]}}
{"ts":1792598400,"request":{"method":"GET","host":"arbitros.lab.beaclis.com","uri":"/old","remote_ip":"198.51.100.10","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":302,"resp_headers":{"Content-Type":["text/html"]}}
{"ts":1792598400,"request":{"method":"GET","host":"drive.lab.beaclis.com","uri":"/","remote_ip":"198.51.100.1","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"],"Referer":["https://www.google.com/search?q=drive"]}},"status":200,"resp_headers":{"Content-Type":["text/html; charset=utf-8"]}}
{"ts":1792598400,"request":{"method":"GET","host":"drive.lab.beaclis.com","uri":"/simulator","remote_ip":"198.51.100.2","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"],"Referer":["https://www.google.com/search?q=simulator"]}},"status":200,"resp_headers":{"Content-Type":["text/html; charset=utf-8"]}}
{"ts":1792598400,"request":{"method":"GET","host":"drive.lab.beaclis.com","uri":"/api/state","remote_ip":"198.51.100.1","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":200,"resp_headers":{"Content-Type":["application/json"]}}
{"ts":1792598400,"request":{"method":"GET","host":"drive.lab.beaclis.com","uri":"/assets/app.js","remote_ip":"198.51.100.1","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":200,"resp_headers":{"Content-Type":["application/javascript"]}}
{"ts":1792598400,"request":{"method":"GET","host":"drive.lab.beaclis.com","uri":"/","remote_ip":"198.51.100.3","headers":{"User-Agent":["Googlebot/2.1"]}},"status":200,"resp_headers":{"Content-Type":["text/html"]}}
{"ts":1792598400,"request":{"method":"GET","host":"drive.lab.beaclis.com","uri":"/","remote_ip":"198.51.100.4","headers":{"User-Agent":["incore-dashboard/1"]}},"status":200,"resp_headers":{"Content-Type":["text/html"]}}
{"ts":1792598400,"request":{"method":"GET","host":"drive.lab.beaclis.com","uri":"/health","remote_ip":"198.51.100.5","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":200,"resp_headers":{"Content-Type":["application/json"]}}
{"ts":1792598400,"request":{"method":"GET","host":"drive.lab.beaclis.com","uri":"/about","remote_ip":"198.51.100.6","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":200,"resp_headers":{"Content-Type":["text/html"]}}
{"ts":1792598400,"request":{"method":"DELETE","host":"drive.lab.beaclis.com","uri":"/api/session","remote_ip":"198.51.100.1","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":204,"resp_headers":{}}
{"ts":1792598400,"request":{"method":"GET","host":"drive.lab.beaclis.com","uri":"/school-guide","remote_ip":"198.51.100.7","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"],"Referer":["https://social.example/community?campaign=demo"]}},"status":200,"resp_headers":{"Content-Type":["text/html"]}}
{"ts":1792598400,"request":{"method":"GET","host":"drive.lab.beaclis.com","uri":"/fonts/site.woff2","remote_ip":"198.51.100.1","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":200,"resp_headers":{"Content-Type":["font/woff2"]}}
{"ts":1792598400,"request":{"method":"GET","host":"drive.lab.beaclis.com","uri":"/robots.txt","remote_ip":"198.51.100.9","headers":{"User-Agent":["Mozilla/5.0 Chrome/120"]}},"status":200,"resp_headers":{"Content-Type":["text/plain"]}}
{"ts":1792598400,"request":{"method":"GET","host":"drive.lab.beaclis.com","uri":"/home","remote_ip":"198.51.100.8","headers":{"User-Agent":["curl/8.0"]}},"status":200,"resp_headers":{"Content-Type":["text/html"]}}
"""


class DemoTrafficTests(unittest.TestCase):
    def test_thirty_records_have_expected_site_day_counts(self):
        self.assertEqual(len(LOGS.splitlines()), 30)
        report = summarize(LOGS.splitlines())
        day = datetime.fromtimestamp(1792598400).date().isoformat()
        self.assertEqual(report, [
            {
                "site": "arbitros", "date": day, "requests": 10, "page_views": 5,
                "unique_visitors": 3,
                "top_referrers": [
                    {"page": "https://www.google.com/search", "visits": 2},
                    {"page": "https://example.org/posts/demo", "visits": 1},
                    {"page": "https://news.example/launch", "visits": 1},
                ],
            },
            {
                "site": "drive", "date": day, "requests": 6, "page_views": 4,
                "unique_visitors": 4,
                "top_referrers": [
                    {"page": "https://www.google.com/search", "visits": 2},
                    {"page": "https://social.example/community", "visits": 1},
                ],
            },
        ])
        self.assertNotIn("198.51.100.", json.dumps(report))

    def test_owner_stdin_command_shape_runs_the_cli(self):
        script = Path(__file__).with_name("demo_traffic.py")
        done = subprocess.run(
            [sys.executable, str(script)], input=LOGS, text=True, encoding="utf-8",
            capture_output=True, check=True,
        )
        report = json.loads(done.stdout)
        self.assertEqual([(row["site"], row["requests"]) for row in report], [("arbitros", 10), ("drive", 6)])
        self.assertNotIn("198.51.100.", done.stdout)


if __name__ == "__main__":
    unittest.main()
