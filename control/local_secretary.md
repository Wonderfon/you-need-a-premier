# 本地总理秘书 V0.2

最后备用的只读问答：宿主挑材料，Qwen 选取原文证据，校验后生成标明「未验收」的新草稿。没有调度接入、自动派活、发送消息或生产部署。每次启动只处理一个问题，结束即退出自己的推理子进程。

在 PowerShell 启动（前台，约数分钟，默认 4B CPU）：

```powershell
wsl -e python3 /srv/orghome/wt/local-secretary-v0/control/local_secretary.py ask --workspace /srv/orghome/orgcore_workspace/incore --model-dir /srv/orghome/models --question '现在各线进展'
```

另一个终端停止（只向秘书运行目录写 STOP，由秘书停止它自己的子进程；不按 PID 搜索或杀其他服务）：

```powershell
wsl -e python3 /srv/orghome/wt/local-secretary-v0/control/local_secretary.py stop --workspace /srv/orghome/orgcore_workspace/incore
```

Windows 外层命令本轮没人执行；只在 WSL 跑过，Windows 未跑。

成功后终端打印完整草稿和它的路径。草稿存入主目录 `local-secretary-drafts/run-<UTC时间>-<随机后缀>/draft.md`。每个运行目录另存材料快照 `packet.json`、原始模型 JSON `model-response.json`、引擎日志、启动元数据和结束结果 `terminal.json`。现有文件不覆盖。失败只留证据，不生成可采纳草稿。停止期间也不自动回落重试或重新启动。

## 读取和写入边界

- 自动读取固定 `projects/backlog.md`，识别 Arbitros、Beacon Listener、量化、情报、生活、组织线，也兼容旧快照的工作线。概况问题优先取 running 和今天/昨天的 done；没有时取 review、todo 或旧 done。再按问题匹配最多两份日期命名的 audits Markdown，不扫描任意目录内容。
- 草稿最上面是「要你做的（宿主统计）」：全部等你决定条数，以及全部 running 的任务行首与行号。下面的概况表把 blocked 分成「等你决定」和「其他 blocked」，再逐线列冲刺、最近两条 done 和每条待决定的行首（到第一个「（」或 60 字，附快照行号；每线最多 5 条，多的写「另有 N 条」）。统计与模型引用共用同一次 backlog 读取及 SHA-256，模型不能修改统计。done-ref 不算新完成。done 只记时刻的，不猜日期；同日按表内顺序取后两条。未记日期的 done 合并成页底一行。backlog 没有挂起日期，不能确定哪项等得最久，按表内顺序列出，不拿任务内的历史日期当挂起时间。
- 受信操作人可用 `--source audits/<文件>.md` 或 `--source records/<文件>.json` 选择最多三份补充材料。records 沿用本次任务明确授权，默认不读取；选型草案曾排除 records，以本任务为准。模型只能报告材料不足，不能自行指定下一次读取路径。
- 只接受白名单目录下的一层 `.md/.json`，拒绝绝对路径、盘符、反斜杠、`..`、凭据命名、软链接、硬链接、Windows reparse point/junction。默认不读录音、input/data、discord/data 或任意 URL。
- 输入台活动库仅由受信资源管理器按 gpu_yield 的既有只读方式检查，绝不作为模型问答材料。CPU 路径不调用资源管理器、不读该库。
- 唯一写口是所选 workspace 下 `local-secretary-drafts/`。模型没有文件名字段；文件名和路径全部由宿主生成，写入使用独占创建。运行元数据、停止信号也在这个目录。
- 模型只回固定 JSON：`conclusions` 是 source_id、真实行号和逐字引文；`next_step`、`missing_evidence` 使用固定枚举。宿主逐字核对本次快照，拒绝额外字段、越界行号、虚假引文、重复 JSON 字段和工具调用。概况语法固定每线选 1 条（在每线 1–3 条的范围内，宿主校验最多 3 条），有 running 时语法只提供该线的 running 引文。材料另附宿主提取的 status/date。任务引用取行首之后、第一个「（」内开始的连续正文，最多 120 字；只有标题、没有「（」正文的行不作为模型引用候选，仍参与宿主统计。模型仅选哪条，不改写正文，不把标题当正文补位。补充 audits/records 的摘录方式不变。
- 一次推理，没有工具、MCP、shell、URL 抓取、模型输出执行循环。llama-server 的 agent、内置工具、Web UI 和 MCP proxy 均不启用；仅绑定 127.0.0.1 临时端口，临时认证值只在内存，不读凭据文件。推理子进程不继承 cloud、proxy、模型下载或工具相关环境配置。

