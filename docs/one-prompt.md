# One prompt: set up your Premier from an empty folder / 一段提示词：从空文件夹建好你的 Premier

Make a folder, open Claude Code in it, paste the prompt below. It checks your machine, builds the layout the code needs (`workspace\incore`) and walks you through the setup one step at a time. The README shows the same text.
建一个文件夹，在里面打开 Claude Code，粘贴下面的提示词。它会检查你的机器、搭好代码需要的目录（`workspace\incore`），然后一步一步带你设置。README 里是同一段文字。

## English

```text
I want to set up my own Premier, an AI organization that runs from plain files, starting from this empty folder. Work one step at a time: before each step tell me in one or two sentences what you are about to do, and wait for my yes. Never ask me for a password, token or key. Never log in anywhere, send a message, register a scheduled task or spend money.

1. Check that python (3.12 or newer), git and claude are on PATH (`python --version`, `git --version`, `claude --version`). If one is missing, stop and tell me how to install it.
2. In this folder create `workspace`, clone https://github.com/Wanderfon/you-need-a-premier.git into `workspace\incore` (the folder must be named incore), create a virtual environment in `venv` here and install `workspace\incore\requirements\input-dashboard.txt` into it.
3. Ask me my name. Copy `workspace\incore\instance\example\instance.json` to `workspace\incore\instance\instance.json` and fill it in: `workspace` is the full path of the `workspace` folder, `user_home` is a new empty folder `orghome` here, `owner_name` is my name. Show me the file before you write it.
4. Read `workspace\incore\docs\start-session.md` and follow it: ask me one question at a time and write my first intent card, my line, my project and my first task rows, showing each file before you write it.
5. Start the dashboard as step 5 of `workspace\incore\docs\start.md` shows, and give me one command to open it and one to stop it. If port 8765 is already taken, use another port as `start.md` says.
```

## 中文

```text
我想从这个空文件夹开始，建我自己的 Premier（一个靠纯文本文件运转的 AI 组织）。请一步一步来：每一步开始前，用一两句话告诉我你要做什么，等我说“好”再动手。不要向我要密码、token 或 key；不要登录任何账号、发消息、注册计划任务或花钱。

1. 检查 python（3.12 或更高）、git、claude 是否在 PATH 里（`python --version`、`git --version`、`claude --version`）。缺哪个就停下，告诉我怎么装。
2. 在当前文件夹下新建 `workspace`，把 https://github.com/Wanderfon/you-need-a-premier.git 克隆到 `workspace\incore`（文件夹必须叫 incore），在当前文件夹下建虚拟环境 `venv`，把 `workspace\incore\requirements\input-dashboard.txt` 装进去。
3. 问我的名字。把 `workspace\incore\instance\example\instance.json` 复制成 `workspace\incore\instance\instance.json` 并填好：`workspace` 填 `workspace` 文件夹的完整路径，`user_home` 填当前文件夹下新建的空文件夹 `orghome`，`owner_name` 填我的名字。写文件前先给我看内容。
4. 读 `workspace\incore\docs\start-session.md` 并照着做：一次问我一个问题，写出我的第一张意图卡、我的线、我的项目和第一批任务行，每个文件写之前先给我看。
5. 照 `workspace\incore\docs\start.md` 第 5 步启动看板，给我一条打开它的命令和一条停止它的命令。如果 8765 端口被占用，照 `start.md` 写的换一个端口。
```
