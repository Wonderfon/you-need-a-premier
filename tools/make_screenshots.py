"""Regenerate the README screenshots (docs/images/*.svg) from the fictional demo instance.

    python tools/make_screenshots.py [output-dir]

Everything shown comes from commands run on a temporary copy of `examples/demo` (fictional data) and from the real
`orgcore.usage.evaluate` gate; nothing is read from a real instance. The SVG files hold plain text, so the private-term
scan (`tools/check_private.py`) can read them. To get PNG files, open an SVG in any browser and save a screenshot
(for example `msedge --headless --screenshot=out.png --force-device-scale-factor=2 --window-size=W,H file:///.../x.svg`,
W and H are in the first line of each SVG).
"""
from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from orgcore import usage  # noqa: E402

FONT = "Consolas, 'Cascadia Mono', 'DejaVu Sans Mono', monospace"
SANS = "'Segoe UI', 'Helvetica Neue', Arial, sans-serif"
CHAR_W, LINE_H = 8.3, 21


def run(cwd: Path, *args: str) -> str:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run([sys.executable, "-m", "orgcore", *args], cwd=cwd, env=env, capture_output=True)
    text = (done.stdout + done.stderr).decode("utf-8", errors="replace").rstrip("\n")
    return text.replace(str(cwd) + os.sep, "")  # show relative instance paths, never the temporary folder


