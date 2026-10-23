"""python -m orgcore check <instance-dir>    validate configuration and the task board of an instance."""
from __future__ import annotations

import argparse
import sys

from . import backlog, config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orgcore")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="validate an instance directory")
    check.add_argument("instance", nargs="?", help=f"instance directory (default: ${config.INSTANCE_ENV})")
    args = parser.parse_args(argv)
    try:
        instance = config.load_instance(args.instance)
        board = backlog.read(instance.backlog_path)
        board.check_against(instance.line_projects())
    except (config.ConfigError, backlog.ParseError) as exc:
        print(f"FAIL\n{exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"FAIL\nmissing file: {exc.filename}", file=sys.stderr)
        return 1
    print(f"OK: {len(instance.org['lines'])} line(s), {len(board.tasks)} task(s) in {instance.root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
