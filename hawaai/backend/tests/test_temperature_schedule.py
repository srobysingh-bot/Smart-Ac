"""Tests for fixed time slots, mode resolution, and AI ±1 °C envelope."""

import os
import sys
import unittest
from datetime import datetime

_BACK = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _BACK not in sys.path:
    sys.path.insert(0, _BACK)

from datetime import datetime, timezone

from temperature_schedule import (
    AI_SCHEDULE_MAX_DELTA_C,
    apply_ai_bounded_adjustment,
    ensure_temperature_schedule_defaults,
    get_time_slot,
    normalize_temperature_mode,
    resolve_base_target_temp,
)


class TestTimeSlots(unittest.TestCase):
    def test_boundaries_local(self):
        utc = timezone.utc

        cases = [
            ((5, 59), "night"),
            ((6, 0), "morning"),
            ((11, 59), "morning"),
            ((12, 0), "afternoon"),
            ((16, 59), "afternoon"),
            ((17, 0), "evening"),
            ((21, 59), "evening"),
            ((22, 0), "night"),
            ((23, 30), "night"),
        ]
        for (h, mn), slot in cases:
            t = datetime(2026, 4, 29, h, mn, tzinfo=utc)
            self.assertEqual(get_time_slot(t), slot, f"hour={h}:{mn}")


class TestModesAndClamp(unittest.TestCase):
    def test_normalize_mode_aliases(self):
        self.assertEqual(normalize_temperature_mode("manual"), "manual")
        self.assertEqual(normalize_temperature_mode("Schedule"), "schedule")
        self.assertEqual(normalize_temperature_mode("schedule_ai"), "schedule_ai")
        self.assertEqual(normalize_temperature_mode("bogus"), "manual")

    def test_manual_uses_slider_target(self):
        cfg = {"target_temp": 23.0}
        ensure_temperature_schedule_defaults(cfg)
        t, lbl = resolve_base_target_temp(cfg)
        self.assertEqual(lbl, "manual")
        self.assertAlmostEqual(t, 23.0)

    def test_schedule_picks_slot_temp(self):
        cfg = {
            "target_temp": 24,
            "temperature_mode": "schedule",
            "schedule": {
                "morning_temp": 26.0,
                "afternoon_temp": 27.0,
                "evening_temp": 25.0,
                "night_temp": 22.0,
            },
            "timezone": "UTC",
        }
        ensure_temperature_schedule_defaults(cfg)
        noon = datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)
        t, lbl = resolve_base_target_temp(cfg, now_local=noon)
        self.assertEqual(lbl, "afternoon")
        self.assertAlmostEqual(t, 27.0)

    def test_ai_clamp_within_one_degree_of_effective_weather_curve(self):
        eff = 24.5
        self.assertAlmostEqual(
            apply_ai_bounded_adjustment(eff, eff + 10.0),
            eff + AI_SCHEDULE_MAX_DELTA_C,
        )
        self.assertAlmostEqual(
            apply_ai_bounded_adjustment(eff, eff - 10.0),
            eff - AI_SCHEDULE_MAX_DELTA_C,
        )
        self.assertAlmostEqual(
            apply_ai_bounded_adjustment(eff, eff + 0.4),
            eff + 0.4,
        )


class TestAIServiceImport(unittest.TestCase):
    """Prompt includes baseline envelope text (standalone load — avoids heavy ai/__init__)."""

    def test_build_prompt_accepts_baseline(self):
        import importlib.util

        path = os.path.join(_BACK, "ai", "ai_prompt.py")
        spec = importlib.util.spec_from_file_location("ai_prompt_standalone", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        text = mod.build_hvac_control_prompt(25.0, 32.0, True, baseline_deg_c=24.5)
        self.assertIn("baseline_effective_after_weather_c=24.5", text)


if __name__ == "__main__":
    unittest.main()
