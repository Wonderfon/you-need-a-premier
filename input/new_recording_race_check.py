"""Browser check for the 新录音 race: real Chrome with a fake mic against a throwaway input server.

Needs `pip install aiohttp playwright` and system Chrome. Uses a temp data dir, a stub ASR and its own
port; never touches production data, port 8765 or the resident Nano worker.
"""
# CONFIG
PORT = 8799
FINALIZE_DELAY_SECONDS = 1.5  # holds the previous recording in its finishing window
RECORD_SECONDS = 6            # longer than one 5 s MediaRecorder slice
HEADLESS = True

import asyncio
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
stub = types.ModuleType("input.transcribe")
stub.transcribe = lambda path: {"text": "stub transcript"}
sys.modules["input.transcribe"] = stub

from aiohttp import web
from playwright.async_api import async_playwright

from input import server


LIVE = "#record-toggle.is-recording:not([disabled])"  # stopping also shows is-recording, but disabled


async def recording(page):
    return await page.locator("#record-toggle").evaluate("b => b.classList.contains('is-recording') && !b.disabled")


async def by_button(page):
    """Stop, then press 新录音 while the first recording is still finalizing."""
    toggle = page.locator("#record-toggle")
    await toggle.click()
    await page.wait_for_selector(LIVE)
    await asyncio.sleep(RECORD_SECONDS)
    await toggle.click()
    await page.wait_for_selector("#result-screen:not([hidden])")
    await page.click("#new-recording")
    await page.wait_for_selector(LIVE, timeout=15000)
    await asyncio.sleep(RECORD_SECONDS)
    alive = await recording(page)
    if alive:
        await toggle.click()
    return alive


async def by_space(page):
    """Space to stop, then Space again right away, before the stop has been handled."""
    await page.evaluate("document.activeElement?.blur()")
    await page.keyboard.press("Space")
    await page.wait_for_selector(LIVE)
    await asyncio.sleep(RECORD_SECONDS)
    await page.keyboard.press("Space")
    await page.keyboard.press("Space")
    await page.wait_for_selector(LIVE, timeout=15000)
    await asyncio.sleep(RECORD_SECONDS)
    alive = await recording(page)
    if alive:
        await page.keyboard.press("Space")
    return alive


async def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        runner = web.AppRunner(server.create_app(Path(tmp), expected_port=PORT))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", PORT).start()
        alive = {}
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(channel="chrome", headless=HEADLESS, args=[
                    "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
                for scenario in (by_button, by_space):
                    page = await browser.new_page()
                    page.on("pageerror", lambda err: print("pageerror:", err))

                    async def slow_finalize(route):
                        await asyncio.sleep(FINALIZE_DELAY_SECONDS)
                        await route.continue_()
                    await page.route("**/finalize", slow_finalize)
                    await page.goto(f"http://127.0.0.1:{PORT}/")
                    await page.wait_for_function("() => typeof indexedDB !== 'undefined'")
                    try:
                        alive[scenario.__name__] = await scenario(page)
                    except Exception as exc:
                        alive[scenario.__name__] = f"failed: {exc.__class__.__name__}: {str(exc).splitlines()[0]}"
                    await asyncio.sleep(FINALIZE_DELAY_SECONDS + 3)
                    await page.close()
                await browser.close()
        finally:
            await runner.cleanup()
        db = sqlite3.connect(Path(tmp) / "input.sqlite3")
        rows = db.execute("select s.status, s.chunk_count, s.total_bytes, "
                          "(select count(*) from chunks c where c.session_id = s.id) "
                          "from sessions s order by s.created_at").fetchall()
        db.close()
    print("second recording still recording before its stop:", alive)
    for status, chunk_count, total_bytes, stored in rows:
        print(f"session status={status} finalized_chunks={chunk_count} bytes={total_bytes} stored_chunks={stored}")
    ok = (all(value is True for value in alive.values()) and len(rows) == 4
          and all(stored >= 2 and chunk_count == stored and status != "collecting"
                  for status, chunk_count, _, stored in rows))
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
