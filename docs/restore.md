# Restore the incore on a new computer

Starting new, with no backup? Use [start.md](start.md) instead. This page is for restoring a Premier you already ran.

Goal: a new Windows machine shows the same dashboard, to-do page and recordings as the one you backed up, without touching
the old machine, the live Drive chain, Discord or mail. This page is written for a Claude Code session that does the work
while you answer a few questions. Steps 0–7 install and check; step 8 is the only one that needs you.
(中文摘要在页末。)

## Ground rules (for the session)

1. Work in order. After each step append one line to `<Root>\restore-progress.md`: step number, `done` / `failed` / `waiting for owner`, the evidence (a version, a count, an exit code). On re-entry, continue at the first step not marked `done`.
2. Ask the owner only what the machine alone knows: where Python and Git are, which folder holds the downloaded packages, which repo URL to clone. Never ask for a password, token or key, and never paste one anywhere. Logins (Claude, Discord, Gmail, GitHub) are done by the owner in the official login window, later, by hand.
3. Do not register scheduled tasks, start services other than the isolated runner in step 6, send any message or mail, or write to the Drive backup chain.
4. Do not overwrite anything. `restore_instance.py` already refuses; if a command says a folder exists, stop and report, do not delete it.
5. Every process you start with `Start-Process -PassThru`: write its PID in the progress file; stop exactly those PIDs at the end (step 8). Do not stop other processes.
6. If a step fails, show the error text to the owner and stop. Do not try a different route around a failed check.

Variables used below (pick the folders with the owner; examples are placeholders):

```powershell
$Root = 'C:\PremierRestore'                 # everything lives under here
$Py   = '<path to python.exe, Python 3.12 or newer>'
$Git  = '<path to git.exe>'                 # or just git if it is on PATH
$Url  = '<repo URL or local path the owner gives you>'
$Branch = '<branch or tag the owner gives you>'
$Pack = "$Root\packages"                    # the owner downloads the day ZIP(s) here
```

## Step 0 — tools (owner may need to install)

```powershell
& $Py --version; & $Git --version; claude --version
```

Checkpoint: Python ≥ 3.12 and Git print a version. Missing one: tell the owner which, and stop. Do not install anything silently.
Run this and every later step from `$Root` or any folder; none of them needs to start inside a repository.

## Step 1 — get the code

```powershell
New-Item -ItemType Directory -Path "$Root\workspace" | Out-Null
& $Git -c safe.directory=* clone --quiet --branch $Branch $Url "$Root\workspace\incore"
& $Git -c safe.directory=* -C "$Root\workspace\incore" rev-parse --short HEAD
```

Checkpoint: a commit id prints. `$Root\workspace\incore\backup\backup.py` and `...\control\restore_instance.py` exist.

## Step 2 — the backup packages

The owner downloads the chosen restore point from Drive, **plus every earlier ZIP the newest one chains to**, into `$Pack`.
List them and show the owner the names and total size.

```powershell
New-Item -ItemType Directory -Path $Pack -Force | Out-Null   # skip if the owner already made it
Get-ChildItem $Pack -Filter *.zip | Sort-Object Name | Select-Object Name, Length
[math]::Round((Get-PSDrive -Name $Root.Substring(0,1)).Free / 1GB, 1)   # free GB on the target drive
```

Checkpoint: at least one ZIP; free space is larger than the ZIP total times 1.5 (the unchained files were 1.2 times the ZIPs in the rehearsal) plus a few GB for the clone and the venv. Step 4 `apply` also takes several minutes on a real package (about 7).
Pick the newest ZIP (by name) as `$Zip`.

## Step 3 — unchain the packages

```powershell
& $Py "$Root\workspace\incore\backup\backup.py" --restore-zip $Zip --restore-to "$Root\out"
```

This prints nothing until it ends and is slow on a real chain: about 47 minutes for 41 ZIPs (12.7 GB, 68k files) on the rehearsal machine, so start it with `Start-Process -PassThru`, write the PID in the progress file, and poll the file count in `$Root\out`; do not run it inside a command with a short timeout. Checkpoint: the output contains `zip_chain_files_sha256_verified`. `$Root\out` must not exist beforehand; if it does, stop and ask. Any hash or SQLite failure: stop. Nothing has been written outside `$Root\out`.

## Step 4 — plan, then apply

```powershell
& $Py "$Root\workspace\incore\control\restore_instance.py" plan "$Root\out" "$Root\workspace"
```

The last argument is the **workspace folder** (`$Root\workspace`, the folder that already holds `incore` from step 1), not `$Root`. The output has one line per file and a summary line starting `files:`; show the owner the summary and the held-back list (counts of `install` / `replace` / `same` / `conflict`, and the held-back list: `external/*` settings, memory and skills, and `restore/`, which are not installed on purpose). `replace` means the clone's checkout copy of a tracked file is unmodified and differs from the backup, so the later backup version replaces it (git keeps the old text; 83 files in the rehearsal). `conflict` means the file differs from the backup and is left alone. The plan is made against the folder as it is now; the code in your step-1 clone already contains some of the backed-up files, so the receipt of `apply` shows fewer `installed` and more `same` than the plan's `install` count. That difference is expected. If the plan looks right:

