"""Terminal meeting tool (owner\x2010-07): the coordinator chairs in a terminal session and calls each attendee's resident
session in turn, so they hear each other through the minutes file.

  python control/meeting.py start <slug> <role...> [议题=<text>]   create incore/meetings/<date>-<slug>.md
  python control/meeting.py say <owner|role> "<text>" [slug]        append a remark verbatim
  python control/meeting.py turn <role> [字数=400] [slug]           one turn in that role's resident session
Roles: coordinator work uvox life quant intelligence (work = Arbitros 线, uvox = Beacon Listener 线). [slug] defaults to the newest meeting file.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tick  # noqa: E402

# ---- CONFIG ----
MEETINGS = tick.WORKSPACE / "incore" / "meetings"
LOCK_RETRY_S = 15      # the role's session is busy (owner terminal, review, executor lane): retry this often ...
LOCK_WAIT_S = 300      # ... for at most this long, then report busy
DEFAULT_WORDS = 400    # per-turn word limit; the coordinator passes 字数=1500 for 述职 turns
# ----------------

PROMPT = ("你是{name}。owner 在终端里召集了一个会议，你的常驻会话被叫来发言，会议由总理主持。议题：{topic}\n\n"
          "以你这条线的视角发言：先结论，再理由，最后写你需要谁做什么，{words} 字以内。只读：可以查 backlog 和 audits 核对事实，"
          "不改任何文件、不派任务、不发 Discord。别人说错了就直说。你的回复会原样记进会议记录并显示给 owner，不要写成给别人的私信。\n\n"
          "截至目前的会议记录：\n\n{minutes}\n\n现在轮到你（{name}）发言。")


def who(speaker: str) -> str:
    return "owner" if speaker == "owner" else f"{tick.ROLE_NAMES[speaker]} · {speaker}"


def find(slug: str | None) -> Path:
    files = sorted(MEETINGS.glob(f"*-{slug}.md" if slug else "*.md"), key=lambda p: p.stat().st_mtime) if MEETINGS.exists() else []
    if not files:
        raise SystemExit(f"没有找到会议记录{'：' + slug if slug else ''}（先 start）")
    return files[-1]


def attendees(path: Path) -> list[str]:
    line = next((x for x in path.read_text(encoding="utf-8").splitlines() if x.startswith("- 与会人：")), "")
    return [r for r in tick.ROLE_NAMES if f"· {r}" in line]


def start(slug: str, *rest: str) -> int:
    roles = [r for r in rest if r in tick.ROLE_NAMES]
    topic = next((r.removeprefix("议题=") for r in rest if r.startswith("议题=")), slug)
    bad = [r for r in rest if r not in tick.ROLE_NAMES and not r.startswith("议题=")]
    if not roles or bad:
        print(f"用法：start <slug> <角色...> [议题=<文字>]；角色只能是 {' '.join(tick.ROLE_NAMES)}" + (f"，不认识：{' '.join(bad)}" if bad else ""))
        return 2
    path = MEETINGS / f"{datetime.now():%Y-%m-%d}-{slug}.md"
    if path.exists():
        print(f"已存在：{path}")
        return 2
    MEETINGS.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# 会议：{slug}\n\n- 日期：{datetime.now():%Y-%m-%d %H:%M}\n- 议题：{topic}\n- 主持：总理 · coordinator\n"
                    f"- 与会人：{'、'.join(f'{tick.ROLE_NAMES[r]} · {r}' for r in roles)}\n", encoding="utf-8")
    print(path)
    return 0


def append(path: Path, speaker: str, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n## {who(speaker)} · {datetime.now():%H:%M}\n\n{text}\n")


def say(speaker: str, text: str, slug: str | None = None) -> int:
    if speaker != "owner" and speaker not in tick.ROLE_NAMES:
        print("say 的发言人只能是 owner 或角色名")
        return 2
    path = find(slug)
    append(path, speaker, text)
    print(f"已记入 {path.name}")
    return 0


def turn(role: str, slug: str | None = None, words: int = DEFAULT_WORDS) -> int:
    if role not in tick.ROLE_NAMES:
        print(f"不认识的角色：{role}")
        return 2
    path = find(slug)
    if role not in attendees(path):
        print(f"{role} 不在 {path.name} 的与会人里")
        return 2
    if not tick.deputy_ready(role):  # a forked role (uvox) before its fork exists never starts blank
        print(f"{tick.ROLE_NAMES[role]} 还没有会话（拆线迁移 line_split.py 没跑，或分叉停下来待查），这次不发言")
        return 5
    if not tick.fork_source_free(role):  # its first turn would fork the work deputy's session, which is in use
        print(f"{tick.ROLE_NAMES[role]} 的首次发言要分叉 Arbitros 副总理的会话，那个会话正忙，稍后再 turn {role}")
        return 3
    lock, waited = tick.deputy_lock(role), 0
    while not tick.take_lock(lock):
        if waited >= LOCK_WAIT_S:
            print(f"{tick.ROLE_NAMES[role]} 的会话正忙，等了 {waited} 秒仍没有空，退出；稍后再 turn {role}")
            return 3
        time.sleep(LOCK_RETRY_S)
        waited += LOCK_RETRY_S
    try:
        text = path.read_text(encoding="utf-8")
        topic = next((x.removeprefix("- 议题：") for x in text.splitlines() if x.startswith("- 议题：")), path.stem)
        prompt = PROMPT.format(name=tick.ROLE_NAMES[role], topic=topic, minutes=text.strip(), words=words)
        code, run_file, ok, result, elapsed = tick.run_resident(
            role, "meeting", prompt, {"kind": "meeting", "text_preview": f"会议 {path.stem}"}, {"meeting": path.name},
            item=str(path))
    finally:
        tick.release_lock(lock)
    if not ok or not result.strip():
        print(f"{tick.ROLE_NAMES[role]} 发言失败（{tick.fail_reason(code)}），见 {run_file}")
        return 4
    append(path, role, result.strip())
    print(f"【{who(role)}】\n{result.strip()}")
    return 0


def main(argv: list[str]) -> int:
    cmd, args = (argv[0] if argv else ""), argv[1:]
    if cmd == "start" and len(args) >= 2:
        return start(*args)
    if cmd == "say" and 2 <= len(args) <= 3:
        return say(*args)
    if cmd == "turn" and 1 <= len(args) <= 3:
        words = next((a.removeprefix("字数=") for a in args if a.startswith("字数=")), str(DEFAULT_WORDS))
        pos = [a for a in args if not a.startswith("字数=")]
        if not words.isdecimal() or int(words) <= 0 or len(pos) not in (1, 2):
            print("用法：turn <角色> [字数=<正整数，默认 400>] [slug]")
            return 2
        return turn(pos[0], *pos[1:], words=int(words))
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
