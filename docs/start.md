# Start here: your own Premier in about 30 minutes

Premier is a small organization of Claude Code sessions that works from files you can read: your intent, your projects, a task list, a dashboard. This page gets you from a fresh clone to a dashboard that shows your first project. Nothing is installed outside the folder you choose, no account is needed, and nothing is sent anywhere.

Already have a backup of a running Premier from another computer? Use [restore.md](restore.md) instead. This page is for starting new.

## What you need
- Windows with [Python](https://www.python.org/downloads/windows/) 3.12 or newer, [Git](https://git-scm.com/download/win) and [Claude Code](https://claude.com/claude-code), all installed and on your PATH. Check in PowerShell: `python --version` (3.12 or higher), `git --version`, `claude --version`. Claude Code is signed in with your own subscription (you sign in yourself, in its own window). If `python` opens the Microsoft Store or says it is not found, install Python from python.org and tick "Add to PATH", then open a new PowerShell.
- About 1 GB of disk. No GPU, no WSL, no Discord, no mail account.

## The five steps

1. **Clone.** The clone address is on this repository's page: click the green **Code** button and copy the HTTPS address. The folder must be named `incore`: the code imports itself by that name. If that folder already exists in `$Root\workspace`, skip this step.
   ```powershell
   $Root = 'C:\Premier'
   New-Item -ItemType Directory -Path "$Root\workspace" | Out-Null
   git clone <the address of the repository you are reading this in> "$Root\workspace\incore"
   ```
2. **Install the one dependency.** Make a fresh venv (never copy someone else's):
   ```powershell
   python -m venv "$Root\venv"
   & "$Root\venv\Scripts\python.exe" -m pip install -r "$Root\workspace\incore\requirements\input-dashboard.txt"
   ```
3. **Fill in your instance.** Inside the clone, copy the example and open it:
   ```powershell
   Copy-Item "$Root\workspace\incore\instance\example\instance.json" "$Root\workspace\incore\instance\instance.json"
   notepad "$Root\workspace\incore\instance\instance.json"
   ```
   Set `workspace` to your `$Root\workspace` path, `user_home` to a new, empty folder (it may sit under Root, for example `$Root\orghome`; do not use your Windows user folder), and `owner_name` to your name. Leave the rest (`owner_aliases` holds the example's placeholder name; replace it with the names you go by, or leave it): the mail addresses and `wsl_user` only matter if you turn those parts on later. The file stays on your machine (git ignores it).
4. **Let Premier set itself up with you.** Open Claude Code in `$Root\workspace` and paste the prompt at the bottom of this page. It asks you one question at a time (what you are trying to get done, what your first project is), shows each file before it writes it, and writes your first intent card, your first line and project, and your own private layer. Every optional component below is a question it asks; "skip" is always a fine answer.
5. **Start the dashboard.**
   ```powershell
   Set-Location "$Root\workspace"
   $p = Start-Process -PassThru -WindowStyle Hidden -FilePath "$Root\venv\Scripts\python.exe" -ArgumentList '-u','-m','incore.input.server'
   $p.Id    # note it; this is the process you stop later
   Start-Process 'http://127.0.0.1:8765/dashboard'
   ```
   If something else already uses port 8765, set `$env:INCORE_INPUT_PORT = '8766'` (any free port) before `Start-Process`, and open that port instead. Your first project is on the dashboard under "Plans by Line" near the bottom of the page: open the card of the line you named and your task row is inside. The page fills that list in by itself a second after it opens, so read it in the browser; a plain download of the page text will not contain it. The "For you" card at the top may say the scheduler is not set up: that is expected, the scheduler is optional. Stop the server with `taskkill /PID <that number> /T /F`; the id is the launcher, and `/T` also ends the server it started.

## Optional parts (skip all of them and it still works)
| Part | What it gives you | What it needs from you |
|---|---|---|
| Voice and text input station (`/`) | Speak or type a note; it is stored and shown on the dashboard | Text notes work as is. Speech-to-text needs a local model and a GPU: see `docs/install-contract.md`; skip at first |
| Discord | Daily reports and commands in a channel | A Discord bot you create and its token, typed by you into a local file, never into the chat |
| Mail | The same reports by email | A mail account and an app password, same rule |
| Backup | A rolling, restorable copy of your files | A Google Drive folder; see `backup/README.md` |
| The scheduler | The organization dispatches work on its own | A scheduled task that you register yourself after reading `control/README.md`. Premier never registers it for you |

Premier does not log in anywhere, store a password, send a message or register a task during this setup.

## What got created
All paths are inside the clone (`$Root\workspace\incore`).
- `instance/instance.json`: paths, the names the dashboard shows, your lines and your projects.
- `projects/intent/<project>.md`: one page per project with your own words, dated. Premier reads it before writing a task.
- `projects/backlog.md`: the task list. A task is one line under its line's `## <heading>`, `- [todo] <Project>: <what, and how you will know it is done>`; the project name at the start sends it to that project's own session.
- `data/config-sync/private-layer/roles/*.md`: your versions of the role prompts. A file here replaces the template of the same name in `control/roles/`, whole. The templates stay as they are.

## What is not finished
- Your lines and projects live in `instance/instance.json` (`lines`: the task-list heading of each line; `projects`: your project names, folders and the words a task row starts with). Premier writes them for you in step 4. A line is one of six slots (`coordinator`, `work`, `uvox`, `quant`, `intelligence`, `life`) and you pick the heading; most people use one or two.
- The scheduler that dispatches work on its own is off until you register it yourself; until then the task list and the dashboard work, and Claude Code sessions you open do the tasks.
- Some pages are still Chinese: the sprint page, the poetry page and a few server error messages.

## Prompt to paste into Claude Code (opened in `<Root>\workspace`)

> Read `incore\docs\start-session.md` and set up my Premier with me. Ask one question at a time. Before you write any file, show me what it will contain and wait for my yes. Never ask me for a password, token or key, and never run a command that logs in, sends a message, registers a task or spends money. Ask me what I want to get done first.

[中文摘要] 这页带你从全新 clone 走到看板显示你的第一个项目：clone（文件夹必须叫 incore）→ 新建 venv 装依赖 → 填 `instance/instance.json` → 在 Claude Code 里粘贴底部提示词，由它一问一答、写文件前先给你看，写出第一张意图卡、第一个项目和你自己的私有层 → 起输入台看板。Discord、邮件、备份、语音、调度器都是可选，全跳过也能用。有旧机器的备份请改读 [restore.md](restore.md)。
