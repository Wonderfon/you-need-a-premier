"""Deterministic stand-ins for an executor and a reviewer. No model, no network; they write only under <instance>/demo/.

The executor's first attempt always leaves out one required line, so a rehearsal shows a real rework round.
"""
from __future__ import annotations

from pathlib import Path

from . import ledger

REQUIRED = ("Summary:", "Sources:")


class FakeExecutor:
    def __init__(self, actor: str = "executor-stub-1"):
        self.actor = actor

    def work(self, root: Path, key: tuple[str, str, str], attempt: int) -> str:
        """Write the output file for this attempt; returns its path relative to the instance (the evidence)."""
        relative = f"demo/{ledger.task_id(key)}.txt"
        lines = [f"Summary: {key[2]}"]
        if attempt >= 2:
            lines.append("Sources: none (offline rehearsal)")
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return relative


class FakeReviewer:
    def __init__(self, actor: str = "reviewer-stub-1"):
        self.actor = actor

    def judge(self, root: Path, evidence: str) -> tuple[str, str]:
        """Read-only check of the evidence file against the acceptance lines. Returns (verdict, evidence text)."""
        target = (root / evidence).resolve()
        if root.resolve() not in target.parents or not target.is_file():
            return "blocked", f"evidence file not found inside the instance: {evidence}"
        present = {line.split(":", 1)[0] + ":" for line in target.read_text(encoding="utf-8").splitlines()}
        missing = [name for name in REQUIRED if name not in present]
        if missing:
            return "rework", f"{evidence}: missing required line {', '.join(missing)}"
        return "pass", f"{evidence}: all {len(REQUIRED)} required lines present"
