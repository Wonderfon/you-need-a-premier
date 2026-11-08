"""Durable scheduler. `tick.py` = supervisor (spawns lanes while usage is under the pacing line of the five-hour
window), `tick.py owner` = owner lane, `tick.py report` = report lane, `tick.py lane <role> [project]` = one unit
of work: the role's deputy unit, or with a project one project executor. Design: audits/2026-09-30-design-v2-deputy-lanes.md,
audits/2026-09-30-executor-per-project.md.

The supervisor is run every 20 min by the Windows scheduled task 'Org Core Coordinator Tick'
(pythonw.exe, cwd = workspace root, IgnoreNew). Rules for the launched runs: roles/tick-rules.md.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import layered  # noqa: E402  (public template + private layer)
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

# ---- CONFIG ----
WORKSPACE = Path(ic.V["workspace"])
CLAUDE = ic.V["claude_exe"]
CONTROL = WORKSPACE / "incore" / "control"
DATA = CONTROL / "data"
MACHINE_HEALTH = WORKSPACE / "incore" / "data" / "machine-health.jsonl"  # one row per 10 min, see machine_watch.py
PAUSE_FILE = DATA / "tick.PAUSE"
LOCK_FILE = DATA / "tick.lock"
OWNER_LOCK = DATA / "owner.lock"
ASK_LOCK = DATA / "ask.lock"
ASK_STATE = DATA / "ask-state.json"  # `等总理` row -> {tries, at}: owner\x2010-05, a deputy's question gets a prompt coordinator run
ASK_NOTICE_STATE = DATA / "ask-notices.json"
SUPERVISOR_STATE = DATA / "supervisor-state.json"
ASK_NOTICE_COLLISION_S = 120
ASK_MAX_TRIES = 2  # a row the coordinator leaves unanswered this often goes to the owner (`等你决定`); each retry waits FAIL_BACKOFF_S
DEPUTIES_FILE = DATA / "deputies.json"
DISCORD_DB = WORKSPACE / "incore" / "discord" / "data" / "discord.sqlite3"
GATEWAY_PY = WORKSPACE / "incore" / "discord" / ".venv" / "Scripts" / "python.exe"
GATEWAY = WORKSPACE / "incore" / "discord" / "gateway.py"
USAGE_FILE = DATA / "usage.jsonl"
LOG_FILE = DATA / "tick-log.jsonl"
TICKS_DIR = DATA / "ticks"
BACKLOG = WORKSPACE / "incore" / "projects" / "backlog.md"
BACKLOG_HISTORY = DATA / "backlog-history"
RECORDS_DIR = WORKSPACE / "incore" / "records"  # read-only here: owner-terminal records feed owner_words_block
RULES_FILE = CONTROL / "roles" / "tick-rules.md"  # the public template; read through read_rules() (private layer first)


def read_rules() -> str:
    return layered.read_text(RULES_FILE)  # RULES_FILE outside control/ (tests) is read as is


WINDOW_SECONDS = 18000
BASELINE_MAX = 90.0  # owner\x2010-05: pacing line ends at 90% (was 80%); he cuts in whenever he needs and the lanes then wait
HARD_CEILING = 95.0  # owner\x2010-05: hard stop at 95% (was 90%)
PACING_SLACK = 2.0
LAUNCH_RESERVE = 8.0  # points of the five-hour window one more unit may use; a launch needs u + this * (running + 1) <= HARD_CEILING
WATCH_EVERY_S = 15  # a run's stream is read this often for rate_limit_events (usage.jsonl, the ceiling stop)
EXECUTOR_MAX_BUDGET_USD = 10.0  # one non-quant executor run's cap during the one-week trial
QUANT_EXECUTOR_MAX_BUDGET_USD = 5.0  # quant keeps its existing cap
CEILING_STOP = -2  # run_claude's exit code for a run stopped at HARD_CEILING
MAX_STDERR_BYTES = 8192  # keep failed CLI startup diagnostics useful without letting a broken CLI grow run records forever
SESSION_UNAVAILABLE = re.compile(
    r"\bno\s+(?:session|conversation)(?:\s+id)?\s+found\b|"
    r"\b(?:session|conversation)(?:\s+id)?\b[^\r\n]{0,80}\b"
    r"(?:not found|does not exist|doesn't exist|is invalid|is unrecoverable|is not recoverable|"
    r"cannot be recovered|is corrupt(?:ed)?)\b|"
    r"\b(?:cannot|can't|could not|unable to)\s+recover\s+(?:the\s+)?(?:session|conversation)\b|"
    r"\binvalid\s+(?:session|conversation)(?:\s+id)?\b", re.I)
# Price of a run that was killed (no result event), estimated from its stream's assistant events (stopped_cost).
# USD per million tokens: (input, 5m cache write, 1h cache write, cache read, output). Source: platform.claude.com
# /docs/en/about-claude/pricing, fetched 2026-10-03 (Opus 5.5 cache reads are 0.05x input). Unknown model: Opus's.
# Sonnet cache reads stay 0.20: the page (10-09) says $0.10 (footnote 2: 0.05x input), but Claude Code's reported costs
# match 0.20 exactly (8 sonnet runs of 10-07..10-09, audits/2026-10-09-haiku-tag.md), and this estimate stands in for those.
# Haiku 5.5 (page fetched 2026-10-09): prompts up to 100k tokens; HAIKU_LONG_PRICES for a request whose prompt (input +
# cache reads + cache writes) is over 100k, priced request by request.
PRICES = {"opus": (4.0, 5.0, 8.0, 0.20, 20.0), "sonnet": (2.0, 2.5, 4.0, 0.20, 10.0), "haiku": (0.10, 0.125, 0.20, 0.01, 0.50)}
HAIKU_LONG_PRICES, HAIKU_LONG_TOKENS = (0.50, 0.625, 1.0, 0.05, 2.50), 100_000
# WebFetch/WebSearch run haiku in the background; the cost shows only in the result's modelUsage. Residual of ledger cost over
# the estimate, per call: 0.012 early (10-02/03), 0.003-0.006 from 10-08; 0.007 keeps all 16 ledger runs with >= 75 calls within +-25%
WEB_TOOL_USD = 0.007
# The stream's output_tokens is a message-start snapshot, so output is estimated: tokens = a * (chars of text + tool_use
# input) + b * (chars of thinking signature; the thinking text itself arrives empty). Least squares on 143 finished
# runs of 2026-09-30..10-03 whose cost was fully explained by their own assistant events (audits/2026-10-03-inflight-ceiling-rework.md).
OUTPUT_TOKENS_PER_CHAR = {"opus": (0.653, 0.215), "sonnet": (0.392, 0.299), "haiku": (0.392, 0.299)}
UTIL_SCALE = 100.0  # the stream reports utilization as a fraction (0.35 = 35 %)
SUPERVISE_MAX_S = 5.5 * 3600  # below the task's PT6H limit
LOOP_S = 30
SAMPLE_EVERY_S = 600  # owner\x2010-02: half as often (was 300); the dashboard calls a sample stale after 15 min
AUX_EVERY_S = 300  # report / owner lane spawns at most this often
MAX_PARALLEL = 3  # live units in total (deputy runs + executors); was 2 lanes
SPAWN_GRACE_S = 90  # a lane spawned this recently counts as live before its lock file exists
PLAN_COOLDOWN_S = 2 * 3600
FAIL_BACKOFF_S = 20 * 60  # a lane whose last run failed waits this long before it is chosen again
SAMPLE_RETRY_S = 60  # after a failed usage sample
REUSE_S = 60  # an observation this fresh (a lane's, a run's stream) is used instead of a new haiku call (owner\x2010-02)
COORDINATE_EVERY_S = 8 * 3600  # owner\x2010-01: a fixed global review every 8 h
COST_FLOOR = 1.0
CACHE_HOT_S = 55 * 60  # the prompt cache lives an hour: a deputy session whose last run ended less ago is still warm
COMPACT_TOKENS = 200_000  # a deputy session above this after a run is /compact-ed in place (owner\x2010-02: 20% of the 1M window; was 500k, before 10-01 100k)
COORDINATOR_COMPACT_TOKENS = 300_000  # same for the coordinator's session (owner\x2010-01: 50%; the 10-02 change named the deputies only; owner\x2010-25: 30%, records\x2f2026-10-25-owner-terminal-efficiency-codex-dotz.json)
EXECUTOR_COMPACT_TOKENS = 250_000  # same for executor sessions (owner\x2010-01: 25% of the 1M window)
CONTEXT_WINDOW = 1_000_000  # the models' window; CLAUDE_AUTOCOMPACT_PCT_OVERRIDE is a percentage of it (measured 10-02)
EXECUTOR_EFFORTS = ["medium"]  # alternates per lane (one entry: always medium; owner\x2010-25, records\x2f2026-10-25-owner-terminal-efficiency-codex-dotz.json)
OPUS_TAG = "〔执行者：Opus〕"  # a task line carrying this runs its executor on Opus (owner\x2010-02: visual-heavy work only)
HAIKU_TAG = "〔执行者：Haiku〕"  # owner\x2010-09 pilot: small numbered patches with tests, read-only inventories; reworks go back to Sonnet
# owner\x2010-04: while Claude usage holds launches back, Codex runs executor todos (not Opus-tagged ones), one at a time;
# 10-07: up to CODEX_SLOTS at a time; 10-07 (terminal): after the FOS video one slot stays open next to Claude
# owner\x2010-25: 3 and 3 to use up the Codex weekly quota before the resets expire (records\x2f2026-10-25-owner-terminal-efficiency-codex-dotz.json)
# owner\x2011-01 terminal: 20 everywhere, use the week up fast and press a reset (records\x2f2026-11-01-owner-terminal-codex-slots-20-now.json)
# owner\x2011-04 terminal: slow down, 20 -> 10 (records\x2f2026-11-04-owner-terminal-codex-slots-10-no-plan.json)
CODEX_SLOTS = 10
CODEX_PARALLEL_SLOTS = 10  # Codex runs this many at a time while Claude is not held back; CODEX_SLOTS while it is
CODEX_BURN_SLOTS = 10  # 10-26: both of the above while the weekly reading is 10+ points behind the burn line (codex_burn.py)
CODEX_LOCKS = max(CODEX_SLOTS, CODEX_BURN_SLOTS)  # codex_lock files in use
# owner\x2011-01 terminal 「改，直接上线」: Codex has no row it can take -> one deputy plan run (codex_plan_lane) that writes rows for
# it; the pacing line does not hold it back, the hard ceiling and the reserve do (records\x2f2026-11-01-owner-terminal-codex-plan-direct.json)
CODEX_PLAN_EVERY_S = 600  # at most one such spawn this often; each line also waits PLAN_COOLDOWN_S after its last plan run
CODEX_PLAN_ON = False  # owner\x2011-04 terminal: stop these plan runs (records\x2f2026-11-04-owner-terminal-codex-slots-10-no-plan.json)
CODEX = ["wsl.exe", "-e", ic.V["wsl_home"] + "/.local/bin/codex"]  # codex-cli 0.157.0 in WSL, logged in there
# (model, efforts tried in order, runs per local day, clear small tasks only), tried in this order for each todo
CODEX_TIERS = (("gpt-6-luna", ("max", "xhigh"), 40, True),  # owner\x2010-05: a weak executor, for clear small tasks only; owner\x2010-25: 40 a day (was 20)
               ("gpt-6.1-sol", ("xhigh", "high"), 200, False))  # owner\x2010-05: trial executor (6-sol: too weak); 10-07: 50 a day; owner\x2010-25: 120, then 200 and xhigh first to use the weekly quota before the 10-29 reset expires (records\x2f2026-10-25-owner-terminal-efficiency-codex-dotz.json)
CODEX_HOLD_H = 24  # a line whose Codex run was sent back (rework/blocked) within this many hours is left to Claude
CLAUDE_TAG = "〔执行者：Claude〕"  # a task line Codex's sandbox cannot do (Windows env keys/venvs, SEC fetches): Codex skips it
# a task line that needs the GPU: Codex runs it without its sandbox, which blocks the GPU (owner\x2011-01 「可以给 Codex 关掉沙箱、放开显卡」,
# records\x2f2026-11-01-owner-terminal-codex-sandbox-off-gpu.json; probe: audits/2026-11-01-codex-gpu-probe-coordinator.md)
GPU_TAG = "〔显卡〕"
# what counts as a clear small task; a line saying 不用 ponytail or 从零 never does (owner\x2010-06: 只读 dropped, it shows up
# in side notes like 两处都只读, and sol takes read-only research anyway)
CODEX_SMALL = re.compile(r"^(?!.*(?:不用 ?ponytail|从零))(?:[^（]*小修|.*(?:ponytail|修补类|每日情报扫描))", re.S)  # 小修 counts in the title only (before the first （)
CODEX_WRITABLE = (WORKSPACE / "incore" / "audits", Path(ic.V["wt_root"]))  # besides the project dir (sandbox)
CODEX_SANDBOX_DIR = ic.V["wsl_home"] + "/.cache/codex-sandbox"  # WSL-local, writable in the sandbox (--add-dir), not the /tmp tmpfs; venvs and the uv cache live here
CODEX_PREAMBLE = ("你是 Codex 执行者，在 WSL 的 bash 里运行，替 Claude 执行者做这一项（Claude 额度在等待中，或另有 Claude 执行者在同时做别的项目）。Windows 路径 "
                  "C:\\X 在这里是 /mnt\x2fc/X；git 用 WSL 自带的 git。下面规则里提到的 PowerShell、Windows 版 git、gateway.py "
                  "和 send_email.py 对你不适用：你不发 Discord，也不发邮件。Windows 程序（python.exe、cmd.exe、wsl.exe）"
                  "在沙箱里启动不了（报 UtilBindVsockAnyPort），任务里写的 C:\\...\\python.exe 一律换成 WSL 的 python3；"
                  "测试缺依赖时建 venv，只能放在 @WSLHOME@/.cache/codex-sandbox/ 里（沙箱里家目录其余位置和 /tmp 都不要用：家目录只读，/tmp 是 16G 的 tmpfs，会被塞满，Codex 就全起不来）（网络可用，只装测试要的包，输入台要 aiohttp）：先 "
                  "export UV_CACHE_DIR=@WSLHOME@/.cache/codex-sandbox/uv-cache，再 @WSLHOME@/.local/bin/uv venv --clear @WSLHOME@/.cache/codex-sandbox/venv && "
                  "@WSLHOME@/.local/bin/uv pip install --python @WSLHOME@/.cache/codex-sandbox/venv/bin/python <包>，用 @WSLHOME@/.cache/codex-sandbox/venv/bin/python 跑测试。"
                  "WSL 里的 git 没配作者身份：提交一律用 git -c user.name=codex -c user.email=codex@local commit，不改 git 配置。"
                  "改 incore 里 control、backup、discord、input、notify 这些在 Windows 上跑的代码时，测试要在 Windows 上也能过：测试里调 git 用 review_packet.GIT（同文件已有 git 常量的用那个），不写裸 git；不写死 /mnt/c 路径；读写文本一律 encoding=\"utf-8\"；结果文件写一句「只在 WSL 跑过，Windows 未跑」。"
                  "其余规则照样遵守。\n\n").replace("@WSLHOME@", ic.V["wsl_home"])
EFFORTS = {"review": "xhigh", "plan": "xhigh", "coordinate": "xhigh", "owner": "xhigh", "report": "xhigh",
           "ask": "xhigh", "lessons": "medium", "meeting": "xhigh"}
RUNS_FILE = DATA / "runs.jsonl"
SESSIONS_DIR = Path(ic.V["sessions_dir"])  # claude transcripts
CLAUDE_PROJECTS = Path(ic.V["claude_projects"])  # transcripts live under the slug of the cwd a session runs in
PROJECTS = {  # executor project: (cwd, item prefixes). A todo whose text (after a leading "（返工）") starts with a
              # prefix goes to that project's resident executor; anything else to the line's "general" one at WORKSPACE
    "uvox": (WORKSPACE / "ClaudeMigration" / "project files" / "Uvox", ("Uvox",)),
    "umba": (WORKSPACE / "ClaudeMigration" / "project files" / "Umba", ("Umba",)),
    "input": (WORKSPACE / "incore" / "input", ("输入台", "语音")),
    "quant": (WORKSPACE / "projects" / "quant-calibration", ("量化",)),
    "incore": (WORKSPACE / "incore", ("组织：",)),
    # owner\x2010-01: every project gets its own session, in the folder he gave (ClaudeMigration/project files) when
    # there is one; all of them run, priority only decides who gets feedback and resources first
    "jianzhixin": (WORKSPACE / "ClaudeMigration" / "project files" / "Blade_Heart_Battle_Demo", ("项目七",)),
    "writing": (WORKSPACE / "ClaudeMigration" / "project files" / "style_rewriter",
                ("写作模型", "写作", "自媒体")),
    "delta": (WORKSPACE / "ClaudeMigration" / "project files" / "Delta", ("Delta",)),
    "thinking": (WORKSPACE / "ClaudeMigration" / "project files" / "Thinking Engines", ("Thinking Engines",)),
    "fieldlistener": (WORKSPACE / "ClaudeMigration" / "project files" / "多模态-车载模型", ("Field Observer", "车载")),
    "handover": (WORKSPACE / "ClaudeMigration" / "project files" / "technical-asset-handover",
                 ("technical-asset-handover", "技术资产交接")),
    "umzy": (WORKSPACE / "ClaudeMigration" / "project files" / "Umzy-main", ("Umzy",)),
    # owner\x2010-01: the three sign-language folders are one project; the agent starts in the only one with code
    "signlang": (WORKSPACE / "ClaudeMigration" / "project files" / "Sign-speak-streamlit-demo",
                 ("手语", "Sign-speak", "Uchu-Deaf")),
    # no folder from the owner: new project, a thesis repo that lives on his other laptop, and two activity lines
    "driving": (WORKSPACE / "projects" / "driving-course-sim", ("驾校模拟器",)),
    "kiki": (WORKSPACE / "projects" / "kiki-research", ("Kiki",)),
    "thesis": (WORKSPACE / "projects" / "thesis", ("论文",)),
    "intel": (WORKSPACE / "projects" / "intel", ("情报", "每日情报扫描", "每周简报", "动态招聘页", "机会识别")),
    "jobs": (WORKSPACE / "projects" / "job-search", ("求职", "追踪表", "博后")),
    # owner\x2010-02: company website, clone of github.com/ARBITROS-AI-COMPANY/arbitrosai.com-website (work line)
    "website": (WORKSPACE / "projects" / "arbitrosai-website", ("官网",)),
    # owner\x2010-02: Beacon Listener LLC website, clone of github.com/BeaconListener/BeaconListenerLLC (private; work line)
    "beaconlistener": (WORKSPACE / "projects" / "beaconlistener-website", ("Beacon Listener",)),
    # owner\x2010-07: FOS, Arbitros's simulation environment, from his Drive fos.zip as is (work line sprint)
    "fos": (WORKSPACE / "projects" / "fos", ("FOS",)),
    # owner\x2010-01 "all projects run": FactoryAlign paper/experiments folder (work line)
    "factoryalign": (WORKSPACE / "projects" / "factoryalign", ("FactoryAlign",)),
}
if ic.get("projects") is not None:  # a Premier instance lists its own projects; the literal above is this machine's (Premier public block 5)
    PROJECTS = {"incore": PROJECTS["incore"], **{key: (WORKSPACE / spec["cwd"], tuple(spec["prefixes"])) for key, spec in ic.get("projects").items()}}
EXECUTOR_ADD_DIRS = (WORKSPACE / "incore", Path(ic.V["wt_root"]))  # results (audits) and worktrees
PROJECT_ADD_DIRS = {"signlang": (WORKSPACE / "ClaudeMigration" / "project files" / "Sign-speak demo",
                                 WORKSPACE / "ClaudeMigration" / "project files" / "Uchu-Deaf"),
                    "writing": (WORKSPACE / "projects" / "self-media-lab",)}  # a project's other folders
EXECUTORS_PER_LINE = 2
STARVE_H = 48  # a non-sprint project with todos whose last executor run is older goes ahead of the sprint project (owner\x2010-04: the second slot goes to the others)
CROSS_STARVE_H = 6  # owner\x2010-17: overnight quant dominated; see records\x2f2026-10-17-owner-discord-overnight-quant.json
SPRINT_FIRST_H = 4  # owner\x2011-03 (records\x2f2026-11-03-owner-terminal-abcd-deploy-now.json): a line's sprint-project row that has stood as todo/review this long goes before every score and CROSS_STARVE_H
REVIEW_STARVE_H = 24 # a non-sprint [review] row whose project's last executor run ended longer ago is reviewed before the sprint's
RETIRE_AFTER_REWORKS = 2
LESSONS_DIR = WORKSPACE / "incore" / "projects" / "lessons"
BACKUP_PROCESS = WORKSPACE / "incore" / "data" / "backups" / "watcher-process.json"
BACKUP_STATUS = WORKSPACE / "incore" / "data" / "backups" / "status.json"
BACKUP_ALERT_FILE = DATA / "backup-alert.json"  # fault kinds already announced
BACKUP_CHECK_S = 60
BACKUP_STALE_S = 600
ARBITROS_RETRY_S = 75 * 60  # hourly pull + 10-minute retry + margin: one failed pull inside this is not yet an alert
WEBSITE_URL = "https://www.arbitrosai.com/api/health"
WEBSITE_ALERT_FILE = DATA / "website-alert.json"  # {"down": true} once announced
WEBSITE_CHECK_S, WEBSITE_TIMEOUT_S, WEBSITE_FAILS = 300, 10, 2  # alert after this many non-200 checks in a row
PYTHONW = ic.V["pythonw_exe"]
TICK_PY = CONTROL / "tick.py"
DETACHED_PROCESS, CREATE_BREAKAWAY_FROM_JOB = 0x00000008, 0x01000000

MODELS = {"executor": "sonnet", "review": "opus", "plan": "opus", "coordinate": "opus", "owner": "opus",
          "report": "opus", "ask": "opus", "lessons": "sonnet", "meeting": "opus"}
SONNET_DEPUTIES = ("intelligence",)  # owner\x2010-02: only the coordinator and the work and quant deputies stay on Opus;
# owner\x2010-27 terminal: life back on Opus (records\x2f2026-10-27-owner-terminal-life-deputy-opus.json)
# owner\x2010-04: the work deputy thinks at medium (still Opus); owner\x2010-25: life, intelligence, coordinator at high, quant
# keeps EFFORTS' xhigh (records\x2f2026-10-25-owner-terminal-efficiency-codex-dotz.json)
# owner\x2010-26/10-27 (records\x2f2026-10-26-owner-discord-split-ok-postprocess-prompt.json): the work line split into Arbitros 线
# (role `work`, key kept for its locks, terminal command and `role:work` trailers) and Beacon Listener 线 (role `uvox`, forked from
# the work deputy's session once; the owner calls it the Beacon Listener line); `uvox` thinks at the work deputy's medium
# owner\x2011-06 Discord: life to xhigh (records\x2f2026-11-06-owner-discord-life-effort-xhigh.json)
DEPUTY_EFFORTS = {"work": "medium", "uvox": "medium", "life": "xhigh", "intelligence": "high", "coordinator": "high"}
LINE_SECTIONS = {role: ic.get("lines").get(role) or f"（无此线：{role}）" for role in ic.ROLE_KEYS}  # role -> its backlog section, one to one (instance.json `lines`; a role without a line gets a title no backlog has)
LEGACY_SECTIONS = {"工作线": "work"}  # (line_map.LEGACY_SECTION) a backlog not yet converted by line_split.py keeps dispatching under the old title
SECTION_ROLES = {**{s: r for r, s in LINE_SECTIONS.items()}, **LEGACY_SECTIONS}  # other sections: no resident role, fresh runs
FORK_ROLES = {"uvox": "work"}  # a role that starts as a one-time fork of another role's session; it never starts blank
TIMEOUTS = {"executor": 7200, "review": 2700, "plan": 2700, "coordinate": 2700, "owner": 7200, "report": 900,
            "ask": 2700, "compact": 900, "lessons": 900, "meeting": 900}
ROLE_NAMES = {role: ic.get("role_names").get(role, role) for role in ("coordinator", "work", "uvox", "life", "quant", "intelligence")}  # instance.json `role_names`
DISCORD_CHUNK = 1900
ATTACH_RE = re.compile(r"^附件[:：]\s*(.+)$", re.M)
ATTACH_ROOTS = (WORKSPACE, Path(ic.V["wt_root"]))  # a deputy may send files from these trees only
MAX_UPLOAD_MB = 10  # Discord's default bot upload limit; larger files go by email
CREDENTIAL_RE = re.compile(r"^\.env|\.dpapi$|^02_secrets$|\.pem$|\.key$|^id_rsa|^client_secret|token|secret|password", re.I)
NOTIFIED = "〔已通知额度〕"
TOOLS = ["PowerShell", "Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebSearch", "WebFetch",
         "TodoWrite", "NotebookEdit"]
EXECUTOR_TASK = (r"代码任务开工先在仓库运行 git worktree add -b claude/<主题> " + ic.V["wt_root"] + r"\<主题> <任务指定基线>，"
                 "再进入该隔离工作树修改；已有任务分支就用 git worktree add <隔离路径> claude/<主题>，"
                 "已有隔离工作树可核对分支后复用。WSL 把 Windows 路径换成 /mnt\x2fc/ 下的对应路径。"
                 "孵化器主目录供生产服务加载，执行者禁止在其中 checkout/switch 或修改入库代码。"
                 "每次提交前先运行 pwd（PowerShell 用 Get-Location）和 git rev-parse --show-toplevel，"
                 r"确认当前目录和仓库根目录都不是 " + ic.V["workspace"] + r"\incore（WSL 对应路径同样禁止），"
                 "仓库根目录必须是 wt 下的隔离工作树；不满足就停止提交。"
                 r"pytest 等测试的临时目录（--basetemp、tmp_path、fixtures 里的 pytest-cases 之类）放 ~/.cache 或 C:\orghome\wt\ 下，不放进孵化器目录；"
                 "WSL 在孵化器里建的符号链接，Windows 读不了，会让备份整轮失败。"
                 "改的是 control/ 或 input/ 下的代码，交审前先把 master 合进本分支、解开冲突、重跑测试。"
                 "任务：{item}。先读这个任务引用的文件{lessons}；不要读整份 incore/projects/backlog.md。"
                 "完成后按规则把结果文件写到 {audits}\\<日期>-<主题>.md。{fresh}")
PROMPTS = {
    "executor": ("你是{name}这条线（{section}）里项目 {project} 的常驻执行者，当前工作目录就是这个项目的目录。"
                 "同一会话接着做这个项目的后续任务和返工。" + EXECUTOR_TASK),
    "executor_general": ("你是{name}这条线（{section}）的通用执行者，当前工作目录是工作区根目录，不属于专属项目的任务由你做。"
                         "同一会话接着做本线后续的通用任务和返工。" + EXECUTOR_TASK),
    "lessons": ("你是项目 {project} 的常驻执行者。这个会话因连续被判返工要被换掉。请写出不超过 5 行、具体且可复用的经验："
                "该看哪些文件、该跑哪些命令、容易踩的坑、审查员驳回过什么。只回复这几行，每行一条，不要改任何文件，不要做别的事。"),
    "review": ("你是{name}，验收你这条线的这一项：{item}。打开它引用的结果文件和分支，抽查关键结论（重跑测试），"
               "然后编辑 backlog.md 把这一行改为 done 或 todo（附返工说明），补上可逆、本地、不需要 owner 决定的后续任务；"
               "需要 owner 决定的写成 blocked: 等你决定。有实质进展时按规则在你这条线的 Discord 频道发一条消息；本地 23:00–08:00 不发进度，写进审查文件，由 09:00 日报带出。生产事故、owner 当时主动来问的回复照发。"
               "新增或返工的 todo 行以项目名开头（Uvox、Umba、输入台、语音、量化、组织：、项目七、驾校模拟器、论文、情报、求职、自媒体），定时器据此派给该项目的执行者。"
               "最后一行单独写 VERDICT: pass、VERDICT: rework 或 VERDICT: blocked。"),
    "coordinate": ("你是总理。跨线复盘（每 8 小时一次）：先读 control/data/review-stats.md（脚本算好的各线和各引擎的次数、花费、一次通过率，Codex 每条的审查结论，久未执行的项目，卡住的行；这些数直接引用，不要再手算）和 control/data/kpi.md，再看 backlog.md 各小节和 deputies.json。"
                   "同时读 control/data/project-delivery.md，逐项目核对距上次交付的天数和过审未合分支数；超过 7 天或 5 条的在复盘中逐项标出，"
                   "超过 5 条暂停自发新草样，先把已有分支叠成可用版本；owner 交办、返工和修复照常。未知不是 0。"
                   "只做三件事：1) 按 owner 意图和 KPI 调整各小节的 `权重：NN%`，每次改动在 audits/coordinator-log.md 记一行理由；"
                   "2) 要给某条线留指示，就写成该小节的 todo；3) 新出现的需要 owner 决定的事汇总成一条 Discord 总理频道消息，"
                   "没有新的就不发。另外在记录里列出有待办却 3 天以上没派到执行的项目（冲刺之外的项目不能一直被晾着）。"
                   "review-stats.md 的「空转」一节里，哪条线没活（没 todo、没待审、没在跑）已超过 2 小时、而且五小时窗口的用量低于节奏线，"
                   "你必须替它写 todo（以项目名开头，定时器据此派活），不能只记一笔；owner 要的是不停。"
                   "不做执行工作，不改代码。"),
    "plan": ("你是{name}。{section} 目前没有可执行任务。阅读 incore/projects/backlog.md、incore/records/ 里 owner "
             "最近的指令和 incore/audits/ 的最新审计，按 owner 意图为这一条线补 2–4 个可逆、本地、不需要 owner 决定的 todo"
             "（执行受阻时就安排研究、信息搜集、机会识别、流程优化或经验总结），写在该小节末尾，然后结束。"
             "本小节「暂停项目」行里的项目不补行、不从长期池取。"
             "新增的 todo 行以项目名开头（Uvox、Umba、输入台、语音、量化、组织：、项目七、驾校模拟器、论文、情报、求职、自媒体），定时器据此派给该项目的执行者。"
             "冲刺项目的行要加〔执行者：Claude〕标签时，行里写一句为什么非 Claude 不可，写不出来就不标。"
             "Codex 周额度到闸门期间（CODEX_WEEKLY_MAX），只读调研、批量生成、批量标注、离线数据核对、能在 dotz 自己环境里跑的测试，"
             "一律写成行末标〔执行者：dotz〕的 todo，一次可以排多行（owner\x2011-05 终端「更充分地利用 Dotz……缓解额度的瓶颈」）；"
             "行里不要出现 records/、input/data/、discord/data/、review-audio/、凭据、生产、花钱、对外联系字样，Kiki 的行不派。"),
    "codex_plan": ("你是{name}。Codex 现在没有能领的行（owner\x2011-01 终端：Codex 并行位提到 20，要在 reset 前把 Codex 周额度猛用掉，"
                   "records\x2f2026-11-01-owner-terminal-codex-slots-20-now.json）。读 incore/projects/backlog.md 的「{section}」、"
                   "本线项目的长期池 incore/projects/pools/、意图卡和最新审计，只改本小节，做两件事然后结束："
                   "本小节「暂停项目」行里的项目不补行、不从长期池取。"
                   "1) 本小节标了〔执行者：Claude〕的 todo，逐行看是否真的需要 Windows 虚拟环境、Windows 用户环境变量里的 key、`claude -p`、"
                   "只有 Windows 才跑得了的测试或 SEC 抓取；都不需要的，去掉这个标签，行的其余文字不动；"
                   "2) 在本小节末尾补 3–6 个 Codex 能领的 todo：以项目名开头，不标〔执行者：Claude〕〔执行者：Opus〕〔执行者：Haiku〕〔执行者：dotz〕，"
                   "在 WSL 里能做完，可逆、本地、不需要 owner 决定，验收逐项编号，单行估计执行 $1.5–10；同一项目同时只跑一个执行者，"
                   "所以尽量分散到不同项目。不碰生产、凭据，不花钱，不对外联系。"
                   "写 todo 前核对可开工：有前置依赖的任务，依赖的审查或合并已完成才写 todo；未完成的写成 [blocked: ...] 并写清在等什么。"
                   "要提交的仓库的 Git 元数据须在 Codex 沙箱内可写；不确定就写成只读产出，或注明在 wt 下另起仓库。执行者标签只放在任务行末尾。"
                   "冲刺项目的行要加〔执行者：Claude〕标签时，行里写一句为什么非 Claude 不可，写不出来就不标。"
                   "例外：Codex 周额度到闸门期间（CODEX_WEEKLY_MAX），只读调研、批量生成、批量标注、离线数据核对、能在 dotz 自己环境里跑的测试，"
                   "一律写成行末标〔执行者：dotz〕的 todo，一次可以排多行（owner\x2011-05 终端「更充分地利用 Dotz……缓解额度的瓶颈」）；"
                   "行里不要出现 records/、input/data/、discord/data/、review-audio/、凭据、生产、花钱、对外联系字样，Kiki 的行不派。"),
    "owner": ("你是{name}（常驻副总理）。owner 刚在 Discord {role} 频道直接给你的指令：{item}。"
              "按规则处理；你的最后一条消息会原样发回该频道（单独一行 `附件：<绝对路径>` 会作为文件发给他，每个 ≤10 MB）。"
              "读的人是 owner 本人，他的注意力最贵：第一眼要看懂，不写给审查员看的标签（VERDICT、返工、回路编号、内部状态词）。"),
    "ask": ("你是总理。现在是 {now}（America/New_York）。副总理（或 owner 让副总理转来）有 {n} 条问题等你回答，"
            "写在 backlog.md 的 `- [blocked: 等总理] <项目名>：<问题、选项、提问方的建议、相关文件>` 行：\n{rows}\n"
            "逐条回答：先读该行引用的文件和 incore/records/ 里 owner 的相关指令，缺事实就先只读查证。每条三选一，直接编辑 backlog.md 里这一行："
            "1) 你能定的：改成 `- [done {stamp}：总理答 <结论和理由>] <原文>`；需要提问那条线去做的事，在那一小节另写成 todo（以项目名开头，定时器据此派活）。"
            "2) 需要 owner 定的（花钱、对外、实盘下单、改组织，或 owner 写过要他批的）：改成 `- [blocked: 等你决定] <原文>（总理建议：…）`，"
            "写清选项和你的建议；不要写“不答就按默认”，沉默不算同意。owner 明确交给总理批的事不在此列，由你在他定的边界内自己批"
            "（例如 10-02 的“量化实盘自动下单授权范围”，records\x2f2026-10-02-quant-account-opening-answers.json 的 Q2）；你自己不下任何订单。"
            "3) 这条已经不成立或重复：改成 `- [done {stamp}：总理答 不再需要，<原因>] <原文>`。"
            "每条答复在 {log} 末尾追加一行（`- {stamp} 答复 <项目名>：<结论>`）。需要 owner 定的按规则合并成一条总理频道 Discord 消息"
            "（每次运行最多一条，写清选项和建议），没有就不发。不做执行工作，不改代码，只改这几行和对应小节的新 todo。"
            "最后一条消息逐条写结论。"),
}
REVIEW_AS_TRIAL = ("审查的目的（owner\x2010-04）：代替 owner 试用，判断成果是否符合他定下的意图，能不能交给他直接对外发布或使用，"
                   "哪怕不完美。像他本人那样实际用一遍：打开、运行、点一遍、读一遍。不要逐行挑细节和小 bug；只有挡住使用、偏离意图，"
                   "或碰了“必须遵守”（花钱、安全、数据、在跑的服务）的问题才判 rework，其余小问题写成后续 todo，照样判 pass。")
SPRINT_REVIEW = ("这一项属于本线的冲刺项目（见本小节开头的“冲刺：”行和它的版本目标）。验收后在同一次运行里接着写这个项目的下一条 "
                 "todo，直到版本目标达成；达成时把能试用的版本（一条命令或网址）和请他看什么，发到你这条线的 Discord 频道请 owner 审，"
                 "并在“冲刺：”行末尾注明“已交付 MM-DD，等 owner 反馈”。换冲刺项目要 owner 批，不要自己改。")
# owner\x2010-12: every deputy reports twice a day at fixed times (morning = since last night, evening = the daytime + blockers
# to clear before bed); one combined email per slot, Discord per deputy channel. The prompts below are (morning, evening).
REPORT_SLOTS = ("09:00", "21:00")  # machine local time (America/New_York)
REPORT_ROLES = ("coordinator", "work", "uvox", "quant", "intelligence", "life")  # run order, and section order in the email
REPORT_WINDOW_MIN = 60  # a report posted this long after its slot is labelled 补发
# owner\x2011-01 (records\x2f2026-11-01-owner-terminal-codex-reset-pressed-two-cards.json): remaining cards' local expiry times.
# The dispatch gate stays at CODEX_WEEKLY_MAX (unreadable: it runs, as before), cached for CODEX_PROBE_S.
# Daily report facts use codex_burn.reset_note's 95%/36 h and final-24 h net-gain rules; owner presses the cards himself.
CODEX_RESETS, CODEX_WEEKLY_MAX, CODEX_PROBE_S = ("2026-11-30 15:00", "2026-12-01 17:33"), 95.0, 300
CODEX_PROBE = {"at": -1e9, "used": None, "behind": None}  # last probe: time.time() and the weekly used_percent (None = unreadable)
REPORT_EMAIL_WAIT_MIN = 90  # the slot's email goes out without the reports still missing this long after the slot
# life-line pushes (owner\x2010-03 "全按建议", audits/2026-10-03-life-alignment-proposal.md): read-only runs in the life
# deputy's session, Discord life channel only, never email. Slots share reports.json under "push:<name>".
LIFE_MORNING = "08:30"
LIFE_MEALS = {"lunch": "12:00", "dinner": "18:30"}  # meal times; the push goes LIFE_MEAL_LEAD_MIN before
LIFE_MEAL_LEAD_MIN = 30
LIFE_NIGHT = "23:00"
LIFE_PUSHES_ON = ("lunch", "dinner", "night")  # owner\x2010-12: no 08:30 briefing, the 09:00 report covers it (code and prompt kept)
PUSH_MAX_CHARS = 300  # a push reply longer than this is not a reminder (10-12 the "send was blocked" explanation went out as one)
PUSH_LATE_MIN = 30  # a push sent this long after its slot is labelled 补发
PUSH_EXPIRE_MIN = 90  # ...and not sent at all once this long after its slot (a lunch tip at 15:00 is noise): marked missed
REPORT_FILE = DATA / "reports.json"
REPORT_LOCK = DATA / "report.lock"
LIFE_DATES_FILE = WORKSPACE / "incore" / "projects" / "life" / "dates.md"
LIFE_DATES_REPORTED_FILE = WORKSPACE / "incore" / "projects" / "life" / "dates-reported.json"
REPORT_TOOLS = ["Read", "Glob", "Grep"]
EMAIL_SCRIPT = WORKSPACE / "incore" / "notify" / "send_email.py"
_REPORT_FACTS = ("先读本提示开头的“今日数据”块（脚本从账本和 backlog 整理好的：{span}通过、返工、受阻的审查，等 owner 的行，"
                 "这段时间的结果文件，花费），它是事实来源，不要再翻账本；挑对 owner 有用的几条用自己的话写，不要逐条转抄，"
                 "要引用细节时再读对应的结果文件。没有这个块时才依据 incore/projects/backlog.md（{span}的 done/blocked 行）"
                 "和 incore/audits/ 里这段时间的文件。")
_OWNER_READS = ("读的人是 owner 本人，他的注意力最贵：第一眼要看懂，不写给审查员看的标签（VERDICT、返工、回路编号、内部状态词）。"
                 "提示开头另有‘机器和备份’行；有异常时先写异常项，数据缺失就写无法核实，不猜。"
                 "「今日数据」里事项前面的方括号编号（如 [D12]）是他在「要你的」页上看到的卡片编号：写到那件事时照抄编号，他可以直接回编号。")
_REPORT_BLOCKER = "每条写清是什么、有哪几个选项、你的建议、打开命令（可直接粘贴的 PowerShell 命令，绝对路径）"
REPORT_PROMPT = (  # (09:00, 21:00)
    "你是{name}。写 {date} {slot} 给 owner 的日报（早报，覆盖{span}，也就是昨晚）。读者是 owner 本人：他要的是结果和要他做的事，"
    "不关心内部怎么排队、审查、控速。只写这几块，没有就省略：1) 昨晚完成的、能用或值得他看的结果，每条一句话说清是什么、"
    "对他有什么用，附文件路径或链接；2) 接下来 12 小时要推进的项目（早报写白天），每个项目一句：它服务 incore/projects/intent/<项目>.md 意图卡里 owner 的哪一条原话、现在离那条还差什么、这 12 小时具体做什么；说不出对应哪条意图和差距的项目不推进、不写进来；本线小节开头「暂停项目」行里列的项目不写进计划、不催 owner，只在日报最后用一行列出它们的名字（「暂停中：…」），等 owner 说恢复；3) 需要他决定或操作的，最多一件，"
    "写清选项、你的建议和截止时间。不要出现定时器、节奏线、审查、返工、backlog、会话、排队这类内部词。" + _OWNER_READS + _REPORT_FACTS
    + "不超过 400 字，中文，直接写正文。日报每天必须发：昨晚没有新结果就用一句话说没有新结果，再写接下来 12 小时的项目计划，"
      "以及在等他哪个决定（如果有）。",
    "你是{name}。写 {date} {slot} 给 owner 的日报（晚报，覆盖{span}，也就是白天）。读者是 owner 本人：他要的是结果和要他做的事，"
    "不关心内部怎么排队、审查、控速。他看完这份、处理完卡点就去睡。只写这几块，没有就省略：1) 今天白天完成的、能用或值得他看的结果，"
    "每条一句话说清是什么、对他有什么用，附文件路径或链接；2) 接下来 12 小时要推进的项目（晚报写今晚），每个项目一句：它服务 incore/projects/intent/<项目>.md 意图卡里 owner 的哪一条原话、现在离那条还差什么、这 12 小时具体做什么；说不出对应哪条意图和差距的项目不推进、不写进来；本线小节开头「暂停项目」行里列的项目不写进计划、不催 owner，只在日报最后用一行列出它们的名字（「暂停中：…」），等 owner 说恢复；"
    "3) 需要他现在马上处理的卡点，" + _REPORT_BLOCKER + "，没有就写一句“今晚没有要你马上处理的事”。"
    "不要出现定时器、节奏线、审查、返工、backlog、会话、排队这类内部词。" + _OWNER_READS + _REPORT_FACTS
    + "不超过 500 字，中文，直接写正文。日报每天必须发：白天没有新结果就用一句话说没有新结果，再写接下来 12 小时的项目计划和卡点。"
      "本线有不止一个项目时，最后单独一行写“明天冲刺：〈项目〉，版本目标：〈给你审的是什么〉”，继续当前冲刺也写，"
      "请他回“批”或改；他批了才改本线的冲刺项目。")
_INTEL_BODY = (
    # owner\x2010-16: concise two-part report; actions first, then links tagged for the relevant line
    "先读 incore/projects/intel-standard.md（标准和写法）、"
    "incore/projects/intel-decisions.md（决策清单 D1–D8）和 incore/data/intel-pushed.jsonl（已报过的），"
    "再读{span}新增或更新的 incore/audits/ 里的情报文件（每日扫描、周简报、调研）。挂不上任何决策的不写，"
    "已报过且没有新事实的不写；几条合起来说明一个趋势的，合成一条。严格按 intel-standard §3 分成上下两段："
    "首行格式为“情报 MM-DD｜要动 N 条，线索 M 条”。上段列要 owner 或某条线采取的动作、决定，最多 5 条，有截止日的排前；"
    "每条一行，格式为“给谁｜做什么｜截止 MM-DD”（没有截止日写“无”），早晚报原本单列的“要你决定”也并入这里，不再另列。"
    "下段以“线索：”开头，一行一个来源链接，末尾标注交给哪条线。不要写总览、把握、未核、A 档/B 档、分数、事件键或扫描过程。" + _OWNER_READS)
_INTEL_END = ("动作和线索都没有时，只写一行“今天没有要你动的事”，再写下一个值得盯的日期和对应的决策。中文，直接写正文。")
REPORT_PROMPT_INTELLIGENCE = (
    "你是情报专员。写 {date} {slot} 给 owner 的情报日报（早报，只写{span}的新情报，也就是昨晚的）。" + _INTEL_BODY
    + _INTEL_END,
    "你是情报专员。写 {date} {slot} 给 owner 的情报日报（晚报，只写{span}的新情报，也就是白天的）。他看完这份、处理完卡点就去睡。" + _INTEL_BODY
    + _INTEL_END)
REPORT_PROMPT_COORDINATOR = (
    "你是总理。写 {date} {slot} 的总理早报。" + _OWNER_READS + "1) 昨晚（{span}）完成了什么，只列对 owner 有用的几条，每条一句话加文件路径；"
    "2) 今天白天的计划，最多三条，一句话一条。事实取自本提示开头“今日数据”块（没有这个块时才读 backlog.md）。"
    "不超过 300 字。早报每天必须发：昨晚没有新结果就写一句“昨晚没有新结果”，再写白天的计划。",
    "你是总理。写 {date} {slot} 的总理汇总（晚报，他看完、处理完就去睡）。" + _OWNER_READS + "分三块：1) 今天白天组织线完成了什么，只列对 owner 有用的几条，"
    "每条一句话加文件路径；2) 今天晚上的计划，最多三条，一句话一条；3) 现在所有需要 owner 决定或操作的事项，按优先级排序，"
    "每条写清是什么、选项、你的建议、打开命令（可直接粘贴的 PowerShell 命令，绝对路径）和文件路径"
    "（事项取自本提示开头“今日数据”块里“现在等你决定的行”和“另有等你操作的行”和判返工、受阻的审查，"
    "没有这个块时才读 backlog.md）；另外一行写今天五小时窗口的用量概况（块里的用量行，或 control/data/usage.jsonl）；"
    "再用一行报各线空转时长（review-stats.md「空转」一节：哪条线没活、已多久、用量是否低于节奏线，以及你替它补了什么 todo；都没有空转就写“没有线空转”）。"
    "不超过 400 字。汇总每天必须发：没有待决事项就写一句“今天没有需要你决定的事项”，再附用量概况。")
REPORT_SUNDAY_COORDINATOR = ("最后一行写：本周跑了几个回路、拿到几个现实信号（读 incore/projects/feedback-log.md），"
                             "离当前阶段的里程碑还差什么（读 audits/2026-10-09-breakthrough-plan.md 第四节）。")
PUSH_NAMES = {"morning": "早简报", "lunch": "午饭提示", "dinner": "晚饭提示", "night": "收工提醒"}
PUSH_PROMPTS = {
    "morning": ("写今天（{date}）给 owner 的早简报，只写正文。试运行期间只写两块，每块一两句：今天吃什么"
                "（读 incore/projects/life/default-meals.md）；今天和未来三天的截止（读 incore/projects/life/ 里的到期表和 "
                "incore/projects/backlog.md 里你线上的日期）。不写练什么，也不写天气。不超过 100 字，中文，直接写正文。"),
    "lunch": ("现在是午饭前 30 分钟。给 owner 一两个吃的选项，只写正文：读 incore/projects/life/default-meals.md，"
              "从默认餐单里挑（别和你上一次推的重复），每个一行：店名、点什么、步行多远。只给选项，不替他下单。不超过 80 字，中文，直接写正文。"),
    "dinner": ("现在是晚饭前 30 分钟。给 owner 一两个吃的选项，只写正文：读 incore/projects/life/default-meals.md，"
               "从默认餐单里挑（别和你上一次推的重复），每个一行：店名、点什么、步行多远。只给选项，不替他下单。不超过 80 字，中文，直接写正文。"),
    "night": ("现在是收工时间。写给 owner 的收工提醒，只写正文：先列明天最重要的三件事（依据 incore/projects/backlog.md "
              "里需要他做或决定的、incore/audits/ 里今天的文件和你的 last；只列真实有的，不够三件就少列），再一句“去睡了”。"
              "不要在这条里问他做任何决定。不超过 100 字，中文，直接写正文。"),
}
PUSH_PROMPTS = {k: v + "不要自己调用 gateway.py 或任何发送命令，你的回复会由脚本发到生活频道；回复里只写提醒正文，不要写别的说明。"
                for k, v in PUSH_PROMPTS.items()}
SKIP_PREFIXES = ("running", "done", "blocked", "excluded", "each tick", "done-ref")
CREATE_NO_WINDOW = 0x08000000
# ----------------

sys.path.insert(0, str(Path(__file__).resolve().parent))  # own directory, so a worktree tests its own modules; data paths still use CONTROL
import backlog_archive
import backlog_history
import backlog_io  # noqa: E402
import timed_rows  # noqa: E402
import codex_burn  # noqa: E402
import dots_watch  # noqa: E402
import dots_review  # noqa: E402
import daily_health  # noqa: E402
import life_dates  # noqa: E402
import machine_watch  # noqa: E402
import kpi  # noqa: E402
import line_map  # noqa: E402
import review_packet  # noqa: E402
import project_delivery  # noqa: E402
import usage_sample  # noqa: E402  (stdlib-only; reused for rate_limit_event parsing and the sampler call)
import review_stats  # noqa: E402
import shadow_review  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "input"))
import pending  # noqa: E402  (the owner's replies on the 等你决定 page, and the one count of those rows)

OWNER_DATA = WORKSPACE / "incore" / "data"  # owner-replies.json: written by the input server, read here

LINE_RE = re.compile(r"^- \[(.+?)\] (.+)$")
TRAILER_RE = re.compile(r"〔msg:(\S+) role:(\w+)〕")
TIME_WINDOW_TAG_RE = re.compile(r"〔时段：([^〕]*)〕")
TIME_WINDOW_RE = re.compile(r"(\d{2}):(\d{2})–(\d{2}):(\d{2})")
OWNER_WORDS_HEADING = "## 你的指令（最优先）"
OWNER_WORDS_LIMIT = 30
OWNER_WORDS_CHARS = 300  # one terminal verbatim longer than this is cut; the full text stays in its record
DISCORD_EPOCH_MS = 1_420_070_400_000
OWNER_WORDS_TIME_RE = re.compile(
    r"(?<!\d)(?:(?P<year>20\d{2})-)?(?P<month>\d{2})-(?P<day>\d{2})[ T]"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?(?P<zone>Z|[+-]\d{2}:\d{2})?"
)
OWNER_WORDS_ROLE_LABELS = (("Beacon Listener 副总理", "uvox"), ("Uvox 副总理", "uvox"),
                           ("Arbitros 副总理", "work"), ("工作副总理", "work"), ("生活副总理", "life"),
                           ("量化副总理", "quant"), ("情报专员", "intelligence"), ("总理", "coordinator"),
                           ("coordinator", "coordinator"), ("work deputy", "work"), ("life deputy", "life"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tag_text(item: str) -> str:
    """Ignore corner-quoted references when reading this row's dispatch tags."""
    return re.sub(r"「[^」]*」", " ", item)


