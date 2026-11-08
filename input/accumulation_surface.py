"""Isolated dashboard preview and 430px acceptance. GET only; originals are read only.

--serve keeps the preview open until Ctrl+C. Without it, capture, recount and exit.
"""
from __future__ import annotations

# CONFIG: the preview's dimensions and snapshot size; all paths come from CLI arguments.
VIEWPORT = {"width": 430, "height": 900}
SCREENSHOT_HEIGHT = 1100

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from input import accumulation, dashboard, server


def source_hashes(root):
    paths = list((root / "records").glob("*.json"))
    paths += [root / name for name in ("records/current-chat/messages.jsonl", "data/owner-replies.json",
                                       "data/owner-ratings.json", "projects/materials.md",
                                       "input/data/input.sqlite3", "discord/data/discord.sqlite3")]
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}


async def run(args):
    args.out.mkdir(parents=True, exist_ok=True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", args.port))
    sock.setblocking(False)
    port = sock.getsockname()[1]
    assert port != 8765
    url = f"http://127.0.0.1:{port}"
    before = await asyncio.to_thread(source_hashes, args.root)
    result = {"viewport": VIEWPORT, "port": port, "pid": os.getpid(),
              "server_file": server.__file__, "counter_file": accumulation.__file__,
              "commit": subprocess.check_output([dashboard._control("review_packet").GIT, "rev-parse", "HEAD"],
                                                cwd=Path(__file__).resolve().parents[1], text=True, encoding="utf-8").strip()}
    # No external product probes during this local acceptance.
    with tempfile.TemporaryDirectory(prefix="accumulation-preview-", dir=args.out) as scratch, \
            patch.object(dashboard, "_fetch_status", return_value="unknown"):
        scratch = Path(scratch)
        @web.middleware
        async def read_only(request, handler):
            if request.method != "GET" or request.path not in ("/dashboard", "/api/organization", "/api/accumulation"):
                return web.Response(status=403)
            return await handler(request)
        app = server.create_app(scratch / "input", expected_port=port, dashboard_root=args.root,
                                pending_dir=scratch / "pending", wt_root=scratch / "wt")
        app.middlewares.insert(0, read_only)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            await web.SockSite(runner, sock).start()
            print(f"Preview: {url}/dashboard#accumulation-panel (PID {os.getpid()}; Ctrl+C stops)", flush=True)
            assert server.ASR_PRELOAD_KEY not in app
            if args.serve:
                await asyncio.Event().wait()
                return
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True, **({"executable_path": args.browser} if args.browser else {}))
                try:
                    context = await browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
                    async def local_get(route):
                        if route.request.method == 'GET' and route.request.url.startswith(url + '/'):
                            await route.continue_()
                        else:
                            await route.abort()
                    await context.route('**/*', local_get)
                    page = await context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(url + '/dashboard#accumulation-panel', wait_until='domcontentloaded')
                    await page.locator('.accumulation-card').last.wait_for(timeout=30000)
                    assert await page.locator('.accumulation-card').count() == len(accumulation.METRICS)
                    result['document_width'] = await page.evaluate('document.documentElement.scrollWidth')
                    assert result['document_width'] <= VIEWPORT['width']
                    result['page_errors'] = errors
                    assert not errors
                    result['displayed'] = await page.locator('.accumulation-card').evaluate_all(
                        '(cards) => Object.fromEntries(cards.map(c => [c.dataset.metric, c.querySelector(".accumulation-value").textContent]))')
                    panel = page.locator('#accumulation-panel')
                    box = await panel.evaluate('(el) => {const r = el.getBoundingClientRect(); return {top:r.top+scrollY, height:r.height};}')
                    result['screenshots'] = []
                    for i, offset in enumerate(range(0, math.ceil(box['height']), SCREENSHOT_HEIGHT), 1):
                        path = args.out / f'accumulation-430-{i}.png'
                        await page.screenshot(path=str(path), full_page=True, clip={'x': 0, 'y': box['top'] + offset,
                                              'width': VIEWPORT['width'], 'height': min(SCREENSHOT_HEIGHT, box['height'] - offset)})
                        result['screenshots'].append(path.name)
                    # The same cached snapshot must be fast and identical on repeat requests.
                    start = time.monotonic()
                    first = await context.request.get(url + '/api/accumulation')
                    first_json = await first.json()
                    for metric in first_json['metrics']:
                        displayed = result['displayed'][metric['key']]
                        number = re.match(r'[\d,.]+', displayed)
                        assert number and metric['total'] is not None
                        tolerance = .005001 if metric['unit'] == '小时' else .000001
                        assert abs(float(number[0].replace(',', '')) - metric['total']) < tolerance, metric['key']
                    result['dom_matches_api'] = True
                    for _ in range(5):
                        response = await context.request.get(url + '/api/accumulation')
                        assert await response.json() == first_json
                    result['six_cached_requests_seconds'] = round(time.monotonic() - start, 4)
                    raw = await first.text()
                    assert all(word not in raw for word in ('owner_verbatim', 'session_id', 'message_id', 'exact_text', str(args.root)))
                    check = await asyncio.create_subprocess_exec(
                        sys.executable, str(Path(__file__).with_name('accumulation_check.py')),
                        '--root', str(args.root), '--url', url, '--out', str(args.out / 'independent-counts.json'),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    stdout, stderr = await check.communicate()
                    result['independent_check'] = {'exit_code': check.returncode, 'stdout': stdout.decode('utf-8'),
                                                   'stderr': stderr.decode('utf-8')}
                    assert check.returncode == 0, result['independent_check']
                finally:
                    await browser.close()
        finally:
            await runner.cleanup()
            sock.close()
    after = await asyncio.to_thread(source_hashes, args.root)
    result['source_files_changed_during_check'] = len([p for p in before.keys() | after.keys() if before.get(p) != after.get(p)])
    result['source_files_checked'] = len(before)
    result['server_stopped'] = True
    probe = socket.socket()
    try:
        result['port_closed'] = probe.connect_ex(('127.0.0.1', port)) != 0
        assert result['port_closed']
    finally:
        probe.close()
    result['pass'] = True
    (args.out / 'surface.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--browser')
    args = parser.parse_args()
    if args.port == 8765:
        parser.error('production 8765 is forbidden')
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
