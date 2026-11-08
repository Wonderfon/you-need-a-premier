from __future__ import annotations

import asyncio
import json
import re
import secrets
from html import escape
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from . import llm_postprocess
from . import asr_hours
from . import accumulation
from . import pending
try:  # personal panel: left out of the public export
    from . import poetry_panel
except ImportError:
    poetry_panel = None
from . import review
from . import i18n
from . import sprint, todo
from .dashboard import build_snapshot
from .store import InputError, InputStore, MAX_CHUNK_BYTES, MAX_SESSION_BYTES
import sys as _sys, pathlib as _pl  # noqa: E402
_sys.path.append(str(_pl.Path(__file__).resolve().parent.parent / "control"))
import instance_config as ic  # noqa: E402  (dashboard names; defaults = today's values)


HERE = Path(__file__).resolve().parent
INCORE_ROOT = HERE.parent
STATIC = HERE / "static"
CSRF_COOKIE = review.CSRF_COOKIE
MUTATIONS = {"POST", "PUT", "PATCH", "DELETE"}
STORE_KEY = web.AppKey("store", InputStore)
PORT_KEY = web.AppKey("expected_port", int)
HTTPS_HOST_KEY = web.AppKey("private_https_host", str)
OWNER_LOGIN_KEY = web.AppKey("owner_login", str)
DASHBOARD_ROOT_KEY = web.AppKey("dashboard_root", Path)
ACCUMULATION_KEY = web.AppKey("accumulation", accumulation.SnapshotCache)
PENDING_DIR_KEY = web.AppKey("pending_dir", Path)
VIA_HTTPS = review.VIA_HTTPS
# One MagicDNS machine name (machine.tailnet.ts.net); no wildcard, scheme, port or IP.
PRIVATE_HTTPS_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){2,}ts\.net")
# One Tailscale login name (name@example.com, name@github); no lists, spaces or wildcards.
OWNER_LOGIN = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}")
# Tailscale Serve replaces any inbound value with the authenticated requester; tagged devices get none.
IDENTITY_HEADER = "Tailscale-User-Login"
PRIVATE_CONFIG = "private-https.json"
PRIVATE_CONFIG_KEYS = {"https_host", "owner_login"}
ASR_QUEUE_KEY = web.AppKey("asr_queue", asyncio.Queue)
QUEUED_ASR_KEY = web.AppKey("queued_asr", set)
ASR_WORKER_KEY = web.AppKey("asr_worker", asyncio.Task)
ASR_PRELOAD_KEY = web.AppKey("asr_preload", asyncio.Task)
LLM_RUNNING_KEY = web.AppKey("llm_running", set)
LLM_ERRORS_KEY = web.AppKey("llm_errors", dict)
LLM_TASKS_KEY = web.AppKey("llm_tasks", set)


def _read_backup_json(path: Path, backup_root: Path, limit: int) -> dict[str, Any] | None:
    try:
        if path.is_symlink():
            return None
        resolved = path.resolve(strict=True)
        resolved.relative_to(backup_root.resolve())
        if not resolved.is_file() or resolved.stat().st_size > limit:
            return None
        with resolved.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _utc_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return None


def _latest_local_manifest(backup_root: Path) -> dict[str, str] | None:
    folder = backup_root / "snapshots"
    if folder.is_symlink() or not folder.is_dir():
        return None
    candidates = []
    try:
        for path in sorted(folder.glob("*.json"), key=lambda p: p.name, reverse=True)[:100]:
            if not re.fullmatch(r"\d{8}T\d{12}Z-[0-9a-f]{10}\.json", path.name):
                continue
            manifest = _read_backup_json(path, backup_root, 16 * 1024 * 1024)
            if not manifest or not isinstance(manifest.get("files"), list):
                continue
            snapshot_id = manifest.get("snapshot_id")
            created_at = _utc_timestamp(manifest.get("created_at"))
            if isinstance(snapshot_id, str) and path.stem == snapshot_id and created_at:
                candidates.append({"snapshot_id": snapshot_id, "verified_at_utc": created_at})
    except OSError:
        return None
    return max(candidates, key=lambda item: item["verified_at_utc"]) if candidates else None


