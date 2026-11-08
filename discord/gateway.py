"""Allowlisted Discord owner channel with durable direct deputy replies."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
TOKEN_SCRIPT = ROOT / "read-token.ps1"
MAX_UPLOAD_MB = 10  # Discord's default bot upload limit on an unboosted server
MAX_FILES = 10
MAX_DOWNLOAD_MB = 100  # inbound attachments larger than this are skipped
ATTACH_DIR = ROOT / "data" / "attachments"
HOLD_MAX_S, HOLD_POLL_S = 15 * 60, 5  # a proactive send waits at most this long for the owner's queued reply in its channel
def notice_tag(line: str) -> str:
    return f"〔{line}主动通知，不是回复你刚才的消息〕"  # first line of a proactive send that goes out while the owner is waiting
CREDENTIAL_RE = re.compile(r"^\.env|\.dpapi$|^02_secrets$|\.pem$|\.key$|^id_rsa|^client_secret|token|secret|password", re.I)


def load_config() -> dict[str, Any]:
    path = CONFIG_PATH
    if not path.exists():
        raise RuntimeError("config.json is missing; copy config.example.json and fill in IDs")
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config.get("channels"), dict):
        raise RuntimeError("config.channels must be an object of role names to channel IDs")
    for key in ("owner_user_id", "guild_id"):
        if not valid_id(config.get(key)):
            raise RuntimeError(f"config.{key} must be a Discord ID")
    channels = {str(role): str(cid) for role, cid in config["channels"].items() if valid_id(cid)}
    if not channels:
        raise RuntimeError("At least one configured channel ID is required")
    if len(set(channels.values())) != len(channels):  # inbound routing takes the first role of an ID: two roles on one channel would lose one
        raise RuntimeError("config.channels: two roles share one channel ID")
    config["channels"] = channels
    return config


def valid_id(value: Any) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool) and str(value).isdigit() and int(value) > 0


def is_allowed(config: dict[str, Any], *, guild_id: Any, channel_id: Any, user_id: Any) -> bool:
    return (
        str(guild_id) == str(config["guild_id"])
        and str(user_id) == str(config["owner_user_id"])
        and str(channel_id) in config["channels"].values()
    )


def connect(config: dict[str, Any], db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else ROOT / "data/discord.sqlite3"
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS inbound (
            message_id TEXT PRIMARY KEY, source TEXT NOT NULL, received_at TEXT NOT NULL,
            guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, author_id TEXT NOT NULL,
            role TEXT NOT NULL, exact_text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS receipts (
            receipt_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, channel_role TEXT NOT NULL,
            channel_id TEXT NOT NULL, message_id TEXT, status TEXT NOT NULL, detail TEXT NOT NULL
        );
    """)
    db.execute("INSERT OR IGNORE INTO state(key,value,updated_at) VALUES('paused','1',?)", (utcnow(),))
    db.commit()
    return db


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_inbound(db: sqlite3.Connection, *, message_id: Any, source: str, received_at: str,
                   guild_id: Any, channel_id: Any, author_id: Any, role: str, exact_text: str) -> bool:
    cur = db.execute(
        "INSERT OR IGNORE INTO inbound VALUES(?,?,?,?,?,?,?,?)",
        (str(message_id), source, received_at, str(guild_id), str(channel_id), str(author_id), role, exact_text),
    )
    db.commit()
    return cur.rowcount == 1


def set_paused(db: sqlite3.Connection, paused: bool) -> None:
    db.execute("UPDATE state SET value=?,updated_at=? WHERE key='paused'", ("1" if paused else "0", utcnow()))
    db.commit()