def owner_words_source(line: str) -> tuple[str | None, bool]:
    """Return (source role, has a Discord/terminal source marker) for an owner instruction row."""
    trailer = TRAILER_RE.search(line)
    if trailer:
        return trailer.group(2), True
    source = re.search(r"【Discord[·・:： ]([^】]+)】", line, re.I)
    if source:
        label = source.group(1)
    elif re.search(r"Discord.{0,24}(?:频道|channel)", line, re.I):
        label = line
    elif re.search(r"终端|PowerShell|terminal", line, re.I):
        return None, True
    else:
        return None, False
    return next((role for name, role in OWNER_WORDS_ROLE_LABELS if name in label), None), True


def owner_words_time(line: str, now: datetime) -> datetime | None:
    """Read an owner-row timestamp from its Discord snowflake or explicit terminal date/time."""
    trailer = TRAILER_RE.search(line)
    if trailer:
        try:
            millis = (int(trailer.group(1)) >> 22) + DISCORD_EPOCH_MS
            return datetime.fromtimestamp(millis / 1000, timezone.utc)
        except (OverflowError, ValueError):
            return None
    local_now = now.astimezone() if now.tzinfo else now.replace(tzinfo=datetime.now().astimezone().tzinfo)
    match = OWNER_WORDS_TIME_RE.search(line)
    if match:
        year = int(match.group("year") or local_now.year)
        zone = match.group("zone") or ""
        if zone == "Z":
            zone = "+00:00"
        value = (f"{year}-{match.group('month')}-{match.group('day')}T{match.group('hour')}:{match.group('minute')}"
                 f":{match.group('second') or '00'}{zone}")
        try:
            stamp = datetime.fromisoformat(value)
            if not stamp.tzinfo:
                stamp = stamp.replace(tzinfo=local_now.tzinfo)
            if not match.group("year") and stamp > local_now + timedelta(days=1):
                stamp = stamp.replace(year=year - 1)
        except ValueError:
            return None
        return stamp.astimezone(timezone.utc)
    if not re.search(r"终端|PowerShell|terminal", line, re.I):
        return None
    # Terminal entries can cite a date separately from their time; use the row's status time in that case.
    date_match = re.search(r"(?<!\d)(?:(20\d{2})-)?(\d{2})-(\d{2})", line)
    status_time = re.search(r"^- \[[^]]*?\b(\d{1,2}):(\d{2})\b", line)
    if not date_match or not status_time:
        return None
    try:
        year = int(date_match.group(1) or local_now.year)
        stamp = datetime(year, int(date_match.group(2)), int(date_match.group(3)), int(status_time.group(1)),
                         int(status_time.group(2)), tzinfo=local_now.tzinfo)
        if not date_match.group(1) and stamp > local_now + timedelta(days=1):
            stamp = stamp.replace(year=year - 1)
        return stamp.astimezone(timezone.utc)
    except ValueError:
        return None


