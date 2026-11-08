"""Append one observation of the account-wide Claude five-hour window to data/usage.jsonl.

Sends a one-word prompt to Haiku and reads the rate_limit_event from stream-json output.
Exit 0 after appending one line; exit 1 without writing when no event is reported.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

# ---- CONFIG ----
CLAUDE = ic.V["claude_exe"]
ARGS = ["-p", "--model", "haiku", "--output-format", "stream-json", "--verbose", "--max-turns", "1",
        "--tools", "", "--no-session-persistence"]
PROMPT = "ok"
TIMEOUT_SECONDS = 120
USAGE_FILE = Path(__file__).resolve().parent / "data" / "usage.jsonl"
SOURCE = "sampler"
# ----------------


def five_hour(stdout: str) -> tuple[float, int] | None:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
            continue
        window = (((event.get("rate_limit_info") or {}).get("unifiedWindows") or {}).get("five_hour") or {})
        utilization, resets_at = window.get("utilization"), window.get("resetsAt")
        if isinstance(utilization, (int, float)) and isinstance(resets_at, (int, float)):
            return float(utilization), int(resets_at)
    return None


def main() -> int:
    try:
        done = subprocess.run([CLAUDE, *ARGS], input=PROMPT, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=TIMEOUT_SECONDS,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"claude run failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    found = five_hour(done.stdout)
    if found is None:
        print(f"no rate_limit_event (claude exit {done.returncode})", file=sys.stderr)
        return 1
    utilization, resets_at = found
    record = {"observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "utilization": utilization, "resets_at": resets_at, "source": SOURCE}
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with USAGE_FILE.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
