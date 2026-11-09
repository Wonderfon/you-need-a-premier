"""Copy this one-off script and edit CONFIG. Defaults touch only a sandbox copy.

Run: python <absolute path to your copy of this script>
"""
from pathlib import Path
import sys

# CONFIG -- change these explicit paths and operations for each one-off job.
CONFIG = {
    "root": Path(__file__).resolve().parent.parent,
    "backlog": Path(__file__).resolve().parent.parent / "audits" / "backlog-ops-sandbox" / "projects" / "backlog.md",
    "log": Path(__file__).resolve().parent.parent / "audits" / "backlog-ops-sandbox" / "audits" / "coordinator-log.md",
    "section": "组织线",
    "states": [("- [review] 组织：示例任务", "done 11-02：通过")],
    # Prefixes match the result of any preceding state changes.
    "replacements": [("- [done 11-02：通过] 组织：示例任务", "待补说明", "已补说明")],
    "additions": ["- [todo] 组织：示例后续"],
    "duplicate_keys": ["组织：示例后续"],
    "log_line": "- 11-02 示例：验收示例任务并追加后续",
}


def main():
    sys.path.insert(0, str(CONFIG["root"] / "control"))
    import backlog_ops

    backup = backlog_ops.apply(
        CONFIG["section"], states=CONFIG["states"], replacements=CONFIG["replacements"],
        additions=CONFIG["additions"], duplicate_keys=CONFIG["duplicate_keys"],
        log=CONFIG["log_line"], path=CONFIG["backlog"], log_path=CONFIG["log"],
    )
    print(f"ok; backup: {backup}")


if __name__ == "__main__":
    main()