这是可信宿主的应用边界，不是 OS 级的恶意二进制沙箱。假定 Python、已安装 llama.cpp 和模型文件可信，工作目录不被另一个恶意本机进程同时换链接。引用校验也不能证明模型选对了材料、材料里的说法为真，或所有事实足够新鲜；恢复的总理仍须审读。

## 模型和让路

不下载模型、不改环境。使用现成 WSL llama.cpp b11815：`~/tools/llama.cpp-b11815/llama-b11815/llama-server`。若不在这里，由受信操作人用 `--engine <绝对路径>` 指定。模型目录缺文件时直接失败。

默认 Qwen3.5-4B Q4_K_M，显式 `--device none --gpu-layers 0 --no-kv-offload --no-op-offload`，8 个 CPU 线程，不把 GPU 不可用误当作可运行。

把启动命令加上 `--mode gpu` 才请求 Qwen3.6-35B-A3B UD-Q4_K_M；启动前必须通过现有 `gpu_yield.can_run` 且空闲显存至少 6500 MiB。读不到 GPU 就不起。启动后按 `gpu_yield.POLL_SECONDS`（30 秒）复查，输入台忙、显存不足或 GPU 不可读时停止自己的子进程；不改现有阈值、窗口或活动判定。一次查询本身最多 10 秒，退出先等最多 3 秒，再只强制结束自己仍未退出的子进程。实际让路最长时间还受查询与进程退出耗时影响，不能宣称瞬时让路。

**ASR 在显存里时，当前配置的 35B 不起，只用默认 4B CPU。** 10-27 总理在 PowerShell 用 `wsl -e python3 … --mode gpu` 实测：`nvidia-smi` 位于 `/usr/lib/wsl/lib/nvidia-smi`，RTX 5070 Laptop 总量 8151 MiB、占用 3055 MiB、空闲约 5096 MiB；真正拒启原因是 `insufficient free VRAM`，不是 Windows 读不到 GPU。10-28 Codex WSL 沙箱仍收到 OS 的 `GPU access blocked`，仅代表这个执行环境，不代表 owner 命令也失败。

不凭历史数字下调启动线。10-23 的 35B（n-cpu-moe 38、16k）同期整卡加载差额为 6046−2376=3670 MiB，全程峰值相对加载前的差额为 6168−2376=3792 MiB；这些包含共存负载，不是隔离进程显存。即便只借用后一个差额估算，再保留现有 1536 MiB 守线，也已是 5328 MiB，高于 5096 MiB。该历史配置不能等同于现在的 8k、q8_0 KV、较小 batch，不能据此判定物理上一定放不下。10-04 的 100k/q8_0 低占用还用了 n-cpu-moe 39，也不能移作 38 的成绩。没有本配置的真实 GPU 峰值和让路实测前，维持 6500 MiB，不改 gpu_yield、不停止 ASR 腾显存。

CPU 路径也会占 CPU 与内存；本轮没有运行生产 ASR 共存测试。

## 测试与回滚

```bash
python3 -m unittest discover -s control -p 'test_local_secretary.py' -v
python3 -m unittest discover -s control -p 'test_gpu_yield.py' -q
```

测试全部用临时 workspace；在 Codex WSL 下设置 `TMPDIR=/srv/wslhome/.cache/codex-sandbox/tmp`，不使用 /tmp。V0.2 同快照新旧问答和平台缺口见 `audits/2026-10-29-local-secretary-v02.md`。

回滚：先执行上面的停止命令，本分支不合并、不接调度即可停用。没有改主目录的代码或任何生产服务，无需重启它们。

推理接口依据：[llama.cpp b11815 官方 server 文档](https://github.com/ggml-org/llama.cpp/blob/7170cad4d02f7907517613931840cbb2367399a3/tools/server/README.md)。
