"""Tests for passive sleep target relaxation."""

import os
import sys
import unittest
from datetime import datetime, timezone

_HAWAAI = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _HAWAAI not in sys.path:
    sys.path.insert(0, _HAWAAI)

from backend.sleep_optimizer import calculate_sleep_adjustment  # noqa: E402


def _cfg(**overrides):
    base = {
        "timezone": "UTC",
        "sleep_optimization_enabled": True,
        "sleep_start_hour": 22,
        "sleep_end_hour": 6,
        "sleep_max_offset": 1.5,
        "sleep_curve_mode": "gradual",
    }
    base.update(overrides)
    return base


def _at(hour, minute=0):
    return datetime(2026, 5, 12, hour, minute, tzinfo=timezone.utc)


class TestSleepOptimizer(unittest.TestCase):
    def test_overnight_offset_progression(self):
        cases = [
            (_at(22, 30), 0.0, "settling"),
            (_at(0, 0), 0.5, "deep_sleep"),
            (_at(2, 0), 1.0, "late_sleep"),
            (_at(4, 0), 1.5, "pre_wake"),
        ]
        for now, offset, phase in cases:
            res = calculate_sleep_adjustment(
                _cfg(),
                current_time=now,
                target_temp=24.0,
                indoor_temp=25.0,
                user_manual_target=30.0,
            )
            self.assertTrue(res.active)
            self.assertAlmostEqual(res.offset, offset)
            self.assertAlmostEqual(res.adjusted_target, 24.0 + offset)
            self.assertEqual(res.phase, phase)

    def test_boundary_times(self):
        start = calculate_sleep_adjustment(
            _cfg(),
            current_time=_at(22, 0),
            target_temp=24.0,
            indoor_temp=24.0,
            user_manual_target=30.0,
        )
        end = calculate_sleep_adjustment(
            _cfg(),
            current_time=_at(6, 0),
            target_temp=24.0,
            indoor_temp=24.0,
            user_manual_target=30.0,
        )
        self.assertTrue(start.active)
        self.assertAlmostEqual(start.offset, 0.0)
        self.assertFalse(end.active)
        self.assertEqual(end.phase, "outside_sleep")

    def test_emergency_suspend_behavior(self):
        res = calculate_sleep_adjustment(
            _cfg(sleep_emergency_margin=4.0),
            current_time=_at(4, 0),
            target_temp=25.0,
            indoor_temp=31.0,
            user_manual_target=30.0,
        )
        self.assertFalse(res.active)
        self.assertEqual(res.suspended, "high_heat")
        self.assertAlmostEqual(res.offset, 0.0)
        self.assertAlmostEqual(res.adjusted_target, 25.0)

    def test_disabled_mode(self):
        res = calculate_sleep_adjustment(
            _cfg(sleep_optimization_enabled=False),
            current_time=_at(4, 0),
            target_temp=24.0,
            indoor_temp=24.0,
            user_manual_target=30.0,
        )
        self.assertFalse(res.active)
        self.assertEqual(res.phase, "disabled")
        self.assertAlmostEqual(res.adjusted_target, 24.0)

    def test_no_effect_outside_sleep_hours(self):
        res = calculate_sleep_adjustment(
            _cfg(),
            current_time=_at(12, 0),
            target_temp=24.0,
            indoor_temp=24.0,
            user_manual_target=30.0,
        )
        self.assertFalse(res.active)
        self.assertEqual(res.phase, "outside_sleep")
        self.assertAlmostEqual(res.offset, 0.0)

    def test_max_offset_clamping(self):
        res = calculate_sleep_adjustment(
            _cfg(sleep_max_offset=0.6),
            current_time=_at(4, 0),
            target_temp=24.0,
            indoor_temp=24.0,
            user_manual_target=30.0,
        )
        self.assertAlmostEqual(res.offset, 0.6)
        self.assertAlmostEqual(res.adjusted_target, 24.6)

    def test_interaction_with_manual_mode_cap(self):
        res = calculate_sleep_adjustment(
            _cfg(temperature_mode="manual"),
            current_time=_at(4, 0),
            target_temp=24.0,
            indoor_temp=24.0,
            user_manual_target=24.0,
        )
        self.assertTrue(res.active)
        self.assertAlmostEqual(res.offset, 0.0)
        self.assertAlmostEqual(res.adjusted_target, 24.0)

    def test_interaction_with_schedule_ai_mode(self):
        res = calculate_sleep_adjustment(
            _cfg(temperature_mode="schedule_ai"),
            current_time=_at(4, 0),
            target_temp=24.0,
            indoor_temp=24.0,
            user_manual_target=26.0,
        )
        self.assertTrue(res.active)
        self.assertAlmostEqual(res.offset, 1.5)
        self.assertAlmostEqual(res.adjusted_target, 25.5)


if __name__ == "__main__":
    unittest.main()
