import io
import os
import struct
import unittest
from array import array
from unittest import mock

from . import nano

LIMIT = 30 * 16000


def spans(*pairs):
    return [{'start': int(a * 16000), 'end': int(b * 16000)} for a, b in pairs]


class PauseCuts(unittest.TestCase):
    def pieces(self, cuts, total):
        return [b - a for a, b in zip([0, *cuts], [*cuts, total])]

    def test_window_up_to_30_s_is_not_cut(self):
        self.assertEqual(nano._pause_cuts(spans((1, 10), (12, 29)), LIMIT, LIMIT), [])
        self.assertEqual(nano._pause_cuts([], 5 * 16000, LIMIT), [])

    def test_cuts_in_the_middle_of_the_last_pause_that_fits(self):
        speech = spans((0, 12), (13, 26), (27, 29.5), (31, 50))     # last pause 29.5-31 straddles the limit
        self.assertEqual(nano._pause_cuts(speech, 60 * 16000, LIMIT)[0], LIMIT)   # its middle (30.25 s) is clamped to 30 s
        speech = spans((0, 12), (13, 26), (27, 29.5), (29.9, 50))
        self.assertEqual(nano._pause_cuts(speech, 60 * 16000, LIMIT)[0], int(29.7 * 16000))
        speech = spans((0, 12), (13, 26), (27, 40))                 # no pause after 27 s fits: cut in the 26-27 s pause
        self.assertEqual(nano._pause_cuts(speech, 45 * 16000, LIMIT)[0], int(26.5 * 16000))

    def test_no_pause_means_hard_cut_and_no_piece_exceeds_the_limit(self):
        total = 100 * 16000
        for speech in ([], spans((0, 100)), spans((0, 50), (50.2, 100)), spans((70, 71))):
            cuts = nano._pause_cuts(speech, total, LIMIT)
            self.assertTrue(all(p <= LIMIT for p in self.pieces(cuts, total)), speech)
            self.assertEqual(sum(self.pieces(cuts, total)), total)
            self.assertEqual(cuts, sorted(set(cuts)))
        self.assertEqual(nano._pause_cuts([], total, LIMIT), [LIMIT, 2 * LIMIT, 3 * LIMIT])


@unittest.skipUnless(os.name == 'nt', 'audio decoder bridge requires Windows drive paths')
class WireFormat(unittest.TestCase):
    def test_windows_of_different_lengths_round_trip(self):
        sizes = [480000, 123457, 1, 300000]
        wire = b'PCM16K2\n' + b''.join(struct.pack('<I', n) + struct.pack('<%df' % n, *([0.5] * n)) for n in sizes)
        process = mock.Mock(stdout=io.BytesIO(wire))
        process.wait.return_value = 0
        process.poll.return_value = 0
        with mock.patch.object(nano.subprocess, 'Popen', return_value=process):
            got = list(nano._audio_windows(nano.ROOT / 'x.webm'))
        self.assertEqual([len(g) for g in got], sizes)
        self.assertTrue(all(isinstance(g, array) and g[0] == 0.5 for g in got))

    def test_truncated_window_is_an_error(self):
        wire = b'PCM16K2\n' + struct.pack('<I', 10) + b'\0' * 12
        process = mock.Mock(stdout=io.BytesIO(wire))
        process.poll.return_value = 0
        with mock.patch.object(nano.subprocess, 'Popen', return_value=process):
            with self.assertRaises(RuntimeError):
                list(nano._audio_windows(nano.ROOT / 'x.webm'))


class OutputTruncated(Exception):   # same class name as transcribe_cpp.OutputTruncated, which is only importable inside the worker
    pass


class TruncatedWindow(unittest.TestCase):
    def fake(self, limit, seen):
        def run(audio):
            seen.append(len(audio))
            if len(audio) > limit:
                raise OutputTruncated('status 18')
            return mock.Mock(text='%d' % len(audio))
        return run

    def test_normal_window_runs_once(self):
        seen = []
        got = nano._run_window(self.fake(10**9, seen), array('f', [0.0]) * 1000)
        self.assertEqual(seen, [1000])
        self.assertEqual([len(p) for p, _ in got], [1000])

    def test_truncated_window_is_halved_and_only_that_window_retried(self):
        seen = []
        audio = array('f', range(1000))
        got = nano._run_window(self.fake(600, seen), audio)
        self.assertEqual(seen, [1000, 500, 500])
        self.assertEqual([len(p) for p, _ in got], [500, 500])
        self.assertEqual(sum((list(p) for p, _ in got), []), list(audio))      # nothing dropped, order kept

    def test_halves_that_are_still_truncated_are_halved_again_then_give_up(self):
        seen = []
        got = nano._run_window(self.fake(300, seen), array('f', [0.0]) * 1000)
        self.assertEqual([len(p) for p, _ in got], [250] * 4)
        with self.assertRaises(OutputTruncated):
            nano._run_window(self.fake(0, []), array('f', [0.0]) * 1000)      # never decodes: depth 3 then the error goes up

    def test_other_errors_are_not_retried(self):
        calls = []
        def run(audio):
            calls.append(1)
            raise RuntimeError('boom')
        with self.assertRaises(RuntimeError):
            nano._run_window(run, array('f', [0.0]) * 100)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