def terminal_owner_words(local_now: datetime, cutoff: datetime) -> list[tuple[datetime, str]]:
    """owner-terminal records from the last 24 h as (time, line); read-only."""
    rows = []
    for path in RECORDS_DIR.glob("*owner-terminal*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        words = rec.get("owner_verbatim")
        if words is None and rec.get("speaker") == "owner":
            words = rec.get("verbatim")
        if words is None:
            continue
        words = " ".join(str(words).split())
        if not words:
            continue
        if len(words) > OWNER_WORDS_CHARS:
            words = f"{words[:OWNER_WORDS_CHARS]}…（全文 records/{path.name}）"

        stamp = None
        for value in (rec.get("recorded_at"), rec.get("time_local")):
            value = str(value or "").strip()
            if "T" not in value and " " not in value:
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            stamp = (parsed.replace(tzinfo=local_now.tzinfo) if parsed.tzinfo is None else parsed).astimezone(timezone.utc)
            break

        if stamp is None:
            day = None
            try:
                day = datetime.strptime(str(rec.get("date") or ""), "%Y-%m-%d").date()
            except ValueError:
                date_match = re.match(r"^(\d{4}-\d{2}-\d{2})", path.name)
                if date_match:
                    try:
                        day = datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
                    except ValueError:
                        pass
            if day is None:
                continue

            clock = re.fullmatch(r"(\d{1,2}):(\d{2}|\d[xX])", str(rec.get("time_local") or "").strip())
            hour, minute = 0, 0
            if clock:
                hour = int(clock.group(1))
                minute_text = clock.group(2)
                minute = int(minute_text) if minute_text.isdigit() else int(minute_text[0]) * 10
            try:
                stamp = datetime(day.year, day.month, day.day, hour, minute, tzinfo=local_now.tzinfo).astimezone(timezone.utc)
            except ValueError:
                continue
        if cutoff <= stamp <= local_now.astimezone(timezone.utc):
            rows.append((stamp, f"- 【终端】{path.name}：{words}"))
    return rows


def owner_words_block(text: str, role: str, now: datetime | None = None) -> str:
    """The last 24 h of other-channel/terminal owner rows, capped at 30 exact backlog lines."""
    local_now = datetime.now().astimezone() if now is None else (
        now.astimezone() if now.tzinfo else now.replace(tzinfo=datetime.now().astimezone().tzinfo))
    cutoff = local_now.astimezone(timezone.utc) - timedelta(hours=24)
    text += "\n" + OWNER_WORDS_HEADING + "\n" + "\n".join(
        row["raw"] for row in backlog_history.archived_rows(BACKLOG)
        if row["section"] == OWNER_WORDS_HEADING[3:])
    rows, in_owner_section = [], False
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("## "):
            in_owner_section = line.strip() == OWNER_WORDS_HEADING
            continue
        if not in_owner_section or not line.startswith("- "):
            continue
        source_role, sourced = owner_words_source(line)
        if not sourced or source_role == role:
            continue
        stamp = owner_words_time(line, local_now)
        if stamp is not None and cutoff <= stamp <= local_now.astimezone(timezone.utc):
            rows.append((stamp, line))
    rows += terminal_owner_words(local_now, cutoff)
    if not rows:
        return ""
    rows.sort(key=lambda row: row[0])
    omitted = max(0, len(rows) - OWNER_WORDS_LIMIT)
    lines = [line for _, line in rows[-OWNER_WORDS_LIMIT:]]
    block = "\n\n【owner 最近24小时来自其他频道或终端的原话】\n" + "\n".join(lines)
    return block + (f"\n另有 {omitted} 条。" if omitted else "")


def coordinator_answers_block(text: str, role: str, now: datetime | None = None) -> str:
    """Recent coordinator answers for this line, using snapshots only for undated owner decisions."""
    section = LINE_SECTIONS.get(role)
    if not section or role == "coordinator":
        return ""
    local_now = local_datetime(now)
    if local_now.tzinfo is None:
        local_now = local_now.astimezone()
    now_utc = local_now.astimezone(timezone.utc)
    cutoff = now_utc - timedelta(hours=24)

    def section_rows(snapshot: str) -> dict[tuple[str, int], tuple[str, int, str]]:
        rows, counts, in_section = {}, {}, False
        for number, raw in enumerate(snapshot.splitlines(), 1):
            line = raw.rstrip("\r")
            if line.startswith("## "):
                in_section = line[3:].strip() == section
                continue
            match = LINE_RE.match(line) if in_section else None
            if match:
                item = re.sub(r"[（(]总理建议[:：].*$", "", match.group(2)).strip()
                occurrence = counts.get(item, 0)
                counts[item] = occurrence + 1
                rows[(item, occurrence)] = (match.group(1).strip(), number, line)
        return rows

    def done_answer(status: str) -> bool:
        return bool(re.match(r"^done(?:\s|$)", status) and "总理答" in status)

    def owner_decision(status: str, line: str) -> bool:
        return bool(re.match(r"^blocked[:：]\s*等你决定", status) and "总理建议" in line)

    def answer_time(status: str) -> datetime | None:
        dated = re.search(r"(?:(20\d{2})-)?(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})", status)
        if dated:
            year = int(dated.group(1) or local_now.year)
            try:
                stamp = datetime(year, int(dated.group(2)), int(dated.group(3)), int(dated.group(4)),
                                 int(dated.group(5)), tzinfo=local_now.tzinfo)
                if not dated.group(1) and stamp > local_now + timedelta(days=1):
                    stamp = stamp.replace(year=year - 1)
                return stamp.astimezone(timezone.utc)
            except ValueError:
                pass
        clock = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", status)
        if clock:
            try:
                stamp = local_now.replace(hour=int(clock.group(1)), minute=int(clock.group(2)), second=0, microsecond=0)
            except ValueError:
                return None
            if stamp > local_now + timedelta(minutes=1):
                stamp -= timedelta(days=1)
            return stamp.astimezone(timezone.utc)
        return None

    def excerpt(status: str, line: str) -> str:
        if "总理答" in status:
            value = status.rsplit("总理答", 1)[1]
        else:
            value = line.rsplit("总理建议", 1)[1]
        return value.split("]", 1)[0].strip(" ：:；;")[:200]

    current_rows = section_rows(text)
    answers = []
    waiting_for_snapshot = {}
    for key, row in current_rows.items():
        status, _, line = row
        if done_answer(status):
            stamp = answer_time(status)
            if stamp is not None and cutoff <= stamp <= now_utc:
                item = key[0]
                project = re.split(r"[：:]", item, maxsplit=1)[0].strip()
                answers.append((stamp, project, excerpt(status, line),
                                f"incore/projects/backlog.md:{row[1]}"))
        elif owner_decision(status, line) and answer_time(status) is None:
            waiting_for_snapshot[key] = row

    snapshots = []
    if waiting_for_snapshot:
        for path in BACKLOG_HISTORY.glob("backlog-*.md"):
            match = re.fullmatch(r"backlog-(\d{8})T(\d{6})\.(\d+)Z\.md", path.name)
            if not match:
                continue
            stamp = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S%f").replace(tzinfo=timezone.utc)
            if cutoff <= stamp <= now_utc:
                snapshots.append((stamp, path))
        snapshots.sort(reverse=True)

    for stamp, path in snapshots:
        if not waiting_for_snapshot:
            break
        try:
            rows = section_rows(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        for key, row in list(waiting_for_snapshot.items()):
            old = rows.get(key)
            if old is None:
                continue
            if owner_decision(old[0], old[2]) and excerpt(old[0], old[2]) == excerpt(row[0], row[2]):
                continue
            del waiting_for_snapshot[key]
            if ASK_RE.match(old[0]):
                item = key[0]
                project = re.split(r"[：:]", item, maxsplit=1)[0].strip()
                answers.append((stamp, project, excerpt(row[0], row[2]),
                                f"incore/projects/backlog.md:{row[1]}"))
    if not answers:
        return ""
    lines = [f"- {project}｜答复时间 {stamp.astimezone(local_now.tzinfo):%m-%d %H:%M}｜答复摘录（前200字）：{preview}｜全文见 {ref}"
             for stamp, project, preview, ref in sorted(answers, key=lambda answer: answer[0])]
    return "\n\n【总理最近24小时给本线的答复】\n" + "\n".join(lines)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def log(decision: str, reason: str = "", **fields) -> None:
    append_jsonl(LOG_FILE, {"at": now_iso(), "decision": decision, "reason": reason, **fields})


# ---- lock ----
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                         text=True, creationflags=CREATE_NO_WINDOW).stdout
    return f'"{pid}"' in out


def lock_live(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    return pid_alive(pid)


def take_lock(path: Path | None = None) -> bool:
    path = path or LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if lock_live(path):
        return False
    path.write_text(str(os.getpid()))
    return True


def release_lock(path: Path | None = None) -> None:
    path = path or LOCK_FILE
    try:
        if path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


# ---- usage / pacing ----
def sample_usage() -> tuple[float, int] | None:
    """One haiku call; appends the observation (source 'tick') and returns (percent, resets_at)."""
    try:
        done = subprocess.run([usage_sample.CLAUDE, *usage_sample.ARGS], input=usage_sample.PROMPT,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=usage_sample.TIMEOUT_SECONDS, creationflags=CREATE_NO_WINDOW,
                              cwd=str(WORKSPACE))
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = usage_sample.five_hour(done.stdout)
    if found is None:
        return None
    append_jsonl(USAGE_FILE, {"observed_at": now_iso(), "utilization": found[0], "resets_at": found[1],
                              "source": "tick"})
    return found[0] * UTIL_SCALE, found[1]


def latest_usage() -> tuple[float, tuple[float, int]] | None:
    """(observed epoch, (percent, resets_at)) of the newest usage.jsonl line (any source: lanes, runs), or None."""
    try:
        with USAGE_FILE.open("rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - 4096))
            rec = json.loads(f.read().decode("utf-8", "replace").splitlines()[-1])
        return (datetime.fromisoformat(rec["observed_at"]).timestamp(),
                (float(rec["utilization"]) * UTIL_SCALE, int(rec["resets_at"])))
    except (OSError, IndexError, ValueError, KeyError, TypeError):
        return None


def fresh_usage() -> tuple[float, int] | None:
    """(percent, resets_at): the newest observation if under REUSE_S old, else sample_usage()."""
    seen = latest_usage()
    if seen and time.time() - seen[0] < REUSE_S:
        return seen[1]
    return sample_usage()


def baseline(now: float, resets_at: int) -> float:
    return min(max(BASELINE_MAX * (now - (resets_at - WINDOW_SECONDS)) / WINDOW_SECONDS, 0.0), BASELINE_MAX)


def launch_block(u: float, base: float, running: int) -> str | None:
    """Why a new unit may not start with `running` others live at u %: hard_ceiling | pacing | reserve | None."""
    if u >= HARD_CEILING:
        return "hard_ceiling"
    if u > base + PACING_SLACK:
        return "pacing"
    return "reserve" if u + LAUNCH_RESERVE * (running + 1) > HARD_CEILING else None


# ---- backlog / ledger / lane choice ----
ASK_RE = re.compile(r"^blocked[:：]\s*等总理")
WEIGHT_RE = re.compile(r"^权重[：:]\s*(\d+(?:\.\d+)?)\s*%")
PAUSE_RE = re.compile(r"^空闲补行暂停到[：:]\s*([^\s（(]+)")  # section line: no plan before this UTC time, e.g. 2026-10-14T20:30Z（原因）
SPRINT_RE = re.compile(r"^冲刺[：:]\s*(.+)")  # owner\x2010-04: a line's sprint project is reviewed and dispatched first
DEFAULT_WEIGHT = 10.0
RUN_DAYS = 7
COST_KINDS = ("review", "plan", "executor", "coordinate", "compact", "lessons")


def _when(value) -> datetime | None:
    try:
        when = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def load_runs(path: Path | None = None) -> list[dict]:
    """runs.jsonl rows, each with a parsed `_at`; a row of the old 工作线 comes back as the line it belongs to now
    (line_map.fold; the file itself is not touched)."""
    rows = []
    try:
        lines = (RUNS_FILE if path is None else path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        at = _when(row.get("at")) if isinstance(row, dict) else None
        if at:
            rows.append(line_map.fold({**row, "_at": at}))
    return rows


def note_malformed(line: str, why: str) -> None:
    """tick-log `malformed` (first 60 chars of the row), at most once per row per UTC day (10-06: `- [todo]（6b …` lost
    two Uvox todos for 17 h). Never edits the row. Several processes parse the backlog, so the day's rows are kept in a file."""
    key, today = line[:60], now_iso()[:10]
    seen_file = DATA / "malformed-seen.json"
    try:
        seen = json.loads(seen_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    if seen.get(key) == today:
        return
    seen = {k: d for k, d in seen.items() if d == today} | {key: today}
    seen_file.parent.mkdir(parents=True, exist_ok=True)
    seen_file.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    log("malformed", why, row=key)


def note_window_skipped(line: str, window: str, now: datetime) -> None:
    """Keep before-window and expired skips distinct, using the entire row identity."""
    start, end = timed_rows.clocks(window)
    day = now.date()
    if end <= start and now.hour * 60 + now.minute < start:
        day -= timedelta(days=1)
    opened, _ = timed_rows.span(day.isoformat(), start, end)
    phase = "before" if now.replace(tzinfo=None) < opened else "expired"
    key, today = f"{timed_rows.identity('', line)}|{day}|{phase}", now.date().isoformat()
    seen_file = DATA / "window-skipped-seen.json"
    try:
        seen = json.loads(seen_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    if seen.get(key) == today:
        return
    seen = {k: d for k, d in seen.items() if d == today} | {key: today}
    seen_file.parent.mkdir(parents=True, exist_ok=True)
    seen_file.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    log("window_skipped", "outside local time window", row=line, window=window,
        window_date=day.isoformat(), phase=phase)


def local_datetime(now: datetime | None = None) -> datetime:
    """A naive input is local wall time; an aware input is converted to the machine's local zone."""
    now = datetime.now().astimezone() if now is None else now
    return now.astimezone() if now.tzinfo else now


def parse_backlog(text: str, now: datetime | None = None) -> dict[str, dict]:
    """section -> {weight, sprint, review: (line, item) | None, todo: [(line, item)], ask: [(line, item)]}, in file
    order. Owner section ignored; items before any `## ` heading belong to section "". `sprint`: the project named by a
    `冲刺：` line near the section top; its review row and todos come first (otherwise file order). `ask`: the
    `[blocked: 等总理]` rows, a deputy's question to the coordinator. A todo with a `〔时段：HH:MM–HH:MM〕` tag is
    available only during that local-time window; active windows go first, earliest end first (before sprint order).
    Its outside-window skip is not malformed."""
    sections: dict[str, dict] = {}
    new = lambda: {"weight": DEFAULT_WEIGHT, "sprint": None, "review": None, "todo": [], "reviews": [], "ask": [], "plan_pause": None}
    local = local_datetime(now)
    minute = local.hour * 60 + local.minute
    window_ends = {}
    cur, skipping, since = "", False, 99
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith("## "):
            cur, since = line[3:].strip(), 0
            skipping = backlog_io.is_owner_heading(line)
            if not skipping:
                sections.setdefault(cur, new())
            continue
        since += 1
        w = WEIGHT_RE.match(line)
        if w and since <= 3 and not skipping:
            sections[cur]["weight"] = float(w.group(1))
            continue
        z = PAUSE_RE.match(line)
        if z and not skipping:
            when = _when(z.group(1).replace("Z", "+00:00"))
            sections[cur]["plan_pause"] = when
            if when is None:
                note_malformed(line, "unparseable time in the plan-pause line: ignored, plan dispatches as usual")
            continue
        s = SPRINT_RE.match(line)
        if s and since <= 4 and not skipping:
            sections[cur]["sprint"] = None if project_of(s.group(1)) == "general" else project_of(s.group(1))
            continue
        m = LINE_RE.match(line)
        if skipping:
            continue
        if not m:
            if line.startswith("- ["):
                note_malformed(line, "a row that starts with `- [` but has no `[status] text`: skipped")
            continue
        status = m.group(1).strip()
        if ASK_RE.match(status):
            sections.setdefault(cur, new())["ask"].append((line, m.group(2)))
            continue
        if status.startswith(SKIP_PREFIXES):
            continue
        sec = sections.setdefault(cur, new())
        if status not in ("todo", "review") and status.startswith(("todo", "review")):
            note_malformed(line, "状态应写成 [todo] 或 [review]，说明放到行尾")
            continue
        if status == "todo" and (dots_watch.tagged(m.group(2)) or dots_watch.misplaced_tag(m.group(2))):
            continue  # dots_watch sends it or blocks a misplaced tag; neither executor takes it
        if status in ("review", "todo"):
            if status == "todo":
                tag = TIME_WINDOW_TAG_RE.search(tag_text(m.group(2)))
                if tag:
                    parsed = TIME_WINDOW_RE.fullmatch(tag.group(1))
                    if not parsed:
                        note_malformed(line, "invalid local time window tag: skipped")
                        continue
                    sh, sm, eh, em = map(int, parsed.groups())
                    if sh > 23 or eh > 23 or sm > 59 or em > 59:
                        note_malformed(line, "invalid local time window tag: skipped")
                        continue
                    start, end = sh * 60 + sm, eh * 60 + em
                    inside = start <= minute < end if start < end else minute >= start or minute < end
                    if not inside:
                        note_window_skipped(line, tag.group(1), local)
                        continue
                    window_ends[line] = (end - minute) % (24 * 60) or 24 * 60
            if project_of(m.group(2)) == "general" and any(p in m.group(2) for _, ps in PROJECTS.values() for p in ps):
                note_malformed(line, "a project prefix appears in the text but not at its start: the row goes to general")
            sec["reviews" if status == "review" else "todo"].append((line, m.group(2)))
    for sec in sections.values():
        first = (lambda rows: sorted(rows, key=lambda r: project_of(r[1]) != sec["sprint"])) if sec["sprint"] else list
        sec["todo"] = sorted(first(sec["todo"]), key=lambda r: window_ends.get(r[0], 24 * 60 + 1))
        sec["review_rows"] = first(sec.pop("reviews"))
        sec["review"] = next(iter(sec["review_rows"]), None)
    return sections


def last_run(runs: list[dict], kind: str, section: str | None = None) -> datetime | None:
    return max((r["_at"] for r in runs if r.get("kind") == kind and (section is None or r.get("section") == section)),
               default=None)


def coordinate_due(runs: list[dict], now: datetime) -> bool:
    last = last_run(runs, "coordinate")
    return last is None or (now - last).total_seconds() >= COORDINATE_EVERY_S


def _item_text(item: str) -> str:
    return item.removeprefix("（返工）").lstrip()


def project_of(item: str) -> str:
    """PROJECTS key whose prefix the todo text starts with (after a leading （返工）), else "general"."""
    text = _item_text(item)
    return next((p for p, (_, prefixes) in PROJECTS.items() if text.startswith(prefixes)), "general")


def unit_key(role: str, project: str | None = None) -> str:
    """A unit is the role's deputy unit (key = role) or one of its executors (key = role:project)."""
    return role if project is None else f"{role}:{project}"


def starve_key(item: str) -> str:
    """What the starvation rule counts a todo's wait by: its project, or for `general` (the rows share one session but
    are separate pieces of work) the first 50 characters of its text."""
    p = project_of(item)
    return p if p != "general" else item.removeprefix("（返工）").lstrip()[:50]


def last_executor_runs(runs: list[dict], role: str) -> dict[str, datetime]:
    """starve_key -> end of the role's latest executor run (the ledger row is written when the run ends)."""
    last: dict[str, datetime] = {}
    for r in runs:
        if r.get("kind") == "executor" and r.get("role") == role:
            p = starve_key(r["item"]) if r.get("item") else r.get("project") or "general"
            last[p] = max(last.get(p, r["_at"]), r["_at"])
    return last


def starved(sec: dict, rows: list, last: dict, now: datetime, hours: float) -> tuple[str, str] | None:
    """The row (line, item) of the non-sprint project that has waited longest (no executor run for `hours`; never run
    = forever), or None."""
    def wait(p): return (now - last[p]).total_seconds() / 3600 if p in last else float("inf")
    due = [(wait(starve_key(r[1])), r) for r in rows if project_of(r[1]) != sec.get("sprint")]
    due = [d for d in due if d[0] >= hours]
    return max(due, key=lambda d: d[0])[1] if due else None


def next_unit(sec: dict, section: str, role: str, runs: list[dict], now: datetime, live: set,
              deputy_free: bool) -> tuple[str, str | None, str | None, str | None, str | None] | None:
    """(kind, project, exact_line, item, starved) of the line's next unit given the live unit keys: the review row when the
    deputy unit is free (a non-sprint project's row waiting REVIEW_STARVE_H first), else the first todo whose executor
    unit is not live (the line has < EXECUTORS_PER_LINE live executors; a non-sprint project that has not run for
    STARVE_H first), else plan (no review, no todo, deputy unit free, after its cooldown and the section's `空闲补行暂停到` time). `starved`: the project the
    starvation rule moved ahead of the sprint project, else None. An active-window todo with a free executor slot
    precedes review and starvation order. A `general` row waits by its own text (starve_key)."""
    last = last_executor_runs(runs, role)
    review = None
    if sec["review"] and deputy_free:
        late = starved(sec, sec.get("review_rows", [sec["review"]]), last, now, REVIEW_STARVE_H)
        review = ("review", None, *(late or sec["review"]), late and late != sec["review"] and project_of(late[1]) or None)
    if sum(1 for k in live if k.startswith(f"{role}:")) < EXECUTORS_PER_LINE:
        free = {}
        for l, i in sec["todo"]:
            if unit_key(role, project_of(i)) not in live:
                free.setdefault(starve_key(i), (l, i))
        late = starved(sec, list(free.values()), last, now, STARVE_H)
        for line, item in sec["todo"]:
            project = project_of(item)
            if unit_key(role, project) not in live:
                if TIME_WINDOW_TAG_RE.search(item):
                    return "executor", project, line, item, None
                if review:
                    return review
                if late and late != (line, item):
                    return "executor", project_of(late[1]), *late, project_of(late[1])
                return "executor", project, line, item, None
    if review:
        return review
    if deputy_free and not sec["review"] and not sec["todo"] and not (sec.get("plan_pause") and now < sec["plan_pause"]):
        last = last_run(runs, "plan", section)
        if last is None or (now - last).total_seconds() >= PLAN_COOLDOWN_S:
            return "plan", None, None, None, None
    return None


def cost_7d(runs: list[dict], now: datetime) -> dict[str, float]:
    """Seven-day cost per section. A row of the old 工作线 that names no project (line_map.fold marks it `legacy_split`) is
    shared between the two new lines in the owner's 24:12 ratio, so the total is the same and neither line starts at zero."""
    cost: dict[str, float] = {}
    share = sum(line_map.LEGACY_SHARE.values())
    for r in runs:
        if r.get("kind") in COST_KINDS and r["_at"] > now - timedelta(days=RUN_DAYS):
            spent = r.get("cost_usd") or 0.0
            if r.get("legacy_split"):
                for section, weight in line_map.LEGACY_SHARE.items():
                    cost[section] = cost.get(section, 0.0) + spent * weight / share
            else:
                cost[r.get("section")] = cost.get(r.get("section"), 0.0) + spent
    return cost


def cache_hot(runs: list[dict], role: str, now: datetime) -> bool:
    """The role's resident (deputy) session ran, and finished, less than CACHE_HOT_S ago. Executor, lessons, scripted and
    Codex rows are other sessions."""
    return any(r.get("role") == role and r.get("kind") not in ("executor", "lessons") and r.get("project") is None
               and not r.get("auto") and not r.get("engine") and (now - r["_at"]).total_seconds() < CACHE_HOT_S
               for r in runs)


def failed_recently(runs: list[dict], section: str, now: datetime) -> bool:
    mine = [r for r in runs if r.get("kind") in COST_KINDS and r.get("section") == section]
    last = max(mine, key=lambda r: r["_at"], default=None)
    return bool(last) and not last.get("ok") and (now - last["_at"]).total_seconds() < FAIL_BACKOFF_S


def _stands(data: bytes, key: str) -> bool:
    """Does the backlog copy `data` hold a [todo]/[review] row whose first 80 characters (_item_text) are `key`?"""
    needle, i = key.encode("utf-8"), 0
    while (i := data.find(needle, i)) != -1:
        end = data.find(b"\n", i)
        m = LINE_RE.match(data[data.rfind(b"\n", 0, i) + 1:end if end != -1 else len(data)].decode("utf-8", "replace").rstrip("\r"))
        if m and m.group(1).strip() in ("todo", "review") and _item_text(m.group(2))[:80] == key:
            return True
        i += 1
    return False


def sprint_waits(items: set, now: datetime) -> dict[str, float]:
    """item -> hours it has stood as [todo]/[review]: now minus the oldest backlog-history snapshot (the newest 50 and one
    per UTC day, by file mtime) in the unbroken run, newest first, of snapshots that hold it in that state. One pass over
    the snapshots for all items. A lower bound: the row may have appeared any time before that snapshot. An item is
    missing when the newest snapshot lacks it (written since, edited, running) or the history cannot be read: no signal."""
    active, earliest = {i: _item_text(i)[:80] for i in items}, {}
    try:
        paths = [*BACKLOG_HISTORY.glob("backlog-*.md"), *(BACKLOG_HISTORY / "daily").glob("backlog-*.md")]
        for mtime, path in sorted(((p.stat().st_mtime, p) for p in paths), reverse=True):
            if not active:
                break
            data = path.read_bytes()
            for item, key in list(active.items()):
                if _stands(data, key):
                    earliest[item] = mtime
                else:
                    del active[item]
    except OSError:
        return {}
    return {i: (now.timestamp() - t) / 3600 for i, t in earliest.items()}


def sprint_first_recent(now: datetime) -> set | None:
    """Sections the tick-log's `spawn` rows (field `sprint_first`) show as having been advanced by the sprint rule within
    SPRINT_FIRST_H, read from the last 1 MB of the log (days of rows). None when the log cannot be read: no priority."""
    try:
        with LOG_FILE.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 1_000_000))
            tail = stream.read()
    except OSError:
        return None
    cutoff, recent = now - timedelta(hours=SPRINT_FIRST_H), set()
    for line in tail.split(b"\n"):
        if b'"sprint_first"' in line:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("decision") == "spawn" and (at := _when(row.get("at"))) and at > cutoff:
                recent.add(row["sprint_first"])
    return recent


def choose_lane(text: str, runs: list[dict], now: datetime, busy: set = frozenset(),
                deputy_busy: set = frozenset(), note: dict | None = None) -> tuple[str, str | None] | None:
    """(role, project or None) of the next unit to start, or None. `busy`: live unit keys. A due coordinate run wins
    (the coordinator's deputy unit); else the section with the lowest (cost_7d + COST_FLOOR) / weight (weight 0
    never; ties: file order) supplies its next_unit; before that, a section whose next_unit is its sprint project's row
    that has stood as todo/review for SPRINT_FIRST_H (sprint_waits) goes first, longest standing first, at most once per
    section per SPRINT_FIRST_H (sprint_first_recent; no readable log: never). `deputy_busy`: roles whose resident session
    is in use by the owner/report lane — their deputy unit is not free. A section whose last run failed waits
    FAIL_BACKOFF_S. `note`, if given, gets "starved" = the project the starvation rule advanced for the chosen unit
    (else None) and "sprint_first" = the section if the sprint rule picked it. A review whose
    deputy session is cache-hot goes before any other unit (it is cheap now, a full cache rewrite later); the pacing,
    ceiling and reserve gates ran before this, in supervise_step."""
    if not {"coordinator"} & (busy | deputy_busy) and coordinate_due(runs, now):
        return "coordinator", None
    cost = cost_7d(runs, now)
    works = []
    for section, sec in parse_backlog(text, now).items():
        role = SECTION_ROLES.get(section)
        if not role or sec["weight"] <= 0 or failed_recently(runs, section, now):
            continue
        work = next_unit(sec, section, role, runs, now, busy, role not in busy | deputy_busy)
        if work is not None:
            works.append((section, sec, role, work))
    # owner\x2011-03: a sprint project's row that has stood SPRINT_FIRST_H goes before everything below; a section gets this once per SPRINT_FIRST_H
    cands = {w[3]: s for s, sec, _, w in works if w[3] and project_of(w[3]) == sec["sprint"]}
    recent = sprint_first_recent(now) if cands else None
    stands = sprint_waits({i for i, s in cands.items() if s not in recent}, now) if recent is not None else {}
    best, best_key = None, None
    for section, sec, role, work in works:
        # a section with work whose last review/executor/plan ended CROSS_STARVE_H ago goes before the score; longest wait first (no run on record: no signal)
        last = max((r["_at"] for r in runs if r.get("kind") in ("review", "executor", "plan") and r.get("section") == section), default=None)
        wait = (now - last).total_seconds() / 3600 if last else 0.0
        stood = stands.get(work[3])
        stood = stood if stood is not None and stood >= SPRINT_FIRST_H else None
        key = (stood is None, -(stood or 0), wait < CROSS_STARVE_H, -wait if wait >= CROSS_STARVE_H else 0,
               not (work[0] == "review" and cache_hot(runs, role, now)), (cost.get(section, 0.0) + COST_FLOOR) / sec["weight"])
        if best_key is None or key < best_key:
            best, best_key = (role, work[1]), key
            if note is not None:
                note["starved"], note["cross_starved"] = work[4], section if not key[2] else None
                note["sprint_first"] = section if stood is not None else None
    return best

def rewrite_line(old: str, new: str) -> bool:
    return backlog_io.rewrite_line(old, new, BACKLOG)


def finish_lane_line(section: str, running: str, after: str) -> str:
    """Finish an executor row under one lock; an edited body is retained for review.

    Fallback requires a unique row in the same section with the same running time,
    project and first 60 body characters. Other writers keep exact-line matching.
    """
    with backlog_io.lock(BACKLOG):
        lines = backlog_io._read(BACKLOG)
        index = next((i for i, raw in enumerate(lines) if raw.rstrip("\r") == running), None)
        reason = ""
        if index is None:
            expected = RUNNING_RE.fullmatch(running)
            project = project_of(expected[3])
            try:
                rows = backlog_io._section_rows(lines, section)
            except ValueError:  # missing/duplicate section headings cannot identify one section
                rows = []
            candidates = []
            for i in rows:
                match = RUNNING_RE.fullmatch(lines[i].rstrip("\r"))
                if match and match.group(1, 2) == expected.group(1, 2) and project != "general" \
                        and project_of(match[3]) == project and match[3][:60] == expected[3][:60]:
                    candidates.append(i)
            fields = {"expected_sha256": backlog_io.line_sha256(running), "candidate_count": len(candidates)}
            if len(candidates) == 1:
                candidate = lines[candidates[0]].rstrip("\r")
                changes = [op for op in difflib.SequenceMatcher(None, running, candidate, autojunk=False).get_opcodes()
                           if op[0] != "equal"]
                fields.update(candidate_sha256=backlog_io.line_sha256(candidate), diff_count=len(changes),
                              diff_summary=[{"op": op, "old_start": a, "new_start": c,
                                             "old": running[a:b][:80], "new": candidate[c:d][:80]}
                                            for op, a, b, c, d in changes[:3]])
                if after == "review":
                    index = candidates[0]
            reason = "正文已被改过" if index is not None else f"backlog line changed, not set to {after}"
            log("backlog_line_changed", reason, section=section, **fields)
        if index is not None:
            raw = lines[index]
            line = raw.rstrip("\r")
            lines[index] = f"- [{after}] {line.partition('] ')[2]}" + raw[len(line):]
            backlog_io._write(BACKLOG, lines)
        return reason


# ---- launch ----
def build_argv(model: str, rules: str, extra: list[str] = (), tools: list[str] = TOOLS,
               effort: str | None = None, only_tools: bool = False, max_budget: float | None = None) -> list[str]:
    """`only_tools`: also pass --tools, so the tool list is exactly `tools` (measured 10-01: a first-turn context of
    28.8k -> 16.6k tokens; Agent + Skill alone add back 9.4k). --strict-mcp-config and --disable-slash-commands
    changed nothing in that first-turn context. `max_budget`: --max-budget-usd, counted per process (measured 10-02 on --resume: a session already
    over the budget ran on, and a tiny budget stopped it after its first turn), not per session."""
    # --strict-mcp-config: skip the claude.ai connectors (all unauthorised), so runs stop ending with "connectors need
    # authorisation" (owner replies are forwarded verbatim). Drop it once owner authorises a connector.
    return [CLAUDE, "--print", "--output-format", "stream-json", "--verbose", "--model", model,
            "--permission-mode", "acceptEdits", "--strict-mcp-config", *(["--effort", effort] if effort else []),
            *(["--max-budget-usd", str(max_budget)] if max_budget else []),
            *(["--tools", *tools] if only_tools else []),
            "--allowedTools", *tools, "--append-system-prompt", rules, *extra]


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                   creationflags=CREATE_NO_WINDOW)


def session_path(sid: str) -> Path:
    """A session's transcript, in whichever project dir it lives (the workspace's by default)."""
    return next((p for p in (SESSIONS_DIR / f"{sid}.jsonl", *CLAUDE_PROJECTS.glob(f"*/{sid}.jsonl")) if p.exists()),
                SESSIONS_DIR / f"{sid}.jsonl")


def session_cost(sid: str) -> float:
    """Cumulative cost of a session: the last `cost-state` totalCostUSD in its transcript (any project dir; 0.0 if none). A resumed
    `claude -p` restores it, so the stream's total_cost_usd is cumulative while its usage is per process."""
    total = 0.0
    path = session_path(sid)
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if '"cost-state"' in line:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("type") == "cost-state":
                    total = float(e.get("totalCostUSD") or total)
    except OSError:
        pass
    return total


def run_claude(kind: str, prompt: str, run_file: Path, extra: list[str] = (), effort: str | None = None,
               model: str | None = None, cwd: Path | None = None, add_dirs: tuple = (),
               compact_at: int | None = None, role: str | None = None) -> int:
    """Run the prompt, raw stream to run_file. Returns the exit code, or -1 after a timeout kill. `effort` defaults
    to EFFORTS[kind] (executors pass theirs), `model` to MODELS[kind] (compaction passes the session's). `cwd`
    defaults to WORKSPACE (`--resume` only finds a session from the cwd it was created in); `add_dirs` become
    `--add-dir`. `compact_at`: context tokens at which Claude Code auto-compacts during the run (without it only near
    the full window). The first line is a `launch` event (argv without the rules text; the prompt goes in on stdin and
    is not recorded); for `--resume <sid>` a cost_baseline (session cost before this process) follows, see
    result_stats."""
    rules = read_rules()
    extra = [*extra, *(["--add-dir", *map(str, add_dirs)] if add_dirs else [])]
    run_file.parent.mkdir(parents=True, exist_ok=True)
    with run_file.open("wb") as out:
        tools = REPORT_TOOLS if kind in ("report", "meeting") else TOOLS  # a meeting turn is read-only
        max_budget = None
        if kind == "executor":
            max_budget = QUANT_EXECUTOR_MAX_BUDGET_USD if role == "quant" else EXECUTOR_MAX_BUDGET_USD
        argv = build_argv(model or MODELS[kind], rules, extra, tools, effort or EFFORTS.get(kind),
                          only_tools=kind == "executor", max_budget=max_budget)
        out.write((json.dumps({"type": "launch", "kind": kind, "cwd": str(cwd or WORKSPACE), "prompt_chars": len(prompt),
                               "argv": [f"<rules {len(rules)} chars>" if a == rules else a for a in argv]},
                              ensure_ascii=False) + "\n").encode("utf-8"))
        if "--resume" in extra and extra.index("--resume") + 1 < len(extra):
            sid = extra[extra.index("--resume") + 1]
            fork = "--fork-session" in extra  # the child's total is not known to continue the parent's: no baseline (run_resident prices a fork from the stream)
            out.write((json.dumps({"type": "cost_baseline", "session_id": sid, **({"fork": True} if fork else {}),
                                   "total_cost_usd": 0.0 if fork else session_cost(sid)}) + "\n").encode("utf-8"))
            out.flush()
        env = None if compact_at is None else {
            **os.environ, "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(max(1, round(100 * compact_at / CONTEXT_WINDOW)))}
        timed_out = False
        with tempfile.TemporaryFile(dir=run_file.parent) as stderr_file:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=stderr_file,
                                    cwd=str(cwd or WORKSPACE), env=env, creationflags=CREATE_NO_WINDOW)
            done, hit, pos = threading.Event(), threading.Event(), [0]
            watcher = threading.Thread(target=watch_stream, args=(proc, run_file, kind, done, hit, pos), daemon=True)
            watcher.start()
            try:
                proc.communicate(prompt.encode("utf-8"), timeout=TIMEOUTS[kind])
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)
                proc.communicate()
                timed_out = True
            finally:
                done.set()
                watcher.join()
                tail_usage(run_file, pos, kind, final=True)
            if proc.returncode not in (0, CEILING_STOP) and not hit.is_set():
                stderr_file.flush()
                stderr_file.seek(0, os.SEEK_END)
                size = stderr_file.tell()
                if size:
                    stderr_file.seek(max(0, size - MAX_STDERR_BYTES))
                    diagnostic = stderr_file.read(MAX_STDERR_BYTES).decode("utf-8", errors="replace")
                    out.write((json.dumps({"type": "stderr", "message": diagnostic,
                                           "truncated": size > MAX_STDERR_BYTES}, ensure_ascii=False) + "\n").encode("utf-8"))
                    out.flush()
    if hit.is_set():
        stopped_cost(run_file)
        return CEILING_STOP
    return -1 if timed_out else proc.returncode


def tail_usage(run_file: Path, pos: list, kind: str, final: bool = False) -> float | None:
    """Read the lines run_file gained since byte offset pos[0] (a half-written last line waits for the next read, unless
    `final`); each rate_limit_event goes to usage.jsonl stamped now. Returns the newest five-hour percent seen, if any."""
    try:
        with run_file.open("rb") as f:
            f.seek(pos[0])
            data = f.read()
    except OSError:
        return None
    end = len(data) if final else data.rfind(b"\n") + 1
    pos[0] += end
    newest = None
    for line in data[:end].decode("utf-8", "replace").splitlines():
        found = usage_sample.five_hour(line) if '"rate_limit_event"' in line else None
        if found:
            append_jsonl(USAGE_FILE, {"observed_at": now_iso(), "utilization": found[0], "resets_at": found[1],
                                      "source": f"tick:{kind}"})
            newest = found[0] * UTIL_SCALE
    return newest


def watch_stream(proc, run_file: Path, kind: str, done: threading.Event, hit: threading.Event, pos: list) -> None:
    """Every WATCH_EVERY_S while the run lives: record its rate_limit_events as they arrive; at HARD_CEILING stop it
    (the session stays, the next run resumes it)."""
    while not done.wait(WATCH_EVERY_S):
        used = tail_usage(run_file, pos, kind)
        if used is not None and used >= HARD_CEILING:
            hit.set()
            log("ceiling_stop", kind=kind, u=used, run_file=str(run_file))
            kill_tree(proc.pid)
            return


def estimate_stream_cost(run_file: Path) -> float:
    """USD this process spent, from the stream's assistant events (a killed run has no result, and the transcript's
    cost-state is only written when the process exits normally). One event per content block repeats its message.id:
    count each message once (its usage input/cache fields are final), but sum the chars of all its blocks."""
    msgs: dict = {}
    web: set = set()
    for line in run_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"assistant"' not in line:
            continue
        try:
            m = json.loads(line)["message"]
        except (ValueError, KeyError, TypeError):
            continue
        d = msgs.setdefault(m.get("id"), {"model": str(m.get("model")), "u": m.get("usage") or {}, "text": 0, "sig": 0})
        for b in m.get("content") or []:
            if b.get("type") == "text":
                d["text"] += len(b.get("text") or "")
            elif b.get("type") == "tool_use":
                d["text"] += len(json.dumps(b.get("input") or {}, ensure_ascii=False))
                if b.get("name") in ("WebFetch", "WebSearch"):
                    web.add(b.get("id"))
            elif b.get("type") == "thinking":
                d["sig"] += len(b.get("signature") or "")
    total = 0.0
    for d in msgs.values():
        fam = next((k for k in PRICES if k in d["model"]), "opus")
        p, (a, b), u = PRICES[fam], OUTPUT_TOKENS_PER_CHAR[fam], d["u"]
        w = u.get("cache_creation") or {}
        w5, w1 = w.get("ephemeral_5m_input_tokens", 0), w.get("ephemeral_1h_input_tokens", 0)
        if not w5 + w1:
            w5 = u.get("cache_creation_input_tokens", 0)
        if fam == "haiku" and u.get("input_tokens", 0) + w5 + w1 + u.get("cache_read_input_tokens", 0) > HAIKU_LONG_TOKENS:
            p = HAIKU_LONG_PRICES
        total += (u.get("input_tokens", 0) * p[0] + w5 * p[1] + w1 * p[2] + u.get("cache_read_input_tokens", 0) * p[3]
                  + (a * d["text"] + b * d["sig"]) * p[4]) / 1e6
    return total + len(web) * WEB_TOOL_USD


def stopped_cost(run_file: Path) -> None:
    """A stopped run has no result event, so the ledger would lose its cost: append one with the estimate (plus the run's
    cost_baseline, which result_stats subtracts again), marked cost_estimated."""
    base = 0.0
    for line in run_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith('{"type": "cost_baseline"'):
            base = float(json.loads(line).get("total_cost_usd") or 0.0)
            break
    with run_file.open("ab") as f:
        f.write((json.dumps({"type": "result", "is_error": True, "result": "ceiling_stop", "cost_estimated": True,
                             "total_cost_usd": base + estimate_stream_cost(run_file)}) + "\n").encode("utf-8"))


# ---- main ----
# ---- owner lane (Discord instructions; only the hard ceiling applies) ----
def owner_items(text: str) -> list[tuple[str, str]]:
    """(exact_line, item_text) of every todo line whose text starts with 【Discord, in file order."""
    found = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        m = LINE_RE.match(line)
        if m and m.group(1).strip() == "todo" and m.group(2).startswith("【Discord"):
            found.append((line, m.group(2)))
    return found


RUNNING_RE = re.compile(r"^- \[running (\d\d):(\d\d)\] (.+)$")


def running_rows(text: str) -> list[dict]:
    """Every `- [running HH:MM] item` row with what ran it, judged from the row itself: an owner item (trailer 〔msg: role:〕)
    is the role's owner run (live while owner.lock or the role's deputy lock is), a row under a line's section is that
    role's executor on project_of(item) (live while its lane lock, or the unit-less lane lock, is). `kind` None = not
    recognised (live None): nothing is done to such a row."""
    out, section = [], ""
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith("## "):
            section = line[3:].strip()
        m = RUNNING_RE.match(line)
        if not m:
            continue
        item, trailer = m.group(3), TRAILER_RE.search(m.group(3))
        role = SECTION_ROLES.get(section)
        row = {"line": line, "section": section, "item": item, "hhmm": f"{m.group(1)}:{m.group(2)}", "kind": None,
               "live": None, "role": None, "project": None}
        if trailer and trailer.group(2) in ROLE_NAMES:
            row.update(kind="owner", role=trailer.group(2), live=lock_live(OWNER_LOCK) or lock_live(deputy_lock(trailer.group(2))))
        elif role:
            project = project_of(item)
            row.update(kind="executor", role=role, project=project,
                       live=any(lock_live(p) for p in (lane_lock(role, project), lane_lock(role))))
        out.append(row)
    return out


def recover_running(runs: list[dict] | None = None) -> int:
    """At supervisor start: put every `[running]` row whose run is gone (power cut, crash, the session killed) back where it
    was before the run: an owner item and an executor row go back to `[todo]`, unless the ledger has that executor run
    ended (its start, ledger end minus elapsed_s, within 2 minutes of the row's HH:MM), then `[review]`, as the run's own
    ending would have written. Rows with a live lock, and rows of unknown kind, are left (the latter show in review_stats).
    One log line per row; returns how many were put back."""
    runs = load_runs() if runs is None else runs
    n = 0
    for row in running_rows(BACKLOG.read_text(encoding="utf-8")):
        if row["live"] is not False:
            continue
        after = "todo"
        if row["kind"] == "executor":
            hh, mm = map(int, row["hhmm"].split(":"))
            for r in runs:
                if r.get("kind") == "executor" and r.get("item") == row["item"] and r.get("ok"):
                    start = r["_at"].astimezone() - timedelta(seconds=r.get("elapsed_s") or 0)
                    if abs(start.hour * 60 + start.minute - (hh * 60 + mm)) <= 2:
                        after = "review"
        if rewrite_line(row["line"], f"- [{after}] {row['item']}"):
            n += 1
            log("running_recovered", "lock not live at supervisor start", item=row["item"], kind=row["kind"],
                section=row["section"], role=row["role"], to=after)
    return n


def eastern_hhmm(ts: float) -> str:
    """America/New_York wall time (US DST rules; the box has no tzdata)."""
    def nth_sunday(year, month, n):
        d = datetime(year, month, 1, tzinfo=timezone.utc)
        return d.replace(day=1 + (6 - d.weekday()) % 7 + 7 * (n - 1))
    t = datetime.fromtimestamp(ts, timezone.utc)
    start = nth_sunday(t.year, 3, 2).replace(hour=7)   # 02:00 EST
    end = nth_sunday(t.year, 11, 1).replace(hour=6)    # 02:00 EDT
    offset = -4 if start <= t < end else -5
    return (t + timedelta(hours=offset)).strftime("%H:%M")


def attachment_problem(path: Path) -> str:
    """Why `path` may not be sent to Discord, or "" if it may."""
    if not any(path.is_relative_to(r) for r in ATTACH_ROOTS):
        return "不在允许的目录内"
    if not path.is_file():
        return "文件不存在"
    if any(CREDENTIAL_RE.search(part) for part in path.parts):
        return "疑似凭据文件"
    if path.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
        return f"超过 {MAX_UPLOAD_MB} MB，请改用邮件"
    return ""


def post_discord(role: str, message: str, reply: bool = False, hold: bool = True, sender: str | None = None) -> bool:
    """Post `message` (chunked, prefixed with the Chinese role name) to the role's channel via gateway.py.
    Lines `附件：<path>` are removed from the text and sent as files with the last chunk.
    reply=True: the answer to an owner message (gateway send --reply). Otherwise the gateway tags it and, unless hold=False
    (the supervisor loop must not stall), holds it while the owner waits."""
    files, notes = [], []
    for raw in ATTACH_RE.findall(message):
        path = Path(raw.strip().strip("\"'`"))
        try:
            problem = attachment_problem(path.resolve())
        except OSError as exc:
            problem = type(exc).__name__
        if problem:
            notes.append(f"（附件 {path.name} 没有发送：{problem}）")
        elif str(path.resolve()) not in files:
            files.append(str(path.resolve()))
    message = "\n".join([ATTACH_RE.sub("", message).strip(), *notes]).strip()
    prefix = f"[{sender or ROLE_NAMES[role]}] "  # sender: the speaker when it is not the channel owner (coordinator answers)
    size = DISCORD_CHUNK - len(prefix)
    ok = True
    for i in range(0, max(len(message), 1), size):
        last = i + size >= len(message)
        extra = [x for f in files for x in ("--file", f)] if last else []
        try:
            done = subprocess.run([str(GATEWAY_PY), str(GATEWAY), "send", "--channel", role,
                                   *(["--reply"] if reply else ["--line", LINE_SECTIONS[role], *([] if hold else ["--no-hold"])]),
                                   "--message", prefix + message[i:i + size], *extra],
                                  capture_output=True, timeout=120 if reply or not hold else 120 + 15 * 60,  # + the gateway's max hold
                                  cwd=str(WORKSPACE), creationflags=CREATE_NO_WINDOW)
            ok = ok and done.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
    return ok


def load_deputies() -> dict:
    try:
        data = json.loads(DEPUTIES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_deputies(data: dict) -> None:
    DEPUTIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEPUTIES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DEPUTIES_FILE)


def fail_reason(code: int) -> str:
    return "超时" if code == -1 else f"额度到 {HARD_CEILING:.0f}% 硬线被停" if code == CEILING_STOP else f"退出码 {code}"


def final_result(run_file: Path) -> tuple[bool, str, bool]:
    """(ok, result_text, did_work) from a stream-json run file."""
    ok, text, worked = False, "", False
    for line in run_file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        worked = worked or event.get("type") == "assistant"
        if event.get("type") == "result":
            ok, text = not event.get("is_error"), str(event.get("result") or "")
    return ok, text, worked


def session_is_unavailable(run_file: Path) -> bool:
    """Only explicit missing or unrecoverable-session diagnostics justify abandoning a resident session."""
    try:
        for line in run_file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "stderr" and \
                    SESSION_UNAVAILABLE.search(str(event.get("message") or "")):
                return True
    except OSError:
        pass
    return False


def deputy_lock(role: str) -> Path:
    return DATA / f"deputy-{role}.lock"


def update_deputy(role: str, **fields) -> dict:
    """Reload deputies.json, create the role's entry on first use, apply fields (an `executor` dict is merged into
    the existing one, an `executors` dict project by project), save; returns the entry. Locked: lanes of different
    roles write this file concurrently."""
    with backlog_io.lock(DEPUTIES_FILE):
        deputies = load_deputies()
        entry = deputies.get(role)
        if not isinstance(entry, dict):
            entry = deputies[role] = {"session_id": None, "created_at": None, "status": "idle", "current": None,
                                      "last": None, "runs": 0}
        if isinstance(fields.get("executor"), dict):
            fields["executor"] = {**(entry.get("executor") or {}), **fields["executor"]}
        if isinstance(fields.get("executors"), dict):
            merged = dict(entry.get("executors") or {})
            for project, new in fields["executors"].items():
                merged[project] = {**(merged.get(project) or {}), **new}
            fields["executors"] = merged
        entry.update(fields)
        save_deputies(deputies)
    return entry


def role_section(role: str) -> str:
    return LINE_SECTIONS[role]


# ---- Beacon Listener line: a one-time fork of the work deputy's session (plan 3.3, owner\x2010-26 split) ----
FORK_INTRO = ("【首次启动】你的会话是从 Arbitros 副总理（原工作副总理）的会话分叉出来的。从现在起你是 Beacon Listener 副总理，"
              "只管 Beacon Listener 线（Uvox、Beacon Listener 官网、Field Observer、Kiki、Umzy），不要再处理 Arbitros 线的事"
              "（FOS/FabGrid、FactoryAlign/CPBench、Arbitros 官网、求职、论文），也不要写回原会话。分叉带来的只是原会话压缩后还能恢复的上下文；"
              "owner 的原话和决定以 incore/records/ 和项目意图卡为准。先读 incore/projects/taste/shared.md（如果存在），"
              "再读本项目的意图卡、口味和经验文件，见 tick-rules.md 的「拆线」一条。\n\n")


class DeputyNotReady(RuntimeError):
    """A forked role was asked to run before its fork exists (or after the fork was stopped for review)."""


def deputy_ready(role: str) -> bool:
    """False for a FORK_ROLES role that has no session of its own and no fork to make (not migrated yet, or `fork_state`
    blocked): it must never start blank. True for every other role."""
    if role not in FORK_ROLES:
        return True
    entry = load_deputies().get(role) or {}
    return bool(entry.get("session_id") or (entry.get("fork_from") and entry.get("fork_state") != "blocked"))


def fork_source_free(role: str) -> bool:
    """True unless `role` still has to fork and the session it forks from is in use (its deputy lock is live: a run, or the
    owner talking to it in a terminal): the fork waits, it does not copy a session in the middle of a turn."""
    entry = load_deputies().get(role) or {}
    return bool(entry.get("session_id")) or not entry.get("fork_from") or not lock_live(deputy_lock(FORK_ROLES[role]))


def stream_session_ids(run_file: Path) -> tuple[str | None, str | None]:
    """(session_id of the first system/init event, session_id of the result event) of a stream-json file."""
    init = result = None
    try:
        lines = run_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init" and init is None:
            init = event.get("session_id")
        elif event.get("type") == "result":
            result = event.get("session_id") or result
    return init, result


def fork_child_id(run_file: Path, source: str) -> tuple[str | None, str]:
    """The child session a `--resume <source> --fork-session` run created: (id, "") or (None, why). The init and result ids
    must agree when both are there, and the id must be a valid UUID different from the source's."""
    init, result = stream_session_ids(run_file)
    if init is None and result is None:
        return None, "no session id in the stream"
    if init is not None and result is not None and init != result:
        return None, "init and result session ids differ"
    sid = str(init or result)
    try:
        uuid.UUID(sid)
    except ValueError:
        return None, "session id is not a UUID"
    if sid == source:
        return None, "session id equals the source session"
    return sid, ""


def parse_verdict(text: str) -> str:
    """Last `VERDICT:` line of a review result: pass | rework | blocked, else unknown."""
    found = re.findall(r"VERDICT:\s*(\w+)", text)
    return found[-1].lower() if found and found[-1].lower() in ("pass", "rework", "blocked") else "unknown"


BATCH_MAX = 3  # review rows one deputy session reviews together (owner\x2010-25 "6")
BATCH_VERDICT_RE = re.compile(r"^[ \t>*`]*VERDICT[ \t]+(\d+)[ \t]*[:：][ \t]*(pass|rework|blocked)\b", re.M | re.I)


def batch_verdicts(text: str, n: int) -> dict[int, str]:
    """`VERDICT <number>: pass|rework|blocked` lines of a batched review, 1..n -> verdict, the last line of a number wins;
    anything else (no number, a number out of range, another word) is not a verdict."""
    out = {}
    for m in BATCH_VERDICT_RE.finditer(text):
        if 1 <= int(m.group(1)) <= n:
            out[int(m.group(1))] = m.group(2).lower()
    return out


def result_stats(run_file: Path) -> dict:
    """Cost and token counts from the stream's result event. cost_usd = result total_cost_usd minus the run's
    cost_baseline line (resumed sessions report cumulative cost; no baseline: 0). context_tokens = input + cache_read +
    cache_creation of the final API iteration (usage.iterations[-1]; fallback: the last assistant message's usage,
    then the totals); after a compact_boundary event it is compact_metadata.post_tokens."""
    cost, usage, last, base, post, estimated, turns = None, {}, {}, 0.0, None, False, None
    for line in run_file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "cost_baseline":
            base = float(event.get("total_cost_usd") or 0.0)
        elif event.get("type") == "system" and event.get("subtype") == "compact_boundary":
            post = (event.get("compact_metadata") or {}).get("post_tokens", post)
        elif event.get("type") == "assistant" and isinstance((event.get("message") or {}).get("usage"), dict):
            last = event["message"]["usage"]
        elif event.get("type") == "result":
            cost, usage, estimated = event.get("total_cost_usd"), event.get("usage") or {}, bool(event.get("cost_estimated"))
            turns = event.get("num_turns")
    its = usage.get("iterations")
    final = its[-1] if isinstance(its, list) and its and isinstance(its[-1], dict) else (last or usage)
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    write = usage.get("cache_creation") or {}  # the 5-minute and 1-hour cache writes are priced differently
    return {"cost_usd": None if cost is None else round(max(cost - base, 0.0), 6),
            "num_turns": turns, "cache_write_5m_tokens": write.get("ephemeral_5m_input_tokens"),
            "cache_write_1h_tokens": write.get("ephemeral_1h_input_tokens"),
            **({"cost_estimated": True} if estimated else {}),
            "input_tokens": usage.get("input_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "context_tokens": int(post) if post is not None else sum(int(final.get(k) or 0) for k in keys) if final
            else None}


def ledger_row(role, section, kind, effort, session, sid, item, elapsed, code, ok, result, run_file,
               at=None, model=None, project=None) -> dict:
    return {"at": at or now_iso(), "role": role, "section": section, "kind": kind, "model": model or MODELS[kind],
            "effort": effort, "session": session, "session_id": sid, "item": item, "elapsed_s": elapsed,
            "exit_code": code, "ok": ok, **result_stats(run_file),
            "verdict": parse_verdict(result) if kind == "review" else None, "project": project,
            "run_file": str(run_file)}


def record_run(*args, metadata: dict | None = None, **kwargs) -> dict:
    row = ledger_row(*args, **kwargs) | (metadata or {})
    append_jsonl(RUNS_FILE, row)
    return row


def project_cwd(project: str) -> Path:
    return PROJECTS[project][0] if project in PROJECTS else WORKSPACE


def executor_launch(project: str) -> dict:
    """run_claude kwargs of an executor-side run (executor, its compaction, its lessons): project cwd, shared dirs."""
    return {"cwd": project_cwd(project), "add_dirs": EXECUTOR_ADD_DIRS + PROJECT_ADD_DIRS.get(project, ())}


def executor_model(item: str) -> str | None:
    """'opus' for a task line tagged OPUS_TAG, 'haiku' for HAIKU_TAG unless the line is a rework (返工), else None
    (MODELS['executor'])."""
    tags = tag_text(item)
    return "opus" if OPUS_TAG in tags else "haiku" if HAIKU_TAG in tags and "返工" not in item else None


def deputy_model(role: str, kind: str) -> str:
    """Model of a run in the role's own resident session (review, plan, owner, report and its compaction)."""
    return "sonnet" if role in SONNET_DEPUTIES else MODELS[kind]


def deputy_effort(role: str, kind: str) -> str | None:
    return EFFORTS.get(kind) if kind == "lessons" else DEPUTY_EFFORTS.get(role, EFFORTS.get(kind))  # lessons stay medium (owner\x2010-25)


def compact_limit(role: str, project: str | None = None) -> int:
    """Context tokens past which a session is compacted: during a run by Claude Code, after it by maybe_compact."""
    return (EXECUTOR_COMPACT_TOKENS if project is not None
            else COORDINATOR_COMPACT_TOKENS if role == "coordinator" else COMPACT_TOKENS)


def tick_path(filename: str) -> Path:
    """New streams use the UTC month; existing ledger paths stay unchanged."""
    return TICKS_DIR / datetime.now(timezone.utc).strftime("%Y-%m") / filename


def session_run(kind: str, prompt: str, label: str, sid: str, resume: bool, effort: str | None,
                on_retry=lambda sid: None, project: str | None = None, model: str | None = None,
                compact_at: int | None = None, fork_from: str | None = None,
                on_fork=lambda run_file: None, role: str | None = None) -> tuple[int, Path, str, bool]:
    """One claude run in session `sid` (--session-id first, then --resume; a failed resume gets one retry with a new
    id only when its diagnostic says the session is unavailable). `project`: an executor session, run in its cwd.
    `fork_from`: instead, `--resume <that> --fork-session` once: the child's id is only in the stream (the caller reads it
    with fork_child_id), there is no retry with a blank session, and `on_fork(run_file)` is told the stream's path first.
    Returns (exit_code, run_file, final_sid, resumed); a fork returns sid "" and resumed False."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_file = tick_path(f"{stamp}-{label}.jsonl")
    launch = {} if project is None else executor_launch(project)
    role_arg = {} if role is None else {"role": role}
    if model:
        launch["model"] = model
    if compact_at:
        launch["compact_at"] = compact_at
    if fork_from:
        on_fork(run_file)
        return run_claude(kind, prompt, run_file, ["--resume", fork_from, "--fork-session"], effort,
                          **launch, **role_arg), run_file, "", False
    code = run_claude(kind, prompt, run_file, ["--resume" if resume else "--session-id", sid], effort,
                      **launch, **role_arg)
    if resume and code not in (0, CEILING_STOP) and not final_result(run_file)[2] and session_is_unavailable(run_file):
        sid, resume = str(uuid.uuid4()), False
        on_retry(sid)
        run_file = tick_path(f"{stamp}-{label}-retry.jsonl")
        code = run_claude(kind, prompt, run_file, ["--session-id", sid], effort, **launch, **role_arg)
    return code, run_file, sid, resume


def maybe_compact(role: str, row: dict, model: str, label: str, project: str | None = None) -> int | None:
    """/compact the row's session in place when its context exceeds COMPACT_TOKENS (the coordinator's:
    COORDINATOR_COMPACT_TOKENS), or EXECUTOR_COMPACT_TOKENS for an executor (caller holds the session's lock; an
    executor's `project` selects the cwd it was created in); ledger row kind 'compact'. Returns the context size to
    store (after compaction: the compact_boundary's post_tokens, None if the stream has none)."""
    context = row["context_tokens"]
    if not context or context <= compact_limit(role, project):
        return context
    started = time.time()
    run_file = tick_path(f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{label}.jsonl")
    launch = {} if project is None else executor_launch(project)
    code = run_claude("compact", "/compact", run_file, ["--resume", row["session_id"]], model=model, **launch)
    ok = final_result(run_file)[0] and code == 0
    done = record_run(role, row["section"], "compact", None, "resume", row["session_id"], "", round(time.time() - started),
                      code, ok, "", run_file, model=model, project=project)
    log("compacted", "" if ok else f"exit {code}", role=role, before_tokens=context, run_file=str(run_file))
    return done["context_tokens"]


def run_resident(role: str, kind: str, prompt: str, current: dict, last_extra: dict, label: str | None = None,
                 item: str = "", section: str | None = None, batch: list[dict] | None = None) -> tuple[int, Path, bool, str, int]:
    """One run in the role's resident session. Caller holds the deputy lock. Appends the ledger row, compacts the
    session when it grew past COMPACT_TOKENS, updates deputies.json status/current/last/runs/context_tokens.
    `batch` ([{"item", "extra"}], a batched review): one ledger row per item that got a `VERDICT <n>:` line, each with its
    verdict and an equal share of the run's cost and tokens; an item without one gets no row and stays [review]; no verdict
    at all is one failed row for the whole batch.
    Returns (exit_code, run_file, ok, result, elapsed_s)."""
    started = time.time()
    if role == "work" and kind in ("plan", "review"):
        try:
            delivery_note = "\n补行前的项目交付核对（本次刷新）：\n" + project_delivery.render(project_delivery.write(sys.modules[__name__]))
        except Exception as exc:
            log("project_delivery_failed", repr(exc)[:200])
            delivery_note = "\n项目交付核对失败；补自发草样前先查明该项目过审未合分支数，不能按 0 处理。"
        head, review_rules, tail = prompt.partition(REVIEW_AS_TRIAL)
        prompt = head + delivery_note + review_rules + tail
    if not deputy_ready(role):
        raise DeputyNotReady(f"{role}: no session and no fork to make (line_split.py not applied, or the fork was stopped)")
    backlog = BACKLOG.read_text(encoding="utf-8") if BACKLOG.exists() else ""
    prompt += owner_words_block(backlog, role)
    if kind in ("owner", "review", "plan"):
        prompt += coordinator_answers_block(backlog, role)
    prompt += pending.replies_block(OWNER_DATA, role_section(role), CONTROL.parent / "input" / "pending.py",
                                    backlog)  # new replies of this line, every kind of run
    entry = load_deputies().get(role) or {}
    fork = None if entry.get("session_id") else entry.get("fork_from")  # first run of a forked role: fork the source once
    if fork:
        child, _why = fork_child_id(Path(entry["fork_run_file"]), fork) if entry.get("fork_run_file") else (None, "")
        if child:  # an earlier fork was cut off after its init event: take that child, never fork twice
            update_deputy(role, session_id=child, created_at=now_iso(), fork_from=None, forked_from=fork, fork_state="forked")
            entry, fork = load_deputies().get(role) or {}, None
    resume = bool(entry.get("session_id"))
    sid = entry["session_id"] if resume else str(uuid.uuid4())
    if fork:  # session_id stays empty until the child's real id is read from the stream
        prompt = FORK_INTRO + prompt
        update_deputy(role, status="running", current={**current, "started_at": now_iso()})
    else:
        update_deputy(role, session_id=sid, created_at=entry.get("created_at") if resume else now_iso(),
                      status="running", current={**current, "started_at": now_iso()})
    source_lock = deputy_lock(FORK_ROLES[role]) if fork else None
    if source_lock and not take_lock(source_lock):  # the source deputy's session is in use: no fork now
        update_deputy(role, status="idle", current=None)
        raise DeputyNotReady(f"{role}: the session to fork from is in use")
    try:
        code, run_file, sid, resumed = session_run(
            kind, prompt, f"{label or kind}-{role}", sid, resume, deputy_effort(role, kind),
            lambda new: update_deputy(role, session_id=new, created_at=now_iso()), model=deputy_model(role, kind),
            compact_at=compact_limit(role),
            **({"fork_from": fork, "on_fork": lambda rf: update_deputy(role, fork_run_file=str(rf))} if fork else {}))
    finally:
        if source_lock:
            release_lock(source_lock)
    ok, result, _ = final_result(run_file)
    ok = ok and code == 0
    elapsed = round(time.time() - started)
    reason = fail_reason(code)
    forked = {}
    if fork:
        sid, why = fork_child_id(run_file, fork)
        if sid:
            update_deputy(role, session_id=sid, created_at=now_iso(), fork_from=None, forked_from=fork, forked_at=now_iso(),
                          fork_state="forked", fork_tries=0)
            raw, est = result_stats(run_file)["cost_usd"], estimate_stream_cost(run_file)
            forked = {"fork_from": fork, "cost_usd": round(est if raw is None else min(raw, est), 6), "cost_estimated": True}
        else:  # no id: nothing was created, so a transient stop (ceiling, timeout) just retries; a bad or conflicting id, or a second miss, stops for review
            tries = int(entry.get("fork_tries") or 0) + 1
            conflict = why != "no session id in the stream"
            update_deputy(role, fork_tries=tries, fork_note=why,
                          **({"fork_state": "blocked"} if conflict or (tries >= 2 and code not in (CEILING_STOP, -1)) else {}))
            ok = False
            log("fork_failed", why, role=role, source=fork, exit_code=code, tries=tries, run_file=str(run_file))
            sid = ""
    session_kind = "fork" if fork else ("resume" if resumed else "new")
    if batch is None:
        row = record_run(role, section or role_section(role), kind, deputy_effort(role, kind), session_kind, sid,
                         item, elapsed, code, ok, result, run_file, model=deputy_model(role, kind),
                         metadata={k: last_extra[k] for k in ("review_id", "review_snapshot") if k in last_extra} | forked)
    else:
        verdicts = batch_verdicts(result, len(batch)) if ok else {}
        stats = result_stats(run_file) | ({"cost_usd": forked["cost_usd"]} if forked else {})
        share = {f: (round(stats[f] / max(len(verdicts), 1), 6) if f == "cost_usd" else round(stats[f] / max(len(verdicts), 1)))
                 for f in ("cost_usd", "input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens")
                 if isinstance(stats.get(f), (int, float))}
        written = [(b["item"], b["extra"], {"verdict": verdicts[i]}) for i, b in enumerate(batch, 1) if i in verdicts] \
            or [("合批：" + "｜".join(b["item"][:20] for b in batch), {}, {"verdict": "unknown"})]
        for it, extra, over in written:
            row = record_run(role, section or role_section(role), kind, deputy_effort(role, kind), session_kind, sid,
                             it, elapsed, code, ok and bool(verdicts), result, run_file, model=deputy_model(role, kind),
                             metadata={k: extra[k] for k in ("review_id", "review_snapshot") if k in extra} | share | over
                             | {"batch": len(batch)} | ({"fork_from": fork, "cost_estimated": True} if forked else {}))
    context = maybe_compact(role, row, deputy_model(role, kind), f"compact-{role}")
    runs = int(load_deputies().get(role, {}).get("runs") or 0) + 1
    update_deputy(role, status="idle", current=None, runs=runs, context_tokens=context,
                  last={**last_extra, "finished_at": now_iso(), "ok": ok, "elapsed_s": elapsed,
                        "summary": (result.strip() if ok else f"失败：{reason}")[:240]})
    if role != "coordinator" and kind != "report":
        try:
            queue_deputy_question_notices(role, backlog, BACKLOG.read_text(encoding="utf-8"), datetime.now().astimezone())
            if pending_direct_ask_notices():
                spawn(["ask-notice"])
        except Exception as exc:  # a notice failure must not change the deputy run
            log("ask_notice_failed", repr(exc)[:200], lane=kind, role=role)
    return code, run_file, ok, result, elapsed


def executor_entry(role: str, project: str) -> dict:
    return (((load_deputies().get(role) or {}).get("executors") or {}).get(project)) or {}


def note_verdict(role: str, item: str, verdict: str) -> None:
    """Review verdict -> the role's executor session whose last_item is the reviewed item: rework counts up
    reworks_in_row, pass resets it (blocked/unknown: unchanged); RETIRE_AFTER_REWORKS in a row sets `retire`."""
    if verdict not in ("pass", "rework") or executor_model(item) == "haiku":  # Haiku rows never touch the project session's count
        return
    with backlog_io.lock(DEPUTIES_FILE):
        deputies = load_deputies()
        ex = next((e for e in ((deputies.get(role) or {}).get("executors") or {}).values()
                   if e.get("last_item") == item), None)
        if ex is None:
            return
        ex["reworks_in_row"] = 0 if verdict == "pass" else int(ex.get("reworks_in_row") or 0) + 1
        if ex["reworks_in_row"] >= RETIRE_AFTER_REWORKS:
            ex["retire"] = True
        save_deputies(deputies)


def retire_executor(role: str, section: str, project: str, ex: dict) -> None:
    """The session was flagged `retire`: one lessons run in it (resume, its own cwd), the reply is appended with the
    date to LESSONS_DIR/<project>.md, then its id moves to `retired`; the next run starts a new session."""
    started, sid = time.time(), ex["session_id"]
    run_file = tick_path(f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-lessons-{role}-{project}.jsonl")
    code = run_claude("lessons", PROMPTS["lessons"].format(project=project), run_file, ["--resume", sid],
                      EFFORTS["lessons"], **executor_launch(project))
    ok, text, _ = final_result(run_file)
    ok = ok and code == 0
    if ok and text.strip():
        LESSONS_DIR.mkdir(parents=True, exist_ok=True)
        with (LESSONS_DIR / f"{project}.md").open("a", encoding="utf-8") as f:
            f.write(f"\n## {datetime.now():%Y-%m-%d}（会话 {sid[:8]}，runs {ex.get('runs') or 0}）\n{text.strip()}\n")
    record_run(role, section, "lessons", EFFORTS["lessons"], "resume", sid, "", round(time.time() - started), code, ok,
               text, run_file, project=project)
    update_deputy(role, executors={project: {
        "session_id": None, "runs": 0, "retire": False, "reworks_in_row": 0, "last_item": None,
        "retired": [*(ex.get("retired") or []), {"session_id": sid, "retired_at": now_iso(), "runs": ex.get("runs") or 0}]}})
    log("retired", "" if ok else f"lessons exit {code}", role=role, project=project, session_id=sid)


def run_executor(role: str, section: str, item: str, project: str = "general") -> tuple[int, Path, bool, str, int, str]:
    """One run in the project's resident executor session (deputies.json <role>.executors.<project>, run in the
    project's cwd): new session first, then always resumed, compacted in place once it grows past COMPACT_TOKENS.
    A session flagged `retire` first gets its lessons run and is replaced. Effort alternates per lane by executor
    runs so far in the ledger. A Haiku run (owner\x2010-09) gets a new session of its own every time and leaves the
    project's session, its run count and context as they were. Returns (exit_code, run_file, ok, result, elapsed_s, effort)."""
    started = time.time()
    ex = executor_entry(role, project)
    if ex.get("retire") and ex.get("session_id"):
        retire_executor(role, section, project, ex)
        ex = executor_entry(role, project)
    effort = EXECUTOR_EFFORTS[sum(1 for r in load_runs() if r.get("kind") == "executor"
                                  and r.get("role") == role) % len(EXECUTOR_EFFORTS)]
    model = executor_model(item)
    own = model != "haiku"  # the run belongs to the project's session
    resume = own and bool(ex.get("session_id"))
    sid = ex["session_id"] if resume else str(uuid.uuid4())
    prior = int(ex.get("runs") or 0) if resume else 0
    began = now_iso()
    fresh = {} if resume else {"reworks_in_row": 0, "retire": False}  # only the reviewer changes these on a live session
    session = {"session_id": sid, "created_at": ex.get("created_at") if resume else began, "runs": prior, **fresh} if own else {}
    update_deputy(role, executors={project: {
        "project": project, **session, "effort": effort, "item_preview": item[:120], "last_item": item,
        "started_at": began, "finished_at": None, "exit_code": None}})
    lessons = LESSONS_DIR / f"{project}.md"
    prompt = PROMPTS["executor" if project in PROJECTS else "executor_general"].format(
        name=ROLE_NAMES[role], section=section, project=project, item=item, fresh="" if resume else "（新会话）",
        lessons=f"，以及 {lessons}（前任执行者留下的经验）" if lessons.exists() else "",
        audits=WORKSPACE / "incore" / "audits")
    code, run_file, sid, resumed = session_run(
        "executor", prompt, f"executor-{role}-{project}", sid, resume, effort,
        lambda new: update_deputy(role, executors={project: {"session_id": new, "created_at": now_iso(), "runs": 0,
                                                             "reworks_in_row": 0, "retire": False}}), project,
        model, compact_limit(role, project), role=role)
    ok, result, _ = final_result(run_file)
    ok = ok and code == 0
    elapsed = round(time.time() - started)
    row = record_run(role, section, "executor", effort, "resume" if resumed else "new", sid, item, elapsed, code, ok,
                     result, run_file, model=model, project=project)
    if own:
        context = maybe_compact(role, row, row["model"], f"compact-executor-{role}-{project}", project)
        session = {"session_id": sid, "runs": (prior if resumed else 0) + 1, "context_tokens": context}
    update_deputy(role, executors={project: {**session, "finished_at": now_iso(), "exit_code": code}})
    return code, run_file, ok, result, elapsed, effort


# ---- Codex executor (owner\x2010-04: while Claude usage waits; models and daily caps in CODEX_TIERS) ----
def wsl_path(p: Path) -> str:
    s = str(p)
    return f"/mnt/{s[0].lower()}{s[2:].replace(chr(92), '/')}" if len(s) > 1 and s[1] == ":" else s


def codex_lock(slot: int = 0) -> Path:
    return DATA / ("codex.lock" if slot == 0 else f"codex-{slot + 1}.lock")


def git_common_dir(cwd: Path) -> Path | None:
    """The real git dir of the repo holding cwd (cwd may be a subfolder, e.g. incore/input; for a worktree, whose
    `.git` file points at <common>/worktrees/<name>, the common dir), or None."""
    g = next((d / ".git" for d in (cwd, *cwd.parents) if (d / ".git").exists()), None)
    if g is None:
        return None
    if g.is_file():
        g = Path(g.read_text(encoding="utf-8").partition(":")[2].strip()).parents[1]
    return g if g.is_dir() else None


def codex_exec(prompt: str, run_file: Path, cwd: Path, effort: str, model: str = CODEX_TIERS[0][0],
               extra: tuple = (), gpu: bool = False) -> int:
    """One `codex exec` in WSL (workspace-write sandbox with network, the project dir plus CODEX_WRITABLE, the project's
    other folders `extra` and the git dirs of it and them, which the sandbox otherwise mounts read-only so `git switch -c`
    fails), its --json stream to run_file. gpu: no sandbox (it blocks the GPU). Returns the exit code, or -1 after a timeout kill."""
    dirs = dict.fromkeys(d for d in (*CODEX_WRITABLE, *extra, *map(git_common_dir, (cwd, *extra))) if d)
    sandbox = ["-s", "danger-full-access"] if gpu else ["-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]
    argv = [*CODEX, "exec", "-m", model, "-c", f'model_reasoning_effort="{effort}"', *sandbox,
            "--skip-git-repo-check", "--json", "-C", wsl_path(cwd),
            *(a for d in dirs for a in ("--add-dir", wsl_path(d))), "--add-dir", CODEX_SANDBOX_DIR, "-"]  # a Linux path: as is
    subprocess.Popen([*CODEX[:2], "mkdir", "-p", CODEX_SANDBOX_DIR], creationflags=CREATE_NO_WINDOW).wait()  # outside the sandbox, which cannot create it
    run_file.parent.mkdir(parents=True, exist_ok=True)
    with run_file.open("wb") as out:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
                                creationflags=CREATE_NO_WINDOW)
        try:
            proc.communicate(prompt.encode("utf-8"), timeout=TIMEOUTS["executor"])
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            proc.communicate()
            return -1
    return proc.returncode


def codex_result(run_file: Path) -> tuple[str, bool, str, dict]:
    """(thread_id, turn completed, last agent message, usage) from a `codex exec --json` stream."""
    tid, done, text, usage = "", False, "", {}
    for line in run_file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("type") == "thread.started":
            tid = e.get("thread_id") or tid
        elif e.get("type") == "turn.completed":
            done, usage = True, e.get("usage") or {}
        elif e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message":
            text = e["item"].get("text") or text
    return tid, done, text, usage


def run_codex(role: str, section: str, item: str, project: str, tier: tuple) -> tuple[int, Path, bool, str, int, str]:
    """One fresh Codex run of an executor item with the executors' rules. The ledger row has engine codex and cost 0
    (it uses no Claude usage). Returns (exit_code, run_file, ok, result, elapsed_s, effort)."""
    started = time.time()
    lessons = LESSONS_DIR / f"{project}.md"
    preamble = CODEX_PREAMBLE.replace(CODEX_SANDBOX_DIR + "/venv", f"{CODEX_SANDBOX_DIR}/venv-{role}-{project}")  # two runs at a time: own venv, no shared --clear
    prompt = preamble + read_rules() + "\n\n" + PROMPTS[
        "executor" if project in PROJECTS else "executor_general"].format(
        name=ROLE_NAMES[role], section=section, project=project, item=item, fresh="（新会话）",
        lessons=f"，以及 {lessons}（前任执行者留下的经验）" if lessons.exists() else "",
        audits=WORKSPACE / "incore" / "audits")
    if GPU_TAG in item:
        prompt += "\n\n本地模型用 `python3 control/llama_wsl.py start|bench|stop` 起停，用完必须 stop。"
    run_file = tick_path(f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-codex-{role}-{project}.jsonl")
    model = tier[0]
    for effort in tier[1]:
        code = codex_exec(prompt, run_file, project_cwd(project), effort, model, PROJECT_ADD_DIRS.get(project, ()),
                          gpu=GPU_TAG in tag_text(item))
        if code in (0, -1) or codex_result(run_file)[1]:  # only a launch refused before any turn tries the next effort
            break
    tid, done, text, usage = codex_result(run_file)
    ok = code == 0 and done
    elapsed = round(time.time() - started)
    append_jsonl(RUNS_FILE, {
        **ledger_row(role, section, "executor", effort, "new", tid, item, elapsed, code, ok, text, run_file,
                     model=model, project=project),
        "engine": "codex", "cost_usd": 0.0, "input_tokens": usage.get("input_tokens"),
        "cache_read_tokens": usage.get("cached_input_tokens"), "cache_creation_tokens": usage.get("cache_write_input_tokens"),
        "output_tokens": usage.get("output_tokens"), "context_tokens": None})
    return code, run_file, ok, text, elapsed, effort


def codex_tier(item: str, runs: list[dict]) -> tuple | None:
    """The first CODEX_TIERS entry that may take this todo today (small-only tiers take CODEX_SMALL items), else None."""
    today = datetime.now().astimezone().date()
    for tier in CODEX_TIERS:
        model, _, cap, small_only = tier
        if small_only and not CODEX_SMALL.search(item):
            continue
        if sum(1 for r in runs if r.get("kind") not in ("shadow_review", "review") and r.get("engine") == "codex" and r.get("model") == model
               and r["_at"].astimezone().date() == today) < cap:
            return tier
    return None


def _codex_runs(runs: list[dict], project: str, now: datetime | None, role: str | None) -> dict[str, str | None]:
    """item[:60] -> verdict of the first review after it (None: not reviewed yet) for the project's Codex executor runs of the
    last CODEX_HOLD_H hours, matched by (role, project). The first review after the run decides (a line keeps its text from
    todo to review), whatever other lines do: Claude's passes on other Uvox lines hid three Codex reworks in a row from a
    'latest review of the project' test (10-05)."""
    since = (now or datetime.now(timezone.utc)) - timedelta(hours=CODEX_HOLD_H)
    reviews = sorted((r for r in runs if r.get("kind") == "review"), key=lambda r: r["_at"])
    out = {}
    for run in runs:
        if run.get("kind") == "executor" and run.get("engine") == "codex" and run.get("project") == project and role in (None, run.get("role")) and run["_at"] >= since:
            key = _item_text(run.get("item") or "")[:60]
            after = next((r for r in reviews if r["_at"] > run["_at"] and _item_text(r.get("item") or "")[:60] == key), None)
            if out.get(key) not in ("rework", "blocked"):
                out[key] = after.get("verdict") if after else None
    return out


def codex_unreviewed(runs: list[dict], project: str, now: datetime | None = None, role: str | None = None) -> set[str]:
    """item[:60] of the project's Codex runs of the last CODEX_HOLD_H hours with no review in the ledger yet. A todo with that
    text is not given to Codex again: the reviewer turns the line back to todo before its verdict reaches the ledger (10-06
    08:48:44Z Codex took the 自媒体 line judged blocked, verdict logged 08:49:03Z)."""
    return {k for k, v in _codex_runs(runs, project, now, role).items() if v is None}


def codex_first_todo(sec: dict, project: str, runs: list[dict] = (), now: datetime | None = None, role: str | None = None) -> tuple[str, str] | None:
    """The project's first todo Codex may take (owner\x2010-25 via the coordinator: Opus-, Claude- and Haiku-tagged lines are
    skipped, not a reason to leave the project; a review row of the project no longer holds it back, as it never did
    Claude's lane: a todo that depends on it is written as blocked); None when there is none. A todo whose text matches a
    Codex run of the last CODEX_HOLD_H hours that was sent back (rework/blocked) or still awaits its review (codex_unreviewed)
    is skipped, the project's others are not (10-28: a rework held the whole project a day; matched by (role, project):
    `general` exists on two lines)."""
    waiting = codex_unreviewed(runs, project, now, role) | {k for k, v in _codex_runs(runs, project, now, role).items() if v in ("rework", "blocked")}
    return next((t for t in sec["todo"] if project_of(t[1]) == project and _item_text(t[1])[:60] not in waiting
                 and not any(tag in tag_text(t[1]) for tag in (OPUS_TAG, CLAUDE_TAG, HAIKU_TAG))), None)


def codex_pick(text: str, runs: list[dict], now: datetime, live: set) -> tuple[str, str] | None:
    """(role, project) of the executor todo Codex takes next: sections by Codex runs already used today, then by
    fair-share score (as choose_lane; Codex rows cost 0, so the score alone sends every run to one section); in a
    section, projects in todo order (sprint first), skipping units already live."""
    cost, secs = cost_7d(runs, now), parse_backlog(text, now)
    today = datetime.now().astimezone().date()
    used = lambda s: sum(1 for r in runs if r.get("kind") not in ("shadow_review", "review") and r.get("engine") == "codex" and r.get("section") == s and r["_at"].astimezone().date() == today)
    order = sorted((used(s), (cost.get(s, 0.0) + COST_FLOOR) / sec["weight"], i, s) for i, (s, sec) in enumerate(secs.items())
                   if SECTION_ROLES.get(s) and sec["weight"] > 0)
    for *_, section in order:
        role = SECTION_ROLES[section]
        for project in dict.fromkeys(project_of(i) for _, i in secs[section]["todo"]):
            first = codex_first_todo(secs[section], project, runs, now, role)
            if first and codex_tier(first[1], runs) and unit_key(role, project) not in live:
                return role, project
    return None


def codex_weekly_used() -> float | None:
    """Codex weekly used_percent from the newest session reading of any age (shadow_review.weekly_used), probed at most
    every CODEX_PROBE_S in this process; None when unreadable. Called only when a Codex run is about to start or a report is written."""
    if time.time() - CODEX_PROBE["at"] >= CODEX_PROBE_S:
        now = datetime.now(timezone.utc)
        try:
            obs = shadow_review.observe_quota(sys.modules[__name__], any_age=True)
            used, behind = shadow_review.weekly_used(obs, now), codex_burn.behind_points(obs, now.astimezone())
        except Exception as exc:  # unreadable: Codex runs as it did before 10-25, the report says so
            log("codex_quota_failed", repr(exc)[:200])
            used = behind = None
        CODEX_PROBE.update(at=time.time(), used=used, behind=behind)
    return CODEX_PROBE["used"]


def codex_burning() -> bool:
    """10-26: the weekly reading is codex_burn.BEHIND_POINTS or more below the burn line (codex_burn.py); an unreadable reading is
    not burning. Rides on codex_weekly_used, so what patches that (tests) leaves it off."""
    codex_weekly_used()
    behind = CODEX_PROBE.get("behind")
    return behind is not None and behind >= codex_burn.BEHIND_POINTS


def codex_parallel_slots() -> int:
    return CODEX_BURN_SLOTS if codex_burning() else CODEX_PARALLEL_SLOTS


def codex_slot_limit() -> int:
    """Lock files a Codex lane may take: CODEX_SLOTS, or CODEX_BURN_SLOTS while burning."""
    return CODEX_BURN_SLOTS if codex_burning() else CODEX_SLOTS


def codex_fallback(st: dict, t: float, text: str, live: set, slots: int = CODEX_SLOTS, *,
                   runs: list[dict] | None = None) -> None:
    """Start one Codex executor run if fewer than `slots` Codex runs are live and some todo has a tier with runs left
    today (codex_tier). Called with CODEX_SLOTS while Claude usage holds launches back, else with CODEX_PARALLEL_SLOTS."""
    if sum(lock_live(codex_lock(i)) for i in range(CODEX_LOCKS)) >= slots or t - st["aux"].get("codex", -1e9) < SPAWN_GRACE_S:
        return
    runs = load_runs() if runs is None else runs
    pick = codex_pick(text, runs, datetime.now(timezone.utc), live)
    if pick:
        used = codex_weekly_used()
        if used is not None and used >= CODEX_WEEKLY_MAX:
            if t - st["aux"].get("codex_full", -1e9) >= CODEX_PROBE_S:
                st["aux"]["codex_full"] = t
                log("codex_quota_full", f"Codex 周额度 {used:.0f}%，到 {CODEX_WEEKLY_MAX:.0f}% 不派新的 Codex 执行", lane=unit_key(*pick))
            return
        spawn(["codex", *pick])
        st["aux"]["codex"] = st["spawned"][unit_key(*pick)] = st.setdefault("codex_spawned", {})[unit_key(*pick)] = t
        log("codex_spawn", lane=unit_key(*pick))


def codex_lane(role: str, project: str) -> int:
    """One Codex executor run of the project's first todo (codex_first_todo); the line goes running -> review."""
    if role not in ROLE_NAMES or project not in ("general", *PROJECTS):
        return 2
    held, unit = [], f"codex {unit_key(role, project)}"
    used = codex_weekly_used()
    if used is not None and used >= CODEX_WEEKLY_MAX:
        log("codex_quota_full", f"Codex 周额度 {used:.0f}%，到 {CODEX_WEEKLY_MAX:.0f}% 不派新的 Codex 执行", lane=unit)
        return 0
    try:
        slot = next((p for p in map(codex_lock, range(codex_slot_limit())) if take_lock(p)), None)
        held += [slot] if slot else []
        if not slot or not take_lock(lane_lock(role, project)):
            log("idle", "codex or executor unit busy", lane=unit)
            return 0
        held.append(lane_lock(role, project))
        section = role_section(role)
        sec = parse_backlog(BACKLOG.read_text(encoding="utf-8")).get(section) or {"todo": []}
        runs = load_runs()
        found = codex_first_todo(sec, project, runs, role=role)
        if found is None:
            log("idle", lane=unit)
            return 0
        line, item = found
        tier = codex_tier(item, runs)
        if tier is None:
            log("codex_cap", lane=unit)
            return 0
        running = f"- [running {datetime.now().strftime('%H:%M')}] {item}"
        if not rewrite_line(line, running):
            log("skipped", "backlog line changed before start", item=item, kind="executor", role=role, engine="codex")
            return 0
        code, run_file, _, _, elapsed, effort = run_codex(role, section, item, project, tier)
        reason = "timeout" if code == -1 else ""
        completion = finish_lane_line(section, running, "review")
        reason = "; ".join(filter(None, (reason, completion)))
        log("ran", reason, item=item, kind="executor", section=section, role=role, project=project, model=tier[0],
            effort=effort, engine="codex", run_file=str(run_file), exit_code=code, elapsed_s=elapsed)
        return 0
    finally:
        for path in held:
            release_lock(path)


def sol_first_spawn(st: dict, t: float, text: str, runs: list[dict] | None = None) -> bool:
    """Claude is held back (hard ceiling, pacing, reserve, MAX_PARALLEL), so the deputy's review lane will not start: hand one
    pending [review] row to a sol-first lane (shadow_review.candidates). One Codex slot free, the weekly gate (CODEX_WEEKLY_MAX)
    and a SPAWN_GRACE_S gap as for codex_fallback; each review_id is tried once per supervisor run; the daily cap and the
    full eligibility test are the lane's. True when a lane was spawned."""
    if sum(lock_live(codex_lock(i)) for i in range(CODEX_LOCKS)) >= codex_slot_limit() \
            or t - st["aux"].get("solfirst", -1e9) < SPAWN_GRACE_S:
        return False
    tried = st.setdefault("solfirst", {})
    runs = load_runs() if runs is None else runs
    pick = next((c for c in shadow_review.candidates(sys.modules[__name__], parse_backlog(BACKLOG.read_text(encoding="utf-8")), runs)
                 if c[4] not in tried), None)
    used = codex_weekly_used() if pick else None
    if pick is None or (used is not None and used >= CODEX_WEEKLY_MAX):
        return False
    tried[pick[4]] = st["aux"]["solfirst"] = t
    spawn(["sol-first", pick[0], pick[4]])
    log("sol_first_spawn", lane=f"sol-first {pick[0]}", item=pick[3][:80], review_id=pick[4])
    return True


def codex_units() -> set:
    """Unit keys whose live lane lock belongs to a Codex lane (same pid as a live codex lock). Codex does not use Claude's
    five-hour window, so MAX_PARALLEL and the reserve count only the other units (owner\x2011-01: 20 Codex slots)."""
    pids = set()
    for i in range(CODEX_LOCKS):
        p = codex_lock(i)
        if lock_live(p):
            try:
                pids.add(p.read_text().strip())
            except OSError:
                pass
    units = set()
    for p in DATA.glob("lane-*.lock"):
        try:
            if p.read_text().strip() in pids and lock_live(p):
                units.add(p.stem.removeprefix("lane-").replace("-", ":", 1))
        except OSError:
            pass
    return units


def codex_plan_spawn(st: dict, t: float, text: str, live: set, claude_live: set,
                     runs: list[dict] | None = None) -> str | None:
    """owner\x2011-01: Codex has a free slot and no row it can take (codex_pick) -> spawn one `codex-plan <role>` for a line
    whose deputy is free and whose last plan run is PLAN_COOLDOWN_S old, fewest todos first. Returns the role or None."""
    if not CODEX_PLAN_ON or t - st["aux"].get("codex-plan", -1e9) < CODEX_PLAN_EVERY_S or len(claude_live) >= MAX_PARALLEL \
            or sum(lock_live(codex_lock(i)) for i in range(CODEX_LOCKS)) >= codex_parallel_slots():
        return None
    now = datetime.now(timezone.utc)
    runs = load_runs() if runs is None else runs
    if codex_pick(text, runs, now, live) is not None:
        return None
    cands = []
    for i, (section, sec) in enumerate(parse_backlog(text, now).items()):
        role = SECTION_ROLES.get(section)
        last = last_run(runs, "plan", section)
        if role in ROLE_NAMES and sec["weight"] > 0 and role not in live and not lock_live(deputy_lock(role)) \
                and deputy_ready(role) and fork_source_free(role) \
                and (last is None or (now - last).total_seconds() >= PLAN_COOLDOWN_S):
            cands.append((len(sec["todo"]), i, role))
    if not cands:
        return None
    used = codex_weekly_used()
    if used is not None and used >= CODEX_WEEKLY_MAX:
        return None
    role = min(cands)[2]
    spawn(["codex-plan", role])
    st["aux"]["codex-plan"] = st["spawned"][role] = t
    log("codex_plan_spawn", lane=role)
    return role


def codex_plan_lane(role: str) -> int:
    """One deputy plan run that writes Codex rows (PROMPTS["codex_plan"]). Held back by the hard ceiling, the reserve and
    MAX_PARALLEL (Claude units only), not by the pacing line (owner\x2011-01)."""
    if role not in ROLE_NAMES:
        return 2
    unit, lock = f"codex-plan {role}", lane_lock(role)
    if not take_lock(lock):
        return 0
    try:
        if not deputy_ready(role) or not fork_source_free(role) or not take_lock(deputy_lock(role)):
            log("deputy_busy", lane=unit, role=role, kind="plan")
            return 0
        try:
            sampled = fresh_usage()
            if sampled is None:
                log("usage_unknown", lane=unit)
                return 0
            u, base = sampled[0], baseline(time.time(), sampled[1])
            running = max(len(lock_units() - codex_units()) - 1, 0)  # lock_units includes this lane's own lock
            block = "capped" if running >= MAX_PARALLEL else launch_block(u, base, running)
            if block in ("capped", "hard_ceiling", "reserve"):
                log(block, lane=unit, u=u, baseline=base, running=running)
                return 0
            section = role_section(role)
            prompt = PROMPTS["codex_plan"].format(name=ROLE_NAMES[role], section=section)
            code, run_file, _, _, elapsed = run_resident(
                role, "plan", prompt, {"source": "codex-plan", "kind": "plan", "text_preview": f"{section}：给 Codex 补行"},
                {"kind": "plan", "section": section, "codex_plan": True}, item="", section=section)
            log("ran", {-1: "timeout", CEILING_STOP: "ceiling_stop"}.get(code, ""), u=u, baseline=base, item="给 Codex 补行",
                kind="plan", section=section, role=role, model=deputy_model(role, "plan"), effort=deputy_effort(role, "plan"),
                run_file=str(run_file), exit_code=code, elapsed_s=elapsed, codex_plan=True)
            return 0
        finally:
            release_lock(deputy_lock(role))
    finally:
        release_lock(lock)


def held_fallback(st: dict, t: float, text: str, live: set, slots: int = CODEX_SLOTS, *,
                  runs: list[dict] | None = None) -> None:
    """What Codex does while Claude is held back: a sol first review if a row is waiting, else an executor run."""
    if not sol_first_spawn(st, t, text, runs):
        codex_fallback(st, t, text, live, slots, runs=runs)


def sol_first_lane(role: str, review_id: str) -> int:
    """One sol first review of the [review] row with this review_id (sol_first_spawn), in a Codex slot. sol pass and not drawn
    for the spot check: shadow_review.primary makes it done. Anything else leaves the row [review] with the ledger row
    shadow_review.run wrote; the deputy reviews it when Claude is free, not shown sol's verdict. No review packet: skipped."""
    if role not in ROLE_NAMES or role == "quant":
        return 2
    unit, mod, held = f"sol-first {role}", sys.modules[__name__], []
    used = codex_weekly_used()
    if used is not None and used >= CODEX_WEEKLY_MAX:
        log("codex_quota_full", f"Codex 周额度 {used:.0f}%，到 {CODEX_WEEKLY_MAX:.0f}% 不起先审", lane=unit)
        return 0
    try:
        slot = next((p for p in map(codex_lock, range(codex_slot_limit())) if take_lock(p)), None)
        if not slot:
            log("idle", "codex slots busy", lane=unit)
            return 0
        held.append(slot)
        row = next((c for c in shadow_review.candidates(mod, parse_backlog(BACKLOG.read_text(encoding="utf-8")), load_runs())
                    if c[4] == review_id and c[0] == role), None)
        if row is None:
            log("idle", "row gone, changed or already tried", lane=unit)
            return 0
        _, section, line, item, _ = row
        packet = review_packet_for(item)
        if not packet or packet["hard"]:  # no packet: nothing to freeze; a hard one is the deputy lane's auto rework
            log("sol_first_skipped", "no review packet" if not packet else "auto rework is the deputy lane's", lane=unit, item=item[:80])
            return 0
        shadow, reason = shadow_review.run(mod, role, section, item, packet)
        if shadow is None:
            log("sol_first_skipped", reason or "未提供跳过原因", lane=unit, item=item[:80])
            return 0
        done = shadow_review.primary(mod, role, section, line, item, shadow)
        log("sol_first", "done" if done else "left for the deputy", lane=unit, item=item[:80], review_id=review_id)
        return 0
    finally:
        for path in held:
            release_lock(path)


def mark_answered(message_id: str) -> None:
    if not DISCORD_DB.exists():
        return
    try:
        with closing(sqlite3.connect(DISCORD_DB, timeout=5)) as db:
            db.execute("UPDATE direct_requests SET status='answered',updated_at=? WHERE message_id=?",
                       (now_iso(), message_id))
            db.commit()
    except sqlite3.Error:
        pass


def run_owner_item(line: str, item: str) -> None:
    m = TRAILER_RE.search(item)
    role = m.group(2) if m else ""
    if role not in ROLE_NAMES:
        rewrite_line(line, f"- [blocked: 无法识别角色 {datetime.now().strftime('%H:%M')}] {item}")
        return
    message_id = m.group(1)
    name = ROLE_NAMES[role]
    text = TRAILER_RE.sub("", item).replace(NOTIFIED, "")
    text = re.sub(r"^【Discord[^】]*】", "", text).strip()
    hhmm = datetime.now().strftime("%H:%M")
    if not deputy_ready(role) or not fork_source_free(role):  # forked role not forked yet (or its source is in use): the item waits as todo
        log("deputy_not_ready", lane="owner", role=role)
        return
    lock = deputy_lock(role)
    if not take_lock(lock):  # another run of this role's session is active; item stays todo
        log("deputy_busy", lane="owner", role=role)
        return
    try:
        if not rewrite_line(line, f"- [running {hhmm}] {item}"):
            return
        prompt = PROMPTS["owner"].format(name=name, role=role, item=text)
        code, run_file, ok, result, elapsed = run_resident(
            role, "owner", prompt, {"message_id": message_id, "source": "discord", "text_preview": text[:120]},
            {"message_id": message_id}, item=text)
    finally:
        release_lock(lock)
    running = f"- [running {hhmm}] {item}"
    if ok:
        posted = post_discord(role, result.strip() or f"已完成，无文字结果，见 ticks/{run_file.name}", reply=True)
        final = f"- [done {datetime.now().strftime('%H:%M')}] {item}"
    else:
        reason = fail_reason(code)
        posted = post_discord(role, f"执行失败（{reason}），详情见 ticks/{run_file.name}。", reply=True)
        final = f"- [blocked: 执行失败 {datetime.now().strftime('%H:%M')}，见 ticks/{run_file.name}] {item}"
    rewrite_line(running, final)
    mark_answered(message_id)
    log("owner_ran", "" if posted else "discord post failed", role=role, item=item, model=deputy_model(role, "owner"),
        run_file=str(run_file), exit_code=code, ok=ok, elapsed_s=elapsed)


def owner_lane() -> int:
    """Run owner items one at a time until none remain; own lock, so it can overlap a backlog executor."""
    if not take_lock(OWNER_LOCK):
        log("owner_busy")
        return 0
    try:
        seen: set[str] = set()
        while True:
            items = [x for x in owner_items(BACKLOG.read_text(encoding="utf-8")) if x[0] not in seen]
            if not items:
                return 0
            sampled = fresh_usage()
            if sampled is None:
                log("usage_unknown", lane="owner")
                return 0
            u, resets_at = sampled
            if u >= HARD_CEILING:
                log("hard_ceiling", lane="owner", u=u)
                for line, item in items:
                    m = TRAILER_RE.search(item)
                    if NOTIFIED in item or not m or m.group(2) not in ROLE_NAMES:
                        continue
                    if post_discord(m.group(2), f"额度已到 {HARD_CEILING:.0f}% 硬线，{eastern_hhmm(resets_at)}（America/New_York）后处理。", reply=True):
                        rewrite_line(line, f"{line}{NOTIFIED}")
                return 0
            seen.add(items[0][0])
            run_owner_item(*items[0])
    finally:
        release_lock(OWNER_LOCK)


# ---- ask lane (owner\x2010-05: a deputy's `[blocked: 等总理]` row gets a coordinator run at once; only the hard ceiling applies) ----
def load_ask_state() -> dict:
    try:
        data = json.loads(ASK_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ask_state(data: dict) -> None:
    ASK_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ASK_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, ASK_STATE)


def ask_pending(text: str, t: float) -> list[tuple[str, str]]:
    """(section, line) of every `等总理` row worth a run now: not tried yet, or tried under ASK_MAX_TRIES times with
    FAIL_BACKOFF_S gone by (the row is still there, so the coordinator did not answer it)."""
    state = load_ask_state()
    tried = lambda line: state.get(line)
    return [(section, line) for section, sec in parse_backlog(text).items() for line, _ in sec["ask"]
            if not tried(line) or (tried(line)["tries"] < ASK_MAX_TRIES and t - tried(line)["at"] >= FAIL_BACKOFF_S)]


def load_ask_notices() -> dict:
    try:
        data = json.loads(ASK_NOTICE_STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {"pending": data.get("pending", {}) if isinstance(data.get("pending"), dict) else {},
                    "attempted": data.get("attempted", {}) if isinstance(data.get("attempted"), dict) else {}}
    except (OSError, ValueError):
        pass
    return {"pending": {}, "attempted": {}}


def save_ask_notices(state: dict) -> None:
    ASK_NOTICE_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ASK_NOTICE_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, ASK_NOTICE_STATE)


def _answer_notice_status(status: str, item: str) -> str | None:
    done = re.match(r"^done\b.*?[：:]总理答\s*(.+)$", status)
    if done:
        return done.group(1).strip()
    if re.match(r"^blocked[:：]\s*等你决定", status):
        suggestion = re.search(r"（总理建议：(.+?)）", item)
        return suggestion.group(1).strip() if suggestion else None
    return None


def _notice_preview(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _ask_notice_row(section: str, line: str, line_no: int, question_limit: int = 40) -> dict | None:
    match = LINE_RE.match(line)
    if not match:
        return None
    item = match.group(2)
    delimiter = "：" if "：" in item else ":" if ":" in item else None
    if delimiter:
        project, question = item.split(delimiter, 1)
    else:
        project, question = section, item
    role = SECTION_ROLES.get(section)
    if role not in ROLE_NAMES:
        return None
    return {"role": role, "section": section, "item": item, "line_no": line_no,
            "project": project.strip(), "question": _notice_preview(question.strip(), question_limit)}


def _ask_notice_fingerprint(section: str, item: str, kind: str = "answer") -> str:
    return hashlib.sha256(f"{kind}\0{section}\0{item}".encode("utf-8")).hexdigest()


def _backlog_notice_rows(text: str):
    section = ""
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r")
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        match = LINE_RE.match(line)
        if match:
            yield section, line_no, line, match.group(1).strip(), match.group(2)


def queue_ask_answer_notices(rows: list[tuple[str, str]], answered_text: str, now: datetime) -> None:
    """Persist only rows from this ask run that changed into one of its two answer statuses."""
    deferred = []
    with backlog_io.lock(ASK_NOTICE_STATE, timeout=2):
        state = load_ask_notices()
        current = list(_backlog_notice_rows(answered_text))
        quiet = local_datetime(now).hour >= 23 or local_datetime(now).hour < 8
        for section, old_line in rows:
            old = LINE_RE.match(old_line)
            if not old:
                continue
            old_item = old.group(2)
            candidate = next((entry for entry in current if entry[0] == section and
                              (entry[4] == old_item or entry[4].startswith(old_item + "（总理建议：")) and
                              _answer_notice_status(entry[3], entry[4])), None)
            if not candidate:
                continue
            answer = _answer_notice_status(candidate[3], candidate[4])
            notice = _ask_notice_row(section, candidate[2], candidate[1])
            if not notice:
                continue
            fingerprint = _ask_notice_fingerprint(section, old_item, "answer")
            if fingerprint in state["pending"] or fingerprint in state["attempted"]:
                continue
            notice.update(fingerprint=fingerprint, answer=_notice_preview(answer, 300),
                          kind="answer", created_at=now.isoformat(), delivery="report" if quiet else "direct")
            state["pending"][fingerprint] = notice
            if quiet:
                deferred.append(notice)
        save_ask_notices(state)
    for notice in deferred:
        log("ask_notice_deferred", "quiet hours; queued for the 09:00 line report", role=notice["role"],
            fingerprint=notice["fingerprint"])


def queue_deputy_question_notices(role: str, before_text: str, after_text: str, now: datetime) -> None:
    """Queue only newly added or newly blocked questions in this deputy's own section."""
    is_ask = lambda status: bool(re.match(r"^blocked[:：]\s*等总理\b", status))
    before = {(section, item) for section, _, _, status, item in _backlog_notice_rows(before_text) if is_ask(status)}
    deferred = []
    with backlog_io.lock(ASK_NOTICE_STATE, timeout=2):
        state = load_ask_notices()
        quiet = local_datetime(now).hour >= 23 or local_datetime(now).hour < 8
        for section, line_no, line, status, item in _backlog_notice_rows(after_text):
            if SECTION_ROLES.get(section) != role or not is_ask(status) or (section, item) in before:
                continue
            fingerprint = _ask_notice_fingerprint(section, item, "question")
            if fingerprint in state["pending"] or fingerprint in state["attempted"]:
                continue
            notice = _ask_notice_row(section, line, line_no, question_limit=200)
            if not notice:
                continue
            notice.update(kind="question", role="coordinator", fingerprint=fingerprint,
                          created_at=now.isoformat(), delivery="report" if quiet else "direct")
            state["pending"][fingerprint] = notice
            if quiet:
                deferred.append(notice)
        save_ask_notices(state)
    for notice in deferred:
        log("ask_notice_deferred", "quiet hours; queued for the 09:00 coordinator report",
            role=notice["role"], fingerprint=notice["fingerprint"])


def recent_owner_reply_pending(role: str, now: datetime) -> bool | None:
    """True if this channel has an unanswered owner message from the last two minutes; None on a DB read error."""
    if not DISCORD_DB.exists():
        return False
    try:
        uri = DISCORD_DB.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            rows = db.execute("SELECT i.received_at FROM inbound i JOIN direct_requests r USING(message_id) "
                              "WHERE i.role=? AND r.status IN ('queued','running')", (role,)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        log("ask_notice_db_failed", repr(exc)[:160], role=role)
        return None
    except (OSError, sqlite3.Error, ValueError) as exc:
        log("ask_notice_db_failed", repr(exc)[:160], role=role)
        return None
    now_ts = now.timestamp()
    return any((received := _when(row[0])) is not None and 0 <= now_ts - received.timestamp() <= ASK_NOTICE_COLLISION_S
               for row in rows)


def _ask_notice_location(notice: dict) -> tuple[str, int]:
    try:
        for row in backlog_history.all_rows(BACKLOG):
            if row["section"] == notice["section"] and (row["item"] == notice["item"] or
                                                   row["item"].startswith(notice["item"] + "（总理建议：")):
                return "projects/" + Path(row["source"]).relative_to(BACKLOG.parent).as_posix(), row["line"]
    except OSError:
        pass
    return "projects/backlog.md（原记录位置，当前未找到）", int(notice["line_no"])


def _ask_notice_line_no(notice: dict) -> int:
    return _ask_notice_location(notice)[1]


def format_ask_notice(notice: dict) -> str:
    if notice.get("kind") == "question":
        return f"【转总理】{notice['project']}：{notice['question']}"
    source, line = _ask_notice_location(notice)
    return (f"{notice['project']}｜问题：{notice['question']}｜答复：{notice['answer']}｜"
            f"{source} 第 {line} 行")


def pending_report_ask_notices(role: str) -> list[dict]:
    with backlog_io.lock(ASK_NOTICE_STATE, timeout=2):
        state = load_ask_notices()
        return [notice for notice in state["pending"].values()
                if notice.get("role") == role and notice.get("delivery") == "report"]


def pending_direct_ask_notices() -> bool:
    return any(notice.get("delivery") == "direct" for notice in load_ask_notices()["pending"].values())


def acknowledge_report_ask_notices(fingerprints: list[str]) -> None:
    if not fingerprints:
        return
    with backlog_io.lock(ASK_NOTICE_STATE, timeout=2):
        state = load_ask_notices()
        for fingerprint in fingerprints:
            notice = state["pending"].pop(fingerprint, None)
            if notice:
                state["attempted"][fingerprint] = {"status": "reported", "at": now_iso()}
        save_ask_notices(state)


def send_pending_ask_notices(now: datetime | None = None) -> None:
    """Send each eligible channel batch once; collision delays stay pending for the next supervisor tick."""
    now = now or datetime.now().astimezone()
    groups: dict[str, list[dict]] = {}
    with backlog_io.lock(ASK_NOTICE_STATE, timeout=2):
        state = load_ask_notices()
        local = local_datetime(now)
        quiet = local.hour >= 23 or local.hour < 8
        for fingerprint, notice in list(state["pending"].items()):
            if notice.get("delivery") == "report":
                continue
            if quiet:
                notice["delivery"] = "report"
                log("ask_notice_deferred", "quiet hours; queued for the 09:00 line report", role=notice["role"],
                    fingerprint=fingerprint)
                continue
            waiting = recent_owner_reply_pending(notice["role"], now)
            if waiting is None:
                continue
            if waiting:
                if not notice.get("waiting_logged"):
                    log("ask_notice_deferred", "recent owner message is still unanswered; retry next tick",
                        role=notice["role"], fingerprint=fingerprint)
                    notice["waiting_logged"] = True
                continue
            groups.setdefault(notice["role"], []).append(notice)
            state["attempted"][fingerprint] = {"status": "sending", "at": now.isoformat()}
            del state["pending"][fingerprint]
        save_ask_notices(state)

    for role, notices in groups.items():
        answers = [notice for notice in notices if notice.get("kind", "answer") == "answer"]
        questions = [notice for notice in notices if notice.get("kind") == "question"]
        parts = []
        if answers:
            parts.append("【总理答复】\n" + "\n".join(format_ask_notice(notice) for notice in answers))
        if questions:
            parts.append("\n".join(format_ask_notice(notice) for notice in questions))
        message = "\n".join(parts)
        try:
            sent = bool(post_discord(role, message, hold=False, **({"sender": ROLE_NAMES["coordinator"]} if answers and not questions else {})))
        except Exception as exc:
            sent = False
            error = repr(exc)[:160]
        else:
            error = ""
        with backlog_io.lock(ASK_NOTICE_STATE, timeout=2):
            state = load_ask_notices()
            for notice in notices:
                state["attempted"][notice["fingerprint"]] = {
                    "status": "sent" if sent else "failed", "at": now.isoformat()}
            save_ask_notices(state)
        log("ask_notice_sent" if sent else "ask_notice_failed", error, role=role, rows=len(notices))


def run_ask(rows: list[tuple[str, str]]) -> None:
    """One coordinator run answering all `rows` (caller holds the coordinator's deputy lock). The rows' tries are counted
    first; afterwards a row that is still `等总理` and has used up its tries goes to the owner, one that changed is forgotten."""
    t, state = time.time(), load_ask_state()
    for _, line in rows:
        state[line] = {"tries": int((state.get(line) or {}).get("tries") or 0) + 1, "at": t}
    save_ask_state(state)
    now = datetime.now()
    prompt = PROMPTS["ask"].format(
        now=now.strftime("%Y-%m-%d %H:%M"), stamp=now.strftime("%m-%d %H:%M"), n=len(rows), log=WORKSPACE / "incore" / "audits" / "coordinator-log.md",
        rows="\n".join(f"{i}. （{section}）{line}" for i, (section, line) in enumerate(rows, 1)))
    code, run_file, ok, result, elapsed = run_resident(
        "coordinator", "ask", prompt, {"source": "ask", "text_preview": f"{len(rows)} 条等总理"}, {"kind": "ask"},
        item=f"{len(rows)} 条等总理的提问")
    answered_text = BACKLOG.read_text(encoding="utf-8")
    left = {line for sec in parse_backlog(answered_text).values() for line, _ in sec["ask"]}
    state = {line: s for line, s in load_ask_state().items() if line in left}  # answered or changed rows are forgotten
    for _, line in rows:
        if line in state and code == CEILING_STOP:
            state[line]["tries"] -= 1  # stopped by the ceiling, not the coordinator's failure
        elif line in state and state[line]["tries"] >= ASK_MAX_TRIES:
            m = LINE_RE.match(line)
            note = f"（总理 {ASK_MAX_TRIES} 次没答复，见 ticks/{run_file.name}）"
            if m and rewrite_line(line, f"- [blocked: 等你决定] {m.group(2)}{note}"):
                del state[line]
    save_ask_state(state)
    log("ask_ran", "" if ok else fail_reason(code), rows=len(rows), left=len(left & {line for _, line in rows}),
        model=deputy_model("coordinator", "ask"), run_file=str(run_file), exit_code=code, ok=ok, elapsed_s=elapsed)
    finished = datetime.now().astimezone()
    try:
        queue_ask_answer_notices(rows, answered_text, finished)
        if pending_direct_ask_notices():
            spawn(["ask-notice"])
    except Exception as exc:  # notifications never change the ask run's result or hold up scheduling
        log("ask_notice_failed", repr(exc)[:200], lane="ask")


def ask_lane() -> int:
    """Answer the `等总理` rows with one coordinator run; own lock, so it overlaps backlog executors and other lanes,
    but not another run in the coordinator's session (its deputy lock)."""
    if not take_lock(ASK_LOCK):
        log("ask_busy")
        return 0
    try:
        rows = ask_pending(BACKLOG.read_text(encoding="utf-8"), time.time())
        if not rows:
            return 0
        lock = deputy_lock("coordinator")
        current = (load_deputies().get("coordinator") or {}).get("current")
        if isinstance(current, dict) and current.get("kind") == "terminal":  # the owner has the session open
            log("deputy_busy", lane="ask", role="coordinator")
            return 0
        if not take_lock(lock):
            log("deputy_busy", lane="ask", role="coordinator")
            return 0
        try:
            sampled = fresh_usage()
            if sampled is None:
                log("usage_unknown", lane="ask")
            elif sampled[0] >= HARD_CEILING:
                log("hard_ceiling", lane="ask", u=sampled[0])
            else:
                run_ask(rows)
        finally:
            release_lock(lock)
        return 0
    finally:
        release_lock(ASK_LOCK)


# ---- report lane (twice-daily reports at fixed slots; only the hard ceiling applies) ----
def report_now() -> datetime:
    return datetime.now()


def load_reports() -> dict:
    try:
        data = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_reports(data: dict) -> None:
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPORT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, REPORT_FILE)


def send_email(subject: str, body: str) -> bool:
    try:
        done = subprocess.run([str(GATEWAY_PY), str(EMAIL_SCRIPT), subject, body], capture_output=True,
                              timeout=120, cwd=str(WORKSPACE), creationflags=CREATE_NO_WINDOW)
        return done.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def resident_prefix(role: str, section: str | None = None) -> str:
    section = section or role_section(role)
    return f"你是{ROLE_NAMES[role]}（常驻副总理），负责{section}。"


def push_slots() -> dict[str, str]:
    """Enabled life pushes as {"push:<name>": "HH:MM"}; meal pushes go LIFE_MEAL_LEAD_MIN before the meal."""
    times = {"morning": LIFE_MORNING, "night": LIFE_NIGHT}
    for meal, hhmm in LIFE_MEALS.items():
        times[meal] = (datetime.strptime(hhmm, "%H:%M") - timedelta(minutes=LIFE_MEAL_LEAD_MIN)).strftime("%H:%M")
    return {f"push:{n}": times[n] for n in PUSH_NAMES if n in LIFE_PUSHES_ON}


def run_push(push: str, now: datetime, late_from: str | None = None) -> tuple[str, str]:
    """One read-only life push (caller holds the life lock): the life deputy writes the text, it goes to the Discord
    life channel only. A reply of just "skip" sends nothing and counts as done for the day."""
    date, name = now.strftime("%Y-%m-%d"), PUSH_NAMES[push]
    prompt = resident_prefix("life") + PUSH_PROMPTS[push].format(date=date)
    code, _, ok, text, _ = run_resident("life", "report", prompt, {"source": "push", "date": date},
                                        {"kind": "push", "date": date}, label=f"push-{push}", item=f"push:{push}")
    text = text.strip()
    if code != 0 or not ok or not text:
        return "failed", fail_reason(code) + ("" if code in (-1, CEILING_STOP) else "，无有效结果")
    if re.fullmatch(r"\W*skip\W*", text, re.I):
        return "skipped", ""
    if len(text) > PUSH_MAX_CHARS:
        return "failed", "回复过长，疑似不是提醒"
    title = f"{name} {now.strftime('%m-%d')}" + (f"（补发，原定 {late_from}）" if late_from else "")
    return ("sent", "") if post_discord("life", title + "\n" + text) else ("failed", "发送失败：discord")


def codex_report_note(now: datetime) -> str:
    """First fact line for the coordinator's daily report; only the two reset-card reminder conditions qualify."""
    now = now.astimezone()
    if not any(datetime.fromisoformat(at).astimezone() > now for at in CODEX_RESETS):
        return ""
    try:
        observation = shadow_review.observe_quota(sys.modules[__name__], any_age=True)
    except Exception:  # an unreadable quota cannot support a reset recommendation
        return ""
    return codex_burn.reset_note(observation, now)


def report_health_block() -> str:
    try:
        return "【机器和备份】\n" + daily_health.report_line() + "\n"
    except Exception as exc:  # health is an aid; it must not stop the report
        log("daily_health_failed", repr(exc)[:200])
        return "【机器和备份】\n机器和备份：异常（采集失败）\n"



def life_date_prompt(now: datetime) -> tuple[str, list[dict]]:
    try:
        return life_dates.prompt_block(LIFE_DATES_FILE, LIFE_DATES_REPORTED_FILE, now.date())
    except Exception as exc:  # the date table is an aid; a bad table must not stop the report
        log("life_dates_failed", repr(exc)[:200])
        return "日期表自检失败。", []


def record_life_date_reminders(role: str, slot: str, status: str, entries: list[dict]) -> None:
    if role != "life" or slot not in REPORT_SLOTS or status != "sent" or not entries:
        return
    try:
        life_dates.record_reported(LIFE_DATES_REPORTED_FILE, entries)
    except Exception as exc:  # a report already sent must not be turned into a report failure
        log("life_dates_record_failed", repr(exc)[:200])


# ---- stuck sprint rows (owner\x2011-03 terminal 「机制 ABCD 都做」C): a sprint project's row stuck for STUCK_H goes to the coordinator ----
STUCK_H = 6  # a row in the same stuck state for this long counts as stuck
STUCK_CHECK_S = 600  # the supervisor looks this often (reads every history copy of the backlog)
STUCK_REPEAT_H = 24  # the same row is written to 组织线 at most this often
STUCK_DAY = (8 * 60, 23 * 60)  # local minutes: the 等总理 row is written only inside; outside it is only noted (and reported at 09:00)
STUCK_SANDBOX_RE = re.compile(r"沙箱|权限|sandbox", re.I)


def stuck_kind(status: str, item: str) -> str | None:
    """Why a sprint row counts as stuck: `blocked: dotz …`, 返回重派, a blocked row naming sandbox/permission (not a
    `等…` row: those already wait for a person), or a Claude-tagged todo; None for any other row."""
    if re.match(r"blocked[:：]\s*dotz", status):
        return "dotz 阻塞"
    if "返回重派" in status:
        return "返回重派"
    if status.startswith("blocked") and not re.match(r"blocked[:：]\s*等", status) and STUCK_SANDBOX_RE.search(status):
        return "沙箱/权限挡住"
    if status == "todo" and CLAUDE_TAG in tag_text(item):
        return "Claude 执行者 todo 没派出去"
    return None


def backlog_snapshots(history: Path | None = None) -> list[tuple[datetime, Path]]:
    """(UTC time, path) of every backlog history copy, oldest first: the newest ones by file name, the first copy of each
    day (history/daily/) by its modification time."""
    history = BACKLOG_HISTORY if history is None else history
    out = []
    for path in history.glob("backlog-*.md"):
        m = re.fullmatch(r"backlog-(\d{8})T(\d{6})\.(\d+)Z\.md", path.name)
        if m:
            out.append((datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S%f").replace(tzinfo=timezone.utc), path))
    for path in (history / "daily").glob("backlog-*.md"):
        try:
            out.append((datetime.fromtimestamp(path.stat().st_mtime, timezone.utc), path))
        except OSError:
            pass
    return sorted(out)


def stuck_since(sigs: set[bytes], now: datetime, history: Path | None = None) -> dict[bytes, datetime]:
    """sig -> the oldest history copy such that every copy from it to now contains the row prefix `sig`; `now` for a row in
    no copy (duration unknown = 0). One pass, newest copy first, stops when every row has dropped out."""
    since, alive = {s: now for s in sigs}, set(sigs)
    for when, path in reversed(backlog_snapshots(history)):
        if not alive:
            break
        try:
            data = path.read_bytes()
        except OSError:
            continue
        alive = {s for s in alive if s in data}
        for s in alive:
            since[s] = when
    return since


def stuck_rows(text: str, now: datetime, *, sprints: dict | None = None,
               history: Path | None = None, runs_file: Path | None = None) -> list[dict]:
    """Every row of a line's sprint project (parse_backlog's `sprint`, any status) that is in a stuck state (stuck_kind)
    and has been in that same state STUCK_H hours or more by the history copies (a todo is also only as old as the
    project's last executor run). Dicts: section, project, status, item, kind, hours.
    Read-only callers supply sprints to bypass parse_backlog's diagnostic writes, and explicit data paths."""
    if sprints is None:
        sprints = {s: sec["sprint"] for s, sec in parse_backlog(text, now).items() if sec["sprint"]}
    found, cur = [], ""
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith("## "):
            cur = line[3:].strip()
        m = LINE_RE.match(line)
        if m and cur in sprints and project_of(m.group(2)) == sprints[cur]:
            kind = stuck_kind(m.group(1).strip(), m.group(2))
            if kind:
                found.append((cur, sprints[cur], m.group(1).strip(), m.group(2), kind))
    if not found:
        return []
    sig = lambda status, item: f"- [{status}] {item[:60]}".encode("utf-8")
    since = stuck_since({sig(s, i) for _, _, s, i, _ in found}, now, history)
    runs = load_runs(runs_file) if any(k.startswith("Claude") for *_, k in found) else []
    out = []
    for section, project, status, item, kind in found:
        start = since[sig(status, item)]
        if kind.startswith("Claude"):  # a run in between would not show in the day-apart copies
            start = max(start, last_executor_runs(runs, SECTION_ROLES.get(section, "")).get(project, start))
        hours = (now - start).total_seconds() / 3600
        if hours >= STUCK_H:
            out.append({"section": section, "project": project, "status": status, "item": item, "kind": kind, "hours": hours})
    return out


def stuck_alarm(st: dict, t: float, now: datetime | None = None) -> None:
    """Every STUCK_CHECK_S: for each stuck sprint row, local 08:00–23:00 append one `[blocked: 等总理]` row to 组织线 (the ask
    lane takes it at once), at most once per STUCK_REPEAT_H per row; outside those hours only log it (the 09:00 report
    lists it). State: data/stuck-alarm.json {"written": {key: iso}, "noted": {key: iso}}."""
    if t - st.get("stuck_at", -1e9) < STUCK_CHECK_S:
        return
    st["stuck_at"] = t
    now = now or datetime.now(timezone.utc)
    rows = stuck_rows(BACKLOG.read_text(encoding="utf-8"), now)
    if not rows:
        return
    state_file = DATA / "stuck-alarm.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    written, noted = state.get("written", {}), state.get("noted", {})
    local = local_datetime(now)
    day = STUCK_DAY[0] <= local.hour * 60 + local.minute < STUCK_DAY[1]
    clean = lambda s, n: re.sub(r"[\[\]\r\n]", " ", s)[:n]

    def append(row):
        def fn(lines):
            rng = backlog_io._section_rows(lines, line_map.ORG)
            end = rng.stop
            while end > rng.start and not lines[end - 1].strip():
                end -= 1
            lines.insert(end, row + ("\r" if end > rng.start and lines[end - 1].endswith("\r") else ""))
            return lines
        return fn
    try:
        for r in rows:
            key = f"{r['status'][:40]}|{r['item'][:60]}"
            if key not in noted:
                noted[key] = now.isoformat(timespec="seconds")
                log("stuck_noted", row=key, kind=r["kind"], hours=round(r["hours"], 1))
            last = _when(written.get(key))
            if not day or (last and (now - last).total_seconds() < STUCK_REPEAT_H * 3600):
                continue
            backlog_io.edit(append(f"- [blocked: 等总理] 组织：卡住报警：{r['project']}…{clean(r['item'], 60)}…卡在 {clean(r['status'], 40)}，"
                                   f"已 {int(r['hours'])} 小时；请总理处理"), BACKLOG)
            written[key] = now.isoformat(timespec="seconds")
            log("stuck_alarm", row=key, kind=r["kind"], hours=round(r["hours"], 1))
    finally:  # a row already written must not be written again because a later one failed
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"written": written, "noted": noted}, ensure_ascii=False, indent=1), encoding="utf-8")


def report_stuck_block(role: str, now: datetime | None = None) -> str:
    """Report prompt block 【卡住的冲刺行】: every stuck sprint row (the coordinator all, a deputy its own line's); empty when none."""
    try:
        rows = stuck_rows(BACKLOG.read_text(encoding="utf-8"), now or datetime.now(timezone.utc))
    except Exception as exc:  # an aid; the report goes out without it
        log("stuck_report_failed", repr(exc)[:200], role=role)
        return ""
    rows = [r for r in rows if role == "coordinator" or r["section"] == LINE_SECTIONS.get(role)]
    if not rows:
        return ""
    return ("【卡住的冲刺行】\n" + "\n".join(f"- {r['project']}｜{r['item'][:60]}｜{r['status'][:60]}（{r['kind']}）｜已 {int(r['hours'])} 小时"
                                        for r in rows)
            + "\n报告第一条原样写「卡住的冲刺行」这一节，把上面全部列出（项目、行头、状态、小时数），不省略，写完再写别的。\n\n")



def write_report(role: str, now: datetime, slot: str, deferred_ask_notices: list[dict] | None = None,
                 machine_block: str | None = None, date_reminder_keys: list[dict] | None = None,
                 standalone: bool = False) -> tuple[str, str]:
    """One read-only report (text, reason), text empty on failure. Normally runs in the role's resident session
    (caller holds the deputy lock); `standalone` uses a new session without touching the resident state while the
    owner holds that lock. Morning slot = what happened since the last slot (last night); evening = since the morning one."""
    i = REPORT_SLOTS.index(slot)
    name, date = ROLE_NAMES[role], now.strftime("%Y-%m-%d")
    since = datetime.combine(now.date() - timedelta(days=1 if i == 0 else 0),
                             datetime.strptime(REPORT_SLOTS[i - 1], "%H:%M").time())  # i == 0 wraps to yesterday's last slot
    template = {"coordinator": REPORT_PROMPT_COORDINATOR, "intelligence": REPORT_PROMPT_INTELLIGENCE}.get(role, REPORT_PROMPT)[i]
    facts = ""
    if role != "intelligence":  # its report is about the intel itself, not about runs
        try:
            facts = review_stats.day_facts(now.date(), None if role == "coordinator" else role_section(role), since=since)
        except Exception as exc:  # the block is an aid; the old prompt wording falls back to backlog.md
            log("report_facts_failed", repr(exc)[:200], role=role)
    machine_block = machine_block or report_health_block()
    sol_note = shadow_review.disabled_note(sys.modules[__name__]) if role == "coordinator" else ""
    codex_note = codex_report_note(now) if role == "coordinator" else ""
    if codex_note:
        facts = codex_note + "\n" + facts
    prompt = resident_prefix(role) + sol_note + report_stuck_block(role) + facts + machine_block + template.format(name=name, date=date, slot=slot, span=f"{since:%m-%d %H:%M} 以来")
    if role == "coordinator" and now.weekday() == 6 and i == len(REPORT_SLOTS) - 1:
        prompt += REPORT_SUNDAY_COORDINATOR
    if role == "life" and slot in REPORT_SLOTS:
        date_prompt, keys = life_date_prompt(now)
        prompt += "\n\n" + date_prompt
        if date_reminder_keys is not None:
            date_reminder_keys.extend(keys)
    if standalone:
        backlog = BACKLOG.read_text(encoding="utf-8") if BACKLOG.exists() else ""
        prompt += owner_words_block(backlog, role)
        prompt += pending.replies_block(OWNER_DATA, role_section(role), CONTROL.parent / "input" / "pending.py", backlog)
        prompt += (f"\n【独立日报会话】总理的 owner 终端正在使用常驻会话，本次只读写稿，不续接或修改那个会话。"
                   f"事实包截止 {now:%Y-%m-%d %H:%M:%S}（本机时间）；终端尚未落盘的新话不在其中，不推测。\n")
        started = time.time()
        code, run_file, sid, _ = session_run("report", prompt, f"report-{role}-standalone", str(uuid.uuid4()), False,
                                             deputy_effort(role, "report"), model=deputy_model(role, "report"),
                                             compact_at=compact_limit(role))
        ok, text, _ = final_result(run_file)
        record_run(role, role_section(role), "report", deputy_effort(role, "report"), "new", sid, "",
                   round(time.time() - started), code, ok and code == 0, text, run_file,
                   model=deputy_model(role, "report"), metadata={"source": "report-when-locked", "date": date, "slot": slot})
    else:
        code, run_file, ok, text, _ = run_resident(role, "report", prompt, {"source": "report", "date": date},
                                                   {"kind": "report", "date": date})
    text = text.strip()
    if code != 0 or not ok or not text:
        return "", fail_reason(code) + ("" if code in (-1, CEILING_STOP) else "，无有效结果")
    if re.fullmatch(r"\W*skip\W*", text, re.I):  # reports are never skipped (owner 2026-09-30)
        text = "这段时间暂无实质进展，流程正常运行。"
    if slot == "09:00" and deferred_ask_notices:
        heading = ("【转总理（夜间延后）】" if any(n.get("kind") == "question" for n in deferred_ask_notices)
                   else "【总理答复（夜间延后）】")
        text += "\n\n" + heading + "\n" + "\n".join(
            format_ask_notice(notice) for notice in deferred_ask_notices)
    if sol_note:
        shadow_review.mark_reminded(sys.modules[__name__])  # said once
    return text, ""


def report_title(now: datetime, slot: str, late: str | None) -> str:
    return f"日报 {now:%m-%d} {slot}" + ("（补发）" if late else "")


def post_report(role: str, text: str, now: datetime, slot: str, late: str | None) -> tuple[str, str]:
    """Discord channel only; the text is already in reports.json, so a failed post is retried without a new run."""
    return ("sent", "") if post_discord(role, report_title(now, slot, late) + "\n" + text) else ("failed", "发送失败：discord")


def send_slot_email(now: datetime, slot: str, day: dict, late: str | None) -> tuple[str, str]:
    """One combined email per slot: every deputy's text in REPORT_ROLES order, 未出 for the ones that never came."""
    body = "\n\n".join(f"【{ROLE_NAMES[r]}】\n" + (day.get(f"text:{r}@{slot}") or "未出") for r in REPORT_ROLES)
    return ("sent", "") if send_email(report_title(now, slot, late), body) else ("failed", "发送失败：email")


def report_lane() -> int:
    """Per slot: the deputies' reports back to back (Discord each), then the slot's one combined email once all are
    done or REPORT_EMAIL_WAIT_MIN after the slot. Keys in reports.json: "<role>@<slot>", "email@<slot>", "push:<name>"
    (state), "text:<role>@<slot>" (the written report)."""
    if not take_lock(REPORT_LOCK):
        log("report_busy")
        return 0
    try:
        tried: set[str] = set()
        machine_blocks: dict[str, str] = {}
        while True:
            now = report_now()
            state = load_reports()
            day = state.setdefault(now.strftime("%Y-%m-%d"), {})
            for old in sorted(state)[:-14]:  # the texts pile up: keep two weeks
                del state[old]
            cands = [(h, f"{r}@{h}") for h in REPORT_SLOTS for r in (*REPORT_ROLES, "email")] + [(h, k) for k, h in push_slots().items()]
            todo = late = None
            for hhmm, key in sorted(cands, key=lambda c: c[0]):  # missed slots are caught up the same day, labelled late
                start = datetime.combine(now.date(), datetime.strptime(hhmm, "%H:%M").time())
                push, email = key.startswith("push:"), key.startswith("email@")
                if now < start or day.get(key) in ("sent", "skipped", "missed") or key in tried:
                    continue
                if key.split("@")[0] in ROLE_NAMES and not deputy_ready(key.split("@")[0]):
                    day[key] = "missed"  # a forked role with no session yet has nothing to report; it never starts blank
                    continue
                if key.split("@")[0] in ROLE_NAMES and not fork_source_free(key.split("@")[0]):
                    log("deputy_busy", lane="report", role=key.split("@")[0], reason="fork source in use")
                    continue  # its source session is in use: retried next tick
                later = [h for h in REPORT_SLOTS if h > hhmm]  # a report not out when the next slot starts is dropped, not sent late
                if (push and now >= start + timedelta(minutes=PUSH_EXPIRE_MIN)) or (not push and later and now >= datetime.combine(
                        now.date(), datetime.strptime(later[0], "%H:%M").time())):
                    day[key] = "missed"
                    continue
                if email and not (all(day.get(f"{r}@{hhmm}") in ("sent", "missed") for r in REPORT_ROLES)
                                  or now >= start + timedelta(minutes=REPORT_EMAIL_WAIT_MIN)):
                    continue
                todo = key
                late = hhmm if now >= start + timedelta(minutes=PUSH_LATE_MIN if push else REPORT_WINDOW_MIN) else None
                break
            save_reports(state)
            if todo is None:
                return 0
            tried.add(todo)
            push = todo.startswith("push:")
            role, slot = ("life", None) if push else todo.split("@")
            result = None
            if role == "email":
                result = send_slot_email(now, slot, day, late)
            elif push or f"text:{todo}" not in day:  # a report whose Discord post failed is re-posted, not rewritten
                lock = deputy_lock(role)
                locked = take_lock(lock)
                current = (load_deputies().get(role) or {}).get("current") if not locked else None
                standalone = (not locked and role == "coordinator" and not push
                              and isinstance(current, dict) and current.get("kind") == "terminal")
                if not locked:
                    log("deputy_busy", lane="report", role=role)
                    if not standalone:  # automatic runs and other roles still wait for the resident session
                        continue
                try:
                    sampled = fresh_usage()
                    if sampled is None:
                        log("usage_unknown", lane="report")
                        return 0
                    if sampled[0] >= HARD_CEILING:
                        result = ("failed", f"额度 {sampled[0]:.0f}% 已到 {HARD_CEILING:.0f}% 硬线")
                    elif push:
                        result = run_push(todo.removeprefix("push:"), now, late)
                    else:
                        deferred = pending_report_ask_notices(role) if slot == "09:00" else []
                        if deferred:
                            day[f"ask_notice_ids:{todo}"] = [n["fingerprint"] for n in deferred]
                        if slot not in machine_blocks:
                            machine_blocks[slot] = report_health_block()
                        date_reminder_keys = []
                        text, reason = write_report(role, now, slot, deferred_ask_notices=deferred,
                                                    machine_block=machine_blocks[slot],
                                                    date_reminder_keys=date_reminder_keys,
                                                    **({"standalone": True} if standalone else {}))
                        if text:
                            if role == "life" and slot in REPORT_SLOTS and date_reminder_keys:
                                day[f"date_reminder_keys:{todo}"] = date_reminder_keys
                            day[f"text:{todo}"] = text
                        else:
                            result = ("failed", reason)
                finally:
                    if locked:
                        release_lock(lock)
            if result is None:
                result = post_report(role, day[f"text:{todo}"], now, slot, late)
            if role == "life" and slot in REPORT_SLOTS:
                record_life_date_reminders(role, slot, result[0],
                                           day.get(f"date_reminder_keys:{todo}", []))
            if result[0] == "sent" and role in ROLE_NAMES and slot == "09:00":
                acknowledge_report_ask_notices(day.get(f"ask_notice_ids:{todo}", []))
            day[todo] = result[0]
            save_reports(state)
            log("report", result[1], role=role, slot=slot, state=result[0])
    finally:
        release_lock(REPORT_LOCK)


# ---- deputy lanes ----
def lane_lock(role: str, project: str | None = None) -> Path:
    return DATA / f"lane-{role}{'' if project is None else '-' + project}.lock"


def lock_units() -> set:
    """Unit keys with a live lane lock: lane-<role>.lock -> role, lane-<role>-<project>.lock -> role:project."""
    return {p.stem.removeprefix("lane-").replace("-", ":", 1) for p in DATA.glob("lane-*.lock") if lock_live(p)}


def review_packet_for(item: str) -> dict | None:
    """review_packet.build for the executor run that produced the [review] row `item` (its latest executor row with this
    exact text); None when that run is not in the ledger or the packet fails: the reviewer then works as before."""
    runs = [r for r in load_runs() if r.get("kind") == "executor" and r.get("item") == item and r.get("project")]
    if not runs:
        return None
    ex = max(runs, key=lambda r: r["_at"])
    start = ex["_at"].astimezone().replace(tzinfo=None) - timedelta(seconds=ex.get("elapsed_s") or 0)
    try:
        return review_packet.build(item, project_cwd(ex["project"]), WORKSPACE / "incore" / "audits", start,
                                   datetime.now(), umba=ex["project"] == "umba", run_file=ex.get("run_file"))
    except Exception as exc:  # a packet is an aid; never let it stop a review
        log("review_packet_failed", repr(exc)[:200], item=item)
        return None


def auto_rework(role: str, section: str, line: str, item: str, reason: str, u: float, base: float) -> None:
    """The packet's hard condition failed: send the row back as a rework todo without an LLM review. The ledger row
    (kind review, model "script", cost 0, verdict rework) keeps the pass rate and the executor's rework count honest."""
    text = item if item.startswith("（返工）") else f"（返工）{item}"
    if not rewrite_line(line, f"- [todo] {text}〔{reason}〕"):
        log("skipped", "backlog line changed before auto rework", u=u, baseline=base, item=item, kind="review")
        return
    append_jsonl(RUNS_FILE, {"at": now_iso(), "role": role, "section": section, "kind": "review", "model": "script",
                             "effort": None, "session": None, "session_id": None, "item": item, "elapsed_s": 0,
                             "exit_code": 0, "ok": True, "cost_usd": 0.0, "input_tokens": None, "cache_read_tokens": None,
                             "cache_creation_tokens": None, "output_tokens": None, "context_tokens": None,
                             "verdict": "rework", "project": project_of(item), "run_file": None, "auto": True})
    note_verdict(role, item, "rework")
    log("auto_rework", reason, u=u, baseline=base, item=item, kind="review", section=section, role=role)


def review_siblings(role: str, section: str, line: str, item: str, packet: dict | None) -> list[dict]:
    """Up to BATCH_MAX - 1 more [review] rows of the section to review in the same session (owner\x2010-25 "6"): another project,
    or the same project on other branches (two rows of a project without branches are not told apart: not batched); a row
    whose packet is hard is left for its own review, which sends it back. No sol run happens here (it would hold the deputy
    lane for up to 600 s per row): a row sol first review applies to (skip_reason None) and has not had its sol run yet is
    left out, for its own lane or the sol-first lane; the others join: not applicable, or sol already ran and left it to the
    deputy (rework, blocked, failed, drawn for the spot check), with its frozen material reused. Never the quant line."""
    if role == "quant":
        return []
    mod = sys.modules[__name__]
    refs_of = lambda p: set(((p or {}).get("facts") or {}).get("refs") or {})
    taken, out = [(project_of(item), refs_of(packet))], []
    for sline, sitem in (parse_backlog(BACKLOG.read_text(encoding="utf-8")).get(section) or {}).get("review_rows", []):
        if len(out) >= BATCH_MAX - 1:
            break
        spacket = review_packet_for(sitem) if sline != line else None
        project, refs = project_of(sitem), refs_of(spacket)
        if (sline == line or project == "quant" or (spacket and spacket["hard"])
                or any(project == p and (not refs or not r or refs & r) for p, r in taken)):
            continue
        ex = shadow_review.executor_for(mod, role, sitem)
        rid = shadow_review.review_id_of(role, section, sitem, ex) if ex else None
        if (ex and shadow_review.skip_reason(mod, role, sitem, spacket, ex) is None and not shadow_review.load_state(mod).get("disabled")
                and not shadow_review.shadow_row(mod, rid)):
            continue  # sol has not seen it yet
        shadow = shadow_review.frozen(mod, rid) if rid else None
        taken.append((project, refs))
        out.append({"line": sline, "item": sitem, "packet": spacket, "shadow": shadow})
    return out


def run_review_batch(role: str, section: str, rows: list[dict]) -> tuple[int, Path, str, int]:
    """One deputy session for several [review] rows: the prompt says so, gives each row's pre-review packet in its own
    numbered part, and asks for one `VERDICT <number>: …` line per row. Ledger rows, per row: run_resident(batch=...).
    note_verdict / note_spot per row with a verdict; a row without one stays [review] for a single review next round.
    Returns (exit_code, run_file, result, elapsed_s)."""
    n, mod = len(rows), sys.modules[__name__]
    sprint = (parse_backlog(BACKLOG.read_text(encoding="utf-8")).get(section) or {}).get("sprint")
    parts = "".join(
        f"\n【第 {i} 条】{r['item']}\n" + (r["packet"]["text"] if r["packet"] else "")
        + dots_review.opinion_text(mod, role, r["item"])
        + (f"本条审查的冻结材料：{r['shadow']['review_snapshot']}。先读它，按其中固定提交号和结果原文验收，不要用后来移动的分支或覆盖的结果替代；"
           "不要读取 shadow_review 的结论。\n" if r["shadow"] else "") for i, r in enumerate(rows, 1))
    prompt = (f"这次一起审 {n} 条（同一条线、互不依赖）。下面按序号分节给出每条的审查前材料；逐条验收，每条各自编辑 backlog.md 里它自己那一行，"
              f"每条的结论各占一行。\n{parts}\n"
              + PROMPTS["review"].format(name=ROLE_NAMES[role], section=section, item=f"上面第 1–{n} 条，逐条做")
              + REVIEW_AS_TRIAL + (SPRINT_REVIEW if any(project_of(r["item"]) == sprint for r in rows) else "")
              + shadow_review.followups(mod, role)
              + f"\n合批的结论格式（覆盖上面「最后一行单独写 VERDICT」那一句）：最后每条单独写一行 `VERDICT <序号>: pass`、`VERDICT <序号>: rework` "
                f"或 `VERDICT <序号>: blocked`，序号是上面的第几条（1–{n}），一条一行，不要写不带序号的 VERDICT 行。某条来不及审，就不写它的结论行，"
                "它留在待审里下一轮单审。")
    code, run_file, ok, result, elapsed = run_resident(
        role, "review", prompt, {"source": "lane", "kind": "review", "text_preview": f"合批 {n} 条：" + rows[0]["item"][:100]},
        {"kind": "review", "section": section}, section=section, batch=[{"item": r["item"], "extra": r["shadow"] or {}} for r in rows])
    verdicts = batch_verdicts(result, n) if ok else {}
    for i, v in verdicts.items():
        note_verdict(role, rows[i - 1]["item"], v)
        if rows[i - 1]["shadow"]:
            shadow_review.note_spot(mod, rows[i - 1]["shadow"]["review_id"], v)
    left = [r["item"][:60] for i, r in enumerate(rows, 1) if i not in verdicts]
    if left:
        log("review_batch_unjudged", f"{len(left)} of {n} without a verdict line, left [review]", role=role, items=left)
    return code, run_file, result, elapsed


def run_lane_work(role: str, section: str, kind: str, line: str | None, item: str | None, u: float, base: float,
                  project: str | None = None, starved_by: str | None = None) -> None:
    effort, running = deputy_effort(role, kind), None
    if kind == "executor":
        running = f"- [running {datetime.now().strftime('%H:%M')}] {item}"
        if not rewrite_line(line, running):
            log("skipped", "backlog line changed before start", u=u, baseline=base, item=item, kind=kind, role=role)
            return
        code, run_file, _, _, elapsed, effort = run_executor(role, section, item, project)
    else:
        if kind == "coordinate":
            delivery_note = ""
            try:
                delivery_note = "\n" + project_delivery.render(project_delivery.write(sys.modules[__name__]))
            except Exception as exc:
                log("project_delivery_failed", repr(exc)[:200])
                delivery_note = "\n本次项目交付核对失败；旧 project-delivery.md 可能过期，不得按 0 或正常处理。"
            try:
                review_stats.write(sys.modules[__name__])
            except Exception as exc:  # the table is an aid; the review still runs without it
                log("review_stats_failed", repr(exc)[:200])
            try:
                kpi.write()
            except (OSError, ValueError) as exc:
                log("kpi_failed", str(exc))
        packet = review_packet_for(item) if kind == "review" else None
        if packet and packet["hard"]:
            auto_rework(role, section, line, item, packet["hard"], u, base)
            return
        prompt = (packet["text"] if packet else "") + PROMPTS[kind].format(name=ROLE_NAMES[role], section=section, item=item)
        if kind == "coordinate":
            prompt += delivery_note
        if kind == "review" and role != "quant":  # owner\x2010-04: quant keeps its review as it is
            prompt += REVIEW_AS_TRIAL
        if kind == "review" and project_of(item or "") == (parse_backlog(BACKLOG.read_text(encoding="utf-8")).get(section)
                                                          or {}).get("sprint"):
            prompt += SPRINT_REVIEW
        if kind == "review":
            prompt += shadow_review.followups(sys.modules[__name__], role)
            prompt += dots_review.opinion_text(sys.modules[__name__], role, item)
        shadow, _ = shadow_review.run(sys.modules[__name__], role, section, item, packet) if kind == "review" else (None, None)
        if shadow and shadow_review.primary(sys.modules[__name__], role, section, line, item, shadow):
            return  # sol passed it: done, no deputy review
        extra = review_siblings(role, section, line, item, packet) if kind == "review" else []
        if shadow or extra:
            if line not in BACKLOG.read_text(encoding="utf-8").splitlines():
                log("shadow_official_deferred", "backlog line changed", item=item, **(shadow or {}))
                return
            # The shadow may have taken minutes. Recheck the existing launch gate before spending Claude usage.
            sampled = fresh_usage()
            u, resets = sampled or (0, 0)
            base = baseline(time.time(), resets)
            block = ("paused" if PAUSE_FILE.exists() else "usage_unknown" if sampled is None
                     else launch_block(u, base, max(len(lock_units()) - 1, 0)))
            if block:
                log("shadow_official_deferred", block, item=item, **(shadow or {}))
                return
            if shadow:
                prompt += (f"\n本次审查的冻结材料：{shadow['review_snapshot']}。先读它，按其中固定提交号和结果原文验收，"
                           "不要用后来移动的分支或覆盖的结果替代；不要读取 shadow_review 的结论。")
            lines = BACKLOG.read_text(encoding="utf-8").splitlines()
            extra = [e for e in extra if e["line"] in lines]  # a row changed meanwhile is left alone
        if extra:
            code, run_file, result, elapsed = run_review_batch(
                role, section, [{"line": line, "item": item, "packet": packet, "shadow": shadow}, *extra])
        else:
            code, run_file, _, result, elapsed = run_resident(
                role, kind, prompt, {"source": "lane", "kind": kind, "text_preview": (item or section)[:120]},
                {"kind": kind, "section": section, **(shadow or {})}, item=item or "", section=section)
            if kind == "review":
                note_verdict(role, item, parse_verdict(result))
                if shadow:
                    shadow_review.note_spot(sys.modules[__name__], shadow["review_id"], parse_verdict(result))
    reason = {-1: "timeout", CEILING_STOP: "ceiling_stop"}.get(code, "")
    after = "todo" if code == CEILING_STOP else "review"  # a ceiling stop is not the work's fault: same line, same session next run
    if running:
        reason = "; ".join(filter(None, (reason, finish_lane_line(section, running, after))))
    log("ran", reason, u=u, baseline=base, item=item, kind=kind, section=section, role=role, project=project,
        model=(executor_model(item) or MODELS[kind]) if kind == "executor" else deputy_model(role, kind),
        effort=effort, run_file=str(run_file),
        exit_code=code, elapsed_s=elapsed, **({"starved": starved_by} if starved_by else {}))


def lane_run(role: str, project: str | None = None) -> int:
    """One unit of work, then exit. With a project: the first todo of that project in the role's section. Without:
    the role's deputy unit (coordinate if due, review, plan); if there is none, the first todo whose executor unit is
    free (this also serves a supervisor that still spawns only `lane <role>`). EXECUTORS_PER_LINE and MAX_PARALLEL
    are enforced here too, counting the live lane locks."""
    if role not in ROLE_NAMES or project not in (None, "general", *PROJECTS):
        return 2
    unit, held = unit_key(role, project), [lane_lock(role, project)]
    if not take_lock(held[0]):
        return 0
    try:
        now, section = datetime.now(timezone.utc), role_section(role)
        runs = load_runs()
        sec = parse_backlog(BACKLOG.read_text(encoding="utf-8")).get(section)
        live = lock_units() - {unit}
        deputy_free = role not in live and not lock_live(deputy_lock(role)) and deputy_ready(role) and fork_source_free(role)
        if project is not None:
            work = next((("executor", project, *t) for t in (sec or {"todo": []})["todo"] if project_of(t[1]) == project),
                        None)
        elif role == "coordinator" and deputy_free and coordinate_due(runs, now):
            work = ("coordinate", None, None, "跨线复盘")
        else:
            work = next_unit(sec, section, role, runs, now, live, deputy_free) if sec else None
        if work is None:
            log("idle", lane=unit)
            return 0
        kind, project, line, item, *rest = work
        starved_by = rest[0] if rest else None
        if kind == "executor" and unit == role:  # fallback: take that executor's unit, give up the role's
            if not take_lock(lane_lock(role, project)):
                log("idle", "executor unit busy", lane=unit)
                return 0
            held.append(lane_lock(role, project))
            release_lock(held.pop(0))
        units = lock_units()
        claude_units = units - codex_units()  # Codex lanes do not use Claude's window (owner\x2011-01: 20 Codex slots)
        if len(claude_units) > MAX_PARALLEL or sum(1 for k in units if k.startswith(f"{role}:")) > EXECUTORS_PER_LINE:
            log("capped", lane=unit, units=sorted(units))
            return 0
        dlock = deputy_lock(role) if kind != "executor" else None  # executors have their own session
        if dlock and not take_lock(dlock):  # the owner/report lane is using the session; the supervisor retries
            log("deputy_busy", lane="lane", role=role, kind=kind)
            return 0
        try:
            sampled = fresh_usage()
            if sampled is None:
                log("usage_unknown", lane=unit)
                return 0
            u = sampled[0]
            base = baseline(time.time(), sampled[1])
            running = max(len(claude_units) - 1, 0)  # `units` includes this lane's own lock
            block = launch_block(u, base, running)
            if block:
                log(block, lane=unit, u=u, baseline=base, running=running)
                return 0
            run_lane_work(role, section, kind, line, item, u, base, project, starved_by)
            return 0
        finally:
            if dlock:
                release_lock(dlock)
    finally:
        for path in held:
            release_lock(path)


# ---- supervisor ----
def spawn(args: list[str]) -> None:
    """Detached child like discord/direct.py:spawn_owner_lane, also leaving the scheduled task's job object (so it
    survives the task's time limit) when the job allows breakaway."""
    kw = dict(cwd=str(WORKSPACE), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True)
    flags = DETACHED_PROCESS | CREATE_NO_WINDOW
    try:
        subprocess.Popen([PYTHONW, str(TICK_PY), *args], creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:  # the job does not allow breakaway
        subprocess.Popen([PYTHONW, str(TICK_PY), *args], creationflags=flags, **kw)


def live_lanes(spawned: dict, t: float) -> set:
    """Live unit keys: a live lane lock, or spawned within SPAWN_GRACE_S."""
    return lock_units() | {k for k, at in spawned.items() if t - at < SPAWN_GRACE_S}


def backup_faults(now: datetime) -> dict[str, str]:
    """Backup problems right now, kind -> text. No model: the watcher PID, status.json errors, last-check age."""
    faults = {}
    try:
        pid = int(json.loads(BACKUP_PROCESS.read_text(encoding="utf-8-sig"))["ProcessId"])  # start-services.ps1 writes a BOM
    except (OSError, ValueError, KeyError, TypeError):
        pid = 0
    if not pid_alive(pid):
        faults["process_down"] = f"备份进程不在了（PID {pid or '未知'}）"
    try:
        status = json.loads(BACKUP_STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = None
    if not isinstance(status, dict):
        faults["status_unreadable"] = "backups/status.json 读不了"
        return faults
    if status.get("staging_error"):
        faults["staging_error"] = f"Drive 暂存失败：{str(status['staging_error'])[:200]}"
    if status.get("stage") == "backup_failed" or status.get("backup_failed"):
        faults["backup_failed"] = f"本地备份失败（{status.get('checked_at')}）：{str(status.get('error', ''))[:200]}"
    ok_at = _when((status.get("arbitros") or {}).get("ok_at"))
    if status.get("arbitros_error") and (ok_at is None or now - ok_at > timedelta(seconds=ARBITROS_RETRY_S)):
        faults["arbitros_error"] = f"Arbitros 官网数据拉取失败：{str(status['arbitros_error'])[:200]}"
    checked = _when(status.get("checked_at"))
    if checked is None or now - checked > timedelta(seconds=BACKUP_STALE_S):
        faults["stale"] = f"备份最近一次检查是 {status.get('checked_at') or '无记录'}，已超过 {BACKUP_STALE_S // 60} 分钟"
    return faults


def backup_watch(st: dict, t: float) -> None:
    """Every BACKUP_CHECK_S: post one Discord line to the coordinator per new fault kind; re-arm once healthy."""
    if t - st.get("backup_at", -1e9) < BACKUP_CHECK_S:
        return
    st["backup_at"] = t
    faults = backup_faults(datetime.now(timezone.utc))
    # tasklist can miss a live watcher PID on the first poll after the scheduler starts.
    down = faults.get("process_down")
    if down is None:
        st.pop("process_down_first", None)
    elif st.get("process_down_first") == down:
        pass
    else:
        st["process_down_first"] = down
        del faults["process_down"]
    # backup_failed stays in status.json until the next round (~68 s) overwrites it, so two checks can see one failure.
    # It alerts only once a second failed round shows (its text carries checked_at); a healthy check starts over.
    failed = faults.get("backup_failed")
    if failed is None:
        st.pop("backup_failed_first", None)
    elif st.setdefault("backup_failed_first", failed) == failed:
        del faults["backup_failed"]
    try:
        sent = set(json.loads(BACKUP_ALERT_FILE.read_text(encoding="utf-8")).get("kinds", []))
    except (OSError, ValueError, AttributeError):
        sent = set()
    new = {k: v for k, v in faults.items() if k not in sent}
    if new and post_discord("coordinator", "备份告警：" + "；".join(new.values()) + "。", hold=False):
        log("backup_alert", kinds=sorted(new))
        sent |= set(new)
    elif new:
        log("backup_alert_unsent", kinds=sorted(new))
    sent &= set(faults)  # a kind that cleared may alert again; healthy -> empty
    BACKUP_ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
    BACKUP_ALERT_FILE.write_text(json.dumps({"kinds": sorted(sent)}), encoding="utf-8")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def website_status(url: str) -> tuple[int | None, str]:
    """(HTTP status or None, error text), redirects not followed."""
    try:
        return urllib.request.build_opener(_NoRedirect).open(url, timeout=WEBSITE_TIMEOUT_S).status, ""
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (OSError, ValueError) as e:  # URLError, timeout, refused
        return None, str(e)[:100]


def website_watch(st: dict, t: float) -> None:
    """Every WEBSITE_CHECK_S: GET WEBSITE_URL; WEBSITE_FAILS non-200 in a row post one Discord line, healthy re-arms."""
    if t - st.get("web_at", -1e9) < WEBSITE_CHECK_S:
        return
    st["web_at"] = t
    code, err = website_status(WEBSITE_URL)
    st["web_fails"] = 0 if code == 200 else st.get("web_fails", 0) + 1
    try:
        sent = bool(json.loads(WEBSITE_ALERT_FILE.read_text(encoding="utf-8")).get("down"))
    except (OSError, ValueError, AttributeError):
        sent = False
    if st["web_fails"] >= WEBSITE_FAILS and not sent:
        sent = post_discord("coordinator", f"官网告警：{WEBSITE_URL} " + (f"返回 {code}" if code else f"连不上（{err}）")
                            + f"，已连续 {st['web_fails']} 次检查不是 200。", hold=False)
        log("website_alert" if sent else "website_alert_unsent", status=code)
    elif st["web_fails"] == 0:
        sent = False
    WEBSITE_ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
    WEBSITE_ALERT_FILE.write_text(json.dumps({"down": sent}), encoding="utf-8")


def main_worktree_guard() -> None:
    """Record each unexpected main-worktree HEAD once; the existing ask lane handles it this loop."""
    branch = review_packet.git(CONTROL.parent, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch or not branch.strip():
        raise OSError("cannot read incore main-worktree HEAD")
    branch = branch.strip()
    if branch == "master":
        return
    if branch == "HEAD":  # detached HEAD also loads unmerged code
        commit = review_packet.git(CONTROL.parent, "rev-parse", "HEAD")
        if not commit:
            raise OSError("cannot read detached incore HEAD")
        branch = "detached@" + commit.strip()
    marker = f"〔主目录 HEAD：{branch}〕"
    with backlog_io.lock(BACKLOG):
        lines = backlog_io._read(BACKLOG)
        rows = backlog_io._section_rows(lines, line_map.ORG)
        if any(marker in lines[i] for i in rows) or any(
                marker in row["raw"] and row["section"] == line_map.ORG
                for row in backlog_history.archived_rows(BACKLOG)):  # includes answered rows; no reset on master
            return
        found = datetime.now().astimezone().isoformat(timespec="seconds")
        row = (f"- [blocked: 等总理] 组织：孵化器主目录不在 master，当前 {branch}，发现时间 {found}；"
               "建议总理核对未提交改动和服务加载版本后恢复 master，执行者不自动切分支或重启。"
               f"依据 audits/2026-11-01-org-batch-review-asr-round2-health.md；{marker}")
        cr = "\r" if any(x.endswith("\r") for x in lines) else ""
        end = rows.stop
        while end > rows.start and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, row + cr)
        backlog_io._write(BACKLOG, lines)
    log("main_worktree_not_master", branch=branch, found_at=found)


def machine_watch_step(st: dict, t: float) -> None:
    """One machine sample per machine_watch.SAMPLE_EVERY_S inside the supervisor loop; a failure never stops scheduling."""
    try:
        machine_watch.step(st.setdefault("machine", {}), t, MACHINE_HEALTH)
    except Exception as exc:
        log("machine_watch_failed", repr(exc)[:200])


def supervise_step(st: dict, t: float) -> None:
    """One supervisor loop: sample usage when due, spawn the report/owner/ask lanes, spawn a deputy lane if allowed."""
    try:
        main_worktree_guard()
    except Exception as exc:  # a failed check must not stop scheduling
        log("main_worktree_guard_failed", repr(exc)[:200])
    backup_watch(st, t)
    website_watch(st, t)
    machine_watch_step(st, t)
    dots_watch.step(BACKLOG, log)
    dots_review.step(sys.modules[__name__])  # owner\x2011-05: first look by dotz at [review] rows; never raises
    try:  # a stuck sprint row goes to the coordinator; an aid, never stops the supervisor
        stuck_alarm(st, t)
    except Exception as exc:
        log("stuck_alarm_failed", repr(exc)[:200])
    try:  # owner cards already answered in a record; once an hour, an aid that never blocks the supervisor
        backlog_io.clear_seen_owner_cards_hourly(t, path=BACKLOG, records_dir=BACKLOG.parent.parent / "records",
                                                  archive=lambda: backlog_archive.daily(BACKLOG))
    except Exception as exc:
        log("owner_cards_clear_failed", repr(exc)[:200])
    fresh = t - st.get("tried", -1e9) >= (SAMPLE_EVERY_S if st["sample"] else SAMPLE_RETRY_S)
    if fresh:
        found = fresh_usage()
        st["tried"] = t
        st["sample"] = (t, found) if found else None  # unknown usage: no spawns until a sample succeeds
        if not found:
            log("usage_unknown")
    seen = latest_usage()  # a lane's own sample or a run's stream may be newer than ours
    if seen and (st["sample"] is None or seen[0] > st["sample"][0]) and t - seen[0] < SAMPLE_EVERY_S:
        st["sample"] = seen
    try:
        text = BACKLOG.read_text(encoding="utf-8")
    except OSError:
        return
    try:
        timed_rows.observe(text, DATA / timed_rows.STATE_FILE, datetime.fromtimestamp(t).astimezone(), log)
    except Exception as exc:  # observation failure must not alter dispatch
        log("timed_rows_failed", repr(exc)[:200])
    if pending_direct_ask_notices() and t - st["aux"].get("ask-notice", -1e9) >= LOOP_S:
        try:
            spawn(["ask-notice"])
            st["aux"]["ask-notice"] = t
        except Exception as exc:  # a notice failure must not stop the scheduler loop
            log("ask_notice_failed", repr(exc)[:200], lane="supervisor")
    ask_due = bool(ask_pending(text, t)) and not lock_live(deputy_lock("coordinator"))  # the coordinator's session is free
    for name, lock, due in (("report", REPORT_LOCK, True), ("owner", OWNER_LOCK, bool(owner_items(text))),
                            ("ask", ASK_LOCK, ask_due)):
        if due and t - st["aux"].get(name, -1e9) >= AUX_EVERY_S and not lock_live(lock):
            spawn([name])
            st["aux"][name] = t
    terminal_open, agents_open = set(), set()
    for r, e in load_deputies().items():
        if r not in ROLE_NAMES or not isinstance(e, dict):
            continue
        # the deputy's own session (deputy lock) and each project agent's (lane lock) can be open in a terminal
        for project, cur in [(None, e.get("current")), *((p, (x or {}).get("current")) for p, x in (e.get("executors") or {}).items())]:
            if not isinstance(cur, dict) or cur.get("kind") != "terminal":
                continue
            (terminal_open if project is None else agents_open).add(r if project is None else unit_key(r, project))
            key = "-".join(["terminal", r, *([project] if project else [])])
            lock = deputy_lock(r) if project is None else lane_lock(r, project)
            if not lock_live(lock) and t - st["aux"].get(key, -1e9) >= AUX_EVERY_S:  # window closed, no `end`
                begun = _when(cur.get("started_at"))
                spawn(["terminal", r, "recover", str(cur.get("since") or (begun.timestamp() - 5 if begun else t)),
                       *([project] if project else [])])
                st["aux"][key] = t
    if st["sample"] is None:
        return
    runs = load_runs()
    u, resets_at = st["sample"][1]
    base = baseline(t, resets_at)
    live = live_lanes(st["spawned"], t) | agents_open  # an agent's terminal session without a live lock awaits recovery
    # Codex lanes hold lane locks too (one executor per project) but not Claude's window: Claude's limits count the rest
    claude_live = live - codex_units() - {k for k, at in st.get("codex_spawned", {}).items() if t - at < SPAWN_GRACE_S}
    if u < HARD_CEILING:
        planned = codex_plan_spawn(st, t, text, live, claude_live, runs)
        if planned:
            live, claude_live = live | {planned}, claude_live | {planned}
    if u >= HARD_CEILING or u > base + PACING_SLACK:
        if fresh:
            log("hard_ceiling" if u >= HARD_CEILING else "pacing", u=u, baseline=base)
        held_fallback(st, t, text, live, runs=runs)
        return
    if len(claude_live) >= MAX_PARALLEL:
        held_fallback(st, t, text, live, codex_parallel_slots(), runs=runs)
        return
    if launch_block(u, base, len(claude_live)) == "reserve":  # hard_ceiling / pacing were handled above
        if fresh:
            log("reserve", u=u, baseline=base, running=len(claude_live))
        held_fallback(st, t, text, live, runs=runs)
        return
    deputy_busy = {r for r in ROLE_NAMES if r not in live and (r in terminal_open or lock_live(deputy_lock(r)))}
    deputy_busy |= {r for r in ROLE_NAMES if not deputy_ready(r) or not fork_source_free(r)}  # a forked role not yet forked: its executors run, its reviews and plans wait
    note: dict = {}
    unit = choose_lane(text, runs, datetime.now(timezone.utc), live, deputy_busy, note)
    if unit:
        key = unit_key(*unit)
        spawn(["lane", *([unit[0]] if unit[1] is None else unit)])
        st["spawned"][key] = t
        log("spawn", lane=key, u=u, baseline=base, **({"starved": note["starved"]} if note.get("starved") else {}),
            **({"cross_starved": note["cross_starved"]} if note.get("cross_starved") else {}), **({"sprint_first": note["sprint_first"]} if note.get("sprint_first") else {}))
        live = live | {key}
    codex_fallback(st, t, text, live, codex_parallel_slots(), runs=runs)


def supervise() -> int:
    if not take_lock():
        log("busy")
        return 0
    try:
        started, st = time.time(), {"sample": None, "spawned": {}, "aux": {}}
        try:
            commit = review_packet.git(Path(__file__).resolve().parent.parent, "rev-parse", "master")
            record = {
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "master_commit": (commit or "").strip() or None,
                "tick_sha256": hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest(),
            }
            SUPERVISOR_STATE.parent.mkdir(parents=True, exist_ok=True)
            temporary = SUPERVISOR_STATE.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(temporary, SUPERVISOR_STATE)
        except Exception as exc:  # status reporting is best-effort; it must not stop scheduling
            try:
                log("supervisor_start_record_failed", repr(exc)[:200])
            except Exception:
                pass
        try:
            recover_running()
        except Exception as exc:  # an aid; the supervisor runs without it
            log("running_recover_failed", repr(exc)[:200])
        while True:
            if PAUSE_FILE.exists():
                log("paused")
                return 0
            supervise_step(st, time.time())
            if time.time() - started >= SUPERVISE_MAX_S:
                return 0
            time.sleep(LOOP_S)
    finally:
        release_lock()


# ---- owner terminal conversations (control/talk.ps1 holds the deputy lock around both calls) ----
TERMINAL_PREVIEW = "owner 在终端对话"


def transcript_stats(path: Path, since: float) -> dict | None:
    """Tokens, cost, model, effort, last context and last reply of the transcript entries written after `since`
    (epoch s); None if the owner's session produced no reply. Each message.id counts once (one entry is written per
    content block, all with the same usage). Cost: last cost-state total in the file minus the last one before the
    first in-window reply (cost-state has no timestamp; its startTime is the session start), None if none after."""
    usage = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}
    last, model, effort, reply, turns = {}, None, None, "", 0
    before, after, seen = 0.0, None, set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("type") == "cost-state" and e.get("totalCostUSD") is not None:
            if turns:
                after = e["totalCostUSD"]
            else:
                before = e["totalCostUSD"]
        if e.get("type") == "system" and e.get("subtype") == "compact_boundary":
            last = {}  # compacted after the last reply: the live context is small again
            continue
        when = _when(e.get("timestamp"))
        if e.get("type") != "assistant" or not when or when.timestamp() < since:
            continue
        msg = e.get("message") or {}
        u = msg.get("usage") or {}
        mid = msg.get("id")
        if not mid or mid not in seen:
            for k in usage:
                usage[k] += int(u.get(k) or 0)
        seen.add(mid)
        last, model, effort, turns = u, msg.get("model") or model, e.get("effort") or effort, turns + 1
        text = "".join(c.get("text", "") for c in msg.get("content") or [] if isinstance(c, dict) and c.get("type") == "text")
        reply = text or reply
    if not turns:
        return None
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {"cost_usd": None if after is None else round(max(after - before, 0.0), 6),
            "input_tokens": usage["input_tokens"], "cache_read_tokens": usage["cache_read_input_tokens"],
            "cache_creation_tokens": usage["cache_creation_input_tokens"], "output_tokens": usage["output_tokens"],
            "context_tokens": sum(int(last.get(k) or 0) for k in keys), "model": model, "effort": effort, "reply": reply}


def terminal(role: str, phase: str, since: str = "0", project: str | None = None) -> int:
    """talk.ps1 calls `begin <epoch>` (dashboard shows the deputy busy; /compact first if the session is already
    past COMPACT_TOKENS) and `end <epoch>`, both while it holds the deputy lock. The supervisor calls
    `recover <epoch>` when the window was closed without `end` (the lock holder is gone): same as end, under the
    deputy lock. With a `project`: the same for that project's resident agent (deputies.json
    <role>.executors.<project>, run in the project's cwd) under its lane lock, compacting at EXECUTOR_COMPACT_TOKENS."""
    if role not in ROLE_NAMES or phase not in ("begin", "end", "recover") or project not in (None, "general", *PROJECTS):
        return 2
    started = float(since)
    sid = ((load_deputies().get(role) or {}) if project is None else executor_entry(role, project)).get("session_id")
    path = session_path(sid)
    lock = deputy_lock(role) if project is None else lane_lock(role, project)
    if phase == "begin":
        current = {"kind": "terminal", "text_preview": TERMINAL_PREVIEW, "started_at": now_iso(), "since": started}
        if project is None:
            update_deputy(role, status="running", current=current)
        else:
            update_deputy(role, executors={project: {"current": current}})
        stats = transcript_stats(path, 0) if sid and path.exists() else None
        if stats:
            maybe_compact(role, {"context_tokens": stats["context_tokens"], "session_id": sid,
                                 "section": role_section(role)},
                          deputy_model(role, "owner") if project is None else MODELS["executor"],
                          f"compact-{role}" if project is None else f"compact-executor-{role}-{project}", project)
        return 0
    if phase == "recover" and not take_lock(lock):
        return 0
    try:
        return record_terminal(role, sid, path, started, project)
    finally:
        if phase == "recover":
            release_lock(lock)


def record_terminal(role: str, sid: str | None, path: Path, started: float, project: str | None = None) -> int:
    """Ledger row (kind 'terminal'), deputies.json last/runs/context, compaction if the session grew past
    COMPACT_TOKENS (an agent's: EXECUTOR_COMPACT_TOKENS, row carries its project)."""
    stats = transcript_stats(path, started) if sid and path.exists() else None
    if stats is None:  # opened and closed without a reply: nothing to record
        if project is None:
            update_deputy(role, status="idle", current=None)
        else:
            update_deputy(role, executors={project: {"current": None}})
        return 0
    elapsed = round(time.time() - started)
    reply = stats.pop("reply")
    row = {"at": now_iso(), "role": role, "section": role_section(role), "kind": "terminal",
           "model": stats.pop("model"), "effort": stats.pop("effort"), "session": "resume", "session_id": sid,
           "item": "", "elapsed_s": elapsed, "exit_code": None, "ok": True, **stats, "verdict": None,
           "project": project, "run_file": str(path)}
    append_jsonl(RUNS_FILE, row)
    log("terminal", role=role, project=project, elapsed_s=elapsed, cost_usd=row["cost_usd"],
        context_tokens=row["context_tokens"])
    last = {"kind": "terminal", "finished_at": now_iso(), "ok": True, "elapsed_s": elapsed, "summary": reply.strip()[:240]}
    if project is None:
        context = maybe_compact(role, row, deputy_model(role, "owner"), f"compact-{role}")
        runs = int(load_deputies().get(role, {}).get("runs") or 0) + 1
        update_deputy(role, status="idle", current=None, runs=runs, context_tokens=context, last=last)
    else:
        context = maybe_compact(role, row, MODELS["executor"], f"compact-executor-{role}-{project}", project)
        runs = int(executor_entry(role, project).get("runs") or 0) + 1
        update_deputy(role, executors={project: {"current": None, "runs": runs, "context_tokens": context, "last": last}})
    return 0


def launch_info(role: str, project: str) -> int:
    """`launch <role> <project>`: JSON of how the scheduler starts that agent (cwd, --add-dir folders, model), for
    talk.ps1."""
    if role not in ROLE_NAMES or project not in ("general", *PROJECTS):
        return 2
    launch = executor_launch(project)
    print(json.dumps({"cwd": str(launch["cwd"]), "add_dirs": [str(d) for d in launch["add_dirs"]],
                      "model": MODELS["executor"]}))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["launch"] and len(argv) == 3:
        return launch_info(*argv[1:3])
    if argv[:1] == ["terminal"] and len(argv) >= 3:  # bookkeeping works even while paused
        return terminal(*argv[1:5])
    if argv in (["owner"], ["report"], ["ask"], ["ask-notice"]) or argv[:1] == ["lane"] or (argv[:1] in (["codex"], ["sol-first"]) and len(argv) == 3) \
            or (argv[:1] == ["codex-plan"] and len(argv) == 2):
        if PAUSE_FILE.exists():
            log("paused")
            return 0
        if argv == ["owner"]:
            return owner_lane()
        if argv == ["ask"]:
            return ask_lane()
        if argv == ["ask-notice"]:
            try:
                send_pending_ask_notices()
            except Exception as exc:
                log("ask_notice_failed", repr(exc)[:200], lane="sender")
            return 0
        if argv[0] == "codex-plan":
            return codex_plan_lane(argv[1])
        if argv[0] == "codex":
            return codex_lane(*argv[1:3])
        if argv[0] == "sol-first":
            return sol_first_lane(*argv[1:3])
        return report_lane() if argv == ["report"] else lane_run(*(argv[1:3] or [""]))
    return supervise()


if __name__ == "__main__":
    raise SystemExit(main())
