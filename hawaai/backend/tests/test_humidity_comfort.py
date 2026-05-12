"""Tests for passive humidity comfort intelligence."""

import os
import sys
import unittest
from datetime import datetime, timezone

_HAWAAI = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _HAWAAI not in sys.path:
    sys.path.insert(0, _HAWAAI)

from backend.humidity_comfort import (  # noqa: E402
    calculate_humidity_comfort,
    dew_point_celsius,
    valid_humidity_percent,
)
from backend.sleep_optimizer import calculate_sleep_adjustment  # noqa: E402


def _cfg(**overrides):
    base = {
        "timezone": "UTC",
        "humidity_comfort_enabled": True,
        "humidity_ideal_min": 40,
        "humidity_ideal_max": 60,
        "humidity_warning_threshold": 65,
        "humidity_critical_threshold": 75,
        "humidity_min_offset": -1.0,
        "humidity_max_offset": 0.5,
        "sleep_optimization_enabled": True,
        "sleep_start_hour": 22,
        "sleep_end_hour": 6,
        "sleep_max_offset": 1.5,
        "sleep_curve_mode": "gradual",
    }
    base.update(overrides)
    return base


class TestHumidityComfort(unittest.TestCase):
    def test_missing_humidity_sensor(self):
        res = calculate_humidity_comfort(
            _cfg(),
            indoor_temp=26.0,
            target_temp=26.0,
            humidity_percent=None,
        )
        self.assertFalse(res.active)
        self.assertEqual(res.reason, "no_valid_humidity")
        self.assertAlmostEqual(res.adjusted_target, 26.0)

    def test_invalid_humidity_values(self):
        self.assertIsNone(valid_humidity_percent(-1))
        self.assertIsNone(valid_humidity_percent(101))
        self.assertIsNone(valid_humidity_percent("unavailable"))

        res = calculate_humidity_comfort(
            _cfg(),
            indoor_temp=26.0,
            target_temp=26.0,
            humidity_percent=150,
        )
        self.assertFalse(res.active)
        self.assertAlmostEqual(res.humidity_offset, 0.0)

    def test_high_humidity_offset(self):
        res = calculate_humidity_comfort(
            _cfg(),
            indoor_temp=26.0,
            target_temp=26.0,
            humidity_percent=80.0,
            ac_on=True,
        )
        self.assertTrue(res.active)
        self.assertEqual(res.humidity_band, "critical")
        self.assertAlmostEqual(res.humidity_offset, -0.5)
        self.assertAlmostEqual(res.adjusted_target, 25.5)
        self.assertGreater(res.feels_like_temp, 28.0)

    def test_low_humidity_offset(self):
        res = calculate_humidity_comfort(
            _cfg(),
            indoor_temp=26.0,
            target_temp=26.0,
            humidity_percent=45.0,
        )
        self.assertEqual(res.humidity_band, "ideal")
        self.assertAlmostEqual(res.humidity_offset, 0.5)
        self.assertAlmostEqual(res.adjusted_target, 26.5)

    def test_dry_mode_recommendation(self):
        res = calculate_humidity_comfort(
            _cfg(),
            indoor_temp=26.0,
            target_temp=26.0,
            humidity_percent=80.0,
            ac_on=True,
        )
        self.assertTrue(res.dry_mode_recommended)

    def test_dew_point_calculation(self):
        self.assertAlmostEqual(dew_point_celsius(26.0, 80.0), 22.3, delta=0.3)

    def test_offset_clamping(self):
        humid = calculate_humidity_comfort(
            _cfg(humidity_min_offset=-0.25),
            indoor_temp=28.0,
            target_temp=26.0,
            humidity_percent=95.0,
        )
        dry = calculate_humidity_comfort(
            _cfg(humidity_max_offset=0.25),
            indoor_temp=26.0,
            target_temp=26.0,
            humidity_percent=35.0,
        )
        self.assertAlmostEqual(humid.humidity_offset, -0.25)
        self.assertAlmostEqual(dry.humidity_offset, 0.25)

    def test_disabled_mode(self):
        res = calculate_humidity_comfort(
            _cfg(humidity_comfort_enabled=False),
            indoor_temp=26.0,
            target_temp=26.0,
            humidity_percent=80.0,
        )
        self.assertFalse(res.active)
        self.assertEqual(res.reason, "disabled")
        self.assertAlmostEqual(res.adjusted_target, 26.0)

    def test_no_sensor_fallback(self):
        res = calculate_humidity_comfort(
            _cfg(humidity_entity_id=""),
            indoor_temp=24.0,
            target_temp=24.0,
            humidity_percent=None,
        )
        self.assertEqual(res.humidity_band, "unavailable")
        self.assertAlmostEqual(res.humidity_offset, 0.0)

    def test_restart_consistency(self):
        kwargs = {
            "indoor_temp": 26.0,
            "target_temp": 26.0,
            "humidity_percent": 80.0,
            "ac_on": True,
        }
        self.assertEqual(
            calculate_humidity_comfort(_cfg(), **kwargs),
            calculate_humidity_comfort(_cfg(), **kwargs),
        )

    def test_schedule_sleep_humidity_stacking(self):
        sleep = calculate_sleep_adjustment(
            _cfg(),
            current_time=datetime(2026, 5, 12, 4, 0, tzinfo=timezone.utc),
            target_temp=24.0,
            indoor_temp=26.0,
            user_manual_target=30.0,
        )
        humid = calculate_humidity_comfort(
            _cfg(),
            indoor_temp=26.0,
            target_temp=sleep.adjusted_target,
            humidity_percent=80.0,
            ac_on=True,
        )
        self.assertAlmostEqual(sleep.adjusted_target, 25.5)
        self.assertAlmostEqual(humid.humidity_offset, -0.5)
        self.assertAlmostEqual(humid.adjusted_target, 25.0)


if __name__ == "__main__":
    unittest.main()
