# 你需要一个总理 (You Need a Premier)

[English](README.md) | 简体中文

让 AI 的执行力接住你的创造力。和 AI 做一次意图对齐，总理会让你专属的 AI 组织自动运转你的项目。

## 使用场景

当你的想法和项目多过你自己能执行的量，并且你在和 AI agent 一起工作时，可以用这个项目。

1. **长期自动运行。** 做一次意图对齐之后，它可以长期自动运行。
2. **接住你的新 idea。** 它可以随时接住并执行你的新 idea，同时合理调度资源。
3. **关键节点主动汇报。** 在任务推进的关键节点，AI 会主动向 Owner（你）汇报并寻求反馈。
4. **数据不浪费。** Owner 的原话、提供的信息和产生的私有数据都会存档，这些宝贵的数据不会被浪费。

## 组织结构

![组织结构图](docs/org-structure.svg)

- **最上层：** Owner（你）、Premier，以及资源池和 Pacer。Premier 和 Owner 对齐意图，管理资源池和 Pacer，同时有自己的 Executor 团队。
- Premier 可以跟 Deputy 沟通，也可以通过管理 to-do list、调度任务和分配资源，协调各条线上的工作。
- 每个 Deputy 都管理自己的 Executor 团队，负责审核交付物和汇报，可以跟 Premier 沟通，也可以直接向 Owner 汇报。
- 一般情况下，Executor 不直接向 Owner 汇报。交付物要先经过 Deputy 或 Premier 审核，通过后才会报给 Owner。
- 不过，Owner 可以随时打开 Executor 的 session，直接跟它对话。这个渠道由 Owner 主动发起（图中虚线）。

## 关键设计

每个设计都对应一个人和 AI 一起工作时遇到的瓶颈。

| 设计 | 作用 | 对应的瓶颈 |
|---|---|---|
| **Premier**（总理） | 负责 resource coordination 和 intent alignment | planning 和 resource management |
| **Deputy**（副总理） | review deliverables，并向 Owner（你）汇报 | review：人没有足够的时间和精力去审核所有产出 |
| **Pacer**（调度器） | 根据一段时间内的 token 使用情况启动任务，自动加快或放慢节奏 | pace adjustment：人自己用 AI 时要时刻关注还剩多少额度，再调整使用速度，这也是一种 mental burden |
| **Executor**（执行任务的 agent） | 一个 agent 对应一个 session，每个项目有一个专门的 agent | context 和 memory 的管理与隔离 |
| **组织看板** | 看整个组织的入口 | 找信息入口：在复杂的系统里，积累了很多信息和文件之后，光是找到从哪里看、去哪里找，就会很费劲 |
| **语音输入台** | 用规则和小模型做后处理和润色，也会适应个人的语言习惯和口音。基于开源模型，见下 | 输入指令，尤其是长指令 |
| **History Keeper** | 记录 Owner 的所有输入，以及 Owner 产生的信息 | 这些数据很有价值，以后可以用来做 personal benchmark，也可以用来训练 personal models |

### Pacer

Pacer 的设计很重要的一点是，它给人留出了随时介入的空间。系统会按时间节奏消耗额度，留出一部分给人用，避免一有额度就全部烧完。即使快到额度窗口结束时，剩余额度已经很少，如果不够用，人也只需要稍微等一下，窗口一重置就又有额度了。

![使用量与节奏线](docs/images/pace-line.png)

*一周的用量（虚构数据）。直线是 Pacer 允许自己走的节奏。用量跑到线前面时工作暂停，留给你的额度不会被动用。*

### 语音输入台：基于开源模型

语音输入台在本地运行开源模型。语音转文字：[Fun-ASR-Nano-2512](https://huggingface.co/FunAudioLLM/Fun-ASR-Nano-2512)（来自 [FunASR](https://github.com/modelscope/FunASR)），使用 GGUF 格式的 Q5_K_M 量化版本。后处理和润色：[Qwen3-0.6B](https://huggingface.co/Qwen/Qwen3-0.6B)，由本地的 [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` 提供服务。感谢这些项目。

## 可选模块

| 模块 | 用途 |
|---|---|
| **Discord** | 跨平台的沟通渠道。如果你想在手机上也能跟系统协作，就可以配置这个模块，也可以换成 Slack 或 Teams。 |
| **Email** | 用途类似。 |
| **Tailscale** | 用于跨设备连接。比如你想在另一台电脑上看组织看板，就可以像搭个人 VPN 一样，把自己的几台设备连起来。 |
| **Colab CLI** | 解决 GPU access 的问题。如果本地电脑的 GPU 不够，一些轻量任务可以用 Colab 的 GPU 跑。 |
| **Google Drive** | 可以用来备份，也可以在不同设备之间传输文件。 |

## 推荐模型配置（不是必须）

| 角色 | 模型 |
|---|---|
| Premier 和 Deputy | Opus 5.5 High |
| Executor | Sonnet 5.5 High |

## 简单教程

1. 先配置好你需要的模块。
2. 跟 Premier 做一次 intent alignment。本仓库的入口是 `python bootstrap.py`，它在官方 Claude Code CLI 里开始 Premier 的访谈（Python 3.11+，只用标准库，见[引导设置](docs/guided-setup.md)）。
3. 再把已有的文件放进去。
4. 接下来，系统就可以自动开始工作了。

## 当前状态

本仓库是 v0.1。目前已有：引导式意图对齐设置、纯文本任务表、独立的审核步骤、Pacer 的用量闸门和离线演练。组织看板、语音输入台、History Keeper、可选模块的接入，以及在新电脑上从备份恢复整个组织，是上文描述的设计，还不在这个包里。

更多：[离线演示](docs/offline-demo.md) · [真实 worker](docs/real-workers.md) · [文件地图](docs/layout.md) · MIT 许可证
