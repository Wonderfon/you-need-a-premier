"""Browser check: an older version opened from History stays in the editor (no jump back to the polished one).

Throwaway server (temp data dir, synthetic entry, own port); never touches production data or port 8765.
Needs aiohttp + playwright (Chromium). Exit code 0 = all checks pass.
"""
# CONFIG
PORT = 18799
HOLD_SECONDS = 12  # the page polls every 3 s; the bug showed within one poll
SHOTS = None  # e.g. r"C:\path\to\dir": screenshots right after opening and after the hold
HEADLESS = True

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import web
from playwright.async_api import async_playwright

from input import server

ORIGINAL = "原始转写：明天下午三点开会，带上预算表。"
DRAFT = "我改过的版本：明天下午三点开会，带上预算表和合同。"
POLISHED = "润色稿：明天下午三点开会，请带上预算表和合同。"
NEWER = "新的润色稿：明天下午三点准时开会，请带上预算表和合同。"
checks = []


def check(name, ok, detail=""):
    checks.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)


def seed(store):
    session, original = store.create_text_session("合成历史记录", ORIGINAL)
    draft = store.append_revision(session["id"], "correction", DRAFT, original["id"], data={"correction_type": "draft"})
    polished = store.append_revision(session["id"], "postprocess", POLISHED, draft["id"], "llm_postprocess",
                                     {"human_verified": False, "review_required": True})
    return session["id"], original, draft, polished


async def open_entry(page, sid, latest=POLISHED):
    await page.goto(f"http://127.0.0.1:{PORT}/?session={sid}")
    await page.wait_for_function("(t) => document.getElementById('result-editor').value === t", arg=latest, timeout=10000)
    await page.evaluate("document.getElementById('history-panel').open = true")


async def open_version(page, text):
    """Click 「在编辑区打开」 on the history card whose text is `text`."""
    await page.locator(".revision", has=page.locator(".revision-content", has_text=text)).get_by_role("button", name="在编辑区打开").click()
    await page.wait_for_function("(t) => document.getElementById('result-editor').value === t", arg=text, timeout=5000)


async def editor(page):
    return await page.input_value("#result-editor")


async def main():
    samples = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        app = server.create_app(Path(tmp), expected_port=PORT)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", PORT).start()
        store = app[server.STORE_KEY]
        sid, original, draft, polished = seed(store)
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=HEADLESS)
                page = await browser.new_page(viewport={"width": 390, "height": 1200})
                page.on("pageerror", lambda err: check("no page errors", False, str(err)))

                # 1. the owner's path: polished entry -> open the older version -> wait
                await open_entry(page, sid)
                check("entry opens on the polished version", await editor(page) == POLISHED)
                await open_version(page, ORIGINAL)
                t0 = time.monotonic()
                if SHOTS:
                    await page.screenshot(path=str(Path(SHOTS) / "1-opened-old-version.png"))
                while time.monotonic() - t0 < HOLD_SECONDS:
                    samples.append({"t": round(time.monotonic() - t0, 1), "editor": (await editor(page))[:12]})
                    await asyncio.sleep(1)
                if SHOTS:
                    await page.screenshot(path=str(Path(SHOTS) / "2-after-hold.png"))
                final = await editor(page)
                check(f"older version still in the editor after {HOLD_SECONDS} s", final == ORIGINAL, f"editor={final[:20]!r}")
                print("samples:", json.dumps(samples, ensure_ascii=False), flush=True)
                check("opening and waiting wrote no revision", len(store.session_detail(sid)["revisions"]) == 3)

                # 2. a genuinely new revision (e.g. a new polish) still shows while the editor is untouched
                store.append_revision(sid, "postprocess", NEWER, draft["id"], "llm_postprocess", {"human_verified": False})
                try:
                    await page.wait_for_function("(t) => document.getElementById('result-editor').value === t", arg=NEWER, timeout=8000)
                    check("a new revision arriving later is still shown", True)
                except Exception:
                    check("a new revision arriving later is still shown", False, f"editor={(await editor(page))[:20]!r}")

                # 3. editing after opening an old version saves a child of that version and keeps the text
                await open_entry(page, sid, NEWER)
                await open_version(page, ORIGINAL)
                edited = ORIGINAL + "（补一句）"
                await page.fill("#result-editor", edited)
                await page.wait_for_function("() => document.getElementById('save-status').textContent.includes('已自动保存')", timeout=5000)
                await asyncio.sleep(4)
                revs = store.session_detail(sid)["revisions"]
                check("edit after opening saved as a child of the opened version",
                      revs[-1]["content"] == edited and revs[-1]["parent_id"] == original["id"], f"parent={revs[-1]['parent_id']}")
                check("edited text stays in the editor", await editor(page) == edited)
                await browser.close()
        finally:
            await runner.cleanup()
    print("PASS" if all(checks) else "FAIL")
    sys.exit(0 if all(checks) else 1)


if __name__ == "__main__":
    asyncio.run(main())