def build_backup_status(backup_root: Path) -> dict[str, Any]:
    """Read backup receipts only; DriveFS staging is never treated as cloud receipt."""
    backup_root = Path(backup_root)
    status = _read_backup_json(backup_root / "status.json", backup_root, 1024 * 1024) or {}
    receipt = _read_backup_json(backup_root / "cloud-receipt.json", backup_root, 256 * 1024) or {}
    stage = status.get("stage")
    status_id = status.get("snapshot_id") if isinstance(status.get("snapshot_id"), str) else None
    local = None
    if stage in {"local_snapshot_verified", "staged_for_drive_sync"} and status_id:
        checked = _utc_timestamp(status.get("checked_at"))
        if checked:
            local = {"snapshot_id": status_id, "verified_at_utc": checked, "verified": True}
    if local is None:
        local = _latest_local_manifest(backup_root)
        if local:
            local["verified"] = False
    last_attempt_at = _utc_timestamp(status.get("checked_at"))
    staging_error = bool(status.get("staging_error"))
    capture = status.get("chat_capture")
    capture_failed = isinstance(capture, dict) and capture.get("status") == "failed"
    failed = stage == "backup_failed" or staging_error or capture_failed
    failure_kind = ("drive_staging" if staging_error else "local_backup" if stage == "backup_failed"
                    else "chat_capture" if capture_failed else None)

    receipt_snapshot = receipt.get("snapshot_id") if isinstance(receipt.get("snapshot_id"), str) else None
    receipt_time = _utc_timestamp(receipt.get("verified_at_utc"))
    zip_mode = status.get("archive_format") == "zip_incremental_v1"
    receipt_kind = "remote_zip_content_verified" if zip_mode else "remote_manifest_content_verified"
    valid_receipt = (
        receipt.get("verification") == receipt_kind
        and bool(receipt_snapshot)
        and bool(receipt_time)
    )
    matches_latest = bool(valid_receipt and local and receipt_snapshot == local["snapshot_id"]
                          and (not zip_mode or (status.get("package_sha256")
                               and receipt.get("package_sha256") == status["package_sha256"])))

    staged = (
        stage == "staged_for_drive_sync"
        and bool(status_id)
        and bool(_utc_timestamp(status.get("staged_at")))
        and (local is None or local["snapshot_id"] == status_id)
    )
    return {
        "local_snapshot_verified": bool(local and local["verified"]),
        "local_snapshot_recorded": local is not None,
        "local_snapshot_id": local["snapshot_id"] if local else None,
        "local_snapshot_at_utc": local["verified_at_utc"] if local else None,
        "drive_staged_for_sync": staged,
        "drive_staged_at_utc": _utc_timestamp(status.get("staged_at")) if staged else None,
        "drive_snapshot_id": status_id if staged else None,
        "daily_archive_waiting": status.get("daily_archive_waiting") is True,
        "last_attempt_failed": failed,
        "last_attempt_at_utc": last_attempt_at,
        "failure_kind": failure_kind,
        "remote_receipt_valid": valid_receipt,
        "remote_verified_at_utc": receipt_time if valid_receipt else None,
        "remote_snapshot_id": receipt_snapshot if valid_receipt else None,
        "remote_matches_latest_local": matches_latest,
    }


@web.middleware
async def trust_boundary(request: web.Request, handler):
    local_host = f"127.0.0.1:{request.app[PORT_KEY]}"
    private_host = request.app.get(HTTPS_HOST_KEY)
    host = request.headers.get("Host", "").lower()
    # Tailscale Serve terminates HTTPS and forwards to loopback keeping the original Host.
    # X-Forwarded-* headers are deliberately ignored; each Host pairs with exactly one Origin.
    if host == local_host:
        expected_origin = f"http://{local_host}"
    elif private_host and host == private_host:
        owner = request.app.get(OWNER_LOGIN_KEY)
        if not owner or request.headers.getall(IDENTITY_HEADER, []) != [owner]:
            raise web.HTTPForbidden(text="只接受已配置的 Tailscale 账号访问。")
        expected_origin = f"https://{private_host}"
        request[VIA_HTTPS] = True
    else:
        raise web.HTTPMisdirectedRequest(text="只接受本机输入页面。")
    origin = request.headers.get("Origin")
    # A sandboxed audit page (opaque origin, see review.PAGE_CSP) loading its own files: only GET, only under the
    # per-start key that other sites cannot know (browsers send no Referer from an opaque origin).
    key = request.app.get(review.PAGE_KEY)
    own_page = bool(key) and request.method == "GET" and request.path.startswith((f"/review/s/{key}/", f"/review/w/{key}/"))
    if origin is not None and origin != expected_origin and not (own_page and origin == "null"):
        raise web.HTTPForbidden(text="请求来源不匹配。")
    fetch_site = request.headers.get("Sec-Fetch-Site")
    # A person clicking a link (e.g. the checklist page opened from disk) to the input page itself: user-activated top-level GET of "/" only.
    clicked_link = (request.method == "GET" and request.path == "/" and request.headers.get("Sec-Fetch-Mode") == "navigate"
                    and request.headers.get("Sec-Fetch-Dest") == "document" and request.headers.get("Sec-Fetch-User") == "?1")
    if fetch_site not in (None, "same-origin", "none") and not own_page and not clicked_link:
        raise web.HTTPForbidden(text="跨站请求已拒绝。")
    if request.method in MUTATIONS:
        cookie = request.cookies.get(CSRF_COOKIE, "")
        header = request.headers.get("X-CSRF-Token", "")
        if origin != expected_origin or not cookie or not secrets.compare_digest(cookie, header):
            raise web.HTTPForbidden(text="页面已过期，请刷新后重试。")
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    )
    return response


@web.middleware
async def translate_json(request: web.Request, handler):
    """With language en, swap the fixed server labels in JSON answers (i18n.DATA_EN); zh passes through."""
    response = await handler(request)
    language = ic.get("language")
    if language != "zh" and isinstance(response, web.Response) and response.content_type == "application/json" and response.body:
        response.text = json.dumps(i18n.data(json.loads(response.body), language), ensure_ascii=False)
    return response