def terminal(title: str, blocks: list[tuple[str, str]], note: str = "") -> str:
    """A dark terminal window. Each block is a command (shown after a prompt) and its output."""
    rows: list[tuple[str, str]] = []
    for command, output in blocks:
        rows.append(("cmd", command))
        rows += [("out", line) for line in output.splitlines()]
        rows.append(("out", ""))
    rows.pop()
    width = int(max(len(text) + (2 if kind == "cmd" else 0) for kind, text in rows) * CHAR_W + 48)
    width = max(width, 560, int(len(note) * 6.9 + 48))
    height = 44 + len(rows) * LINE_H + 24 + (26 if note else 0)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           f'<title>{html.escape(title)}</title>',
           f'<rect width="{width}" height="{height}" rx="10" fill="#1e1f24"/>',
           f'<rect width="{width}" height="32" rx="10" fill="#2b2d34"/><rect y="20" width="{width}" height="12" fill="#2b2d34"/>',
           '<circle cx="18" cy="16" r="6" fill="#ff5f56"/><circle cx="38" cy="16" r="6" fill="#ffbd2e"/><circle cx="58" cy="16" r="6" fill="#27c93f"/>',
           f'<text x="{width // 2}" y="21" fill="#9aa0aa" font-family="{SANS}" font-size="13" text-anchor="middle">{html.escape(title)}</text>']
    y = 56
    for kind, text in rows:
        if kind == "cmd":
            out.append(f'<text x="20" y="{y}" font-family="{FONT}" font-size="14" xml:space="preserve">'
                       f'<tspan fill="#7ee787">$ </tspan><tspan fill="#f0f3f6">{html.escape(text)}</tspan></text>')
        else:
            colour = "#ffa657" if any(k in text for k in ("closed", "ahead of the pace", "REWORK", "reaches")) else "#c9d1d9"
            if "PASS" in text or "open" in text.split("Dispatch gate:")[-1][:6]:
                colour = "#7ee787"
            out.append(f'<text x="20" y="{y}" fill="{colour}" font-family="{FONT}" font-size="14" xml:space="preserve">'
                       f'{html.escape(text)}</text>')
        y += LINE_H
    if note:
        out.append(f'<text x="20" y="{height - 14}" fill="#8b949e" font-family="{SANS}" font-size="12">{html.escape(note)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def pace_chart() -> str:
    """One 7-day window. Each half-hour the real gate decides whether the next unit may start; a unit that starts adds
    usage. The owner's own use (a burst on day 3) pushes the line ahead of the pace line, so dispatch pauses until the
    pace line catches up. The markers are coloured by `usage.evaluate`, not by the drawing code."""
    length, share, reserve, slack = 168, 50.0, 30.0, 5.0
    policy = {"live_enabled": False, "subscription_windows": [
        {"name": "seven_day", "reserve_percent": reserve, "max_share_percent": share, "max_observation_age_minutes": 360,
         "length_hours": length, "pacing_slack_percent": slack}]}
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    end = start + timedelta(hours=length)
    used, points = 0.0, []
    for step in range(length * 2 + 1):
        t = step / 2
        now = start + timedelta(hours=t)
        if 60 <= t < 64:
            used += 1.0  # the owner's own session on the same account
        reading = usage.Observation("seven_day", min(used, 100.0), now, end, "demo")
        gate = usage.evaluate(policy, [reading], now)
        points.append((t, reading.used_percent, gate.allowed))
        if gate.allowed:
            used += 0.6  # one unit of work started in this half hour
    w, h, left, top, plot_w, plot_h = 900, 460, 70, 60, 790, 300
    x = lambda t: left + plot_w * t / length  # noqa: E731
    y = lambda p: top + plot_h * (1 - p / 100)  # noqa: E731
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
           "<title>Usage against the pace line over one 7-day window (fictional data)</title>",
           f'<rect width="{w}" height="{h}" rx="10" fill="#fbfaf7"/>',
           f'<text x="{left}" y="30" font-family="{SANS}" font-size="18" font-weight="600" fill="#26231f">'
           "Work follows a straight line through the week</text>",
           f'<text x="{left}" y="48" font-family="{SANS}" font-size="12.5" fill="#6f675d">'
           "Account usage in one 7-day window, checked every half hour by the dispatch gate. Fictional data.</text>"]
    for p in range(0, 101, 20):
        svg.append(f'<line x1="{left}" y1="{y(p):.1f}" x2="{left + plot_w}" y2="{y(p):.1f}" stroke="#e4ded4"/>'
                   f'<text x="{left - 8}" y="{y(p) + 4:.1f}" font-family="{SANS}" font-size="11" fill="#6f675d" text-anchor="end">{p}%</text>')
    for day in range(0, 8):
        svg.append(f'<text x="{x(day * 24):.1f}" y="{top + plot_h + 18}" font-family="{SANS}" font-size="11" fill="#6f675d" '
                   f'text-anchor="middle">day {day}</text>')
    svg.append(f'<line x1="{x(0):.1f}" y1="{y(share):.1f}" x2="{x(length):.1f}" y2="{y(share):.1f}" stroke="#b35c00" stroke-dasharray="6 4"/>'
               f'<text x="{left + 4}" y="{y(share) - 6:.1f}" font-family="{SANS}" font-size="11.5" fill="#b35c00">'
               f"max share {share:g}% (never go past)</text>")
    svg.append(f'<line x1="{x(0):.1f}" y1="{y(100 - reserve):.1f}" x2="{x(length):.1f}" y2="{y(100 - reserve):.1f}" stroke="#a33" stroke-dasharray="2 4"/>'
               f'<text x="{left + plot_w - 4}" y="{y(100 - reserve) - 6:.1f}" font-family="{SANS}" font-size="11.5" fill="#a33" text-anchor="end">'
               f"reserve boundary {100 - reserve:g}% (kept for you)</text>")
    pace = [(0, slack), (length, share * 1 + slack)]
    svg.append(f'<polyline points="{" ".join(f"{x(t):.1f},{y(min(p, 100)):.1f}" for t, p in pace)}" fill="none" stroke="#2f6f6a" stroke-width="2.5"/>'
               f'<text x="{x(78):.1f}" y="{y(share * 78 / length + slack) + 30:.1f}" font-family="{SANS}" font-size="12.5" fill="#2f6f6a" '
               f'font-weight="600" transform="rotate(-6 {x(78):.1f} {y(share * 78 / length + slack) + 30:.1f})">'
               "pace line = max share x elapsed fraction + slack</text>")
    svg.append(f'<polyline points="{" ".join(f"{x(t):.1f},{y(p):.1f}" for t, p, _ in points)}" fill="none" stroke="#26231f" stroke-width="1.4"/>')
    for t, p, ok in points[::2]:
        svg.append(f'<circle cx="{x(t):.1f}" cy="{y(p):.1f}" r="2.6" fill="{"#2e8b57" if ok else "#c0392b"}"/>')
    svg.append(f'<text x="{x(40):.1f}" y="{y(40):.1f}" font-family="{SANS}" font-size="12" fill="#c0392b" text-anchor="middle">'
               "your own session (day 2.5) pushes usage ahead of the line:</text>"
               f'<text x="{x(40):.1f}" y="{y(40) + 16:.1f}" font-family="{SANS}" font-size="12" fill="#c0392b" text-anchor="middle">'
               "the gate stays closed until the line catches up</text>")
    ly = top + plot_h + 52
    svg.append(f'<circle cx="{left + 6}" cy="{ly}" r="4" fill="#2e8b57"/><text x="{left + 16}" y="{ly + 4}" font-family="{SANS}" font-size="12" fill="#26231f">gate open: a unit may start</text>'
               f'<circle cx="{left + 220}" cy="{ly}" r="4" fill="#c0392b"/><text x="{left + 230}" y="{ly + 4}" font-family="{SANS}" font-size="12" fill="#26231f">gate closed</text>'
               f'<line x1="{left + 330}" y1="{ly}" x2="{left + 360}" y2="{ly}" stroke="#26231f" stroke-width="1.4"/><text x="{left + 366}" y="{ly + 4}" font-family="{SANS}" font-size="12" fill="#26231f">usage on the account</text>'
               f'<line x1="{left + 520}" y1="{ly}" x2="{left + 550}" y2="{ly}" stroke="#2f6f6a" stroke-width="2.5"/><text x="{left + 556}" y="{ly + 4}" font-family="{SANS}" font-size="12" fill="#26231f">pace line</text>')
    svg.append(f'<text x="{left}" y="{h - 14}" font-family="{SANS}" font-size="11" fill="#6f675d">'
               "Source: tools/make_screenshots.py; every dot is the answer of orgcore.usage.evaluate for that moment.</text>")
    svg.append("</svg>")
    return "\n".join(svg) + "\n"


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="orgcore-shots-"))
    try:
        demo = work / "demo-instance"
        shutil.copytree(ROOT / "examples" / "demo", demo)
        board_before = (demo / "projects" / "backlog.md").read_text(encoding="utf-8").rstrip("\n")
        rehearsal = run(work, "rehearse", "demo-instance")
        rated = run(work, "rate", "demo-instance", "useful")
        board_after = (demo / "projects" / "backlog.md").read_text(encoding="utf-8").rstrip("\n")
        (out_dir / "rehearsal.svg").write_text(terminal(
            "offline rehearsal: one row, fake workers, no model", [
                ("cat projects/backlog.md", board_before),
                ("python -m orgcore rehearse demo-instance", rehearsal),
                ("python -m orgcore rate demo-instance useful", rated),
                ("cat projects/backlog.md", board_after)],
            "Fictional demo instance. The executor is a stub that leaves one line out the first time, so the reviewer sends it back."),
            encoding="utf-8")

        # the dispatch gate: a fictional account that is ahead of its pace line
        policy_path = demo / "policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["subscription_windows"][1].update(length_hours=168, pacing_slack_percent=5)
        policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        now = usage.now_utc()
        stamp = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        readings = [{"window": "five_hour", "used_percent": 12, "observed_at": stamp(now),
                     "resets_at": stamp(now + timedelta(hours=3)), "source": "demo-fixture"},
                    {"window": "seven_day", "used_percent": 36, "observed_at": stamp(now),
                     "resets_at": stamp(now + timedelta(hours=108)), "source": "demo-fixture"}]
        (demo / "state").mkdir(exist_ok=True)
        (demo / "state" / "usage.json").write_text(json.dumps({"observations": readings}, indent=2), encoding="utf-8")
        (out_dir / "usage-gate.svg").write_text(terminal(
            "dispatch gate: the week is 36% used, the pace line allows 23%", [
                ("python -m orgcore usage demo-instance", run(work, "usage", "demo-instance"))],
            "Fictional readings. The gate names the window and the cause; an unknown or stale reading closes it too."),
            encoding="utf-8")

        # one real run, captured on 2026-10-26 and kept as plain files in docs/real-run (fictional garden task)
        real = ROOT / "docs" / "real-run"
        read = lambda name: (real / name).read_text(encoding="utf-8").rstrip("\n")  # noqa: E731
        (out_dir / "real-run.svg").write_text(terminal(
            "one real unit: Claude executor, then a separate Claude reviewer", [
                ("cat projects/backlog.md", read("board-before.md")),
                ("python -m orgcore run my-org", read("run-output.txt")),
                ("cat projects/backlog.md", read("board-after.md")),
                ("cat work/garden/seed-checklist.md", read("seed-checklist.md"))],
            "A real run on a fictional garden task (docs/real-run). Two real Claude sessions; the usage figure is whole percent points."),
            encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    (out_dir / "pace-line.svg").write_text(pace_chart(), encoding="utf-8")
    for name in ("pace-line", "usage-gate", "rehearsal", "real-run"):
        print(out_dir / f"{name}.svg")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "images")
