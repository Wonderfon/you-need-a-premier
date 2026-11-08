"""Browser check: edits are saved as drafts; the purpose is recorded only when “确认保存” is clicked.

Real Chrome with a fake mic against a throwaway input server (temp data dir, stub ASR, own port);
never touches production data, port 8765 or the resident Nano worker. Needs aiohttp + playwright.
"""
# CONFIG
PORT = 8798
RECORD_SECONDS = 3
HEADLESS = True
SCREENSHOT = None  # e.g. r"C:\path\confirm.png"

import asyncio
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
stub = types.ModuleType("input.transcribe")
stub.transcribe = lambda path: {"text": "明天下午三点开会"}
sys.modules["input.transcribe"] = stub

from aiohttp import web
from playwright.async_api import async_playwright

from input import server

checks = []


def check(name, ok, detail=""):
    checks.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))


def latest(store, sid):
    revs = store.session_detail(sid)["revisions"]
    return revs[-1], revs


async def record_once(page):
    await page.click("#record-toggle")
    await page.wait_for_selector("#record-toggle.is-recording:not([disabled])")
    await asyncio.sleep(RECORD_SECONDS)
    await page.click("#record-toggle")
    await page.wait_for_function("() => document.getElementById('result-editor').value === '明天下午三点开会'", timeout=20000)


async def confirm(page, purpose):
    await page.check(f'input[name="purpose"][value="{purpose}"]')
    await page.click("#verify-revision")
    await page.wait_for_function("() => document.getElementById('verify-revision').textContent === '已确认'", timeout=10000)


async def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        app = server.create_app(Path(tmp), expected_port=PORT)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", PORT).start()
        store = app[server.STORE_KEY]
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(channel="chrome", headless=HEADLESS, args=[
                    "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
                page = await browser.new_page()
                page.on("pageerror", lambda err: check("no page errors", False, str(err)))
                await page.goto(f"http://127.0.0.1:{PORT}/")

                await record_once(page)
                sid = store.list_sessions()[0]["id"]
                look = await page.evaluate("""() => ['#new-recording', '#verify-revision', '.purpose-choice label'].map((sel) => {
                    const s = getComputedStyle(document.querySelector(sel));
                    return [sel, s.borderTopWidth, s.backgroundColor];
                })""")
                check("buttons have a visible border and fill", all(b != "0px" and bg != "rgba(0, 0, 0, 0)" for _, b, bg in look), str(look))

                await page.fill("#result-editor", "明天下午四点开会，在三楼")
                await page.wait_for_function("() => document.getElementById('save-status').textContent.includes('修改稿')", timeout=5000)
                rev, _ = latest(store, sid)
                check("autosaved edit is a draft", rev["data"].get("correction_type") == "draft", rev["data"])

                await confirm(page, "user_rewrite")
                rev, _ = latest(store, sid)
                check("confirm as 表达改写 records that label on the same text",
                      rev["data"].get("correction_type") == "user_rewrite" and rev["content"] == "明天下午四点开会，在三楼")
                check("a rewrite is not a verbatim reference", store.list_verified_training_rows() == [])

                await confirm(page, "recognition_correction")
                rev, revs = latest(store, sid)
                rows = store.list_verified_training_rows()
                check("re-confirm as 听写纠错 overrides the earlier label",
                      rev["data"].get("correction_type") == "recognition_correction" and rev["verification"]["current"])
                check("verified row is exactly that revision", [r["revision_id"] for r in rows] == [rev["id"]])
                check("history kept (asr, draft, rewrite, correction)", [r["kind"] for r in revs] == ["asr", "correction", "correction", "correction"],
                      str([(r["kind"], r["data"].get("correction_type")) for r in revs]))
                status = await page.text_content("#verify-status")
                check("status says what was confirmed", "已确认为听写纠错" in status, status)

                await page.click("#new-recording")
                await page.wait_for_selector("#record-toggle.is-recording:not([disabled])")
                await asyncio.sleep(RECORD_SECONDS)
                await page.click("#record-toggle")
                await page.wait_for_function("() => document.getElementById('result-editor').value === '明天下午三点开会'", timeout=20000)
                sid2 = next(s["id"] for s in store.list_sessions() if s["id"] != sid)
                await confirm(page, "recognition_correction")
                rev, revs = latest(store, sid2)
                check("unchanged transcript confirmed as 听写纠错 verifies the ASR itself",
                      len(revs) == 1 and rev["kind"] == "asr" and rev["verification"]["current"])
                if SCREENSHOT:
                    await page.screenshot(path=SCREENSHOT, full_page=True)
                await browser.close()
        finally:
            await runner.cleanup()
    print("PASS" if all(checks) else "FAIL")
    sys.exit(0 if all(checks) else 1)


if __name__ == "__main__":
    asyncio.run(main())