@web.middleware
async def safe_errors(request: web.Request, handler):
    try:
        return await handler(request)
    except InputError as exc:
        return web.json_response({"error": exc.message}, status=exc.status)
    except web.HTTPException:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "请求内容格式不正确。"}, status=400)
    except Exception:
        # Keep local transcript/audio content and exception details out of logs.
        return web.json_response({"error": "本地保存遇到问题；已收到的原始片段仍会保留。"}, status=500)


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError(400, "请求格式不正确。")
    return value


async def _body_json(request: web.Request) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise InputError(415, "请使用 JSON 请求格式。")
    try:
        return _json_object(await request.json())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise InputError(400, "请求内容格式不正确。") from None


def _store(request: web.Request) -> InputStore:
    return request.app[STORE_KEY]


def _enqueue(request: web.Request, session_id: str) -> None:
    queued: set[str] = request.app[QUEUED_ASR_KEY]
    if session_id not in queued:
        queued.add(session_id)
        request.app[ASR_QUEUE_KEY].put_nowait(session_id)


def localized_data(text: str) -> str:
    """One fixed server message in the instance language (i18n.DATA_EN)."""
    return i18n.data(text, ic.get("language"), "message")


def localized(text: str) -> str:
    """The page text in the instance language (instance.json: language). zh is the source language and passes through."""
    return i18n.page(text, ic.get("language"))


async def static_script(request: web.Request) -> web.Response:
    """app.js and rules.js carry fixed Chinese text too; serve them through the same switch."""
    path = STATIC / request.match_info["name"]
    return web.Response(text=localized(path.read_text(encoding="utf-8")), content_type="application/javascript", charset="utf-8")


async def index(request: web.Request) -> web.Response:
    # Keep other open tabs and long dictation sessions valid in this browser.
    token = request.cookies.get(CSRF_COOKIE, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        token = secrets.token_urlsafe(32)
    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__CSRF_TOKEN__", token)
    html = localized(html.replace("__ASR_HOURS__", "统计中…"))
    response = web.Response(text=html, content_type="text/html", charset="utf-8")
    response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="Strict", path="/",
                        secure=bool(request.get(VIA_HTTPS)))
    return response


async def list_sessions(request: web.Request) -> web.Response:
    return web.json_response({"sessions": _store(request).list_sessions()})


async def asr_hours_api(request: web.Request) -> web.Response:
    """Read-only trainable-hours line; computed off the event loop so the page never waits for it."""
    try:
        summary = await asyncio.to_thread(asr_hours.read_summary, _store(request))
    except Exception:
        return web.json_response({"text": localized_data("可训练语音小时数暂时读不出来（不影响录音）")}, status=503)
    return web.json_response({"text": asr_hours.summary_text(summary), **summary})


async def asr_status(request: web.Request) -> web.Response:
    try:
        from .transcribe import runtime_status
        return web.json_response(runtime_status())
    except Exception:
        return web.json_response({"available": False, "message": "本地 ASR 状态暂不可读取；原始音频仍可保存。"})


async def backup_status(request: web.Request) -> web.Response:
    return web.json_response(build_backup_status(INCORE_ROOT / "data" / "backups"))


def dashboard_labels(html: str) -> str:
    """Fill the instance-specific names (instance.json: coordinator_label, coordinator_note, role_names) and the optional poetry link."""
    names = json.dumps(ic.get("role_names"), ensure_ascii=False).replace("<", "\\u003c")
    return (html.replace("__POETRY_LINK__", '<a href="/poetry">' + localized("好诗集") + '</a><span class="sep">·</span>' if poetry_panel else "")
            .replace("__SECRETARY_HIDDEN__", "" if ic.get("secretary_card") else "hidden")
            .replace("__SECRETARY_MODEL__", escape(ic.get("secretary_model")))
            .replace("__SECRETARY_LANG__", "English" if ic.get("language") == "en" else "Chinese")
            .replace("__COORDINATOR_LABEL__", escape(ic.get("coordinator_label")))
            .replace("__COORDINATOR_NOTE__", escape(ic.get("coordinator_note")))
            .replace("__ROLE_NAMES__", names))


async def dashboard_page(request: web.Request) -> web.Response:
    nonce = secrets.token_urlsafe(16)
    html = (STATIC / "dashboard.html").read_text(encoding="utf-8").replace("__CSP_NONCE__", nonce)
    html = dashboard_labels(localized(html))
    response = web.Response(text=html, content_type="text/html", charset="utf-8")
    # Same policy as trust_boundary, except the single inline style/script is allowed by this nonce.
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    )
    return response


async def organization_status(request: web.Request) -> web.Response:
    # Read-only: no models, probes or dispatch, and no writes to operational data.
    snapshot = await asyncio.to_thread(build_snapshot, request.app[DASHBOARD_ROOT_KEY])
    snapshot["review"] = await asyncio.to_thread(review.counts, request.app[DASHBOARD_ROOT_KEY])  # card on /dashboard
    return web.json_response(snapshot)


