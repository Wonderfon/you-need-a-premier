"""Offline Markdown/HTML -> phone-sized PNGs, using Playwright Chromium.

Usage: python control/md2png.py <file.md|file.html> [<file> ...]
Writes <file>/page-001.png etc.; Markdown first goes through md2html.
Install Playwright and its Chromium in an isolated environment before use.
PLAYWRIGHT_BROWSERS_PATH selects the browser cache; no browser is downloaded here.
"""
import ctypes
import math
import os
import re
import subprocess
import sys
from pathlib import Path

CONFIG = {
    "width": 430,
    "screen_height": 860,
    "screens_per_image": 1.5,
    "device_scale_factor": 2,
    "max_bytes": 10_000_000,
    "linux_library_dirs": (
        "~/.cache/codex-sandbox/native-libs/usr/lib/x86_64-linux-gnu",
        "~/.cache/codex-sandbox/playwright-private-libs/usr/lib/x86_64-linux-gnu",
        "~/.cache/codex-sandbox/md2html-ol-libs/root/usr/lib/x86_64-linux-gnu",
    ),
}

# Also reflow older and hand-written HTML without changing the source file.
MOBILE_CSS = """
html{scroll-behavior:auto!important}
body{overflow-wrap:anywhere!important}
pre,pre code{white-space:pre-wrap!important;overflow-wrap:anywhere!important;word-break:break-word!important}
table{display:table!important;width:100%!important;table-layout:fixed!important;overflow:visible!important}
th,td{overflow-wrap:anywhere!important}
img,svg,video{max-width:100%!important;height:auto!important}
"""


def chromium_env():
    """Use existing cached Linux libraries only when the system lacks libnspr4."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        ctypes.CDLL("libnspr4.so")
        return None
    except OSError:
        directories = [Path(p).expanduser() for p in CONFIG["linux_library_dirs"]]
        for directory in directories:
            if (directory / "libnspr4.so").is_file():
                return {**os.environ, "LD_LIBRARY_PATH": os.pathsep.join(
                    filter(None, (str(directory), os.environ.get("LD_LIBRARY_PATH"))))}
        raise OSError("libnspr4.so unavailable; checked cache directories: "
                      + ", ".join(map(str, directories)))


def slices(height, limit):
    """Contiguous CSS-pixel slices; the last one stops at the page bottom."""
    if height <= 0 or limit <= 0:
        raise ValueError("Page and slice heights must be positive")
    return [(y, min(limit, height - y)) for y in range(0, height, limit)]


def layout_metrics(page):
    return page.evaluate("""() => ({
        width: innerWidth,
        scroll_width: document.documentElement.scrollWidth,
        height: Math.ceil(Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)),
        overflow: [...document.querySelectorAll('body *')].filter(e => {
            const r = e.getBoundingClientRect();
            return r.width && r.height && (r.left < -1 || r.right > innerWidth + 1 ||
                (e.clientWidth > 0 && e.scrollWidth > e.clientWidth + 1));
        }).slice(0, 5).map(e => e.tagName + (e.className ? '.' + e.className : ''))
    })""")


def render(source, browser):
    source = Path(source).resolve()
    if source.suffix.lower() not in (".md", ".html") or not source.is_file():
        raise ValueError(f"Expected an existing .md or .html file: {source}")
    html_path = source
    if source.suffix.lower() == ".md":
        converted = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("md2html.py")), str(source)],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        html_path = source.with_suffix(".html")
        if converted.stdout.strip() != str(html_path):
            raise ValueError(converted.stdout.strip())  # Preserve md2html's hand-written HTML guard.

    limit = math.floor(CONFIG["screen_height"] * CONFIG["screens_per_image"])
    context = browser.new_context(
        viewport={"width": CONFIG["width"], "height": limit},
        device_scale_factor=CONFIG["device_scale_factor"],
        color_scheme="light", java_script_enabled=False, service_workers="block",
    )
    try:
        # Local reports stay offline, including images/fonts linked to remote URLs.
        context.route("http://**/*", lambda route: route.abort())
        context.route("https://**/*", lambda route: route.abort())
        page = context.new_page()
        page.goto(html_path.as_uri(), wait_until="load")
        page.evaluate("css => { const style = document.createElement('style'); style.textContent = css; document.head.append(style); }", MOBILE_CSS)
        page.evaluate("() => document.fonts.ready")
        metrics = layout_metrics(page)
        if metrics["scroll_width"] > metrics["width"] or metrics["overflow"]:
            raise ValueError(f"Content still overflows at {CONFIG['width']}px: {metrics}")

        output = source.with_suffix("")
        output.mkdir(exist_ok=True)
        paths = []
        for number, (y, height) in enumerate(slices(metrics["height"], limit), 1):
            png = page.screenshot(
                full_page=True, clip={"x": 0, "y": y, "width": CONFIG["width"], "height": height},
                animations="disabled", caret="hide", scale="device",
            )
            if len(png) > CONFIG["max_bytes"]:
                raise ValueError("PNG exceeds 10 MB; reduce screens_per_image in CONFIG")
            path = output / f"page-{number:03d}.png"
            path.write_bytes(png)
            paths.append(path)
        # These names belong to this tool; discard surplus pages on a shorter rerun.
        for old in output.glob("page-*.png"):
            if old not in paths and re.fullmatch(r"page-[0-9]{3,}\.png", old.name):
                old.unlink()
        return paths
    finally:
        context.close()


def main(args=None):
    args = sys.argv[1:] if args is None else args
    if not args:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        print("md2png: install playwright and its Chromium in an isolated environment first", file=sys.stderr)
        return 1
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(env=chromium_env())
            try:
                for arg in args:
                    for path in render(arg, browser):
                        print(path)
            finally:
                browser.close()
    except (Error, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"md2png: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
