import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orgcore import config, usage
from orgcore.usage import FakeUsage, JsonFileUsage, Observation, UsageUnavailable

from .support import InstanceCase

NOW = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)
POLICY = {
    "live_enabled": False,
    "subscription_windows": [
        {"name": "five_hour", "reserve_percent": 30, "max_share_percent": 50, "max_observation_age_minutes": 15},
    ],
}


def reading(used=10, age_min=1, resets_in_h=3, synthetic=False, window="five_hour", source="adapter-x"):
    return Observation(window, used, NOW - timedelta(minutes=age_min), NOW + timedelta(hours=resets_in_h), source, synthetic)


class Gate(unittest.TestCase):
    def blocked(self, readings, policy=POLICY):
        gate = usage.evaluate(policy, readings, NOW)
        self.assertFalse(gate.allowed)
        return " | ".join(gate.reasons)

    def test_healthy_reading_passes(self):
        self.assertTrue(usage.evaluate(POLICY, [reading()], NOW).allowed)

    def test_unknown_is_not_zero(self):
        self.assertIn("unknown, no observation", self.blocked([]))
        self.assertIn("unknown", self.blocked([reading(window="other_window")]))

    def test_stale_reset_and_future_readings_block_and_say_why(self):
        self.assertIn("stale, observed 16 min ago by 'adapter-x'", self.blocked([reading(age_min=16)]))
        self.assertIn("predates the window reset", self.blocked([reading(resets_in_h=0)]))
        self.assertIn("dated in the future", self.blocked([reading(age_min=-5)]))

    def test_reserve_and_share_limits(self):
        policy = json.loads(json.dumps(POLICY))
        policy["subscription_windows"][0].update(reserve_percent=60, max_share_percent=90)
        self.assertIn("reserve boundary (40%)", self.blocked([reading(used=40)], policy))
        self.assertIn("maximum share (50%)", self.blocked([reading(used=50)]))
        self.assertTrue(usage.evaluate(POLICY, [reading(used=49.9)], NOW).allowed)

    def test_pacing_line_is_configurable(self):
        policy = json.loads(json.dumps(POLICY))
        policy["subscription_windows"][0].update(length_hours=5, pacing_slack_percent=5)
        # resets in 3h of a 5h window: 40% elapsed, pace = 50 * 0.4 + 5 = 25
        self.assertIn("ahead of the pace line (25.0%)", self.blocked([reading(used=30)], policy))
        self.assertTrue(usage.evaluate(policy, [reading(used=24)], NOW).allowed)

    def test_newest_reading_per_window_wins(self):
        self.assertTrue(usage.evaluate(POLICY, [reading(used=90, age_min=10), reading(used=5, age_min=1)], NOW).allowed)

    def test_synthetic_only_counts_outside_live_mode(self):
        self.assertTrue(usage.evaluate(POLICY, [reading(synthetic=True)], NOW).allowed)
        live = dict(POLICY, live_enabled=True)
        self.assertIn("not accepted in live mode", self.blocked([reading(synthetic=True)], live))

    def test_every_window_must_pass(self):
        policy = json.loads(json.dumps(POLICY))
        policy["subscription_windows"].append(dict(policy["subscription_windows"][0], name="seven_day"))
        text = self.blocked([reading(), reading(window="seven_day", used=80)], policy)
        self.assertIn("seven_day", text)
        self.assertNotIn("five_hour", text)


class Sources(InstanceCase):
    def test_json_file_source_roundtrip_and_failures(self):
        path = self.tmp / "usage.json"
        with self.assertRaises(UsageUnavailable) as caught:
            JsonFileUsage(path).observe()
        self.assertIn("not found", str(caught.exception))
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(UsageUnavailable):
            JsonFileUsage(path).observe()
        path.write_text(json.dumps({"observations": [
            {"window": "five_hour", "used_percent": 12.5, "observed_at": "2026-01-05T11:59:00Z",
             "resets_at": "2026-01-05T15:00:00Z", "source": "file"}]}), encoding="utf-8")
        [seen] = JsonFileUsage(path).observe()
        self.assertEqual((seen.window, seen.used_percent, seen.source, seen.synthetic), ("five_hour", 12.5, "file", False))
        path.write_text(json.dumps({"observations": [{"window": "five_hour", "used_percent": 120}]}), encoding="utf-8")
        with self.assertRaises(UsageUnavailable):
            JsonFileUsage(path).observe()

    def test_reading_needs_source_and_valid_percent(self):
        with self.assertRaises(UsageUnavailable):
            reading(source="")
        with self.assertRaises(UsageUnavailable):
            reading(used=101)

    def test_healthy_fake_covers_every_configured_window(self):
        readings = FakeUsage.healthy(self.instance().policy, NOW).observe()
        self.assertEqual({r.window for r in readings}, {"five_hour", "seven_day"})
        self.assertTrue(all(r.synthetic and r.source for r in readings))

    def test_pacing_fields_must_come_together(self):
        policy = json.loads((self.root / "policy.json").read_text(encoding="utf-8"))
        policy["subscription_windows"][0]["length_hours"] = 5
        with self.assertRaises(config.ConfigError) as caught:
            config.validate("policy.json", policy)
        self.assertIn("go together", str(caught.exception))
        policy["subscription_windows"][0]["pacing_slack_percent"] = 5
        config.validate("policy.json", policy)


if __name__ == "__main__":
    unittest.main()
