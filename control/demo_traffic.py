"""Summarize Caddy JSON access logs by site and local calendar day."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


# CONFIG
SITE_DOMAIN_SUFFIX = ".lab.beaclis.com"
BOT_UA_KEYWORDS = (
    "bot", "crawl", "spider", "slurp", "curl", "wget", "python", "headless",
    "scrapy", "httpclient", "go-http-client",
)
OWN_MONITOR_UA_KEYWORDS = ("incore-dashboard", "prometheus", "uptime-kuma")
HEALTH_PATHS = frozenset(("/health", "/healthz", "/ready", "/readyz", "/live", "/livez", "/ping"))
STATIC_PATH_PREFIXES = ("/assets/", "/static/", "/u/")
STATIC_EXTENSIONS = frozenset((
    ".avif", ".css", ".eot", ".gif", ".ico", ".jpeg", ".jpg", ".js", ".json",
    ".map", ".mjs", ".mp3", ".mp4", ".pdf", ".png", ".svg", ".txt", ".ttf",
    ".wasm", ".webmanifest", ".webm", ".webp", ".woff", ".woff2", ".xml",
))


def _header(headers: object, name: str) -> str:
    if not isinstance(headers, dict):
        return ""
    value = next((v for k, v in headers.items() if k.casefold() == name.casefold()), "")
    return str(value[0] if isinstance(value, list) and value else value or "")


def _host(value: object) -> str:
    raw = str(value or "").strip()
    try:
        return (urlsplit("//" + raw).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def _site(host: str) -> str:
    if host.endswith(SITE_DOMAIN_SUFFIX):
        return host[:-len(SITE_DOMAIN_SUFFIX)]
    return host


def _referrer_page(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme and parsed.hostname:
            hostname = parsed.hostname.casefold()
            if ":" in hostname:
                hostname = f"[{hostname}]"
            netloc = hostname + (f":{parsed.port}" if parsed.port else "")
            page = f"{parsed.scheme.casefold()}://{netloc}{parsed.path or '/'}"
            return page, _site(parsed.hostname.casefold().rstrip("."))
        if parsed.path.startswith("/"):
            return parsed.path, ""
    except ValueError:
        pass
    return None


def _is_static(path: str) -> bool:
    folded = path.casefold()
    return folded.startswith(STATIC_PATH_PREFIXES) or Path(folded).suffix in STATIC_EXTENSIONS


def _is_health(path: str) -> bool:
    folded = path.casefold().rstrip("/") or "/"
    return folded in HEALTH_PATHS or any(folded.startswith(prefix + "/") for prefix in HEALTH_PATHS)


def _is_html_page(record: dict, method: str) -> bool:
    if method != "GET" or not 200 <= int(record.get("status", 0)) < 300:
        return False
    headers = record.get("resp_headers", {})
    content_type = _header(headers, "Content-Type").split(";", 1)[0].strip().casefold()
    return content_type in ("text/html", "application/xhtml+xml")


def summarize(lines) -> list[dict]:
    """Aggregate Caddy's newline-delimited JSON access records without retaining IPs."""
    groups: dict[tuple[str, str], dict] = {}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            request = record["request"]
            timestamp = float(record["ts"])
            method = str(request["method"]).upper()
            uri = str(request["uri"])
            host = _host(request.get("host"))
            if not isinstance(request, dict) or not host or not method:
                raise ValueError
            day = datetime.fromtimestamp(timestamp).date().isoformat()
        except (KeyError, TypeError, ValueError, OverflowError, OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid Caddy access record on line {number}") from exc

        path = urlsplit(uri).path or "/"
        headers = request.get("headers", {})
        user_agent = _header(headers, "User-Agent")
        ua = user_agent.casefold()
        if (any(word in ua for word in BOT_UA_KEYWORDS + OWN_MONITOR_UA_KEYWORDS)
                or _is_health(path) or _is_static(path)):
            continue

        site = _site(host)
        key = (site, day)
        bucket = groups.setdefault(key, {"requests": 0, "page_views": 0, "visitors": set(), "referrers": Counter()})
        bucket["requests"] += 1
        if not _is_html_page(record, method):
            continue

        bucket["page_views"] += 1
        ip = str(request.get("client_ip") or request.get("remote_ip") or "")
        bucket["visitors"].add(hashlib.sha256((ip + "\0" + user_agent).encode("utf-8")).hexdigest())
        referrer = _referrer_page(_header(headers, "Referer"))
        if referrer and referrer[1] != site:
            bucket["referrers"][referrer[0]] += 1

    return [
        {
            "site": site,
            "date": day,
            "requests": bucket["requests"],
            "page_views": bucket["page_views"],
            "unique_visitors": len(bucket["visitors"]),
            "top_referrers": [
                {"page": page, "visits": visits}
                for page, visits in sorted(bucket["referrers"].items(), key=lambda item: (-item[1], item[0]))[:5]
            ],
        }
        for (site, day), bucket in sorted(groups.items())
    ]


def _lines(paths: list[str]):
    if not paths or "-" in paths:
        if len(paths) > 1:
            raise ValueError("stdin '-' must be the only input")
        yield from sys.stdin
        return
    for name in paths:
        with Path(name).open("r", encoding="utf-8") as stream:
            yield from stream


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="Caddy JSON log files; read stdin when omitted or when given '-'")
    args = parser.parse_args(argv)
    try:
        report = summarize(_lines(args.files))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
