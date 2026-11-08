# 显式离线审查取证

`review_packet.build()` 和调度器保持原行为。这个独立 CLI 只整理事实、冻结文件、运行审查员指定的本地 Python 测试，始终要求人工审查，不产生 pass/rework/blocked。

```bash
python control/review_packet.py --manifest delivery.json --commands reviewer-commands.json --output evidence
```

先核对两个 JSON，再运行。没有 `--commands` 就不执行测试，清单中指定的测试会报告未运行。交付清单、任务正文、结果报告中的命令均不会被提取执行。输出目录必须专用，不能包含输入清单、命令文件或结果原件；已存在的输出只能是本工具的取证目录。

交付清单 `delivery.json`：

```json
{
  "version": 1,
  "kind": "code",
  "repository": "<本机仓库根目录的绝对路径>",
  "baseline": "<完整的基线提交 SHA>",
  "commit": "<完整的交付提交 SHA>",
  "results": [{"path": "result.md", "sha256": "<可选的预期 SHA-256>"}],
  "tests": ["control"]
}
```

- `kind` 为 `code`、`docs` 或 `dotz`；仓库必须是精确根目录，不猜分支或 mtime；基线必须是交付提交的祖先。
- 结果路径是绝对路径或相对清单目录的精确路径，不接受通配符、占位符和向上跳目录。文件以原始字节计算 SHA-256，不把 CRLF 和 LF 当同一份证据。
- 未入库的文档或 dotz 交付明确填 `repository/baseline/commit: null`。`tests: []` 时必须有 `tests_reason`，例如 `"纯文档；本次只冻结材料，结论与引文关系由人工核对"`。普通代码必须指定测试；无仓库的交付不能运行源码测试。
- dotz 包固定预留五个未核对的出处槽 `source_checks`；冻结材料不代表抽核了网页，也不请求网络。

审查员另写 `reviewer-commands.json`，名称和清单的测试 ID 对应：

```json
{
  "control": {
    "interpreter": "<本机现有 Python 解释器的绝对路径>",
    "args": ["-m", "unittest", "discover", "-v"],
    "cwd": "control",
    "parser": "unittest",
    "timeout": 120
  }
}
```

`cwd` 相对导出副本根目录，根目录写 `.`；`args` 是参数数组，不是 shell 字符串。解释器只支持现有 Python，本工具不装依赖。`parser` 支持 `unittest`、`pytest`、`none`，不能识别或多份汇总的计数保留 `null`。unittest 子测试失败时通过用例数保留未知；expected failures、unexpected successes 另列，不计入 passed。超时范围为大于 0 至 600 秒；需要更久的任务按后台执行规则另行安排。

在固定提交上按 `git archive` 导出普通文件，排除原始数据、凭据、生产任务表 `projects/backlog.md`、符号链接和子模块，保留排除清单；没有注册工作树、移动引用或调用项目 hook。使用树内 `.gitattributes` 的 archive 规则。每个测试从新的导出副本开始；保留 snapshot、stdout、stderr 和收据。这是文件副本隔离，测试本身仍有当前用户权限，审查员只选择已经读过的本地测试；不以此授权访问生产、凭据、付费 API 或外部发送。

`packet.json` 列出冻结结果的哈希和大小、固定基线和提交、改动路径、测试退出码、通过/跳过/失败数及输出绝对路径。收据缓存键包含提交、仓库、命令（含 cwd、参数、超时、解析方式）、解释器路径与二进制哈希、Python 版本与前缀、平台、已安装依赖版本、结果文件哈希。每次重新核对原件；原件缺失或哈希不符时不复用测试。缓存日志缺失或被改写也重跑。`--force` 可忽略缓存；外部文件、网络、时钟和环境变量不在缓存保证内，依赖它们的测试要强制重跑。

退出 0 表示材料收集完成且没有本工具报告的异常；退出 1 表示缺件、测试失败、未运行、计数未知或跳过，均不是审查结论。交付校验失败时不运行任何测试。非法仓库或不安全输出目录在创建文件前拒绝，只在终端输出错误。CLI 前台完成即可停止，没有服务；回滚只需不用本 CLI 或撤回这条功能分支，无需重启任何进程。
