import asyncio
from datetime import datetime, timedelta, timezone
from unittest import TestCase, mock

from backend import logic_engine


def _lg_cfg(**overrides):
    cfg = {
        "lg_fan_guard_enabled": True,
        "fan_guard_profile": "lg_f1_f5_turbo",
        "auto_turbo_allowed": False,
        "allow_manual_turbo": True,
        "default_safe_fan_mode": "f3",
        "preserve_last_non_turbo_fan": True,
        "turbo_auto_timeout_minutes": 10,
    }
    cfg.update(overrides)
    return cfg


class LGFanGuardTests(TestCase):
    def setUp(self):
        logic_engine._runtime_by_room.clear()

    def test_f1_f5_allowed_for_automation_and_saved_as_last_normal(self):
        cfg = _lg_cfg()
        for fan in ("F1", "F2", "F3", "F4", "F5"):
            rid = f"lg-{fan.lower()}"
            guarded = logic_engine.guard_automation_fan_mode(rid, cfg, fan, "schedule")
            self.assertEqual(guarded, fan)
            self.assertEqual(logic_engine._rt(rid).last_non_turbo_fan_mode, fan)

    def test_turbo_blocked_for_automation_uses_f3_then_last_fan(self):
        cfg = _lg_cfg()
        rid = "lg-block"

        self.assertEqual(
            logic_engine.guard_automation_fan_mode(rid, cfg, "Turbo", "auto_comfort"),
            "f3",
        )
        self.assertIsNone(logic_engine._rt(rid).last_non_turbo_fan_mode)

        logic_engine.guard_automation_fan_mode(rid, cfg, "F2", "schedule")
        self.assertEqual(
            logic_engine.guard_automation_fan_mode(rid, cfg, "turbo", "schedule_ai"),
            "F2",
        )
        self.assertEqual(logic_engine._rt(rid).last_non_turbo_fan_mode, "F2")

    def test_manual_turbo_allowed_and_not_saved_as_last_non_turbo(self):
        cfg = _lg_cfg()
        rid = "lg-manual"

        logic_engine.record_user_fan_command(rid, cfg, "F4")
        logic_engine.record_user_fan_command(rid, cfg, "Turbo")

        st = logic_engine._rt(rid)
        self.assertTrue(st.turbo_user_active)
        self.assertIsNotNone(st.turbo_started_at)
        self.assertEqual(st.last_non_turbo_fan_mode, "F4")
        self.assertIsNone(
            logic_engine.guard_automation_fan_mode(rid, cfg, "F3", "smart_fan")
        )

    def test_timeout_restores_last_fan_or_f3(self):
        async def run_case():
            cfg = _lg_cfg()
            rid = "lg-timeout"
            st = logic_engine._rt(rid)
            st.last_non_turbo_fan_mode = "F5"
            st.turbo_user_active = True
            st.turbo_started_at = datetime.now(timezone.utc) - timedelta(minutes=11)

            with mock.patch.object(logic_engine.ha_client, "call_service", new=mock.AsyncMock(return_value=True)) as call:
                restored = await logic_engine.maybe_restore_lg_turbo_timeout(
                    rid,
                    cfg,
                    "climate.lg",
                    datetime.now(timezone.utc),
                )

            self.assertEqual(restored, "F5")
            call.assert_awaited_once_with(
                "climate",
                "set_fan_mode",
                {"entity_id": "climate.lg", "fan_mode": "F5"},
            )
            self.assertFalse(st.turbo_user_active)
            self.assertIsNone(st.turbo_started_at)

            rid_default = "lg-timeout-default"
            st_default = logic_engine._rt(rid_default)
            st_default.turbo_user_active = True
            st_default.turbo_started_at = datetime.now(timezone.utc) - timedelta(minutes=11)
            with mock.patch.object(logic_engine.ha_client, "call_service", new=mock.AsyncMock(return_value=True)):
                restored_default = await logic_engine.maybe_restore_lg_turbo_timeout(
                    rid_default,
                    cfg,
                    "climate.lg",
                    datetime.now(timezone.utc),
                )
            self.assertEqual(restored_default, "f3")

        asyncio.run(run_case())

    def test_18_degrees_allowed_with_f1_f5(self):
        cfg = _lg_cfg(target_temp=18)
        for fan in ("F1", "F2", "F3", "F4", "F5"):
            self.assertEqual(
                logic_engine.guard_automation_fan_mode(f"lg-18-{fan}", cfg, fan, "pre_cool"),
                fan,
            )

    def test_other_ac_profiles_unaffected(self):
        cfg = {
            "lg_fan_guard_enabled": False,
            "fan_guard_profile": "other",
            "default_safe_fan_mode": "f3",
        }
        rid = "other-ac"
        self.assertEqual(
            logic_engine.guard_automation_fan_mode(rid, cfg, "Turbo", "schedule"),
            "Turbo",
        )
        st = logic_engine._rt(rid)
        self.assertFalse(st.turbo_user_active)
        self.assertIsNone(st.last_non_turbo_fan_mode)
