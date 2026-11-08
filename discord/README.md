# Discord gateway

A small local bot that lets you talk to each role of your organization from its own Discord channel, and lets the organization send you reports there. It stores every allowed inbound message exactly as sent and replies with an acknowledgement; replies from a role use your model subscription under the quota, pause and serialization rules in `control/`.

## What it does
- Only the owner, in the configured server and role channels, is heard. Slash commands `/ask`, `/status`, `/pause` and direct mentions are handled; ordinary text needs the Message Content intent (the gateway checks the application's flags at startup).
- `/ask` saves the exact text, source, time, author, channel and routed role in SQLite (`data/discord.sqlite3`) and acknowledges it. `/ask queue_only:true` queues without a model reply. `/pause` records a pause flag; `/status` shows the pause state and queue size.
- A role's direct reply is text only; it has no execution tools. A request that needs action becomes a durable `deputy_execution_request` task for an executor; creating it does not mean it ran.
- Reports are sent only by an explicit command containing the authored text. Each attempt writes a delivery receipt; `allowed_mentions` is disabled everywhere.

## Install
1. Create a Discord application and bot, add it to your server, and give it View Channel, Send Messages and Read Message History in the role channels (Attach Files only if you want report attachments). Do not grant Administrator.
2. Copy `config.example.json` to `config.json` (git-ignored) and fill in your user ID, the server ID and one channel ID per role (`coordinator`, `life`, `work`, `uvox`, `quant`, `intelligence`; use the role keys your organization defines). Two roles on one channel are refused at load. Until all IDs are set the gateway fails closed. `gateway.py channels` lists the channels the bot can see (read-only) to help you find IDs.
3. Create the environment with `./setup.ps1` (Python 3.12 under `-Python`, installs the pinned `discord.py`), then store the bot token with a hidden prompt: `./setup.ps1 -SetToken` from PowerShell 7. The token is encrypted per Windows user (DPAPI) under `.secrets/` and never appears in config, arguments or logs.

## Use
```powershell
./run.ps1                                      # start the gateway in the foreground
./run.ps1 -Send -Channel work -Message "Report text"   # send an authored report
./run.ps1 -Test                                # dependency-free transport check
```

`data/` holds your original messages, receipts and run records: protect it, back it up as user data and never publish it. Start-up after a reboot is manual unless you install the login startup script (`install-login-startup.ps1` in the incore root) yourself.