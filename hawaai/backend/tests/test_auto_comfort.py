import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

_BACK = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _BACK not in sys.path:
    sys.path.insert(0, _BACK)

from auto_comfort import (
    DEFAULT_MAX_TARGET_C,
    DEFAULT_MIN_TARGET_C,
    evaluate_cooling_effectiveness,
    resolve_auto_comfort_target,
)


def _decision(**kwargs):
    now = kwargs.pop("now", datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc))
    base = {
        "now": now,
        "base_target": 23.0,
        "base_source": "room_target",
        "indoor_temp": 29.0,
        "outdoor_temp": 42.0,
        "humidity_percent": 70.0,
        "occupied": True,
        "ac_on": True,
        "thermal_load_level": "medium",
        "thermal_load_confidence": "medium",
        "cooling_saturated": False,
        "cooling_effectiveness": "unknown",
        "learned_band": "evening",
        "learned_offset": 0.0,
        "learned_sample_count": 0,
        "runtime_sample_count": 40,
    }
    base.update(kwargs)
    return resolve_auto_comfort_target({}, **base)


class TestAutoComfortResolver(unittest.TestCase):
    def test_defaults_are_comfort_first(self):
        decision = resolve_auto_comfort_target(
            {},
            now=datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc),
            base_target=30.0,
            base_source="fallback",
            indoor_temp=32.0,
            outdoor_temp=None,
            humidity_percent=None,
            occupied=True,
            ac_on=False,
            thermal_load_level="low",
            thermal_load_confidence="low",
            cooling_saturated=False,
            cooling_effectiveness="unknown",
            learned_band="morning",
        )
        self.assertGreaterEqual(decision.target, DEFAULT_MIN_TARGET_C)
        self.assertLessEqual(decision.target, DEFAULT_MAX_TARGET_C)
        self.assertEqual(decision.profile, "comfort")

    def test_extreme_heat_lowers_target_gently_and_within_bounds(self):
        decision = _decision(
            base_target=23.0,
            outdoor_temp=44.0,
            thermal_load_level="high",
            thermal_load_confidence="high",
            runtime_sample_count=40,
        )
        self.assertLessEqual(decision.target, 23.0)
        self.assertGreaterEqual(decision.target, 21.0)
        self.assertEqual(decision.confidence, "medium")

    def test_missing_room_temp_degrades_without_crash(self):
        decision = _decision(indoor_temp=None, previous_target=22.0)
        self.assertEqual(decision.status, "degraded")
        self.assertEqual(decision.reason, "room_temp_sensor_required")
        self.assertAlmostEqual(decision.target, 22.0)

    def test_saturation_blocks_extra_thermal_and_cooling_offsets(self):
        decision = _decision(
            base_target=17.0,
            cooling_saturated=True,
            thermal_load_level="high",
            thermal_load_confidence="high",
            cooling_effectiveness="poor",
        )
        self.assertEqual(decision.status, "saturated")
        self.assertEqual(decision.thermal_load_offset, 0.0)
        self.assertEqual(decision.cooling_effectiveness_offset, 0.0)
        self.assertIn("cooling_headroom_exhausted", decision.warnings)

    def test_max_step_enforced(self):
        decision = _decision(
            base_target=23.0,
            outdoor_temp=44.0,
            thermal_load_level="high",
            thermal_load_confidence="high",
            previous_target=24.0,
            previous_target_at=datetime(2026, 5, 28, 9, 0, tzinfo=timezone.utc),
        )
        self.assertAlmostEqual(decision.target, 23.5)
        self.assertTrue(decision.capped_by_step)

    def test_min_change_window_holds_small_target_changes(self):
        now = datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc)
        decision = _decision(
            now=now,
            outdoor_temp=36.0,
            humidity_percent=50.0,
            thermal_load_level="low",
            previous_target=23.0,
            previous_target_at=now - timedelta(minutes=4),
        )
        self.assertTrue(decision.held_previous)
        self.assertAlmostEqual(decision.target, 23.0)

    def test_learning_requires_applied_samples_for_confidence(self):
        learning = _decision(runtime_sample_count=0, learned_sample_count=2)
        medium = _decision(runtime_sample_count=0, learned_sample_count=3)
        self.assertEqual(learning.confidence, "learning")
        self.assertEqual(medium.confidence, "medium")

    def test_cooling_effectiveness_statuses(self):
        self.assertEqual(
            evaluate_cooling_effectiveness(
                ac_on=False,
                elapsed_seconds=None,
                start_temp=None,
                current_temp=None,
                target_gap=None,
                outdoor_temp=None,
                humidity_percent=None,
                cooling_saturated=False,
            ).status,
            "unknown",
        )
        self.assertEqual(
            evaluate_cooling_effectiveness(
                ac_on=True,
                elapsed_seconds=1200,
                start_temp=29.0,
                current_temp=27.5,
                target_gap=4.0,
                outdoor_temp=40.0,
                humidity_percent=55.0,
                cooling_saturated=False,
            ).status,
            "good",
        )
        self.assertEqual(
            evaluate_cooling_effectiveness(
                ac_on=True,
                elapsed_seconds=1200,
                start_temp=29.0,
                current_temp=29.2,
                target_gap=4.0,
                outdoor_temp=43.0,
                humidity_percent=70.0,
                cooling_saturated=False,
            ).status,
            "poor",
        )


if __name__ == "__main__":
    unittest.main()
