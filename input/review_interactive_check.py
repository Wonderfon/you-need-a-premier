"""Replay real owner pages on an isolated input copy; never use the production data directory.

Run from the branch root: python -m input.review_interactive_check --source-root <incore>
  --runtime <scratch-directory> --evidence <output-directory> [--browser <chromium>]
Requires aiohttp and playwright (including its Chromium browser).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from aiohttp import web
from playwright.async_api import async_playwright

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import review_packet
from input import server


PAGES = (
    "2026-10-20-tm-ideas2-tick.html",
    "2026-10-18-writing-m2-pairs-pilot.html",
    "2026-10-22-writing-humanizer-test/blind-read.html",
)


def digest(paths):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


async def run(args):
    branch = Path(__file__).resolve().parent.parent
    runtime, evidence = args.runtime.resolve(), args.evidence.resolve()
    # Use a fresh scratch directory: even a mistaken argument cannot point the server at production input/data.
    runtime.mkdir(parents=True, exist_ok=False)
    evidence.mkdir(parents=True, exist_ok=True)
    fixtures = runtime / "fixtures"
    sources = [args.source_root / "audits" / rel for rel in PAGES]
    before = digest(sources)
    for rel, source in zip(PAGES, sources):
        target = fixtures / "audits" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    md = fixtures / "audits" / f"{date.today().isoformat()}-owner-report.md"
    md.write_text("# Owner report\n\n[TM 勾选页](2026-10-20-tm-ideas2-tick.html)\n", encoding="utf-8")
    subprocess.run([sys.executable, str(branch / "control" / "md2html.py"), str(md)],
                   check=True, capture_output=True, text=True, encoding="utf-8")
    backlog = fixtures / "projects" / "backlog.md"
    backlog.parent.mkdir(parents=True)
    backlog.write_text(f"- [done {date.today():%m-%d}；给 owner：勾三条，audits/{PAGES[0]}] Thinking Engines：勾选页\n",
                       encoding="utf-8")
    fixture_files = [p for p in fixtures.rglob("*") if p.is_file()]
    fixtures_before = digest(fixture_files)
    app = server.create_app(data_root=runtime / "input-copy", expected_port=args.port, dashboard_root=fixtures,
                            wt_root=runtime / "empty-wt", pending_dir=runtime / "pending-copy")
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    results = {"branch": subprocess.check_output([review_packet.GIT, "-C", str(branch), "branch", "--show-current"],
                                                 text=True, encoding="utf-8").strip(),
               "commit": subprocess.check_output([review_packet.GIT, "-C", str(branch), "rev-parse", "HEAD"],
                                                  text=True, encoding="utf-8").strip(),
               "loaded_server": str(Path(server.__file__).resolve()), "port": args.port}
    try:
        await web.TCPSite(runner, "127.0.0.1", args.port).start()
        base = f"http://127.0.0.1:{args.port}"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, executable_path=args.browser)
            try:
                page = await browser.new_page(viewport={"width": 1100, "height": 850})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                # The owner starts at /review and follows the ordinary report link (not a hand-constructed sandbox URL).
                await page.goto(base + "/review")
                await page.locator(f'#trial a[href="/review/a/{PAGES[0]}"]').click()
                assert "/review/s/" in page.url
                checks = [('.row[data-id="Q1.1"] input[data-k="n"]', "新"),
                          ('.row[data-id="Q2.4"] input[data-k="d"]', "想做"),
                          ('.row[data-id="Q4.12"] input[data-k="n"]', "新")]
                for selector, _ in checks:
                    await page.locator(selector).check()
                await page.get_by_role("button", name="生成回复文本", exact=True).click()
                reply = await page.locator("#out").input_value()
                assert reply == "勾选结果（未列出的＝都没勾）\nQ1.1: 新\nQ2.4: 想做\nQ4.12: 新", reply
                assert await page.locator("#out").evaluate("e => e.selectionEnd - e.selectionStart === e.value.length")
                (evidence / "tm-reply.txt").write_text(reply + "\n", encoding="utf-8")
                await page.screenshot(path=str(evidence / "tm-three-checks.png"))
                results["tm_reply"] = reply
                results["isolation"] = await page.evaluate("""async () => {
                    const r = {origin: window.origin};
                    for (const [k, read] of [['cookie', () => document.cookie], ['storage', () => localStorage.length]]) {
                        try { read(); r[k] = 'accessible'; } catch(e) { r[k] = e.name; }
                    }
                    r.api = await fetch('/api/sessions').then(r => r.status).catch(() => 'blocked');
                    r.write = await fetch('/api/text', {method:'POST', headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({text:'MUST NOT SAVE'})}).then(r => r.status).catch(() => 'blocked');
                    return r;
                }""")
                assert results["isolation"] == {"origin": "null", "cookie": "SecurityError", "storage": "SecurityError",
                                                 "api": "blocked", "write": "blocked"}, results
                # Deselecting and regenerating must replace the output, rather than retain stale selections.
                await page.locator(checks[-1][0]).uncheck()
                await page.get_by_role("button", name="生成回复文本", exact=True).click()
                assert "Q4.12" not in await page.locator("#out").input_value()
                for selector, _ in checks[:-1]:
                    await page.locator(selector).uncheck()
                await page.get_by_role("button", name="生成回复文本", exact=True).click()
                assert (await page.locator("#out").input_value()).strip() == "勾选结果（未列出的＝都没勾）"
                # Existing owner_html.py output: text comments, radios, and its localStorage fallback all work.
                await page.goto(base + "/review/a/" + PAGES[1])
                await page.locator('input[name="pA01"][value="可以"]').check()
                await page.locator('input[type="text"]').first.fill("本地验收：保留这句")
                await page.get_by_role("button", name="生成回复并复制", exact=True).click()
                owner_reply = await page.locator("#out").input_value()
                assert "A01 可以：本地验收：保留这句" in owner_reply
                (evidence / "owner-html-reply.txt").write_text(owner_reply + "\n", encoding="utf-8")
                await page.screenshot(path=str(evidence / "owner-html-reply.png"))
                await page.goto(base + "/review/a/" + PAGES[2])
                for n in (1, 3, 6):
                    await page.locator(f'input[name="q{n}"][value="像我"]').check()
                await page.get_by_role("button", name="生成回复", exact=True).click()
                blind_reply = await page.locator("#out").input_value()
                assert all(f"第{n}段：像我" in blind_reply for n in (1, 3, 6)), blind_reply
                (evidence / "blind-reply.txt").write_text(blind_reply + "\n", encoding="utf-8")
                await page.screenshot(path=str(evidence / "blind-reply.png"))
                # A fresh md2html report and its relative link reach the same interactive TM page.
                await page.goto(base + "/review/a/" + md.with_suffix(".html").name)
                await page.get_by_role("link", name="TM 勾选页", exact=True).click()
                await page.locator(checks[0][0]).check()
                await page.get_by_role("button", name="生成回复文本", exact=True).click()
                assert "Q1.1: 新" in await page.locator("#out").input_value()
                assert not errors, errors
                results.update(md2html=True, owner_html=True, blind_read=True, deselect_and_empty=True, page_errors=errors)
                # A fresh trusted tab: navigating out of an opaque page is itself a cross-site request.
                trusted = await browser.new_page()
                await trusted.goto(base + "/")
                assert await trusted.evaluate("fetch('/api/sessions').then(r=>r.json())") == {"sessions": []}
                await trusted.close()
            finally:
                await browser.close()
        assert before == digest(sources)
        assert fixtures_before == digest(fixture_files)
        results.update(source_files_unchanged=True, fixture_files_unchanged=True, sessions_created=0)
    finally:
        await runner.cleanup()
    results["server_stopped"] = True
    (evidence / "surface.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--browser")
    args = parser.parse_args()
    if args.port == 8765:
        parser.error("Use an isolated port, never production port 8765")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
