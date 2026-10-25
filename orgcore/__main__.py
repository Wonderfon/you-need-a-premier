"""python -m orgcore <command> <instance-dir>

check      validate configuration, task board and their agreement; every problem is listed
rehearse   offline run of the demo rows with fake workers (zero model requests); results first, then `rate` (or --rating)
rate       record the owner's verdict on the finished demo rows of a rehearsal
feedback   summary of the owner's ratings: latest per row, finished rows not yet rated, rows rated "rework"
report     write the local report that is due now (reports/<date>-<HHMM>.md), at most one, never twice for a slot
recover    put abandoned `running` rows back to todo (rows whose worker is still alive are left alone)
usage      show the usage readings in state/usage.json and whether the dispatch gate is open
usage-capture  status-line hook for the official CLI: reads its status JSON on stdin and records the usage windows
enable-live  show what real workers would get, then (with --yes) write a consent for one project and one task
disable-live withdraw that consent (the offline demo result stays)
run        one real unit: executor session, then a separate reviewer session, then record the usage it consumed
setup      guided-setup writer: setup <instance-dir> start|status|show|save|accept|compose (see docs/guided-setup.md)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from . import adapter, backlog, check, config, doctor, feedback, live, rehearsal, report, statusline, usage, workflow
from . import setup as guided

RATINGS = ["useful", "not-useful", "rework"]


def _setup_command(args) -> int:
    root = config.resolve_instance(args.instance) if args.action != "start" else Path(args.instance).expanduser().resolve()
    try:
        if args.action == "start":
            guided.start(root)
            print("\n".join(guided.describe(root)))
        elif args.action == "status":
            print("\n".join(guided.describe(root)))
        elif args.action == "show":
            print(guided.show(root, args.block))
        elif args.action == "compose":
            written = guided.compose(root)
            print("Wrote: " + ", ".join(written) if written else "Nothing to write: every file already matches the saved answers.")
        elif args.action in ("save", "accept"):
            if not args.block:
                raise guided.SetupError([f"{args.action} needs a block name"])
            if args.action == "accept":
                print(guided.accept(root, args.block))
            else:
                if args.file:
                    text = Path(args.file).read_text(encoding="utf-8")
                elif args.json == "-":
                    text = sys.stdin.read()
                elif args.json:
                    text = args.json
                else:
                    raise guided.SetupError(["save needs the answer: --json '<object>', --json - (stdin) or --file <path>"])
                try:
                    answer = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise guided.SetupError([f"the answer is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"])
                print(guided.save(root, args.block, answer, change=args.change))
    except guided.SetupError as exc:
        print("FAIL", *exc.errors, sep="\n", file=sys.stderr)
        return 1
    return 0


def _live_readiness(root: Path) -> list[str]:
    claude = doctor.find_claude()
    if not claude:
        return ["the Claude Code CLI was not found"]
    return live.readiness(root, claude, usage.JsonFileUsage(usage.usage_path(root)), usage.now_utc(), os.environ, Path.home())


def _live_status(reasons: list[str], root: Path) -> list[str]:
    if not reasons:
        return ["Everything needed is in place. Start one real unit with: python -m orgcore run " + str(root)]
    return (["Real workers cannot start yet (the offline demo result stays valid):"] + [f"  - {r}" for r in reasons] +
            ["Headless sessions give no usage readings. Keep one interactive Claude Code session open (see README, 'Enable real workers'),",
             "then check again with: python -m orgcore usage " + str(root)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orgcore")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "rehearse", "rate", "recover", "feedback", "report", "usage", "enable-live", "disable-live", "run"):
        command = sub.add_parser(name)
        command.add_argument("instance", nargs="?", help=f"instance directory (default: ${config.INSTANCE_ENV})")
    sub.choices["rehearse"].add_argument("--rating", choices=RATINGS,
                                         help="record this verdict on each finished demo row right away "
                                              "(without it the run stops at the results, so they can be read first)")
    sub.choices["rate"].add_argument("rating", choices=RATINGS, help="the owner's verdict on the finished demo rows")
    for name in ("rehearse", "rate"):
        sub.choices[name].add_argument("--tasks", type=int, default=1, help="demo rows to take to done and rate")
    sub.choices["rehearse"].add_argument("--crash-after", choices=rehearsal.STEPS,
                                         help="testing aid: kill this process abruptly after the first such step")
    sub.choices["recover"].add_argument("--max-age-minutes", type=int, required=True)
    sub.choices["enable-live"].add_argument("--project", required=True, help="the one project real workers may touch")
    sub.choices["enable-live"].add_argument("--task", required=True, help="exact text of the one waiting task row")
    sub.choices["enable-live"].add_argument("--yes", action="store_true", help="write the consent (without it only the preview is shown)")
    sub.add_parser("usage-capture").add_argument("instance", help="instance directory")
    setup = sub.add_parser("setup")
    setup.add_argument("instance", help="instance directory")
    setup.add_argument("action", choices=["start", "status", "show", "save", "accept", "compose"])
    setup.add_argument("block", nargs="?", help="block name for show, save and accept")
    setup.add_argument("--json", help="the answer as one JSON object; '-' reads it from standard input")
    setup.add_argument("--file", help="read the answer from this JSON file")
    setup.add_argument("--change", action="store_true", help="replace an answer that is already saved (ask the user first)")
    args = parser.parse_args(argv)
    if args.command == "setup":
        return _setup_command(args)
    if args.command == "usage-capture":  # runs as the CLI's status line: quick, quiet, never fails the session
        try:
            print(statusline.capture(config.resolve_instance(args.instance), sys.stdin.read(), usage.now_utc()))
        except Exception as exc:
            print(f"usage capture failed ({type(exc).__name__})")
        return 0

    errors, instance = check.check_instance(args.instance)
    if errors:
        print("FAIL", *errors, sep="\n", file=sys.stderr)
        return 1
    if args.command == "check":
        board = backlog.read(instance.backlog_path)
        print(f"OK: {len(instance.org['lines'])} line(s), {len(board.tasks)} task(s) in {instance.root}")
        for warning in check.warnings(instance):
            print(f"WARNING: {warning}", file=sys.stderr)
    elif args.command == "rehearse":
        def after_step(step: str) -> None:
            if step == args.crash_after:
                os._exit(75)  # no cleanup on purpose

        # each line is flushed as it happens: a crash right after a step must not swallow the lines before it
        rehearsal.rehearse(instance, args.rating.replace("-", "_") if args.rating else None, args.tasks,
                           after_step=after_step, emit=lambda line: print(line, flush=True))
        if not args.rating:
            print(f"Read the result above, then record your verdict: python -m orgcore rate {instance.root} useful|not-useful|rework")
    elif args.command == "rate":
        rehearsal.rate(instance, args.rating.replace("-", "_"), args.tasks, emit=lambda line: print(line, flush=True))
    elif args.command == "usage":
        now = usage.now_utc()
        try:
            readings = usage.JsonFileUsage(usage.usage_path(instance.root)).observe()
        except usage.UsageUnavailable as exc:
            print(f"No usage readings: {exc}\nDispatch gate: closed (unknown is never treated as zero)")
            return 0
        for r in readings:
            print(f"{r.window}: {r.used_percent:g}% used, observed {backlog.format_time(r.observed_at)} by {r.source}"
                  f"{' (synthetic)' if r.synthetic else ''}, resets {backlog.format_time(r.resets_at)}")
        gate = usage.evaluate(instance.policy, readings, now)
        print("Dispatch gate: open" if gate.allowed else "Dispatch gate: closed\n" + "\n".join(f"  - {r}" for r in gate.reasons))
    elif args.command == "enable-live":
        print("\n".join(live.preview(instance, args.project, args.task)))
        if not args.yes:
            print("\nNothing was written. To give this consent, run the same command again with --yes.")
            return 0
        try:
            live.grant(instance.root, args.project, args.task, usage.now_utc())
        except ValueError as exc:
            print(f"FAIL\n{exc}", file=sys.stderr)
            return 1
        reasons = _live_readiness(instance.root)
        print("\nConsent written (policy.json: live_enabled true, scoped to this project and task).")
        print("\n".join(_live_status(reasons, instance.root)))
    elif args.command == "disable-live":
        live.revoke(instance.root)
        print("Real workers are off again (live_enabled false, consent removed). The offline demo is unaffected.")
    elif args.command == "run":
        try:
            lines = live.run_unit(instance.root, doctor.find_claude() or [], os.environ, Path.home())
        except adapter.Refused as exc:
            print("Not started:", *[f"  - {r}" for r in exc.reasons], sep="\n", file=sys.stderr)
            return 1
        except (adapter.SessionFailed, workflow.RoleError) as exc:
            print(f"FAIL: {exc}\nIf the row stayed in running, see: python -m orgcore recover {instance.root} --max-age-minutes 0",
                  file=sys.stderr)
            return 1
        print("\n".join(lines))
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
