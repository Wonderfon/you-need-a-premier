"""10-26 Codex weekly burn line: the arithmetic (codex_burn.py), the 5-slot switch (tick.py) and the wider sol first scope
(shadow_review.py). The fall-DST window starts 10-26 02:24 EDT and resets 11-02 01:24 EST; reading 4% at 10-26 15:30."""
import json
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import codex_burn
import review_stats
import shadow_review as shadow
import tick

ZONE = datetime.fromisoformat("2026-10-26T15:30:00-04:00").tzinfo
START = datetime.fromisoformat("2026-10-26T02:24:00-04:00")
RESETS = datetime.fromisoformat("2026-11-02T01:24:00-05:00").timestamp()
NOW = datetime.fromisoformat("2026-10-26T15:30:00-04:00")


def obs(used, resets=RESETS):
    return {"at": NOW.isoformat(), "limits": {"secondary": {"used_percent": used, "window_minutes": 10080, "resets_at": resets}}}


class BurnMath(unittest.TestCase):
    def test_line_target_and_gap_for_the_owners_example(self):
        s = codex_burn.standing(obs(4), NOW)
        self.assertEqual(s["target"], codex_burn._local_time(RESETS))
        self.assertAlmostEqual(s["line"], 95 * 13.1 / 168, places=3)
        self.assertAlmostEqual(s["behind"], s["line"] - 4)
        self.assertLess(s["behind"], 10)

    def test_target_moves_to_the_next_unexpired_card_then_to_resets_at(self):
        first = datetime.fromisoformat("2026-11-30T15:00:00-05:00")
        second = datetime.fromisoformat("2026-12-01T17:33:00-05:00")
        reset = second + timedelta(days=1)
        self.assertEqual(tick.CODEX_RESETS, codex_burn.CODEX_RESET_CARDS)
        for now, expected in ((first - timedelta(days=1), first), (first, second), (second, reset)):
            with self.subTest(now=now):
                self.assertEqual(codex_burn.target_time(now, reset.timestamp()), expected)
        earlier_reset = first - timedelta(hours=1)
        self.assertEqual(codex_burn.target_time(first - timedelta(days=1), earlier_reset.timestamp()), earlier_reset)

    def test_line_reaches_95_at_each_cards_exact_expiry_time(self):
        for expiry in ("2026-11-30T15:00:00-05:00", "2026-12-01T17:33:00-05:00"):
            target = datetime.fromisoformat(expiry)
            resets = (target + timedelta(days=1)).timestamp()
            s = codex_burn.standing(obs(4, resets), target - timedelta(hours=1))
            self.assertEqual(s["target"], target)
            self.assertAlmostEqual(s["line"], 95 * 143 / 144)
            self.assertEqual(codex_burn.line_percent(target, resets, s["target"]), 95)

    def test_reset_and_window_start_use_their_own_offset_across_fall_dst(self):
        resets = datetime.fromisoformat("2026-11-02T01:24:00-05:00").timestamp()
        now = datetime.fromisoformat("2026-10-30T12:00:00-04:00")
        s = codex_burn.standing(obs(4, resets), now)
        self.assertEqual((s["resets_at"].month, s["resets_at"].day, s["resets_at"].hour, s["resets_at"].minute),
                         (11, 2, 1, 24))
        self.assertEqual(s["resets_at"].utcoffset().total_seconds(), -5 * 3600)
        self.assertEqual((s["target"].month, s["target"].day, s["target"].hour, s["target"].minute), (11, 2, 1, 24))
        self.assertEqual(s["target"].utcoffset().total_seconds(), -5 * 3600)
        start = codex_burn._local_time(resets - 7 * 86400)
        self.assertEqual(start, START)
        self.assertEqual((start.month, start.day, start.hour, start.minute), (10, 26, 2, 24))
        self.assertEqual(start.utcoffset().total_seconds(), -4 * 3600)
        self.assertAlmostEqual(s["line"], 95 * 105.6 / 168, places=3)

    def test_ahead_unreadable_and_expired_are_never_zero(self):
        self.assertLess(codex_burn.behind_points(obs(60), NOW), 0)
        for bad in (None, {}, obs(4, resets=NOW.timestamp() - 1), {"limits": {"primary": {"window_minutes": 300, "used_percent": 1, "resets_at": RESETS}}}):
            self.assertIsNone(codex_burn.behind_points(bad, NOW), bad)

    def test_projection_uses_the_last_six_hours_of_this_window(self):
        t = NOW.timestamp()
        hist = [[t - 7 * 3600, 0.0, RESETS], [t - 3 * 3600, 2.0, RESETS], [t, 4.0, RESETS], [t - 1000, 50.0, RESETS + 1]]
        target = NOW + timedelta(hours=20)
        got = codex_burn.projection(hist, NOW, RESETS, 4.0, target)  # 2 points per 3 h, 20 h to go
        self.assertAlmostEqual(got, 4 + 2 / 3 * (target - NOW).total_seconds() / 3600)
        self.assertIsNone(codex_burn.projection(hist[2:3], NOW, RESETS, 4.0, target))
        self.assertIsNone(codex_burn.projection([[t - 600, 3.0, RESETS], [t, 4.0, RESETS]], NOW, RESETS, 4.0, target))

    def test_section_text_and_unreadable(self):
        text = codex_burn.section(tick, NOW, obs(4), [[NOW.timestamp() - 3600, 3.0, RESETS], [NOW.timestamp(), 4.0, RESETS]])
        slots = f"并行 {tick.CODEX_PARALLEL_SLOTS} 槽"
        for needle in ("## Codex 燃烧", "4%", "11-02 01:24", "落后 3.4 点", slots, "预计用到", "≥36 小时", "不按，让它过期"):
            self.assertIn(needle, text)
        self.assertIn("读不到", codex_burn.section(tick, NOW, {}, []))
        self.assertIn(slots, codex_burn.section(tick, NOW, obs(50), []))
        self.assertIn("样本不足", codex_burn.section(tick, NOW, obs(50), []))

    def test_reset_net_lists_only_unexpired_cards_and_keeps_negative_points(self):
        resets = datetime.fromisoformat("2026-11-02T01:24:00-05:00").timestamp()
        text = codex_burn.section(tick, NOW, obs(4, resets), [])
        self.assertIn("2026-11-30 15:00 -3.8 点", text)
        self.assertIn("2026-12-01 17:33 -3.8 点", text)
        self.assertIn("OpenAI 帮助页 20001507", text)

        after_first_card = datetime.fromisoformat("2026-11-30T15:00:00-05:00")
        resets = (after_first_card + timedelta(days=2)).timestamp()
        text = codex_burn.section(tick, after_first_card, obs(4, resets), [])
        self.assertNotIn("2026-11-30 15:00", text)
        self.assertIn("2026-12-01 17:33", text)

    def test_review_stats_build_appends_the_section_when_live(self):
        with mock.patch.object(review_stats, "render", return_value="BODY"), \
                mock.patch.object(review_stats, "branch_facts", return_value=[]), \
                mock.patch.object(review_stats, "owner_candidates", return_value=""), \
                mock.patch.object(review_stats, "diff_section", return_value=""), \
                mock.patch.object(codex_burn, "section", return_value="## Codex 燃烧\n") as sec:
            self.assertTrue(review_stats.build(tick).endswith("\n## Codex 燃烧\n"))
            sec.assert_called_once()
            self.assertEqual(review_stats.build(tick, now=NOW).strip(), "BODY")  # a replay does not read the live quota


