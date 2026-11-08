"""Owner messages from allowlisted channels: archive (gateway.record_inbound), queue at the top of
backlog.md, and wake the tick's owner lane. This module never runs a model itself."""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent.parent
sys.path.insert(0, str(ROOT.parent / 'control'))
import backlog_io
import instance_config as ic

LABELS = {'work': 'Arbitros 副总理', 'uvox': 'Beacon Listener 副总理', 'life': '生活副总理', 'quant': '量化副总理',
          'intelligence': '情报专员', 'coordinator': '总理'}  # work = Arbitros 线, uvox = Beacon Listener 线 (split of 10-27)
PYTHONW = ic.V['pythonw_exe']
TICK = WORKSPACE / 'incore' / 'control' / 'tick.py'
DEPUTIES = WORKSPACE / 'incore' / 'control' / 'data' / 'deputies.json'
DETACHED_PROCESS, CREATE_NO_WINDOW = 0x00000008, 0x08000000


def initialize(db):
    # The dashboard reads this table; it is only a per-message receipt now.
    db.execute('''CREATE TABLE IF NOT EXISTS direct_requests (
      message_id TEXT PRIMARY KEY, status TEXT NOT NULL, queued_only INTEGER NOT NULL,
      updated_at TEXT NOT NULL, next_attempt REAL NOT NULL DEFAULT 0,
      response TEXT, detail TEXT, ack_id TEXT, reply_ids TEXT)''')
    db.commit()


def queue_only(text):
    return bool(re.search(r'/queue\b|\bqueue[ -]only\b|仅入队|只入队|只(?:需要)?加到队列|不(?:用|需要)(?:立刻|立即|马上|现在)?回复|无需(?:立即|立刻)?回复', text, re.I))


def accept(db, message_id, role, text, paused=False, only=False, attachments=()):
    """Queue an archived owner message. Returns (ack_text, spawn_owner_lane)."""
    only = only or queue_only(text)
    status = 'queued' if only else 'todo'
    files = f" 〔附件：{'；'.join(str(p) for p in attachments)}〕" if attachments else ''
    backlog_io.insert_owner_item(f"- [{status}] 【Discord·{LABELS[role]}】{' '.join(text.split())}{files} 〔msg:{message_id} role:{role}〕")
    db.execute('INSERT OR IGNORE INTO direct_requests(message_id,status,queued_only,updated_at) VALUES(?,?,?,?)',
               (str(message_id), 'queued_only' if only else 'queued', int(only), datetime.now(timezone.utc).isoformat()))
    db.commit()
    label = f'[{LABELS[role]}]' + (f'（已保存 {len(attachments)} 个附件）' if attachments else '')
    if only:
        return f'{label} 原文已保存，仅入队。', False
    if paused:
        return f'{label} 收到，已排到最前面，但派发标志处于暂停，不立即启动，等下一次定时 tick 处理。', False
    return f'{label} 收到，已排到最前面，马上开始处理。', True


def spawn_owner_lane():
    subprocess.Popen([PYTHONW, str(TICK), 'owner'], cwd=str(WORKSPACE), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                     creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW)


def status_text(paused, backlog=None, deputies=None):
    try:
        deputies = json.loads(DEPUTIES.read_text(encoding='utf-8')) if deputies is None else deputies
    except (OSError, ValueError):
        deputies = {}
    text = (backlog_io.BACKLOG.read_text(encoding='utf-8') if backlog is None else backlog)
    todo = len(re.findall(r'^- \[todo\] 【Discord', text, re.M))
    queued = len(re.findall(r'^- \[queued\] 【Discord', text, re.M))
    rows = []
    for role, label in LABELS.items():
        e = deputies.get(role)
        if not isinstance(e, dict):
            rows.append(f'{label}：未启用')
            continue
        cur = e.get('current')
        rows.append(f"{label}：{e.get('status', '?')}，累计 {e.get('runs', 0)} 次"
                    + (f"，正在处理「{cur.get('text_preview', '')}」" if isinstance(cur, dict) else ''))
    return (f"派发标志：{'暂停' if paused else '开启'}；owner 待处理 {todo}，仅入队 {queued}。\n" + '\n'.join(rows))
