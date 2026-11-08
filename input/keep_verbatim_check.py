"""Browser check: a 听写纠错 confirmation stays valid after a different-text 表达改写 is confirmed on top of it.

Real Chrome with a fake mic against a throwaway input server (temp data dir, stub ASR, own port);
never touches production data, port 8765 or the resident Nano worker. Needs aiohttp + playwright.
"""
# CONFIG
PORT = 8799
RECORD_SECONDS = 3
HEADLESS = True
SCREENSHOT = None  # e.g. r"C:\path\keep.png"

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


def by_text(revs, text):
    return any(r["content"] == text for r in revs)


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

                await page.click("#record-toggle")
                await page.wait_for_selector("#record-toggle.is-recording:not([disabled])")
                await asyncio.sleep(RECORD_SECONDS)
                await page.click("#record-toggle")
                await page.wait_for_function("() => document.getElementById('result-editor').value === '明天下午三点开会'", timeout=20000)
                sid = store.list_sessions()[0]["id"]

                await page.fill("#result-editor", "明天下午四点开会，在三楼")
                await page.wait_for_function("() => document.getElementById('save-status').textContent.includes('修改稿')", timeout=5000)
                await confirm(page, "recognition_correction")
                verbatim = store.session_detail(sid)["revisions"][-1]
                check("听写纠错 confirmed", verbatim["verification"]["current"] and verbatim["content"] == "明天下午四点开会，在三楼")

                await page.fill("#result-editor", "下午四点在三楼开会")
                await page.wait_for_function("() => document.getElementById('save-status').textContent.includes('修改稿')", timeout=5000)
                await confirm(page, "user_rewrite")

                revs = store.session_detail(sid)["revisions"]
                kinds = [(r["kind"], r["data"].get("correction_type")) for r in revs]
                check("both versions kept (听写纠错 and 表达改写 both on file, no overwrite)",
                      ("correction", "recognition_correction") in kinds and kinds[-1] == ("correction", "user_rewrite")
                      and by_text(revs, "明天下午四点开会，在三楼") and by_text(revs, "下午四点在三楼开会"), str(kinds))
                by_id = {r["id"]: r for r in revs}
                check("听写纠错 check still current after the rewrite", by_id[verbatim["id"]]["verification"]["current"])
                check("training rows still hold the verbatim text",
                      [(r["revision_id"], r["text"]) for r in store.list_verified_training_rows()] == [(verbatim["id"], "明天下午四点开会，在三楼")])
                cards = await page.evaluate("""() => [...document.querySelectorAll('.revision')].map((c) => c.textContent)""")
                shown = [c for c in cards if "明天下午四点开会，在三楼" in c and "修改稿" not in c]
                check("verbatim card shows 已确认, not 取代", len(shown) == 1 and "人工核对全段录音" in shown[0] and "不再有效" not in shown[0],
                      str(shown)[:200])
                check("no stale prompt", "这一版之后又有修改" not in (await page.text_content("#verify-status")))
                if SCREENSHOT:
                    await page.screenshot(path=SCREENSHOT, full_page=True)
                await browser.close()
        finally:
            await runner.cleanup()
    print("PASS" if all(checks) else "FAIL")
    sys.exit(0 if all(checks) else 1)


if __name__ == "__main__":
    asyncio.run(main())