async def accumulation_status(request: web.Request) -> web.Response:
    return web.json_response(await request.app[ACCUMULATION_KEY].get())


async def pending_page(request: web.Request) -> web.Response:
    return web.Response(status=302, headers={"Location": "/todo"})  # the old 等你决定 page; its /api/pending routes stay


async def todo_page(request: web.Request) -> web.Response:
    nonce = secrets.token_urlsafe(16)
    token = request.cookies.get(CSRF_COOKIE, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        token = secrets.token_urlsafe(32)
    html = (STATIC / "todo.html").read_text(encoding="utf-8").replace("__CSP_NONCE__", nonce).replace("__CSRF_TOKEN__", token)
    html = localized(html)
    response = web.Response(text=html, content_type="text/html", charset="utf-8")
    response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="Strict", path="/",
                        secure=bool(request.get(VIA_HTTPS)))
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    )
    return response


async def sprint_page(request: web.Request) -> web.Response:
    nonce = secrets.token_urlsafe(16)
    html = (STATIC / "sprint.html").read_text(encoding="utf-8").replace("__CSP_NONCE__", nonce)
    response = web.Response(text=html, content_type="text/html", charset="utf-8")
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    )
    return response


async def sprint_status(request: web.Request) -> web.Response:
    # Read-only: backlog.md, runs.jsonl and the backlog-history copies are read, nothing is written.
    return web.json_response(await asyncio.to_thread(sprint.snapshot, request.app[DASHBOARD_ROOT_KEY]))


def _pending_snapshot(root: Path, data_dir: Path, key: str, wt: Path) -> dict[str, Any]:
    snapshot = pending.snapshot(root, data_dir)
    for item in snapshot["pending"]["items"] + snapshot["results"]:
        item["links"] = [review.audit_href(root, c, key, wt) for c in item["commands"]]  # same order; None = no link
    return snapshot


def _todo_snapshot(root: Path, data_dir: Path, key: str, wt: Path) -> dict[str, Any]:
    snapshot = todo.snapshot(root, data_dir)
    for item in snapshot["cards"] + snapshot["waiting"] + snapshot["recent"]:
        item["command_hrefs"] = [review.audit_href(root, c, key, wt) for c in item["commands"]]  # same order; None = no link
    return snapshot


async def todo_status(request: web.Request) -> web.Response:
    snapshot = await asyncio.to_thread(_todo_snapshot, request.app[DASHBOARD_ROOT_KEY], request.app[PENDING_DIR_KEY],
                                       request.app[review.PAGE_KEY], request.app[review.WT_KEY])
    return web.json_response(snapshot)


async def todo_act(request: web.Request) -> web.Response:
    body = await _body_json(request)
    if not all(isinstance(body.get(k, ""), str) for k in ("code", "action", "option", "note")):
        raise InputError(400, "请求格式不正确。")
    try:
        result = await asyncio.to_thread(todo.act, request.app[DASHBOARD_ROOT_KEY], request.app[PENDING_DIR_KEY],
                                         body.get("code", ""), body.get("action", ""), body.get("option", ""), body.get("note", ""))
    except ValueError as exc:
        raise InputError(400, str(exc)) from None
    except LookupError as exc:
        raise InputError(409, str(exc)) from None
    return web.json_response(result)


async def todo_batch(request: web.Request) -> web.Response:
    body = await _body_json(request)
    try:
        result = await asyncio.to_thread(todo.act_batch, request.app[DASHBOARD_ROOT_KEY], request.app[PENDING_DIR_KEY],
                                         body.get("codes"), body.get("action"))
    except ValueError as exc:
        raise InputError(400, str(exc)) from None
    return web.json_response(result)


async def pending_status(request: web.Request) -> web.Response:
    # Reads backlog.md fresh on every call; never writes it.
    snapshot = await asyncio.to_thread(_pending_snapshot, request.app[DASHBOARD_ROOT_KEY], request.app[PENDING_DIR_KEY],
                                       request.app[review.PAGE_KEY], request.app[review.WT_KEY])
    return web.json_response(snapshot)


async def _pending_write(request: web.Request, save, key: str) -> web.Response:
    body = await _body_json(request)
    try:
        record = await asyncio.to_thread(save, request.app[DASHBOARD_ROOT_KEY], request.app[PENDING_DIR_KEY],
                                         str(body.get("id", "")), body.get(key, ""), body.get("note", ""))
    except ValueError as exc:
        raise InputError(400, str(exc)) from None
    except LookupError as exc:
        raise InputError(409, str(exc)) from None
    return web.json_response(record)


async def pending_reply(request: web.Request) -> web.Response:
    return await _pending_write(request, pending.save_reply, "option")


async def pending_rating(request: web.Request) -> web.Response:
    return await _pending_write(request, pending.save_rating, "verdict")


