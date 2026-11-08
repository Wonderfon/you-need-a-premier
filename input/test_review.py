from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from . import review
from .server import IDENTITY_HEADER, create_app

LOCAL = "127.0.0.1:8765"
PRIVATE_HOST = "owner-box.tail1234.ts.net"
OWNER = "owner@example.com"
TODAY = datetime.now().astimezone().date()
STAMP = TODAY.isoformat()
MMDD = TODAY.strftime("%m-%d")
OLD = TODAY - timedelta(days=20)
PASSWORD = "hunter2-placeholder-pw"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def put(root: Path, rel: str, data: bytes | str = b"x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def make_tree(root: Path) -> None:
    put(root, f"audits/{STAMP}-report.md", "# 今日报告\n\n正文 **重点**，见 [图](shots/01.png)\n")
    put(root, f"audits/{STAMP}-page.html", "<html><head><title>短标题</title><style>p{color:red}</style></head><body><h1>页面标题</h1>"
                                          "<img src='shots/01.png'><script>alert(1)</script></body></html>")
    put(root, f"audits/{STAMP}-page.md", "# 页面标题 md\n")           # md source of the page above: listed once
    put(root, f"audits/{OLD.isoformat()}-old.md", "# 太旧\n")
    put(root, "audits/notes.md", "# 没有日期\n")
    put(root, "audits/shots/01.png", PNG)
    put(root, "audits/shots/two words #1.png", PNG)
    put(root, "audits/shots/round2/a.jpg", PNG)
    put(root, "audits/shots/run.exe", b"MZ")
    put(root, "audits/shots/script.py", "print(1)")
    put(root, "audits/docs-only/readme.md", "# no images\n")
    put(root, "records/secret.json", '{"token": "SECRET"}')
    put(root, "data/.env", "KEY=SECRET")
    put(root, "audits/big.png", PNG + b"\0" * (5 * 1024 * 1024))
    put(root, "projects/backlog.md", f"""## 工作线
- [done {MMDD} 19:5x：做完；启动 `wsl -e bash /mnt\x2fc/x/product.sh`，看 https://demo.example.com/a.；audits/{STAMP}-report.md 和 audits/shots/；等 owner 试用] FOS：产品 demo（带括号说明）
- [done {MMDD}：合并完；等 owner 合并并重启] 官网：只等合并
- [done {MMDD}：等你看了再排；`Start-Process 'C:\\orghome\\incore\\audits\\shots\\01.png'`；audits/../records/secret.json] 求职：追踪表
- [done {MMDD}：原状态：等你试用，已处理] 组织：历史说明
- [done 16:59；owner 已处理 10-21；给 owner {MMDD}：旧评价] Thinking Engines：已处理
- [done 17:00；owner 已答 10-21；给 owner {MMDD}：旧答复] Thinking Engines：已答
- [done {OLD.strftime('%m-%d')}：等 owner 试用] 组织：太旧
- [todo] 组织：等 owner 试用 还没做完
- [done 16:56；给 owner {MMDD}：勾 48 条，约 10 分钟，audits/{STAMP}-page.html] 写作模型：盲读页
- [done 16:57；给 owner {OLD.strftime('%m-%d')}：太旧的标记] 写作模型：旧标记
- [done 16:58：验收通过，没有标给谁] 写作模型：没日期也没标记
""")
    put(root, "projects/deployments.json", json.dumps({"products": [
        {"name": "公开站", "url": "https://a.example.com/", "access": "public", "commit": "abcdef1234567", "deployed_at": f"{STAMP}T15:40:20-04:00"},
        {"name": "带密码", "url": "https://b.example.com/", "access": "password", "password": PASSWORD, "commit": None, "deployed_at": "2026-10-04"},
        {"name": "坏链接", "url": "javascript:alert(1)", "access": "public"},
    ]}))


def tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        h.update(str(path.relative_to(root)).encode())
        if path.is_file():
            h.update(path.read_bytes())
    return h.hexdigest()


class AuditPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "inc"
        make_tree(self.root)

    def test_allowed_files(self) -> None:
        self.assertTrue(review.audit_path(self.root, f"{STAMP}-report.md"))
        self.assertTrue(review.audit_path(self.root, "shots/two words #1.png"))
        self.assertTrue(review.audit_path(self.root, "shots", want_dir=True))
        self.assertIsNone(review.audit_path(self.root, "shots"))  # a folder is not a file

    def test_rejected_names(self) -> None:
        for rel in ["", "../records/secret.json", "shots/../../records/secret.json", "..", ".", "/records/secret.json",
                    "//server/share/x.md", "C:/Windows/win.ini", "C:\\Windows\\win.ini", "shots\\01.png",
                    f"{STAMP}-report.md::$DATA", f"{STAMP}-report.md.", f"{STAMP}-report.md ", "nul.md", "NUL", "aux.txt",
                    "shots//01.png", "shots/", "shots/run.exe", "shots/script.py", "missing.md", "\x00.md",
                    "../data/.env", "..%2f..%2frecords%2fsecret.json"]:
            self.assertIsNone(review.audit_path(self.root, rel), rel)

    def test_links_leaving_audits_are_refused(self) -> None:
        outside = Path(self.tmp.name) / "outside"
        put(outside, "leak.md", "# leak")
        put(outside, "leak.png", PNG)
        made = []
        if os.name == "nt":  # a junction needs no privilege
            done = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.root / "audits" / "junc"), str(outside)], capture_output=True)
            made.append(done.returncode == 0)
        try:
            os.symlink(outside / "leak.md", self.root / "audits" / "link.md")
            os.symlink(outside, self.root / "audits" / "linkdir", target_is_directory=True)
            made.append(True)
        except OSError:
            pass  # symlinks need privilege on Windows
        if not any(made):
            self.skipTest("cannot create a junction or symlink here")
        for rel in ["junc/leak.md", "junc/leak.png", "link.md", "linkdir/leak.png"]:
            self.assertIsNone(review.audit_path(self.root, rel), rel)
        for rel in ["junc", "linkdir"]:
            self.assertIsNone(review.audit_path(self.root, rel, want_dir=True), rel)
        self.assertNotIn("junc", [g["name"] for g in review.galleries(self.root, TODAY)])
        if os.name == "nt":
            os.rmdir(self.root / "audits" / "junc")  # remove the junction, never its target


class DataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "inc"
        make_tree(self.root)

    def test_reports_last_seven_days_one_entry_per_page(self) -> None:
        found = review.reports(self.root, TODAY)
        self.assertEqual(sorted(r["name"] for r in found), [f"{STAMP}-page.html", f"{STAMP}-report.md"])
        page = next(r for r in found if r["name"].endswith(".html"))
        self.assertEqual((page["title"], page["md"]), ("页面标题", f"{STAMP}-page.md"))  # h1 beats the short <title>
        self.assertEqual(next(r for r in found if r["name"].endswith(".md"))["title"], "今日报告")
        put(self.root, f"audits/{STAMP}-no-heading.md", "just text")
        self.assertIn(f"{STAMP}-no-heading", [r["title"] for r in review.reports(self.root, TODAY)])  # filename fallback
        self.assertEqual(len(review.reports(self.root, TODAY + timedelta(days=30))), 0)

    def test_galleries_need_images_and_recency(self) -> None:
        self.assertEqual([(g["name"], g["images"]) for g in review.galleries(self.root, TODAY)], [("shots", 3)])

    def test_trials_patterns(self) -> None:
        found = review.trials(self.root, TODAY)
        self.assertEqual([t["project"] for t in found], ["求职", "写作模型", "FOS"])  # date first, stable content for same-day cards across files
        tagged = next(t for t in found if t["project"] == "写作模型")
        self.assertEqual((tagged["title"], tagged["day"]), ("盲读页", TODAY))  # day from the 给 owner tag when the stamp has none
        self.assertEqual(tagged["summary"], "勾 48 条，约 10 分钟")  # only what he is asked to do, no reviewer notes or path
        self.assertEqual([k["href"] for k in tagged["links"]], [f"/review/a/{STAMP}-page.html"])
        fos = next(t for t in found if t["project"] == "FOS")
        self.assertEqual(fos["title"], "产品 demo")
        self.assertEqual(fos["commands"], [])
        self.assertEqual(fos["urls"], ["https://demo.example.com/a"])
        self.assertEqual([k["href"] for k in fos["links"]], [f"/review/a/{STAMP}-report.md"])
        jobs = next(t for t in found if t["project"] == "求职")
        self.assertEqual((jobs["summary"], jobs["commands"]), ("", []))
        self.assertEqual(jobs["links"], [{"label": "audits/shots/01.png", "href": "/review/a/shots/01.png"}])  # '..' path dropped
        self.assertFalse(any("已处理" in item["title"] or "已答" in item["title"] for item in review.trials(self.root, TODAY)))
        for phrase in ["等 owner 试用", "等你试用", "等 owner 看", "交 owner 看", "待 owner 听", "owner 看过再定", "给 owner：读", "给 owner\x2010-21：勾"]:
            self.assertTrue(review.WAITING.search(phrase), phrase)
        for phrase in ["等 owner 合并", "等你决定", "owner\x2010-13 看过后选 A"]:
            self.assertFalse(review.WAITING.search(phrase), phrase)

    def test_deployments_never_carry_passwords(self) -> None:
        found = review.deployments(self.root)
        self.assertEqual(sorted(p["name"] for p in found), ["公开站", "带密码"])  # the javascript: url is not a deployment
        self.assertNotIn(PASSWORD, json.dumps(found, ensure_ascii=False))
        site = next(p for p in found if p["name"] == "公开站")
        self.assertEqual((site["at"], site["password"], site["commit"]), (f"{STAMP} 15:40", False, "abcdef123456"))
        self.assertTrue(next(p for p in found if p["name"] == "带密码")["password"])

    def test_archived_trial_keeps_master_fields_and_dismissal(self) -> None:
        day = TODAY - timedelta(days=7)
        row = (f"- [done {day:%m-%d}；给 owner {day:%m-%d}：看页面，audits/{STAMP}-page.html] "
               "组织：边界卡\n")
        put(self.root, "projects/backlog.md", "## 组织线\n" + row)
        before = review.trials(self.root, TODAY)[0]
        put(self.root, "projects/backlog.md", "## 组织线\n")
        path = put(self.root, f"projects/backlog-archive/{day:%Y-%m}.md", "# 月归档\n## 组织线\n" + row)
        after = review.trials(self.root, TODAY)[0]
        without_source = lambda item: {k: v for k, v in item.items() if k not in ("source", "line", "n")}
        self.assertEqual(without_source(after), without_source(before))
        self.assertEqual((after["section"], after["tagged"]), ("组织线", True))
        self.assertEqual((after["source"], after["line"]), (Path(path).as_posix(), 3))
        self.assertTrue(review._set_dismissed(self.root, after["id"], "dismiss"))
        self.assertIn(after["id"], review._dismissed(self.root))
        self.assertTrue(review._set_dismissed(self.root, after["id"], "undo"))
        self.assertNotIn(after["id"], review._dismissed(self.root))

    def test_missing_sources_are_empty_not_errors(self) -> None:
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertEqual(review.counts(empty, TODAY), {"reports": 0, "trials": 0, "deployments": 0})
        self.assertEqual((review.reports(empty, TODAY), review.galleries(empty, TODAY)), ([], []))


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "inc"
        make_tree(self.root)

    async def client(self, **private: str) -> TestClient:
        app = create_app(data_root=Path(self.tmp.name) / "input-data", expected_port=8765, dashboard_root=self.root,
                         wt_root=self.root / "wt", **private)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_review_page_content(self) -> None:
        client = await self.client()
        before = tree_digest(self.root)
        page = await client.get("/review/all", headers={"Host": LOCAL})
        self.assertEqual(page.status, 200)
        html = await page.text()
        nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", page.headers["Content-Security-Policy"]).group(1)
        self.assertEqual(html.count(f'nonce="{nonce}"'), 2)  # the one style and the one script
        self.assertIn("script-src 'nonce-", page.headers["Content-Security-Policy"])
        for needle in ["给你的：要你读、勾或试", "盲读页", "其余报告", "产品 demo", "看过了", "已看过 0 条",
                       f'href="/review/a/{STAMP}-page.html"', "页面标题", "md 原文", 'href="/review/g/shots"', "3 张",
                       "公开站", "密码访问", 'href="https://a.example.com/"']:
            self.assertIn(needle, html)
        self.assertNotIn("等 owner 试用", html)
        self.assertNotIn("https://demo.example.com/a", html)
        self.assertNotIn("wsl -e bash /mnt\x2fc/x/product.sh", html)
        self.assertNotIn("太旧", html)
        self.assertNotIn(PASSWORD, html)
        self.assertNotIn("javascript:alert", html)
        self.assertNotIn("innerHTML", html)
        self.assertEqual(tree_digest(self.root), before)  # reading writes nothing

    async def test_dismiss_restore_and_post_trust_boundary(self) -> None:
        client = await self.client()
        page = await client.get("/review/all", headers={"Host": LOCAL})
        token = page.cookies[review.CSRF_COOKIE].value
        item_id = review.trials(self.root, TODAY)[0]["id"]
        path = "/api/review/dismiss"
        body = {"id": item_id, "action": "dismiss"}
        origin = {"Host": LOCAL, "Origin": f"http://{LOCAL}"}
        self.assertEqual((await client.post(path, json=body, headers=origin)).status, 403)
        self.assertEqual((await client.post(path, json=body,
                                            headers={"Host": "evil.example", "Origin": f"http://{LOCAL}",
                                                     "X-CSRF-Token": token})).status, 421)
        self.assertEqual((await client.post(path, json=body,
                                            headers={**origin, "Origin": "http://evil.example",
                                                     "X-CSRF-Token": token})).status, 403)
        self.assertEqual((await client.post(path, json={**body, "extra": "no"},
                                            headers={**origin, "X-CSRF-Token": token})).status, 400)
        self.assertEqual((await client.post(path, json={"id": item_id, "action": []},
                                            headers={**origin, "X-CSRF-Token": token})).status, 400)
        before = review.counts(self.root, TODAY)["trials"]
        saved = await client.post(path, json=body, headers={**origin, "X-CSRF-Token": token})
        self.assertEqual(saved.status, 200)
        page = await client.get("/review/all", headers={"Host": LOCAL})
        html = await page.text()
        self.assertIn(f'data-dismiss-id="{item_id}" data-action="restore"', html)
        jump_count = re.search(r'<a id="jump-trial"[^>]*>([^<]*)</a>', html).group(1)
        self.assertEqual(jump_count, f"给你的 {before - 1}")
        trial_count = re.search(r'<span id="trial-count">([^<]*)</span>', html).group(1)
        self.assertEqual(trial_count, f"{before - 1} 条待你试用")
        dismissed = json.loads((self.root / "data" / review._DISMISSED_FILE).read_text(encoding="utf-8"))
        self.assertEqual(set(dismissed), {item_id})
        self.assertRegex(dismissed[item_id], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        restored = await client.post(path, json={"id": item_id, "action": "restore"},
                                     headers={**origin, "X-CSRF-Token": page.cookies[review.CSRF_COOKIE].value})
        self.assertEqual(restored.status, 200)
        self.assertEqual(json.loads((self.root / "data" / review._DISMISSED_FILE).read_text(encoding="utf-8")), {})
        self.assertEqual(review.counts(self.root, TODAY)["trials"], before)
        html = await (await client.get("/review/all", headers={"Host": LOCAL})).text()
        self.assertIn(f'<span id="trial-count">{before} 条待你试用</span>', html)

    async def test_html_escaped(self) -> None:
        put(self.root, f"audits/{STAMP}-evil.md", "# <img src=x onerror=alert(1)>\n")
        html = await (await (await self.client()).get("/review/all", headers={"Host": LOCAL})).text()
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x", html)

    async def test_files_render_and_headers(self) -> None:
        client = await self.client()
        md = await client.get(f"/review/a/{STAMP}-report.md", headers={"Host": LOCAL})
        self.assertEqual((md.status, md.content_type), (200, "text/html"))
        body = await md.text()
        self.assertIn("<h1>今日报告</h1>", body)
        self.assertIn("<strong>重点</strong>", body)
        self.assertIn('href="/todo"', body)
        self.assertIn("script-src 'none'", md.headers["Content-Security-Policy"])
        page = await client.get(f"/review/a/{STAMP}-page.html", headers={"Host": LOCAL})
        self.assertEqual((page.status, page.content_type, page.charset), (200, "text/html", "utf-8"))
        self.assertIn("<script>alert(1)</script>", await page.text())  # runs only in the opaque-origin sandbox
        csp = page.headers["Content-Security-Policy"]
        self.assertEqual(csp, review.PAGE_CSP)
        self.assertNotIn("allow-same-origin", csp)
        self.assertEqual(page.headers["X-Content-Type-Options"], "nosniff")
        img = await client.get("/review/a/shots/two%20words%20%231.png", headers={"Host": LOCAL})
        self.assertEqual((img.status, img.content_type), (200, "image/png"))
        self.assertEqual(await img.read(), PNG)

    async def test_all_html_entry_points_redirect_to_the_read_only_sandbox(self) -> None:
        client = await self.client()
        key = client.server.app[review.PAGE_KEY]
        put(self.root, "audits/two words #1.HTML", "<button onclick='reply()'>Reply</button>")
        for name in [f"{STAMP}-page.html", "two words #1.HTML"]:
            url = f"/review/a/{review._u(name)}?mode=a%26b&mode=2"
            response = await client.get(url, headers={"Host": LOCAL}, allow_redirects=False)
            self.assertEqual(response.status, 302)
            self.assertEqual(response.headers["Location"], f"/review/s/{key}/{review._u(name)}?mode=a%26b&mode=2")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            page = await client.get(response.headers["Location"], headers={"Host": LOCAL})
            self.assertEqual((page.status, page.headers["Content-Security-Policy"]), (200, review.PAGE_CSP))

    async def test_large_files_stream_with_ranges(self) -> None:
        client = await self.client()
        part = await client.get("/review/a/big.png", headers={"Host": LOCAL, "Range": "bytes=0-9"})
        self.assertEqual((part.status, len(await part.read())), (206, 10))
        self.assertEqual(part.headers["Content-Range"].split("/")[1], str(len(PNG) + 5 * 1024 * 1024))

    async def test_oversized_markdown_is_streamed_as_text(self) -> None:
        put(self.root, f"audits/{STAMP}-huge.md", "# huge\n" + "x" * (review.MD_LIMIT + 1))
        response = await (await self.client()).get(f"/review/a/{STAMP}-huge.md", headers={"Host": LOCAL})
        self.assertEqual((response.status, response.content_type, response.charset), (200, "text/plain", "utf-8"))

    async def test_gallery(self) -> None:
        client = await self.client()
        page = await client.get("/review/g/shots", headers={"Host": LOCAL})
        html = await page.text()
        self.assertEqual(page.status, 200)
        for needle in ['src="/review/a/shots/01.png"', 'src="/review/a/shots/two%20words%20%231.png"', 'href="/review/g/shots/round2"', "01.png"]:
            self.assertIn(needle, html)
        self.assertNotIn("run.exe", html)
        sub = await client.get("/review/g/shots/round2", headers={"Host": LOCAL})
        self.assertIn('href="/review/g/shots"', await sub.text())
        self.assertEqual((await client.get("/review/g/docs-only", headers={"Host": LOCAL})).status, 200)  # empty folder: a page, not an error
        for path in ["/review/g/records", "/review/g/..%2frecords", "/review/g/nothing", f"/review/g/{STAMP}-report.md"]:
            self.assertEqual((await client.get(path, headers={"Host": LOCAL})).status, 404, path)

    async def test_traversal_and_forbidden_files_are_404(self) -> None:
        client = await self.client()
        for path in ["/review/a/../records/secret.json", "/review/a/..%2frecords%2fsecret.json", "/review/a/%2e%2e/records/secret.json",
                     "/review/a/%2e%2e%2frecords%2fsecret.json", "/review/a/..%5crecords%5csecret.json", "/review/a/shots%5c..%5c..%5crecords%5csecret.json",
                     "/review/a/%252e%252e%252frecords%252fsecret.json", "/review/a//records/secret.json", "/review/a/%2Frecords/secret.json",
                     "/review/a/C:/Windows/win.ini", "/review/a/C%3A%5CWindows%5Cwin.ini", "/review/a/%5C%5Cserver%5Cshare%5Cx.md",
                     "/review/a/..%2fdata%2f.env", "/review/a/shots/run.exe", "/review/a/shots/script.py", "/review/a/shots",
                     "/review/a/nul.md", f"/review/a/{STAMP}-report.md::$DATA", "/review/a/%00.md"]:
            response = await client.get(path, headers={"Host": LOCAL})
            self.assertEqual(response.status, 404, path)
            self.assertNotIn("SECRET", await response.text(), path)
        # the literal form is normalized by HTTP clients, so send it by hand
        host, port = client.server.host, client.server.port
        for target in ["/review/a/../records/secret.json", "/review/a/shots/../../records/secret.json"]:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(f"GET {target} HTTP/1.1\r\nHost: {LOCAL}\r\nConnection: close\r\n\r\n".encode())
            raw = await reader.read()
            writer.close()
            self.assertNotIn(b"SECRET", raw, target)
            self.assertTrue(raw.startswith((b"HTTP/1.1 404", b"HTTP/1.1 400")), raw[:40])

    async def test_sandboxed_pages_run_scripts_and_load_their_own_files_but_nothing_else(self) -> None:
        client = await self.client()
        key = client.server.app[review.PAGE_KEY]
        page = await client.get(f"/review/s/{key}/{STAMP}-page.html", headers={"Host": LOCAL})
        csp = page.headers["Content-Security-Policy"]
        self.assertTrue(page.status == 200 and csp.startswith("sandbox allow-scripts ") and "allow-same-origin" not in csp)
        img = await client.get(f"/review/s/{key}/shots/01.png", headers={"Host": LOCAL})
        self.assertEqual((img.status, img.headers["Content-Security-Policy"]), (200, review.FILE_CSP))
        sandbox = {"Host": LOCAL, "Sec-Fetch-Site": "cross-site", "Origin": "null"}  # what the sandboxed page's requests carry
        self.assertEqual((await client.get(f"/review/s/{key}/shots/01.png", headers=sandbox)).status, 200)
        for path in ["/api/pending", "/api/todo", "/api/sessions", "/pending", "/todo", "/review", "/review/all", "/review/a/shots/01.png",  # not its APIs
                     "/review/s/wrongkey/shots/01.png", f"/review/s/{key}x/shots/01.png"]:  # nor a guessed key
            self.assertIn((await client.get(path, headers=sandbox)).status, (403, 404), path)
        self.assertEqual((await client.get("/review/s/wrongkey/shots/01.png", headers={"Host": LOCAL})).status, 404)
        self.assertEqual((await client.post(f"/review/s/{key}/shots/01.png", headers=sandbox)).status, 403)
        self.assertEqual((await client.get(f"/review/s/{key}/..%2frecords%2fsecret.json", headers=sandbox)).status, 404)

    async def test_wt_files_are_served_keyed_and_sandboxed_without_repo_config(self) -> None:
        for rel, body in [("wt/demo/index.html", "<script src='app.js'></script>"), ("wt/demo/app.js", "1"),
                          ("wt/demo/api_token.json", '{"t": "SECRET"}'), ("wt/demo/.git/HEAD.txt", "SECRET"),
                          ("wt/demo/node_modules/x.js", "SECRET"), ("wt/demo/.env", "SECRET"), ("wt/demo/run.py", "SECRET"),
                          ("wt/fsa-run/auth-test.json", "SECRET"), ("wt/a/Lib/site-packages/x.js", "SECRET"),
                          ("wt/a/.claude/settings.json", "SECRET"), ("wt/a/user-data/x.json", "SECRET"),
                          ("wt/a/edge-profile/x.json", "SECRET"), ("wt/a/chrome-profile/x.json", "SECRET"),
                          ("wt/a/profile/x.json", "SECRET"),
                          ("wt/a/edge-profile7/Default/Extensions/x.json", "SECRET"), ("wt/a/edge-profile-ar/x.json", "SECRET"),
                          ("wt/a/User Data/Default/x.json", "SECRET"), ("wt/umba-x/data/1/project.json", "SECRET")]:
            put(self.root, rel, body)
        client = await self.client()
        key = client.server.app[review.PAGE_KEY]
        page = await client.get(f"/review/w/{key}/demo/index.html", headers={"Host": LOCAL})
        self.assertEqual(page.status, 200)
        self.assertTrue(page.headers["Content-Security-Policy"].startswith("sandbox allow-scripts "))
        sandbox = {"Host": LOCAL, "Sec-Fetch-Site": "cross-site", "Origin": "null"}
        self.assertEqual((await client.get(f"/review/w/{key}/demo/app.js", headers=sandbox)).status, 200)
        for rel in ["demo/api_token.json", "demo/.git/HEAD.txt", "demo/node_modules/x.js", "demo/.env", "demo/run.py",
                    "fsa-run/auth-test.json", "a/Lib/site-packages/x.js", "a/.claude/settings.json", "a/user-data/x.json",
                    "a/edge-profile/x.json", "a/chrome-profile/x.json", "a/profile/x.json", "a/edge-profile7/Default/Extensions/x.json", "a/edge-profile-ar/x.json",
                    "a/User Data/Default/x.json", "umba-x/data/1/project.json",
                    "demo/../../records/secret.json", "..%2frecords%2fsecret.json", "demo"]:
            response = await client.get(f"/review/w/{key}/{rel}", headers={"Host": LOCAL})
            self.assertEqual(response.status, 404, rel)
            self.assertNotIn("SECRET", await response.text(), rel)
        self.assertEqual((await client.get("/review/w/wrongkey/demo/index.html", headers={"Host": LOCAL})).status, 404)

    @unittest.skipUnless(os.name == "nt", "Start-Process links require Windows drive paths")
    def test_open_links_for_commands_into_wt(self) -> None:
        put(self.root, "wt/demo/index.html", "<h1>demo</h1>")
        put(self.root, "wt/demo/api_token.json", '{"t": "SECRET"}')
        put(self.root, "wt/demo/run.py", "print(1)")
        wt = self.root / "wt"
        self.assertEqual(review.audit_href(self.root, f"Start-Process '{wt}\\demo\\index.html'", "K", wt), "/review/w/K/demo/index.html")
        for command in [f"Start-Process '{wt}\\demo\\api_token.json'", f"Start-Process '{wt}\\demo'", f"Start-Process '{wt}\\demo\\run.py'"]:
            self.assertIsNone(review.audit_href(self.root, command, "K", wt), command)

    @unittest.skipUnless(os.name == "nt", "Start-Process links require Windows drive paths")
    def test_open_links_for_commands_into_audits(self) -> None:
        root = str(self.root)
        self.assertEqual(review.audit_href(self.root, f"Start-Process '{root}\\audits\\{STAMP}-report.md'", "K"), f"/review/s/K/{STAMP}-report.md")
        self.assertEqual(review.audit_href(self.root, f"Start-Process notepad '{root}\\audits\\shots\\two words #1.png'", "K"),
                         "/review/s/K/shots/two%20words%20%231.png")
        self.assertEqual(review.audit_href(self.root, f"Start-Process '{root}\\audits\\shots'", "K"), "/review/g/shots")
        for command in [f"Start-Process '{root}\\records\\secret.json'", f"Start-Process '{root}\\audits\\..\\records\\secret.json'",
                        f"Start-Process '{root}\\audits\\shots\\run.exe'", f"Start-Process '{root}\\audits\\missing.md'",
                        "Start-Process 'http://127.0.0.1:8893/'", "wsl -e bash x.sh"]:
            self.assertIsNone(review.audit_href(self.root, command, "K"), command)

    async def test_read_only_methods(self) -> None:
        client = await self.client()
        before = tree_digest(self.root)
        for path in ["/review/all", "/todo", "/api/todo/act", f"/review/a/{STAMP}-report.md", "/review/g/shots"]:
            for method in (client.post, client.put, client.delete, client.patch):
                self.assertIn((await method(path, headers={"Host": LOCAL})).status, (403, 405), path)  # CSRF gate or no such method
        self.assertEqual(tree_digest(self.root), before)

    async def test_host_guard_matches_dashboard(self) -> None:
        client = await self.client(https_host=PRIVATE_HOST, owner_login=OWNER)
        for path in ["/review/all", "/todo", "/api/todo", f"/review/a/{STAMP}-report.md", "/review/a/shots/01.png", "/review/g/shots"]:
            self.assertEqual((await client.get(path, headers={"Host": "evil.example"})).status, 421, path)
            self.assertEqual((await client.get(path, headers={"Host": PRIVATE_HOST})).status, 403, path)
            self.assertEqual((await client.get(path, headers={"Host": PRIVATE_HOST, IDENTITY_HEADER: "someone@example.com"})).status, 403, path)
            self.assertEqual((await client.get(path, headers={"Host": PRIVATE_HOST, IDENTITY_HEADER: OWNER})).status, 200, path)
            self.assertEqual((await client.get(path, headers={"Host": LOCAL, "Sec-Fetch-Site": "cross-site"})).status, 403, path)
            self.assertEqual((await client.get(path, headers={"Host": LOCAL, "Origin": "http://evil.example"})).status, 403, path)

    async def test_dashboard_entry(self) -> None:
        client = await self.client()
        body = await (await client.get("/api/organization", headers={"Host": LOCAL})).json()
        self.assertEqual(body["schema_version"], 2)
        self.assertEqual(body["review"], {"reports": 2, "trials": 3, "deployments": 2})  # javascript: url not counted
        html = await (await client.get("/dashboard", headers={"Host": LOCAL})).text()
        self.assertIn('href="/todo"', html)
        self.assertIn("renderReview", html)


if __name__ == "__main__":
    unittest.main()