class ResetAdvice(unittest.TestCase):
    NOW = datetime.fromisoformat("2026-11-02T09:00:00-05:00")

    def note(self, used, hours, now=None):
        now = now or self.NOW
        return codex_burn.reset_note(obs(used, now.timestamp() + hours * 3600), now)

    def test_high_usage_requires_at_least_36_hours_before_the_natural_reset(self):
        for hours in (50, 36):
            self.assertIn("建议现在按 11-30 到期那张", self.note(95, hours))
        for used, hours in ((95, 20), (95, 35.99), (94.9, 50)):
            self.assertEqual(self.note(used, hours), "")
        self.assertIn("净得约 24.7 点", self.note(95, 50))

    def test_expiry_day_overrides_the_high_usage_and_36_hour_thresholds(self):
        now = datetime.fromisoformat("2026-11-29T15:00:00-05:00")
        self.assertIn("建议现在按 11-30 到期那张", self.note(94, 20, now))
        self.assertIn("不按，让它过期", self.note(70, 20, now))
        self.assertIn("净得约 -18.2 点", self.note(70, 20, now))
        self.assertIn("不按，让它过期", self.note(57.2, 72, now))  # exactly zero
        self.assertEqual(self.note(94, 20, now - timedelta(seconds=1)), "")

    def test_second_card_reminder_uses_its_minutes_and_excludes_expired_cards(self):
        now = datetime.fromisoformat("2026-11-30T17:33:00-05:00")
        self.assertIn("建议现在按 12-01 到期那张", self.note(70, 72, now))
        self.assertEqual(self.note(70, 72, now - timedelta(seconds=1)), "")
        self.assertEqual(self.note(100, 72, now + timedelta(days=1)), "")

    def test_unreadable_or_expired_windows_never_recommend_pressing_a_card(self):
        for bad in (None, {}, obs(95, self.NOW.timestamp() - 1)):
            self.assertEqual(codex_burn.reset_note(bad, self.NOW), "")

    def test_review_stats_section_contains_the_same_action_as_the_report(self):
        observation = obs(95, self.NOW.timestamp() + 50 * 3600)
        self.assertIn(codex_burn.reset_note(observation, self.NOW),
                      codex_burn.section(tick, self.NOW, observation, []))