async def poetry_page(request: web.Request) -> web.Response:
    # Same token/CSP handling as pending_page; data lives in data/poetry-collection/ (outside git, backed up).
    nonce = secrets.token_urlsafe(16)
    token = request.cookies.get(CSRF_COOKIE, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        token = secrets.token_urlsafe(32)
    response = web.Response(text=poetry_panel.page(nonce, token), content_type="text/html", charset="utf-8")
    response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="Strict", path="/",
                        secure=bool(request.get(VIA_HTTPS)))
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src 'self' data: blob:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    )
    return response


async def poetry_font(request: web.Request) -> web.StreamResponse:
    # Card fonts (OFL) from incore/data/fonts/, not in git; missing file -> 404 and the page falls back to system fonts.
    entry = poetry_panel.FONTS.get(request.match_info["key"])
    path = entry and request.app[DASHBOARD_ROOT_KEY] / "data" / "fonts" / entry[1]
    if not path or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Type": "font/ttf", "Cache-Control": "public, max-age=604800"})


async def poetry_api(request: web.Request) -> web.Response:
    body = b""
    if request.method != "GET":
        if request.content_type != "application/json":
            raise InputError(415, "请使用 JSON 请求格式。")
        body = await request.read()
    data = request.app[DASHBOARD_ROOT_KEY] / "data" / "poetry-collection" / "poems.json"
    status, ctype, payload = await asyncio.to_thread(poetry_panel.handle, request.method, request.path, body, data)
    return web.Response(body=payload, status=status, content_type=ctype.split(";")[0], charset="utf-8")

async def create_audio(request: web.Request) -> web.Response:
    body = await _body_json(request)
    row = _store(request).create_audio_session(
        str(body.get("source", "")),
        str(body.get("title", "")),
        str(body.get("mime_type", "")),
        str(body.get("filename", "")),
    )
    return web.json_response({"session": row}, status=201)


async def create_text(request: web.Request) -> web.Response:
    body = await _body_json(request)
    session, revision = _store(request).create_text_session(str(body.get("title", "")), str(body.get("text", "")))
    return web.json_response({"session": session, "revision": revision}, status=201)


async def get_session(request: web.Request) -> web.Response:
    return web.json_response({"session": _store(request).session_detail(request.match_info["session_id"])})


async def put_chunk(request: web.Request) -> web.Response:
    if request.content_type != "application/octet-stream":
        raise InputError(415, "音频片段格式不正确。")
    length = request.content_length
    if length is not None and (length <= 0 or length > MAX_CHUNK_BYTES):
        raise InputError(413, "音频片段为空或超过 8 MiB。")
    data = await request.read()
    result = await asyncio.to_thread(
        _store(request).put_chunk, request.match_info["session_id"], int(request.match_info["seq"]), data,
        request.headers.get("X-Chunk-SHA256", ""),
    )
    return web.json_response(result)


async def finalize_audio(request: web.Request) -> web.Response:
    body = await _body_json(request)
    result = await asyncio.to_thread(
        _store(request).finalize_audio, request.match_info["session_id"], body.get("chunk_count"), body.get("total_bytes")
    )
    if result["status"] == "queued":
        _enqueue(request, result["id"])
    return web.json_response({"session": result})


async def correct_revision(request: web.Request) -> web.Response:
    body = await _body_json(request)
    correction_type = body.get("correction_type", "recognition_correction")
    # "draft": autosaved edit, no purpose yet; the purpose is set when the user confirms.
    if correction_type not in {"draft", "recognition_correction", "user_rewrite", "other"}:
        raise InputError(400, "修订类型无效。")
    revision = _store(request).append_revision(
        request.match_info["session_id"], "correction", str(body.get("text", "")),
        str(body.get("parent_revision_id", "")), "user_correction", {"correction_type": correction_type},
    )
    return web.json_response({"revision": revision}, status=201)


async def create_instruction(request: web.Request) -> web.Response:
    body = await _body_json(request)
    revision = _store(request).append_revision(
        request.match_info["session_id"], "instruction", str(body.get("text", "")),
        str(body.get("parent_revision_id", "")), "explicit_user_instruction",
    )
    return web.json_response({"revision": revision}, status=201)


async def verify_revision(request: web.Request) -> web.Response:
    body = await _body_json(request)
    verification = _store(request).verify_revision(
        request.match_info["session_id"], str(body.get("revision_id", "")), str(body.get("original_sha256", "")),
    )
    return web.json_response({"verification": verification}, status=201)


def _llm_status(app: web.Application) -> dict[str, Any]:
    try:
        settings = llm_postprocess.load_settings(app[STORE_KEY].root)
        return {"enabled": settings["enabled"], "auto": settings["auto"], "model": settings["model"],
                "running": sorted(app[LLM_RUNNING_KEY]), "errors": dict(app[LLM_ERRORS_KEY])}
    except llm_postprocess.LLMError as exc:
        return {"enabled": False, "auto": False, "model": "", "running": [], "errors": {}, "config_error": str(exc)}