def get_paused(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM state WHERE key='paused'").fetchone()
    return row is None or row[0] == "1"


def token_from_dpapi() -> str:
    if os.name != "nt":
        raise RuntimeError("The encrypted token setup is Windows-user scoped")
    result = subprocess.run(
        ["pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(TOKEN_SCRIPT)],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("Encrypted bot token decrypted to an empty value")
    return token


async def save_attachments(message_id: Any, attachments: list[Any], root: Path = ATTACH_DIR) -> tuple[list[Path], list[str]]:
    """Save owner attachments under root/<message_id>/. Returns (saved paths, skipped descriptions)."""
    folder = root / str(message_id)
    saved, skipped = [], []
    for a in attachments:
        if a.size > MAX_DOWNLOAD_MB * 1024 * 1024:
            skipped.append(f"{a.filename}（超过 {MAX_DOWNLOAD_MB} MB）")
            continue
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f〔〕；]', "_", re.split(r"[\\/]", str(a.filename))[-1]).strip(" .") or "file"
        path, n = folder / name, 1
        while path.exists():
            n += 1
            path = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            await a.save(path)
        except Exception as exc:
            skipped.append(f"{a.filename}（保存失败：{type(exc).__name__}）")
            continue
        saved.append(path)
    return saved, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Local allowlisted Discord transport")
    sub = parser.add_subparsers(dest="command")
    send = sub.add_parser("send", help="send explicitly authored text and record a receipt")
    send.add_argument("--channel", required=True, help="configured role key, e.g. work")
    send.add_argument("--message", required=True, help="exact coordinator-authored report text")
    send.add_argument("--file", action="append", default=[], help="attach a file (repeatable)")
    send.add_argument("--reply", action="store_true", help="this is the answer to an owner message: no hold, no source line")
    send.add_argument("--no-hold", action="store_true", help="proactive send that must not wait (supervisor loop): still gets the source line")
    send.add_argument("--line", default="组织线", help="line name for the source line, e.g. 量化线")
    sub.add_parser("channels", help="read-only: list the guild channels the bot can see (id, name, type, configured role)")
    args = parser.parse_args()
    config = load_config()
    if args.command == "send":
        asyncio.run(send_report(config, args.channel, args.message, args.file, args.reply, not args.no_hold, args.line))
    elif args.command == "channels":
        asyncio.run(list_channels(config))
    else:
        asyncio.run(run_gateway(config))


def channel_lines(channels: list[Any], configured: dict[str, str]) -> list[str]:
    """`<id>  <name>  <type>  [role]` for each channel, sorted by name; role = the config key already using that ID."""
    role_of = {cid: role for role, cid in configured.items()}
    return [f"{ch.id}\t{ch.name}\t{getattr(getattr(ch, 'type', None), 'name', '?')}\t{role_of.get(str(ch.id), '')}"
            for ch in sorted(channels, key=lambda c: str(c.name))]


async def list_channels(config: dict[str, Any]) -> None:
    """Read-only (one REST call, nothing is sent or changed): print the channels of the configured guild the bot can see.
    Only id, name, type and the configured role are printed; the token stays inside token_from_dpapi."""
    import discord

    async with discord.Client(intents=discord.Intents.none()) as client:
        await client.login(token_from_dpapi())
        guild = await client.fetch_guild(int(config["guild_id"]))
        for line in channel_lines(list(await guild.fetch_channels()), config["channels"]):
            print(line)


def check_upload_files(files: list[str]) -> None:
    """Raise before login if any file may not be uploaded."""
    if len(files) > MAX_FILES:
        raise RuntimeError(f"At most {MAX_FILES} files per message; nothing was sent")
    for f in files:
        p = Path(f)
        if not p.is_file():
            raise RuntimeError(f"Attachment is not an existing file: {p}")
        if any(CREDENTIAL_RE.search(part) for part in p.parts):
            raise RuntimeError(f"Attachment looks like a credential file, refused: {p}")
        if p.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
            raise RuntimeError(f"Attachment {p.name} is over {MAX_UPLOAD_MB} MB; send it by email instead")


def owner_waiting(db: sqlite3.Connection, channel_id: str) -> bool:
    """True while an owner message in this channel is queued or running (not answered, not queue-only)."""
    try:
        return bool(db.execute("SELECT 1 FROM direct_requests r JOIN inbound i USING(message_id) "
                               "WHERE i.channel_id=? AND r.status IN ('queued','running') LIMIT 1", (str(channel_id),)).fetchall())
    except sqlite3.OperationalError:  # no direct_requests table yet: nothing to wait for
        return False


async def hold_for_owner_reply(db: sqlite3.Connection, channel_id: str, max_s: float = HOLD_MAX_S, poll_s: float = HOLD_POLL_S) -> float:
    """Wait until the owner has no pending request in this channel, at most max_s. Returns seconds waited."""
    start = time.monotonic()
    while owner_waiting(db, channel_id) and time.monotonic() - start < max_s:
        await asyncio.sleep(poll_s)
    return time.monotonic() - start


async def send_report(config: dict[str, Any], role: str, content: str, files: list[str] | None = None, reply: bool = False,
                      hold: bool = True, line: str = "组织线") -> None:
    import discord

    files = files or []
    if role not in config["channels"]:
        raise RuntimeError("Channel role is not allowlisted")
    check_upload_files(files)
    intents = discord.Intents.none()
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
    db = connect(config)
    channel_id = config["channels"][role]
    try:
        if not reply and owner_waiting(db, channel_id):  # proactive send while the owner waits: say where it comes from, let his reply go first
            if hold:
                waited = await hold_for_owner_reply(db, channel_id)
                if waited >= 1:
                    print(f"held {waited:.0f}s for the owner's pending request")
            content = f"{notice_tag(line)}\n{content}"
        async with client:
            await client.login(token_from_dpapi())
            message_id = await send_and_record(
                client, db, role=role, channel_id=channel_id, guild_id=config["guild_id"],
                content=content, allowed_mentions=discord.AllowedMentions.none(), files=files,
            )
        print(f"sent role={role} channel_id={channel_id} message_id={message_id}")
    finally:
        db.close()


async def send_and_record(client: Any, db: Any, *, role: str, channel_id: str,
                          guild_id: Any, content: str, allowed_mentions: Any, files: list[str] | None = None) -> str:
    try:
        channel = await client.fetch_channel(int(channel_id))
    except Exception as exc:
        raise RuntimeError(f"Channel lookup failed before sending ({type(exc).__name__}); no message was sent") from None

    actual_guild_id = getattr(getattr(channel, "guild", None), "id", None)
    if str(getattr(channel, "id", "")) != str(channel_id) or str(actual_guild_id) != str(guild_id):
        try:
            db.execute("INSERT INTO receipts(created_at,channel_role,channel_id,status,detail) VALUES(?,?,?,?,?)",
                       (utcnow(), role, channel_id, "blocked", "Fetched channel did not match configured channel and guild"))
            db.commit()
            receipt = "block receipt recorded"
        except Exception:
            db.rollback()
            receipt = "block receipt could not be recorded"
        raise RuntimeError(f"Outbound blocked: fetched channel did not match configured channel and guild; no message was sent ({receipt})")

    try:
        kwargs = {}
        if files:
            import discord
            kwargs["files"] = [discord.File(p) for p in files]
        message = await channel.send(content, allowed_mentions=allowed_mentions, **kwargs)
    except Exception as exc:
        try:
            db.execute("INSERT INTO receipts(created_at,channel_role,channel_id,status,detail) VALUES(?,?,?,?,?)",
                       (utcnow(), role, channel_id, "failed", type(exc).__name__))
            db.commit()
            receipt = "failure receipt recorded"
        except Exception:
            db.rollback()
            receipt = "failure receipt could not be recorded"
        raise RuntimeError(f"Discord send attempt failed ({type(exc).__name__}); delivery is unconfirmed; {receipt}") from None

    message_id = str(message.id)
    try:
        db.execute("INSERT INTO receipts(created_at,channel_role,channel_id,message_id,status,detail) VALUES(?,?,?,?,?,?)",
                   (utcnow(), role, channel_id, message_id, "sent",
                    "Discord accepted message" + (f" with files: {', '.join(Path(p).name for p in files)}" if files else "")))
        db.commit()
    except Exception as exc:
        db.rollback()
        raise RuntimeError(
            f"delivered_receipt_unpersisted: Discord accepted message_id={message_id}, but the sent receipt could not be saved ({type(exc).__name__})"
        ) from None
    return message_id


async def run_gateway(config: dict[str, Any]) -> None:
    import discord
    from discord import app_commands
    import direct

    guild_id = int(config["guild_id"])
    owner_id = str(config["owner_user_id"])
    channels = config["channels"]
    intents = discord.Intents.none()
    intents.guilds = True
    intents.messages = True
    token = token_from_dpapi()
    async with discord.Client(intents=discord.Intents.none()) as probe_client:
        await probe_client.login(token)
        app = await probe_client.application_info()
        if str(app.owner.id) != owner_id:
            raise RuntimeError('Discord application owner does not match configured owner')
        intents.message_content = bool(app.flags.gateway_message_content or app.flags.gateway_message_content_limited)
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
    tree = app_commands.CommandTree(client)
    db = connect(config)
    direct.initialize(db)
    guild = discord.Object(id=guild_id)

    def authorized(interaction: discord.Interaction) -> bool:
        return bool(interaction.guild_id and interaction.channel_id and is_allowed(
            config, guild_id=interaction.guild_id, channel_id=interaction.channel_id,
            user_id=interaction.user.id,
        ))

    def save(interaction: discord.Interaction, source: str, text: str) -> bool:
        if not authorized(interaction):
            return False
        role = next(role for role, cid in channels.items() if cid == str(interaction.channel_id))
        return record_inbound(db, message_id=interaction.id, source=source,
                              received_at=interaction.created_at.isoformat(), guild_id=interaction.guild_id,
                              channel_id=interaction.channel_id, author_id=interaction.user.id,
                              role=role, exact_text=text)

    async def acknowledge(message_id, role, channel_id, text, only=False, attachments=()):
        content, spawn = direct.accept(db, message_id, role, text, get_paused(db), only, attachments)
        receipt = await send_and_record(client, db, role=role, channel_id=channel_id,
                  guild_id=guild_id, content=content, allowed_mentions=discord.AllowedMentions.none())
        db.execute('UPDATE direct_requests SET ack_id=? WHERE message_id=?', (receipt,str(message_id)));db.commit()
        if spawn:
            direct.spawn_owner_lane()

    @tree.command(name="ask", description="Directly ask this channel's deputy", guild=guild)
    @app_commands.describe(text="Message to preserve and queue")
    async def ask(interaction: discord.Interaction, text: str, queue_only: bool = False) -> None:
        if not save(interaction, "slash:ask", text):
            await interaction.response.send_message("Not authorized for this server or channel.", ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
            return
        await interaction.response.defer(ephemeral=True)
        role = next(role for role,cid in channels.items() if cid == str(interaction.channel_id))
        await acknowledge(interaction.id, role, str(interaction.channel_id), text, queue_only)
        await interaction.followup.send('已在本频道确认接收。', ephemeral=True)

    @tree.command(name="status", description="Show local transport pause and queue state", guild=guild)
    async def status(interaction: discord.Interaction) -> None:
        if not save(interaction, "slash:status", "/status"):
            await interaction.response.send_message("Not authorized for this server or channel.", ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
            return
        await interaction.response.send_message(
            direct.status_text(get_paused(db)), ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tree.command(name="pause", description="Record whether the coordinator queue is paused", guild=guild)
    @app_commands.describe(paused="True pauses the local queue state; False resumes it")
    async def pause(interaction: discord.Interaction, paused: bool) -> None:
        text = f"/pause paused={str(paused).lower()}"
        if not save(interaction, "slash:pause", text):
            await interaction.response.send_message("Not authorized for this server or channel.", ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
            return
        set_paused(db, paused)
        state = "paused" if paused else "resumed"
        await interaction.response.send_message(
            f"派发暂停标志：{state}。受管执行器会检查此标志；恢复标志不会绕过额度或总理分配。原始输入归档继续。", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @client.event
    async def on_ready() -> None:
        await tree.sync(guild=guild)
        print(f"Discord transport ready for guild {guild_id}; allowed channels={len(channels)}; natural_text={intents.message_content}")

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot or client.user is None:
            return
        if not is_allowed(config, guild_id=message.guild.id if message.guild else None,
                          channel_id=message.channel.id, user_id=message.author.id):
            return
        role = next(role for role, cid in channels.items() if cid == str(message.channel.id))
        if not message.content and not message.attachments:
            await message.reply('当前无法读取这条消息正文。请使用 /ask text，或 @Org Core 后直接写内容。',
                                mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
        added = record_inbound(db, message_id=message.id, source="direct:message",
                               received_at=message.created_at.isoformat(), guild_id=message.guild.id,
                               channel_id=message.channel.id, author_id=message.author.id,
                               role=role, exact_text=message.content)
        if added:
            saved, skipped = await save_attachments(message.id, message.attachments)
            if skipped:
                await message.reply("未保存的附件：" + "；".join(skipped), mention_author=False,
                                    allowed_mentions=discord.AllowedMentions.none())
            await acknowledge(message.id, role, str(message.channel.id), message.content, attachments=saved)

    try:
        await client.start(token)
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"gateway error: {error}", file=sys.stderr)
        raise SystemExit(1)