class BurnSlots(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(tick.CODEX_PROBE, at=time.time(), used=4.0, behind=None)
        p.start()
        self.addCleanup(p.stop)
        # the burn switch is tested with the 10-26 values; the live values are 20 everywhere since 11-01
        c = mock.patch.multiple(tick, CODEX_SLOTS=3, CODEX_PARALLEL_SLOTS=3, CODEX_BURN_SLOTS=5, CODEX_LOCKS=5)
        c.start()
        self.addCleanup(c.stop)

    def slots(self, behind, used=4.0):
        tick.CODEX_PROBE.update(at=time.time(), used=used, behind=behind)
        return tick.codex_parallel_slots()

    def test_five_slots_only_when_ten_or_more_points_behind(self):
        self.assertEqual((tick.CODEX_PARALLEL_SLOTS, tick.CODEX_BURN_SLOTS), (3, 5))
        self.assertEqual(self.slots(15.0), 5)
        self.assertEqual(self.slots(10.0), 5)
        self.assertEqual(self.slots(5.0), 3)
        self.assertEqual(self.slots(-20.0), 3)  # ahead of the line
        self.assertEqual(self.slots(None, used=None), 3)  # unreadable

    def test_probe_derives_behind_from_the_real_observation(self):
        tick.CODEX_PROBE.update(at=-1e9, used=None, behind=None)
        # window 5 days old, nothing used: far behind whatever the target is
        with mock.patch.object(tick.shadow_review, "observe_quota", return_value=obs(0, resets=time.time() + 2 * 86400)):
            self.assertEqual(tick.codex_parallel_slots(), 5)
            self.assertGreaterEqual(tick.CODEX_PROBE["behind"], 10)
        tick.CODEX_PROBE.update(at=-1e9, used=None, behind=None)
        with mock.patch.object(tick.shadow_review, "observe_quota", side_effect=RuntimeError("wsl down")), mock.patch.object(tick, "log"):
            self.assertEqual(tick.codex_parallel_slots(), 3)

    def test_codex_fallback_fills_five_slots_when_behind_but_three_otherwise(self):
        for behind, live_count, spawned in ((15.0, 3, True), (15.0, 5, False), (5.0, 3, False), (None, 2, True)):
            tick.CODEX_PROBE.update(at=time.time(), used=4.0, behind=behind)
            flags = [True] * live_count + [False] * (tick.CODEX_LOCKS - live_count)
            with mock.patch.object(tick, "lock_live", side_effect=lambda p, f=iter(flags * 4): next(f)), \
                    mock.patch.object(tick, "load_runs", return_value=[]), mock.patch.object(tick, "codex_pick", return_value=("work", "delta")), \
                    mock.patch.object(tick, "spawn") as spawn, mock.patch.object(tick, "log"):
                tick.codex_fallback({"aux": {}, "spawned": {}}, 1e9, "", set(), tick.codex_parallel_slots())
            self.assertEqual(spawn.called, spawned, (behind, live_count))

    def test_sol_first_takes_a_fourth_slot_only_while_behind(self):
        for behind, spawned in ((15.0, True), (5.0, False), (None, False)):
            tick.CODEX_PROBE.update(at=time.time(), used=4.0, behind=behind)
            flags = [True] * 3 + [False] * (tick.CODEX_LOCKS - 3)
            row = ("work", "Arbitros 线", "- [review] x", "x", "rid1")
            with mock.patch.object(tick, "lock_live", side_effect=lambda p, f=iter(flags * 4): next(f)), \
                    mock.patch.object(tick, "load_runs", return_value=[]), mock.patch.object(tick, "log"), mock.patch.object(tick, "spawn") as spawn, \
                    mock.patch.object(tick.shadow_review, "candidates", return_value=[row]), \
                    mock.patch.object(tick, "parse_backlog", return_value={}), mock.patch.object(tick, "BACKLOG", mock.Mock(read_text=lambda **k: "")):
                self.assertEqual(tick.sol_first_spawn({"aux": {}}, 1e9, ""), spawned, behind)
            self.assertEqual(spawn.called, spawned)


class WiderSolFirst(unittest.TestCase):
    ITEM = "Delta：给 owner 试用的小修"
    packet = {"text": "PRE", "hard": None, "facts": {"repo": None, "refs": {}, "base": None,
                                                      "changed": ["control/tick.py"], "result_files": ["r.md"]}}

    def ex(self, **kw):
        return {"ok": True, "engine": "codex", "project": "delta", "item": self.ITEM, **kw}

    def why(self, burning, role="work", **kw):
        with mock.patch.object(tick, "codex_burning", return_value=burning):
            return shadow.skip_reason(tick, role, self.ITEM, self.packet, self.ex(**kw))

    def test_widens_for_codex_rows_while_behind_and_returns_when_caught_up(self):
        self.assertIsNone(self.why(True))
        self.assertIn("给", self.why(False))  # owner-facing words: the deputy reviews it
        packet = {**self.packet, "facts": {**self.packet["facts"], "changed": ["src.py"]}}
        with mock.patch.object(tick, "codex_burning", return_value=False):
            self.assertIn("给", shadow.skip_reason(tick, "work", self.ITEM, packet, self.ex()))
            self.assertIn("tick.py", shadow.skip_reason(tick, "work", "Delta：小修", self.packet, self.ex(item="Delta：小修")))
        with mock.patch.object(tick, "codex_burning", return_value=True):
            self.assertIsNone(shadow.skip_reason(tick, "work", "Delta：小修", self.packet, self.ex(item="Delta：小修")))

    def test_quant_claude_rows_and_hard_limits_are_never_widened(self):
        self.assertEqual(self.why(True, role="quant"), "量化线")
        self.assertEqual(self.why(True, project="quant"), "量化线")
        self.assertIn("给", self.why(True, engine="claude"))  # only Codex-executed rows
        with mock.patch.object(tick, "codex_burning", return_value=True):
            self.assertEqual(shadow.skip_reason(tick, "work", self.ITEM + tick.OPUS_TAG, self.packet, self.ex()), "〔执行者：Opus〕")
            merge = {**self.packet, "facts": {**self.packet["facts"], "repo": "C:/x/Thinking Engines"}}
            self.assertIn("可合并", shadow.skip_reason(tick, "work", self.ITEM, merge, self.ex()))
            protected = {**self.packet, "facts": {**self.packet["facts"], "changed": ["records/a.json"]}}
            self.assertIn("records/a.json", shadow.skip_reason(tick, "work", self.ITEM, protected, self.ex()) or "")

    def test_candidates_include_owner_facing_codex_rows_only_while_behind(self):
        secs = {"Arbitros 线": {"review_rows": [("- [review] " + self.ITEM, self.ITEM)]}}
        run = {"kind": "executor", "role": "work", "item": self.ITEM, "ok": True, "engine": "codex", "project": "delta",
               "_at": NOW, "at": NOW.isoformat(), "run_file": "e1.jsonl"}
        with mock.patch.object(shadow, "load_state", return_value={}):
            with mock.patch.object(tick, "codex_burning", return_value=False):
                self.assertEqual(shadow.candidates(tick, secs, [run]), [])
            with mock.patch.object(tick, "codex_burning", return_value=True):
                self.assertEqual(len(shadow.candidates(tick, secs, [run])), 1)
                self.assertEqual(shadow.candidates(tick, secs, [{**run, "engine": "claude"}]), [])
                self.assertEqual(shadow.candidates(tick, secs, [{**run, "project": "quant"}]), [])
            quant = {"量化线": {"review_rows": [("- [review] q", "q")]}}
            with mock.patch.object(tick, "codex_burning", return_value=True):
                self.assertEqual(shadow.candidates(tick, quant, [{**run, "role": "quant", "item": "q"}]), [])


if __name__ == "__main__":
    unittest.main()