async def _llm_run(app: web.Application, sid: str, revision_id: str | None = None) -> dict[str, Any]:
    running: set[str] = app[LLM_RUNNING_KEY]
    if sid in running:
        raise InputError(409, "这条记录正在润色。")
    running.add(sid)
    try:
        revision = await asyncio.to_thread(llm_postprocess.run, app[STORE_KEY], sid, None, revision_id)
        app[LLM_ERRORS_KEY].pop(sid, None)
        return revision
    except InputError as exc:
        app[LLM_ERRORS_KEY][sid] = exc.message
        raise
    finally:
        running.discard(sid)


async def llm_status(request: web.Request) -> web.Response:
    return web.json_response(_llm_status(request.app))


async def llm_set_auto(request: web.Request) -> web.Response:
    body = await _body_json(request)
    try:
        await asyncio.to_thread(llm_postprocess.set_auto, _store(request).root, body.get("auto") is True)
    except llm_postprocess.LLMError as exc:
        raise InputError(500, str(exc)) from None
    return web.json_response(_llm_status(request.app))


async def llm_postprocess_session(request: web.Request) -> web.Response:
    # Old callers send no body: then the source is the rules version of the transcript. The page sends the revision the editor shows.
    body = await _body_json(request) if request.can_read_body and request.content_type == "application/json" else {}
    revision = await _llm_run(request.app, request.match_info["session_id"], str(body.get("revision_id") or "") or None)
    return web.json_response({"revision": revision}, status=201)


async def _llm_auto(app: web.Application, sid: str) -> None:
    try:
        await _llm_run(app, sid)
    except InputError:
        pass  # kept in app[LLM_ERRORS_KEY]; the page shows it


async def retry_asr(request: web.Request) -> web.Response:
    sid = request.match_info["session_id"]
    _store(request).retry_transcription(sid)
    _enqueue(request, sid)
    return web.json_response({"session": _store(request).get_session(sid)}, status=202)


def _rules_view(store: InputStore) -> dict[str, Any]:
    from . import rules, suggest
    return {**rules.snapshot(store.rules_path), **suggest.recommendations(store, store.rules_path)}


async def _rules_call(request: web.Request, fn, *args) -> dict[str, Any]:
    from .rules import RuleError
    try:
        await asyncio.to_thread(fn, *args)
    except RuleError as exc:
        raise InputError(exc.status, exc.message) from None
    return await asyncio.to_thread(_rules_view, _store(request))


async def get_rules(request: web.Request) -> web.Response:
    return web.json_response(await asyncio.to_thread(_rules_view, _store(request)))


async def accept_rule(request: web.Request) -> web.Response:
    from . import suggest
    body, store = await _body_json(request), _store(request)
    return web.json_response(await _rules_call(request, suggest.accept, store, store.rules_path, str(body.get("from", "")), str(body.get("to", "")), body.get("near") is True))


async def add_rule(request: web.Request) -> web.Response:
    from . import rules, suggest
    body, store = await _body_json(request), _store(request)
    try:
        heard, corrected = rules._clean(body.get("from", ""), body.get("to", ""))
    except rules.RuleError as exc:
        raise InputError(exc.status, exc.message) from None
    texts, _ = await asyncio.to_thread(suggest._texts, store)
    hits, false, checked = suggest.check(texts, heard, corrected)
    view = await _rules_call(request, rules.add_rule, store.rules_path, heard, corrected, "manual")
    return web.json_response({**view, "added": {"hits": hits, "false_hits": false, "checked": checked}}, status=201)


async def ignore_rule(request: web.Request) -> web.Response:
    from . import rules
    body, store = await _body_json(request), _store(request)
    return web.json_response(await _rules_call(request, rules.ignore, store.rules_path, str(body.get("from", "")), str(body.get("to", ""))))


async def delete_rule(request: web.Request) -> web.Response:
    from . import rules
    store = _store(request)
    return web.json_response(await _rules_call(request, rules.delete_rule, store.rules_path, request.match_info["rule_id"]))


async def toggle_builtin(request: web.Request) -> web.Response:
    from . import rules
    body, store = await _body_json(request), _store(request)
    return web.json_response(await _rules_call(request, rules.set_builtin, store.rules_path, request.match_info["rule_id"], body.get("enabled") is True))


async def undo_rule_action(request: web.Request) -> web.Response:
    from . import rules
    store = _store(request)
    return web.json_response(await _rules_call(request, rules.undo, store.rules_path, request.match_info["log_id"]))


async def audio_file(request: web.Request) -> web.StreamResponse:
    path = _store(request).get_session_path(request.match_info["session_id"])
    response = web.FileResponse(path)
    disposition = "attachment" if request.query.get("download") == "1" else "inline"
    response.headers["Content-Disposition"] = f'{disposition}; filename="{path.name}"'
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


async def export_revision(request: web.Request) -> web.Response:
    revision = _store(request).get_revision(request.match_info["revision_id"])
    response = web.Response(body=revision["content"].encode("utf-8"), content_type="text/plain", charset="utf-8")
    response.headers["Content-Disposition"] = f"attachment; filename=revision-{revision['id']}.txt"
    return response


async def export_instructions(request: web.Request) -> web.Response:
    response = web.json_response({"schema_version": 1, "instructions": _store(request).list_instructions()})
    response.headers["Content-Disposition"] = "attachment; filename=explicit-instructions.json"
    return response