```powershell
& $Py "$Root\workspace\incore\control\restore_instance.py" apply "$Root\out" "$Root\workspace" --yes
```

Checkpoint: JSON receipt with no `HASH MISMATCH`; `$Root\workspace\restore-receipt.json` exists; there is still only one `incore` folder (`$Root\workspace\incore`), none directly under `$Root`. Running `apply` again changes nothing.

## Step 5 — a Python environment for the runner

Never reuse an old machine's virtual environment.

```powershell
& $Py -m venv "$Root\venv"
& "$Root\venv\Scripts\python.exe" -m pip install --quiet aiohttp
& "$Root\venv\Scripts\python.exe" -c "import aiohttp; print(aiohttp.__version__)"
```

Checkpoint: a version prints. If the runner in step 6 reports another missing module, install that one module the same way and write its name in the progress file.

## Step 6 — start the isolated copy (port 18765, no scheduler, no ASR, no messages)

```powershell
$p = Start-Process -PassThru -WindowStyle Hidden -FilePath "$Root\venv\Scripts\python.exe" `
  -ArgumentList '-X','utf8',"$Root\workspace\incore\control\restore_instance.py",'run',"$Root\workspace",'18765' `
  -RedirectStandardOutput "$Root\runner.out.log" -RedirectStandardError "$Root\runner.err.log"
$p.Id   # write this PID in the progress file
(Invoke-WebRequest http://127.0.0.1:18765/todo -UseBasicParsing).StatusCode
```

Checkpoint: status 200 (wait up to 20 s and retry). Also check `/dashboard` and `/` return 200, and read:

```powershell
(Invoke-RestMethod http://127.0.0.1:18765/api/todo).cards.Count
(Invoke-RestMethod http://127.0.0.1:18765/api/sessions).sessions.Count
```

Report both counts. The to-do count should match the cards the owner saw on the old machine at the restore point (ask the owner), and the sessions count the recordings and notes at that time. A card the owner answered before that point still shows, with the answer kept in `$Root\workspace\incore\data\owner-replies.json`.

## Step 7 — Claude and Codex conversations (only if the owner has a session export)

The day package does not carry the conversations. If the owner also brought a folder made by `control\session_map.py export`, put it back under the new workspace:

```powershell
& $Py "$Root\workspace\incore\control\session_map.py" restore '<export folder>' --cwd "$Root\workspace"
```

It never overwrites. Claude sessions land under `%USERPROFILE%\.claude\projects` and Codex sessions under `--codex-home` (default: the WSL home). To rehearse without touching a real profile, first set `$env:USERPROFILE` to a scratch folder and add `--codex-home <scratch folder>` to the command. Afterwards list what was written (`Get-ChildItem -Recurse` on the scratch folder) and check that every restored file's hash equals the one in the export's `manifest.json`. Without an export, write `7 skipped: no session export` and go on; the roles then start fresh from the lessons and records in the restored folders.

## Step 8 — owner check and stop

Give the owner these two lines to paste (the copy runs on port 18765 so it cannot collide with a live instance):

```powershell
Start-Process 'http://127.0.0.1:18765/dashboard'
Start-Process 'http://127.0.0.1:18765/todo'
```

Leave the runner up until the owner says they are done looking (if nobody is there to look, stop it at once and tell the owner to repeat step 6 to view the pages). Then stop the runner you started, by the PID in the progress file only:

```powershell
Stop-Process -Id <pid from the progress file>
```

Not done by this page, and needing the owner's separate go-ahead later: registering scheduled tasks, starting the real services,
logins and secrets (Discord bot token, Gmail app password, GitHub, Tailscale), enabling backups or messages. Never run the old
machine and the new one as live instances at the same time.

Write the final status table in the progress file: each step `pass` / `fail` / `not tested`, and the time each took.

---

## Bootstrap prompt (the owner pastes this into Claude Code opened in `<Root>`)

> Follow `<Root>\workspace\incore\docs\restore.md` to restore the incore on this computer. Work step by step and keep `<Root>\restore-progress.md` up to date. Ask me only for things only this machine knows: folder locations and the repo URL. Never ask me for a password or token; I will log in myself in the official window. Do not register scheduled tasks, start services other than the isolated copy on port 18765, send messages, or touch the old machine or the Drive backup. Stop after each failed check and show me the error. Start by checking only: Step 0.

(If the repo is not cloned yet, the first command is the Step 1 clone, then open Claude Code in `<Root>\workspace` and paste the prompt with the path to `docs\restore.md` in that clone.)

## 中文摘要

在新 Windows 电脑上恢复：检查 Python、Git → clone 代码 → 下载日包（含全部前代 ZIP）到 `packages` → `backup.py --restore-zip` 解链并校验 → `restore_instance.py plan` 再 `apply --yes`（不覆盖，`external/*` 和 `restore/` 故意不装）→ 新建 venv 装 aiohttp → 起 18765 端口的隔离副本（不起调度、不发消息）→ 看 `/dashboard` 和 `/todo` 与恢复点一致。会话（Step 7）只有在带了 `session_map.py export` 的导出时才恢复。每步在 `restore-progress.md` 记一行；只问机器才知道的事；密码和令牌由 owner 自己在官方登录界面填；结束时只按记下的 PID 停掉自己起的 runner。
