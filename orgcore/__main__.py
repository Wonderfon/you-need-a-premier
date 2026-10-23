"""python -m orgcore <command> <instance-dir>

check      validate configuration, task board and their agreement; every problem is listed
rehearse   offline run of the demo rows with fake workers (zero model requests), ending with one owner rating
feedback   summary of the owner's ratings: latest per row, finished rows not yet rated, rows rated "rework"
report     write the local report that is due now (reports/<date>-<HHMM>.md), at most one, never twice for a slot
recover    put abandoned `running` rows back to todo (rows whose worker is still alive are left alone)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta

from . import backlog, check, config, feedback, rehearsal, report, workflow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orgcore")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "rehearse", "recover", "feedback", "report"):
        command = sub.add_parser(name)
        command.add_argument("instance", nargs="?", help=f"instance directory (default: ${config.INSTANCE_ENV})")
    sub.choices["rehearse"].add_argument("--rating", required=True, choices=["useful", "not-useful", "rework"],
                                         help="the owner's verdict to record on each finished demo row")
    sub.choices["rehearse"].add_argument("--tasks", type=int, default=1, help="demo rows to take to done and rate")
    sub.choices["rehearse"].add_argument("--crash-after", choices=rehearsal.STEPS,
                                         help="testing aid: kill this process abruptly after the first such step")
    sub.choices["recover"].add_argument("--max-age-minutes", type=int, required=True)
    args = parser.parse_args(argv)

    errors, instance = check.check_instance(args.instance)
    if errors:
        print("FAIL", *errors, sep="\n", file=sys.stderr)
        return 1
    if args.command == "check":
        board = backlog.read(instance.backlog_path)
        print(f"OK: {len(instance.org['lines'])} line(s), {len(board.tasks)} task(s) in {instance.root}")
    elif args.command == "rehearse":
        def after_step(step: str) -> None:
            if step == args.crash_after:
                os._exit(75)  # no cleanup on purpose

        # each line is flushed as it happens: a crash right after a step must not swallow the lines before it
        rehearsal.rehearse(instance, args.rating.replace("-", "_"), args.tasks, after_step=after_step,
                           emit=lambda line: print(line, flush=True))
    elif args.command == "feedback":
        print("\n".join(feedback.describe(feedback.summarize(instance))))
    elif args.command == "report":
        try:
            written = report.run(instance)
        except report.ReportError as exc:
            print(f"FAIL\n{exc}", file=sys.stderr)
            return 1
        print("\n".join(f"Wrote {path}" for path in written) or "No report is due.")
    else:
        reset = backlog.recover_running(instance.backlog_path, timedelta(minutes=args.max_age_minutes),
                                        alive=workflow.worker_alive(instance.root))
        print(f"recovered {len(reset)} row(s)")
        for task in reset:
            print(f"  {task.section}/{task.project}: {task.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