async def transcribe_queue(app: web.Application) -> None:
    queue: asyncio.Queue[str] = app[ASR_QUEUE_KEY]
    store: InputStore = app[STORE_KEY]
    while True:
        sid = await queue.get()
        try:
            store.set_status(sid, "transcribing")
            try:
                from .transcribe import transcribe
            except ModuleNotFoundError as exc:
                if exc.name == f"{__package__}.transcribe":
                    store.set_status(sid, "transcription_unavailable", "ASR 尚未连接；原始音频已保存。")
                    continue
                store.set_status(sid, "transcription_failed", "ASR 运行环境暂不可用；原始音频已保存。")
                continue
            except Exception:
                store.set_status(sid, "transcription_failed", "ASR 适配器无法加载；原始音频已保存。")
                continue
            try:
                audio_path = store.get_session_path(sid).resolve(strict=True)
                audio_path.relative_to(store.root.resolve(strict=True))
                result = await asyncio.to_thread(transcribe, audio_path)
                if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                    raise ValueError("ASR adapter must return a dictionary with a text string")
                store.append_asr_result(sid, result)
                store.set_status(sid, "transcribed")
                try:
                    settings = llm_postprocess.load_settings(store.root)
                except llm_postprocess.LLMError:
                    settings = None
                if settings and settings["enabled"] and settings["auto"]:
                    task = asyncio.create_task(_llm_auto(app, sid))
                    app[LLM_TASKS_KEY].add(task)
                    task.add_done_callback(app[LLM_TASKS_KEY].discard)
            except Exception:
                store.set_status(sid, "transcription_failed", "转写失败；完整原始音频仍已保存，可重试。")
        finally:
            app[QUEUED_ASR_KEY].discard(sid)
            queue.task_done()


async def preload_nano() -> None:
    from .nano import preload
    try:
        await asyncio.to_thread(preload)
    except Exception:
        pass  # Status endpoint exposes failed loading; originals still accept/retry.


async def startup(app: web.Application) -> None:
    store: InputStore = app[STORE_KEY]
    await asyncio.to_thread(llm_postprocess.cleanup_orphan_server, store.root)
    app[ASR_QUEUE_KEY] = asyncio.Queue()
    app[QUEUED_ASR_KEY] = set()
    app[LLM_RUNNING_KEY], app[LLM_ERRORS_KEY], app[LLM_TASKS_KEY] = set(), {}, set()
    for sid in store.pending_transcriptions():
        _enqueue_from_startup(app, sid)
    if store.root.resolve() == (HERE / "data").resolve():
        app[ASR_PRELOAD_KEY] = asyncio.create_task(preload_nano())
    app[ASR_WORKER_KEY] = asyncio.create_task(transcribe_queue(app))


def _enqueue_from_startup(app: web.Application, sid: str) -> None:
    app[QUEUED_ASR_KEY].add(sid)
    app[ASR_QUEUE_KEY].put_nowait(sid)


async def cleanup(app: web.Application) -> None:
    preload = app.get(ASR_PRELOAD_KEY)
    if preload:
        from .nano import shutdown
        shutdown()
        preload.cancel()
        await asyncio.gather(preload, return_exceptions=True)
    task = app.get(ASR_WORKER_KEY)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def load_private_config(data_root: Path, env_host: str | None = None) -> dict[str, str] | None:
    """Read <data>/private-https.json; absent keeps localhost-only, anything unclear refuses to start."""
    path = Path(data_root) / PRIVATE_CONFIG
    env_host = (env_host or "").strip().lower() or None
    if not path.exists() and not path.is_symlink():
        if env_host:
            raise ValueError(f"INCORE_INPUT_HTTPS_HOST also needs {PRIVATE_CONFIG} with owner_login")
        return None
    try:
        readable = not path.is_symlink() and path.is_file() and path.stat().st_size <= 4096
        value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_keys) if readable else None
    except (OSError, ValueError):
        value = None
    if (not isinstance(value, dict) or set(value) != PRIVATE_CONFIG_KEYS
            or not all(isinstance(item, str) for item in value.values())):
        raise ValueError(f"{PRIVATE_CONFIG} must be one JSON object with exactly https_host and owner_login")
    if env_host and env_host != value["https_host"].lower():
        raise ValueError(f"INCORE_INPUT_HTTPS_HOST conflicts with {PRIVATE_CONFIG}")
    return value


