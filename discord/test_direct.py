import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gateway
import direct

BACKLOG = "# 待办\n\n规则\n\n## 工作线\n\n- [todo] old\n"


class DirectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.backlog = self.dir / 'backlog.md'
        self.backlog.write_bytes(BACKLOG.encode('utf-8'))
        self.db = gateway.connect({}, self.dir / 'discord.db')
        direct.initialize(self.db)
        real = direct.backlog_io.insert_owner_item
        p = patch.object(direct.backlog_io, 'insert_owner_item', lambda line: real(line, self.backlog))
        p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        self.db.close(); self.temp.cleanup()

    def test_queue_only_detection(self):
        self.assertTrue(direct.queue_only('这件事先加到队列里，不需要立刻回复'))
        self.assertTrue(direct.queue_only('Please handle this later, queue only.'))
        self.assertFalse(direct.queue_only('为什么没有回复我？'))

    def test_accept_queues_at_top_acks_and_requests_spawn(self):
        ack, spawn = direct.accept(self.db, '123', 'work', '做\n这件  事')
        self.assertEqual(ack, '[Arbitros 副总理] 收到，已排到最前面，马上开始处理。')
        self.assertTrue(spawn)
        text = self.backlog.read_bytes().decode('utf-8')
        self.assertIn('## 你的指令（最优先）\n- [todo] 【Discord·Arbitros 副总理】做 这件 事 〔msg:123 role:work〕\n', text)
        self.assertLess(text.index('你的指令'), text.index('## 工作线'))
        self.assertEqual(self.db.execute('SELECT status,queued_only FROM direct_requests').fetchone(), ('queued', 0))

    def test_attachments_sit_before_trailer_and_survive_tick_parsing(self):
        import re
        ack, spawn = direct.accept(self.db, '9', 'work', '看看这个', attachments=[Path(r'C:\a b\报告.pdf'), Path(r'C:\x\y.csv')])
        self.assertIn('（已保存 2 个附件）', ack)
        self.assertTrue(spawn)
        line = [l for l in self.backlog.read_bytes().decode('utf-8').splitlines() if 'Discord' in l][0]
        self.assertEqual(line, r'- [todo] 【Discord·Arbitros 副总理】看看这个 〔附件：C:\a b\报告.pdf；C:\x\y.csv〕 〔msg:9 role:work〕')
        trailer = re.compile(r"〔msg:(\S+) role:(\w+)〕")  # same as control/tick.py TRAILER_RE
        self.assertEqual(trailer.search(line).groups(), ('9', 'work'))
        self.assertIn(r'C:\a b\报告.pdf', trailer.sub('', line))

    def test_attachment_only_message_is_accepted(self):
        direct.accept(self.db, '10', 'life', '', attachments=[Path(r'C:\x.png')])
        self.assertIn(r'【Discord·生活副总理】 〔附件：C:\x.png〕 〔msg:10 role:life〕', self.backlog.read_bytes().decode('utf-8'))

    def test_queue_only_saves_without_spawn(self):
        ack, spawn = direct.accept(self.db, '5', 'quant', '仅入队：稍后做')
        self.assertEqual(ack, '[量化副总理] 原文已保存，仅入队。')
        self.assertFalse(spawn)
        self.assertIn('- [queued] 【Discord·量化副总理】仅入队：稍后做 〔msg:5 role:quant〕', self.backlog.read_bytes().decode('utf-8'))
        self.assertEqual(self.db.execute('SELECT status,queued_only FROM direct_requests').fetchone(), ('queued_only', 1))

    def test_explicit_only_flag_from_slash_ask(self):
        self.assertFalse(direct.accept(self.db, '6', 'life', '普通文字', only=True)[1])

    def test_paused_says_paused_and_does_not_spawn(self):
        ack, spawn = direct.accept(self.db, '7', 'life', 'x', paused=True)
        self.assertIn('暂停', ack)
        self.assertFalse(spawn)
        self.assertIn('- [todo] 【Discord·生活副总理】x', self.backlog.read_bytes().decode('utf-8'))

    def test_second_message_goes_after_first(self):
        direct.accept(self.db, '1', 'work', 'first')
        direct.accept(self.db, '2', 'work', 'second')
        text = self.backlog.read_bytes().decode('utf-8')
        self.assertLess(text.index('first'), text.index('second'))
        self.assertLess(text.index('second'), text.index('## 工作线'))

    def test_spawn_argv_is_detached(self):
        workspace = Path(__file__).resolve().parent.parent.parent  # the same way direct.py derives WORKSPACE
        with patch.object(direct.subprocess, 'Popen') as popen:
            direct.spawn_owner_lane()
        self.assertEqual(popen.call_args.args[0], [
            r'C:\orghome\AppData\Local\Programs\Python\Python312\pythonw.exe',
            str(workspace / 'incore' / 'control' / 'tick.py'), 'owner'])
        kw = popen.call_args.kwargs
        self.assertEqual(kw['creationflags'], 0x00000008 | 0x08000000)
        self.assertEqual(kw['cwd'], str(workspace))

    def test_status_text(self):
        deputies = {'work': {'status': 'running', 'runs': 3, 'current': {'text_preview': '写报告'}},
                    'life': {'status': 'idle', 'runs': 1, 'current': None}}
        out = direct.status_text(False, backlog='- [todo] 【Discord·x】a\n- [queued] 【Discord·x】b\n- [todo] plain\n',
                                 deputies=deputies)
        self.assertIn('待处理 1，仅入队 1', out)
        self.assertIn('Arbitros 副总理：running，累计 3 次，正在处理「写报告」', out)
        self.assertIn('量化副总理：未启用', out)


if __name__ == '__main__':
    unittest.main()
