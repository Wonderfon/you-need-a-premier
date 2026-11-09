# FAQ: seven questions before you start / 开始前的七个问题

Short answers, each with the file it comes from. Where nothing was tested, it says so. / 每条答案都附出处文件；没测过的会直说。

## English

**1. What does it do after setup, and what does it not do?**
Setup writes your intent card, your line, your project and first task rows, and the dashboard shows them (`docs/start.md`, `docs/start-session.md`). Nothing runs by itself: the scheduler is optional and "dispatch starts paused and disabled" (`control/README.md`), and the setup session never registers a scheduled task. The voice input desk and the History Keeper are not in this package (`README.md`, Status).

**2. How much quota or money does it use?**
Setup is an ordinary Claude Code conversation on your own subscription; we have not measured how much of it that takes. The scheduler's usage gate needs a fresh `/usage` snapshot before it makes a call (`control/README.md`), and the rules forbid paid APIs and top-ups (`control/roles/tick-rules.md`, "Spend nothing"). With the scheduler off, nothing else spends anything.

**3. Does it work on Mac or Linux?**
Only Windows is documented (`docs/start.md`, "What you need"). The dashboard is plain Python on `aiohttp`, but we have not tried it on Mac or Linux, and the machine monitor calls Windows-only functions (`control/machine_watch.py`, `ctypes.WinDLL`) while the commands in the docs are PowerShell.

**4. Where is my data, and does it leave my machine?**
On your machine: dashboard data under `input/data/`, queue data under `data/` (`input/README.md`, `control/README.md`), and `.gitignore` keeps your instance file, intent cards, task list and `data/` out of git. The dashboard listens on `127.0.0.1` only (`input/README.md`). `docs/start.md` says nothing in the setup is sent anywhere; your Claude Code conversation itself goes to Anthropic, as it always does.

**5. How do I remove it cleanly?**
Stop the dashboard with the `taskkill` command from `docs/start.md` step 5, then delete your Root folder (`workspace`, `venv`, `orghome`): `docs/start.md` says nothing is installed outside it. If you later turned on optional parts yourself (a startup script such as `install-login-startup.ps1`, a scheduled task), remove those too. We have not run an uninstall as a separate test.

**6. Can I use it on a project I already have?**
Yes in principle: a project entry in `instance.json` takes a `cwd` that is relative to the workspace or absolute, and Premier does not read inside it (`docs/start-session.md`, step 3). The walkthroughs so far used an empty project folder, not a real existing one.

**7. What does a finished task look like?**
A task row moves `todo` → `review` (with the result file named in the row) → `done`; `demo/projects/backlog.md` shows each state on invented data, and `python demo/run_demo.py` serves it as a dashboard (`demo/run_demo.py`). Setup writes your first row with a way to tell it is done; v0.1 does not finish it for you until you run a session or the scheduler yourself.

## 中文

**1. 装完会做什么，不会做什么？**
设置会写下你的意图卡、线、项目和第一批任务行，看板显示出来（`docs/start.md`、`docs/start-session.md`）。不会自己运转：调度器是可选的，“派活默认暂停并关闭”（`control/README.md`），设置会话也不会注册计划任务。语音输入台和历史存档不在这个包里（`README.md` 的 Status）。

**2. 会花多少额度或钱？**
设置就是你自己订阅下的一次普通 Claude Code 对话，它占多少额度我们没测过。调度器的用量闸门在每次调用前要一份新的 `/usage` 快照（`control/README.md`），规则也禁止付费 API 和充值（`control/roles/tick-rules.md`，“Spend nothing”）。调度器不开，别的地方不花钱。

**3. Mac 或 Linux 能用吗？**
文档只写了 Windows（`docs/start.md`，“What you need”）。看板是 `aiohttp` 写的普通 Python，但没在 Mac 或 Linux 上试过；机器监测调用了只有 Windows 才有的函数（`control/machine_watch.py` 的 `ctypes.WinDLL`），文档里的命令也是 PowerShell。

**4. 数据存在哪，会不会离开本机？**
在你的机器上：看板数据在 `input/data/`，队列数据在 `data/`（`input/README.md`、`control/README.md`），`.gitignore` 把你的实例文件、意图卡、任务表和 `data/` 挡在 git 之外。看板只监听 `127.0.0.1`（`input/README.md`）。`docs/start.md` 说设置里没有任何东西被发出去；你和 Claude Code 的对话本身会发给 Anthropic，这一点和平时一样。

**5. 怎么干净卸掉？**
按 `docs/start.md` 第 5 步的 `taskkill` 命令停掉看板，再删掉你的 Root 文件夹（`workspace`、`venv`、`orghome`）：`docs/start.md` 说 Root 以外没装任何东西。以后你自己开的可选部分（如 `install-login-startup.ps1` 这样的开机启动脚本、计划任务）要另外删。卸载本身没有单独测过。

**6. 已有的项目能接吗？**
原则上能：`instance.json` 里的项目条目有 `cwd`，可以是相对工作区的路径或绝对路径，Premier 不会去读里面的内容（`docs/start-session.md` 第 3 步）。目前的走查用的都是空项目文件夹，没接过真实的已有项目。

**7. 一个完成的任务长什么样？**
任务行从 `todo` 到 `review`（行里写着结果文件）再到 `done`；`demo/projects/backlog.md` 用虚构数据展示每个状态，`python demo/run_demo.py` 把它当看板跑起来（`demo/run_demo.py`）。设置会写下你的第一行任务，并写明怎么算做完；v0.1 不会替你做完它，要你自己开会话或开调度器。