def create_app(data_root: Path | None = None, expected_port: int = 8765,
               https_host: str | None = None, owner_login: str | None = None,
               dashboard_root: Path | None = None, pending_dir: Path | None = None,
               wt_root: Path | None = None) -> web.Application:
    if (https_host is None) != (owner_login is None):
        raise ValueError("private HTTPS needs both the exact host and one owner login")
    if https_host is not None:
        https_host = str(https_host).lower()
        if len(https_host) > 253 or not PRIVATE_HTTPS_HOST.fullmatch(https_host):
            raise ValueError("private HTTPS host must be one MagicDNS name like machine.tailnet.ts.net")
        if not isinstance(owner_login, str) or not OWNER_LOGIN.fullmatch(owner_login):
            raise ValueError("owner login must be one Tailscale login like name@example.com")
    root = data_root if data_root is not None else HERE / "data"
    app = web.Application(
        client_max_size=MAX_CHUNK_BYTES,
        middlewares=[safe_errors, trust_boundary, translate_json],
    )
    app[STORE_KEY] = InputStore(root)
    app[PORT_KEY] = expected_port
    app[DASHBOARD_ROOT_KEY] = Path(dashboard_root) if dashboard_root is not None else INCORE_ROOT
    app[ACCUMULATION_KEY] = accumulation.SnapshotCache(app[DASHBOARD_ROOT_KEY])
    app[PENDING_DIR_KEY] = Path(pending_dir) if pending_dir is not None else app[DASHBOARD_ROOT_KEY] / "data"
    if https_host is not None:
        app[HTTPS_HOST_KEY] = https_host
        app[OWNER_LOGIN_KEY] = owner_login
    app.router.add_get("/", index)
    app.router.add_get("/api/sessions", list_sessions)
    app.router.add_get("/api/asr", asr_status)
    app.router.add_get("/api/asr-hours", asr_hours_api)
    app.router.add_get("/api/backup", backup_status)
    app.router.add_get("/dashboard", dashboard_page)
    app.router.add_get("/api/organization", organization_status)
    app.router.add_get("/api/accumulation", accumulation_status)
    app.router.add_get("/pending", pending_page)
    app.router.add_get("/sprint", sprint_page)
    app.router.add_get("/api/sprint", sprint_status)
    app.router.add_get("/todo", todo_page)
    app.router.add_get("/api/todo", todo_status)
    app.router.add_post("/api/todo/act", todo_act)
    app.router.add_post("/api/todo/batch", todo_batch)
    app.router.add_get("/api/pending", pending_status)
    app.router.add_post("/api/pending/reply", pending_reply)
    app.router.add_post("/api/pending/rating", pending_rating)
    if poetry_panel:
        app.router.add_get("/poetry", poetry_page)
        app.router.add_get("/poetry/fonts/{key}", poetry_font)
        app.router.add_get("/api/poetry", poetry_api)
        app.router.add_post("/api/poetry", poetry_api)
        app.router.add_patch("/api/poetry/{poem_id}", poetry_api)
    review.add_routes(app, app[DASHBOARD_ROOT_KEY], wt_root)
    app.router.add_post("/api/sessions", create_audio)
    app.router.add_post("/api/text", create_text)
    app.router.add_get("/api/sessions/{session_id}", get_session)
    app.router.add_put("/api/sessions/{session_id}/chunks/{seq:\\d+}", put_chunk)
    app.router.add_post("/api/sessions/{session_id}/finalize", finalize_audio)
    app.router.add_post("/api/sessions/{session_id}/corrections", correct_revision)
    app.router.add_post("/api/sessions/{session_id}/instructions", create_instruction)
    app.router.add_post("/api/sessions/{session_id}/verifications", verify_revision)
    app.router.add_post("/api/sessions/{session_id}/retry-asr", retry_asr)
    app.router.add_get("/api/llm-postprocess", llm_status)
    app.router.add_post("/api/llm-postprocess/auto", llm_set_auto)
    app.router.add_post("/api/sessions/{session_id}/llm-postprocess", llm_postprocess_session)
    app.router.add_get("/api/sessions/{session_id}/audio", audio_file)
    app.router.add_get("/api/rules", get_rules)
    app.router.add_post("/api/rules/accept", accept_rule)
    app.router.add_post("/api/rules/add", add_rule)
    app.router.add_post("/api/rules/ignore", ignore_rule)
    app.router.add_delete("/api/rules/owner/{rule_id}", delete_rule)
    app.router.add_post("/api/rules/builtin/{rule_id}/enabled", toggle_builtin)
    app.router.add_post("/api/rules/log/{log_id}/undo", undo_rule_action)
    app.router.add_get("/api/revisions/{revision_id}/export.txt", export_revision)
    app.router.add_get("/api/instructions.json", export_instructions)
    app.router.add_get("/static/{name:(app|rules)\\.js}", static_script)
    app.router.add_static("/static/", STATIC, show_index=False)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def record_process(path: Path) -> None:
    """On Windows the venv python.exe is a launcher and this process is its child: record the one that holds the port."""
    import os

    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        record = {}
    record.update(ProcessId=os.getpid(), LauncherProcessId=os.getppid(), Component="input")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    import os

    port = int(os.environ.get("INCORE_INPUT_PORT", "8765"))
    # No config file keeps localhost-only; the listener stays on loopback either way.
    config = load_private_config(HERE / "data", os.environ.get("INCORE_INPUT_HTTPS_HOST"))
    def listening(message: str) -> None:  # run_app calls this once the port is bound
        print(message)
        record_process(HERE / "data" / "server-process.json")

    web.run_app(create_app(expected_port=port, **(config or {})),
                host="127.0.0.1", port=port, access_log=None, print=listening)


if __name__ == "__main__":
    main()
