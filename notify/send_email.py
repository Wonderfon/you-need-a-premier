"""Send one plain-text email to the owner via Gmail SMTP (App Password stored with DPAPI).

Usage: python send_email.py "<subject>" "<body>" [file ...]
Receipts (no body) go to notify/data/sent.jsonl.
"""
import json
import mimetypes
import re
import smtplib
import subprocess
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "control"))
import instance_config as ic

# ---- CONFIG ----
SENDER = ic.V["sender_email"]  # instance/instance.json; default = this machine today
RECIPIENT = ic.V["owner_email"]
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
SUBJECT_PREFIX = "[Org Core] "
MAX_ATTACH_MB = 18  # raw total; Gmail's 25 MB limit counts base64 overhead
# ----------------

ROOT = Path(__file__).resolve().parent


def app_password() -> str:
    out = subprocess.run(["pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
                          str(ROOT / "read-gmail-password.ps1")],
                         check=True, capture_output=True, text=True, encoding="utf-8")
    return out.stdout.replace(" ", "").strip()


CREDENTIAL_RE = re.compile(r"^\.env|\.dpapi$|^02_secrets$|\.pem$|\.key$|^id_rsa|^client_secret|token|secret|password", re.I)


def build_message(subject: str, body: str, files=()) -> EmailMessage:
    paths = [Path(f) for f in files]
    for p in paths:
        if not p.is_file():
            raise ValueError(f"attachment is not an existing file: {p}")
        if any(CREDENTIAL_RE.search(part) for part in p.parts):
            raise ValueError(f"attachment looks like a credential file, refused: {p}")
    if sum(p.stat().st_size for p in paths) > MAX_ATTACH_MB * 1024 * 1024:
        raise ValueError(f"attachments exceed {MAX_ATTACH_MB} MB in total; nothing was sent")
    msg = EmailMessage()
    msg["From"], msg["To"] = SENDER, RECIPIENT
    msg["Subject"] = SUBJECT_PREFIX + subject
    msg["Message-ID"] = make_msgid(domain="demo01-incore.local")
    msg.set_content(body)
    for p in paths:
        maintype, _, subtype = (mimetypes.guess_type(p.name)[0] or "application/octet-stream").partition("/")
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    return msg


def send(subject: str, body: str, files=()) -> str:
    msg = build_message(subject, body, files)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.login(SENDER, app_password())
        smtp.send_message(msg)
    receipt = {"sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "to": RECIPIENT,
               "subject": msg["Subject"], "message_id": msg["Message-ID"]}
    if files:
        receipt["attachments"] = [{"name": Path(f).name, "bytes": Path(f).stat().st_size} for f in files]
    (ROOT / "data").mkdir(exist_ok=True)
    with (ROOT / "data" / "sent.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(receipt, ensure_ascii=False) + "\n")
    return msg["Message-ID"]


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit('usage: send_email.py "<subject>" "<body>" [file ...]')
    print("sent", send(sys.argv[1], sys.argv[2], sys.argv[3:]))
