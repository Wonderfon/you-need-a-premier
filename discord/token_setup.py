"""One-time loopback page for saving a Discord token with Windows-user DPAPI."""
from __future__ import annotations

import hmac
import html
import os
import secrets
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent
TOKEN_PATH = ROOT / ".secrets" / "bot-token.dpapi"
MAX_BODY = 8192
TIMEOUT_SECONDS = 15 * 60

_DPAPI_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$Secret = [Console]::In.ReadToEnd()
$Secure = $null
$Ciphertext = $null
try {
    $Secure = ConvertTo-SecureString -String $Secret -AsPlainText -Force
    $Ciphertext = ConvertFrom-SecureString -SecureString $Secure
    [Console]::Out.Write($Ciphertext)
}
finally {
    if ($null -ne $Secure) { $Secure.Dispose() }
    $Secret = $null
    $Ciphertext = $null
}
"""


def encrypt_for_current_user(token: str) -> str:
    """Send plaintext only over a child process stdin pipe; return DPAPI ciphertext."""
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is available only on Windows")
    powershell = shutil.which("pwsh.exe")
    if powershell is None:
        raise RuntimeError("PowerShell 7 is required for local DPAPI encryption")
    secret = bytearray(token.encode("utf-8"))
    token = None
    process = subprocess.Popen(
        [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _DPAPI_SCRIPT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        stdout, _ = process.communicate(input=secret, timeout=30)
        ciphertext = stdout.decode("ascii", errors="strict")
        return_code = process.returncode
        if return_code != 0 or not ciphertext:
            raise RuntimeError("DPAPI encryption failed")
        return ciphertext
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError("DPAPI encryption failed") from None
    finally:
        secret[:] = b"\0" * len(secret)
        if process.poll() is None:
            process.kill()
            process.communicate()


def store_encrypted_token(token: str, destination: Path = TOKEN_PATH, encrypt=encrypt_for_current_user) -> None:
    if not token or any(character.isspace() for character in token):
        raise ValueError("Token must be nonempty and contain no whitespace")
    try:
        ciphertext = encrypt(token)
    finally:
        token = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(ciphertext, encoding="ascii")


def parse_form_token(raw_body: bytearray, csrf: str) -> str:
    fields = parse_qs(raw_body.decode("ascii"), keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"csrf", "token"} or any(len(values) != 1 for values in fields.values()):
        raise ValueError("Invalid local token setup form")
    if not hmac.compare_digest(fields["csrf"][0], csrf):
        raise ValueError("Invalid local token setup form")
    token = fields["token"][0]
    if not token or any(character.isspace() for character in token):
        raise ValueError("Invalid local token setup form")
    return token


def page(csrf: str) -> bytes:
    csrf_field = html.escape(csrf, quote=True)
    document = f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Discord 令牌本机设置</title>
<body><main><h1>Discord 机器人令牌设置</h1>
<p>此页面只会将令牌发送到本机。令牌将使用当前 Windows 用户的 DPAPI 加密并保存在本机。请勿将令牌发到聊天中；页面不会显示令牌内容。</p>
<form method="post" action="/save" autocomplete="off">
<input type="hidden" name="csrf" value="{csrf_field}">
<label for="token">机器人令牌</label><br>
<input id="token" name="token" type="password" required autocomplete="new-password" spellcheck="false">
<button type="submit">保存令牌</button>
<button type="reset">清空</button>
</form></main></body></html>"""
    return document.encode("utf-8")


def make_handler(csrf: str, host: str, origin: str, destination: Path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def respond(self, status: int, message: str) -> None:
            body = ("<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><title>令牌设置</title>"
                    f"<body><p>{html.escape(message)}</p></body></html>").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def send_security_headers(self) -> None:
            self.close_connection = True
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")

        def valid_host(self) -> bool:
            return self.headers.get("Host") == host

        def do_GET(self) -> None:
            if not self.valid_host() or self.path != "/":
                self.respond(400, "本机设置请求无效，已拒绝。")
                return
            body = page(csrf)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if not self.valid_host() or self.headers.get("Origin") != origin or self.path != "/save":
                self.respond(403, "本机设置请求无效，已拒绝。")
                return
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                self.respond(415, "本机设置请求无效，已拒绝。")
                return
            if self.headers.get("Transfer-Encoding") is not None:
                self.respond(400, "本机设置请求无效，已拒绝。")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.respond(400, "本机设置请求无效，已拒绝。")
                return
            if length <= 0 or length > MAX_BODY:
                self.respond(413, "本机设置请求无效，已拒绝。")
                return

            raw_body = bytearray()
            fields = None
            token = None
            try:
                raw_body.extend(self.rfile.read(length))
                if len(raw_body) != length:
                    self.respond(400, "本机设置请求无效，已拒绝。")
                    return
                try:
                    token = parse_form_token(raw_body, csrf)
                except (UnicodeDecodeError, ValueError):
                    self.respond(403, "本机设置请求无效，已拒绝。")
                    return
                store_encrypted_token(token, destination)
                token = None
                self.respond(200, "令牌已为当前 Windows 用户加密并保存在本机。现在可以关闭此页面。")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            except Exception:
                self.respond(500, "保存失败。令牌内容不会显示在此错误信息中。")
            finally:
                for index in range(len(raw_body)):
                    raw_body[index] = 0
                raw_body.clear()
                token = None

    return Handler


class LocalHTTPServer(HTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(20)
        return connection, address


def run() -> None:
    if os.name != "nt":
        raise SystemExit("This setup page stores a Windows-user DPAPI secret and runs only on Windows.")
    csrf = secrets.token_urlsafe(32)
    server = LocalHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    host = f"127.0.0.1:{port}"
    origin = f"http://{host}"
    server.RequestHandlerClass = make_handler(csrf, host, origin, TOKEN_PATH)
    timer = threading.Timer(TIMEOUT_SECONDS, server.shutdown)
    timer.daemon = True
    timer.start()
    print(f"{origin}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        timer.cancel()
        server.server_close()
        csrf = None


if __name__ == "__main__":
    run()
