"""Adaptive room thermal-load comfort remains passive and bounded."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

_HAWAAI = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _HAWAAI not in sys.path:
    sys.path.insert(0, _HAWAAI)

from backend import logic_engine as le  # noqa: E402


class TestThermalLoadComfort(unittest.TestCase):
    def tearDown(self):
        for room_id in ("thermal-high", "thermal-saturated"):
            le._runtime_by_room.pop(room_id, None)

    def test_sustained_high_load_applies_bounded_comfort_offset(self):
        room_id = "thermal-high"
        now = datetime.now(timezone.utc)
        st = le._rt(room_id)
        st.thermal_load_candidate_since = now - timedelta(minutes=8)
        st.thermal_load_last_high_at = now - timedelta(seconds=30)

        adjusted = le._apply_thermal_load_comfort_layer(
            room_id,
            {"adaptive_thermal_load_enabled": True},
            now=now,
            indoor_temp=30.0,
            outdoor_temp=44.0,
            humidity_percent=76.0,
            target_before_thermal=24.0,
            ac_on=True,
            occupied=True,
            climate_data={"target_temp": 24.0, "fan_mode": "medium"},
            log_change=False,
        )

        self.assertEqual(adjusted, 23.0)
        self.assertEqual(st.thermal_load_level, "high")
        self.assertEqual(st.thermal_load_confidence, "high")
        self.assertEqual(st.thermal_load_compensation_offset, -1.0)
        self.assertTrue(st.thermal_load_compensation_active)
        self.assertFalse(st.cooling_saturated)

    def test_saturation_prevents_extra_compensation(self):
        room_id = "thermal-saturated"
        now = datetime.now(timezone.utc)
        st = le._rt(room_id)
        st.thermal_load_candidate_since = now - timedelta(minutes=10)

        adjusted = le._apply_thermal_load_comfort_layer(
            room_id,
            {"adaptive_thermal_load_enabled": True},
            now=now,
            indoor_temp=29.0,
            outdoor_temp=43.0,
            humidity_percent=70.0,
            target_before_thermal=16.0,
            ac_on=True,
            occupied=True,
            climate_data={"target_temp": 16.0, "fan_mode": "high"},
            log_change=False,
        )

        self.assertEqual(adjusted, 16.0)
        self.assertEqual(st.thermal_load_compensation_offset, 0.0)
        self.assertFalse(st.thermal_load_compensation_active)
        self.assertTrue(st.cooling_saturated)


if __name__ == "__main__":
    unittest.main()
