"""Open every fixed HTML page on an isolated input server and check browser assets."""

import asyncio
import json
import socket
import sys
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

# CONFIG
HOST = "127.0.0.1"
PORT = 0  # 0 selects an unused local port; never use the production port 8765.
DATA_DIR: Path | None = None  # None creates and removes an empty temporary data directory.
SCREENSHOT_DIR = Path(__file__).resolve().parents[1] / "audits" / f"{date.today().isoformat()}-input-page-smoke-evidence"
VIEWPORT = {"width": 390, "height": 844}
EXPECTED_PAGES = ("/", "/dashboard", "/pending", "/poetry", "/review/all", "/sprint", "/todo")  # /pending redirects (302) to /todo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from input import server


def route_path(route) -> str:
    return route.resource.canonical


def fixed_pages(app) -> list[dict[str, str]]:
    pages = {}
    for route in app.router.routes():
        name = getattr(route.handler, "__name__", "")
        path = route_path(route)
        if route.method == "GET" and (name == "index" or name.endswith("_page")) and "{" not in path:
            pages[path] = name
    return [{"path": path, "handler": pages[path]} for path in sorted(pages)]


def assert_page_coverage(pages: list[dict[str, str]]) -> None:
    actual = {item["path"] for item in pages}
    expected = set(EXPECTED_PAGES)
    if not (Path(__file__).parent / "poetry_panel.py").exists():  # personal panel left out of the public export
        expected.discard("/poetry")
    if not actual:
        raise AssertionError(f"No page routes discovered; expected {sorted(expected)}")
    missing, unexpected = sorted(expected - actual), sorted(actual - expected)
    if missing or unexpected:
        raise AssertionError(f"Page route coverage mismatch: missing={missing}; unexpected={unexpected}")


def get_route_inventory(app) -> list[tuple[str, str, str]]:
    return sorted({
        (route.method, route_path(route), getattr(route.handler, "__name__", ""))
        for route in app.router.routes()
    })


def open_data_dir():
    if DATA_DIR is None:
        return tempfile.TemporaryDirectory(prefix="input-page-smoke-")
    path = Path(DATA_DIR)
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"DATA_DIR must be empty synthetic data: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return None


async def run() -> dict:
    if not 0 <= PORT <= 65535 or PORT == 8765:
        raise ValueError("PORT must be 0 or a non-production TCP port")

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    data_context = open_data_dir()
    data_path = Path(data_context.name) if data_context else Path(DATA_DIR).resolve()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, PORT))
    sock.listen()
    sock.setblocking(False)
    port = sock.getsockname()[1]
    if port == 8765:
        sock.close()
        if data_context:
            data_context.cleanup()
        raise AssertionError("refusing to use production port 8765")

    original_incubator_root = server.INCORE_ROOT
    server.INCORE_ROOT = data_path
    app = server.create_app(
        data_path,
        expected_port=port,
        dashboard_root=data_path,
        pending_dir=data_path / "pending",
        wt_root=data_path / "wt",
    )
    runner = web.AppRunner(app, access_log=None)
    result = {
        "server_file": str(Path(server.__file__).resolve()),
        "data_dir": str(data_path),
        "port": port,
        "viewport": VIEWPORT,
        "routes": get_route_inventory(app),
        "pages": [],
        "blocked_requests": [],
    }
    try:
        await runner.setup()
        await web.SockSite(runner, sock).start()
        base_url = f"http://{HOST}:{port}"
        pages = fixed_pages(app)
        result["registered_route_count"] = len(result["routes"])
        result["page_routes"] = pages
        assert_page_coverage(pages)
        failures = []

        async def guard_request(route):
            request = route.request
            if request.method != "GET":
                result["blocked_requests"].append({"method": request.method, "url": request.url})
                await route.abort()
            elif not request.url.startswith(base_url + "/") and request.url != base_url:
                result["blocked_requests"].append({"method": request.method, "url": request.url})
                await route.abort()
            else:
                await route.continue_()

        from playwright.async_api import async_playwright  # lazy: tests import this module without playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
            await context.route("**/*", guard_request)
            for index, item in enumerate(pages, start=1):
                path = item["path"]
                slug = "home" if path == "/" else path.strip("/").replace("/", "-")
                page = await context.new_page()
                console_errors: list[str] = []
                page_errors: list[str] = []
                static_files: list[dict] = []
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
                page.on("pageerror", lambda error: page_errors.append(str(error)))

                def record_response(response):
                    request = response.request
                    suffix = Path(urlsplit(response.url).path).suffix.lower()
                    if suffix in {".js", ".mjs", ".css"}:
                        static_files.append({
                            "url": response.url,
                            "status": response.status,
                            "type": request.resource_type,
                        })

                page.on("response", record_response)
                response = None
                navigation_error = None
                try:
                    response = await page.goto(f"{base_url}{path}", wait_until="networkidle", timeout=15000)
                    await page.wait_for_timeout(300)
                    await page.screenshot(path=str(SCREENSHOT_DIR / f"page-{slug}.png"), full_page=True)
                except Exception as exc:
                    navigation_error = f"{type(exc).__name__}: {exc}"
                status = response.status if response else None
                screenshot = str((SCREENSHOT_DIR / f"page-{slug}.png").resolve())
                item_result = {
                    **item,
                    "status": status,
                    "console_errors": console_errors,
                    "page_errors": page_errors,
                    "static_files": static_files,
                    "screenshot": screenshot,
                    "navigation_error": navigation_error,
                }
                result["pages"].append(item_result)
                if status != 200:
                    failures.append(f"{path}: page status {status}")
                if console_errors or page_errors:
                    failures.append(f"{path}: {len(console_errors) + len(page_errors)} browser errors")
                for asset in static_files:
                    if asset["status"] != 200:
                        failures.append(f"{path}: {asset['status']} {asset['url']}")
                if navigation_error:
                    failures.append(f"{path}: {navigation_error}")
                await page.close()
            await browser.close()

        if result["blocked_requests"]:
            failures.append(f"blocked non-GET or external requests: {len(result['blocked_requests'])}")
        result["pass"] = not failures
        result["failures"] = failures
        result_path = SCREENSHOT_DIR / "page-smoke-result.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Routes registered: {result['registered_route_count']}")
        print("GET pages:")
        for item in pages:
            assets = ", ".join(Path(urlsplit(asset["url"]).path).name for asset in next(
                (entry["static_files"] for entry in result["pages"] if entry["path"] == item["path"]), []
            )) or "(none)"
            print(f"  {item['path']} ({item['handler']}): {assets}")
        print("\nPage | Status | Browser errors | Screenshot")
        for item in result["pages"]:
            errors = len(item["console_errors"]) + len(item["page_errors"])
            print(f"{item['path']} | {item['status']} | {errors} | {item['screenshot']}")
        print(f"Result JSON: {result_path.resolve()}")
        if failures:
            raise AssertionError("Page smoke failed: " + "; ".join(failures))
        return result
    finally:
        await runner.cleanup()
        sock.close()
        server.INCORE_ROOT = original_incubator_root
        if data_context:
            data_context.cleanup()
        if DATA_DIR is None:
            result["temporary_data_removed"] = not data_path.exists()


if __name__ == "__main__":
    asyncio.run(run())
