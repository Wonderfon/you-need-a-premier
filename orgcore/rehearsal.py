"""Offline rehearsal: fake executor and fake reviewer take demo rows through todo -> running -> review -> rework ->
running -> review -> done, then one owner rating is recorded. Zero model requests, nothing sent.

Every step is durable and re-entrant, so a process killed at any point can be started again and continues the same row
instead of starting another. Only projects marked "demo": true in org.json are touched.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from . import backlog, ledger, scheduler, usage, workflow
from .config import Instance
from .fake import FakeExecutor, FakeReviewer

STEPS = ("dispatched", "submitted", "reviewed")


def rehearse(instance: Instance, rating: str, tasks: int = 1, source: usage.UsageSource | None = None,
             now: datetime | None = None, after_step: Callable[[str], None] = lambda step: None) -> list[str]:
    """Run until `tasks` demo rows have an owner rating. `after_step` is called after each durable step (testing hook)."""
    executor, reviewer = FakeExecutor(), FakeReviewer()
    demo = instance.demo_projects()
    if not demo:
        return ["No demo project is configured (a project with \"demo\": true); nothing to rehearse."]
    moment = now or usage.now_utc()
    source = source or usage.FakeUsage.healthy(instance.policy, moment)
    out: list[str] = []

    def rated() -> set[str]:
        return {e["task"] for e in ledger.read(instance.root, ledger.FEEDBACK)}

    def work_and_submit(key, attempt):
        evidence = executor.work(instance.root, key, attempt)
        out.append(f"{_name(key)}  running -> review   {executor.actor} submitted {evidence}")
        workflow.submit(instance, key, executor.actor, evidence, moment)
        after_step("submitted")

    while True:
        board = backlog.read(instance.backlog_path)
        mine = [t for t in board.tasks if t.project in demo]
        # 1. rows that passed review but have no owner rating yet (a run was killed right after the verdict)
        for task in mine:
            passed = [e for e in ledger.for_task(ledger.read(instance.root), task.key, "reviewed") if e["verdict"] == "pass"]
            if task.state == "done" and passed and ledger.task_id(task.key) not in rated() and len(rated()) < tasks:
                _rate(instance, task, rating, moment, out)
        if len(rated()) >= tasks:
            break
        # 2. a row this executor holds but nobody is working on (its process died): continue it, same attempt
        resumed = False
        for task in mine:
            last = ledger.for_task(ledger.read(instance.root), task.key, "dispatched")
            if task.state == "running" and last and last[-1]["actor"] == executor.actor:
                try:
                    with workflow.claim_lock(instance.root, task.key, timeout=0.2):
                        out.append(f"{_name(task.key)}  resuming attempt {last[-1]['attempt']} after an interrupted run")
                        work_and_submit(task.key, last[-1]["attempt"])
                        resumed = True
                except TimeoutError:
                    continue
                break
        if resumed:
            continue
        # 3. a row waiting for review
        review_row = next((t for t in mine if t.state == "review"), None)
        if review_row:
            evidence = ledger.for_task(ledger.read(instance.root), review_row.key, "submitted")[-1]["evidence"]
            verdict, text = reviewer.judge(instance.root, evidence)
            workflow.review(instance, review_row.key, reviewer.actor, verdict, text, moment)
            target = {"pass": "done", "rework": "rework", "blocked": "blocked"}[verdict]
            out.append(f"{_name(review_row.key)}  review -> {target:<8} {reviewer.actor}: {verdict.upper()}  {text}")
            after_step("reviewed")
            if verdict == "blocked":
                break
            continue
        # 4. dispatch the next row
        sent_back = [t.project for t in mine if t.state == "rework"]  # finish a returned row before starting another
        result = scheduler.dispatch(instance, source, executor.actor, moment, sent_back[:1] or demo)
        if isinstance(result, scheduler.Refusal):
            out.append(f"Stopped: {result.code}: " + "; ".join(result.reasons))
            break
        key = result.task.key
        out.append(f"{_name(key)}  {'todo' if result.attempt == 1 else 'rework'} -> running  "
                   f"{executor.actor} claimed attempt {result.attempt}")
        after_step("dispatched")
        with workflow.claim_lock(instance.root, key):
            work_and_submit(key, result.attempt)
    out.append("Model requests: 0. Messages sent: 0.")
    return out


def _name(key) -> str:
    return f"DEMO-{ledger.task_id(key)}"


def _rate(instance, task, rating, moment, out):
    workflow.record_feedback(instance, task.key, rating, "recorded by the offline rehearsal", moment)
    out.append(f"{_name(task.key)}  owner feedback saved: {rating.replace('_', ' ')}")
