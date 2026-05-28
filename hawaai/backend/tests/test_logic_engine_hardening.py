"""logic_engine: room id normalization, case-insensitive resolve, runtime state keys."""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_HAWAAI = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _HAWAAI not in sys.path:
    sys.path.insert(0, _HAWAAI)

from backend import logic_engine  # noqa: E402


class TestStartupStabilization(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        logic_engine.end_startup_stabilization()

    async def test_tick_uses_hydrate_only_during_startup_stabilization(self):
        logic_engine.start_startup_stabilization(60)
        with (
            mock.patch.object(logic_engine, "startup_hydrate_room", new=mock.AsyncMock()) as hydrate,
            mock.patch.object(logic_engine.live_broadcast, "broadcast_room_update", new=mock.AsyncMock()) as broadcast,
            mock.patch.object(logic_engine, "_tick_impl", new=mock.AsyncMock()) as tick_impl,
        ):
            await logic_engine.tick("Study")

        hydrate.assert_awaited_once_with("Study")
        tick_impl.assert_not_awaited()
        broadcast.assert_awaited_once_with("study")


class TestLogicEngineHardening(unittest.TestCase):
    def test_normalize_room_id_strip_lower(self):
        self.assertEqual(logic_engine.normalize_room_id("  BedROOM "), "bedroom")
        self.assertEqual(logic_engine.normalize_room_id(""), "")

    def test_resolve_room_definition_case_insensitive(self):
        cfg = {
            "rooms": [
                {
                    "id": "aBc123XYZ789",
                    "name": "Living",
                    "climate_entity": "climate.ac_living",
                },
            ]
        }
        r = logic_engine.resolve_room_definition(cfg, "  abc123xYz789 ")
        self.assertIsNotNone(r)
        self.assertEqual(r["id"], "aBc123XYZ789")

    def test_tick_impl_uses_live_presence_without_stabilized_runtime_fallback(self):
        logic_engine._runtime_by_room.clear()
        rid = "tick-now"
        cfg = {
            "rooms": [
                {
                    "id": rid,
                    "climate_entity": "climate.test",
                    "presence_entity": "binary_sensor.presence",
                    "indoor_temp_entity": "sensor.temp",
                    "control_mode": "presence_only",
                    "manual_override": True,
                    "use_presence": True,
                },
            ],
        }

        async def run_case():
            async def fake_get_state(entity_id):
                if entity_id == "sensor.temp":
                    return "25"
                return "off"

            with (
                mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine, "_load_startup_state", new=mock.AsyncMock()),
                mock.patch.object(
                    logic_engine.ha_client,
                    "get_climate_state",
                    new=mock.AsyncMock(return_value={"state": "off"}),
                ),
                mock.patch.object(
                    logic_engine.ha_client,
                    "get_state",
                    side_effect=fake_get_state,
                ),
                mock.patch.object(
                    logic_engine,
                    "_stabilize_presence",
                    return_value=True,
                ) as stabilize_presence,
            ):
                await logic_engine._tick_impl(rid, rid)
            stabilize_presence.assert_not_called()

        asyncio.run(run_case())

    def test_get_runtime_state_matches_canonical_room(self):
        """API may pass mixed-case URL id — merge config from resolve_room_definition."""
        rid = "cafef00dbabe"
        logic_engine._runtime_by_room.clear()
        st = logic_engine._rt(rid)
        st.ac_is_on = True
        st.physical_ac_on = True
        st.effective_ac_on = True
        st.ac_state = "on"
        st.effective_ac_idle = False
        st.effective_power_source = "internal"
        st.effective_control_source = "test"
        st.effective_target_temp = 24.0
        st.last_command_source = "system"
        base = {
            "rooms": [
                {
                    "id": rid,
                    "climate_entity": "climate.x",
                    "min_command_interval_seconds": 150,
                },
            ]
        }
        with mock.patch.object(logic_engine.config_manager, "load_config", return_value=base):
            out = logic_engine.get_runtime_state("  CaFeF00DBabE ")
        self.assertTrue(out["ac_is_on"])
        self.assertTrue(out["physical_ac_on"])
        self.assertTrue(out["effective_ac_on"])
        self.assertEqual(out["ac_state"], "on")
        self.assertIn(out["ac_state_source"], ("power", "inferred", "system"))

    def test_energy_runtime_parser_distinguishes_invalid_from_zero(self):
        self.assertEqual(logic_engine._parse_energy_sensor_value("0"), 0.0)
        self.assertEqual(logic_engine._parse_energy_sensor_value(0), 0.0)
        self.assertEqual(logic_engine._parse_energy_sensor_value("611.5"), 611.5)
        self.assertIsNone(logic_engine._parse_energy_sensor_value("unknown"))
        self.assertIsNone(logic_engine._parse_energy_sensor_value("unavailable"))
        self.assertIsNone(logic_engine._parse_energy_sensor_value(None))
        self.assertIsNone(logic_engine._parse_energy_sensor_value("not-a-number"))

    def test_read_runtime_energy_tracks_current_separately_from_last_valid(self):
        st = logic_engine.RoomRuntime()

        async def run_case():
            first_states = [
                {
                    "state": "611.5",
                    "attributes": {
                        "device_class": "power",
                        "state_class": "measurement",
                        "unit_of_measurement": "W",
                    },
                },
                {
                    "state": "42.25",
                    "attributes": {
                        "device_class": "energy",
                        "state_class": "total_increasing",
                        "unit_of_measurement": "kWh",
                    },
                },
            ]
            with mock.patch.object(
                logic_engine.ha_client,
                "get_entity_state_full",
                new=mock.AsyncMock(side_effect=first_states),
            ):
                watts, kwh = await logic_engine._read_runtime_energy(
                    "room-x",
                    {
                        "energy_power_entity": "sensor.room_power",
                        "energy_kwh_entity": "sensor.room_kwh",
                    },
                    st,
                )
            self.assertEqual(watts, 611.5)
            self.assertEqual(kwh, 42.25)
            self.assertEqual(st.energy_watts, 611.5)
            self.assertEqual(st.energy_kwh, 42.25)
            self.assertEqual(st.last_valid_power_watts, 611.5)
            self.assertEqual(st.last_valid_energy_kwh, 42.25)

            second_states = [
                {
                    "state": "unknown",
                    "attributes": {
                        "device_class": "power",
                        "state_class": "measurement",
                        "unit_of_measurement": "W",
                    },
                },
                {
                    "state": "bad-kwh",
                    "attributes": {
                        "device_class": "energy",
                        "state_class": "total_increasing",
                        "unit_of_measurement": "kWh",
                    },
                },
            ]
            with mock.patch.object(
                logic_engine.ha_client,
                "get_entity_state_full",
                new=mock.AsyncMock(side_effect=second_states),
            ):
                watts2, kwh2 = await logic_engine._read_runtime_energy(
                    "room-x",
                    {
                        "energy_power_entity": "sensor.room_power",
                        "energy_kwh_entity": "sensor.room_kwh",
                    },
                    st,
                )
            self.assertEqual(watts2, 611.5)
            self.assertEqual(kwh2, 42.25)
            self.assertEqual(st.energy_watts, 611.5)
            self.assertEqual(st.energy_kwh, 42.25)
            self.assertFalse(st.telemetry_power_live_valid)
            self.assertFalse(st.telemetry_kwh_live_valid)
            self.assertTrue(st.telemetry_gap)
            self.assertEqual(st.telemetry_status, "recovering")
            self.assertEqual(st.last_valid_power_watts, 611.5)
            self.assertEqual(st.last_valid_energy_kwh, 42.25)
            self.assertIsNotNone(st.last_valid_timestamp)

        asyncio.run(run_case())

    def test_read_runtime_energy_uses_normalized_power_value(self):
        st = logic_engine.RoomRuntime()

        async def run_case():
            states = [
                {
                    "state": "8218",
                    "attributes": {
                        "device_class": "power",
                        "state_class": "measurement",
                        "unit_of_measurement": "W",
                        "scale": 1,
                    },
                },
                {
                    "state": "12.5",
                    "attributes": {
                        "device_class": "energy",
                        "state_class": "total_increasing",
                        "unit_of_measurement": "kWh",
                    },
                },
            ]
            with mock.patch.object(
                logic_engine.ha_client,
                "get_entity_state_full",
                new=mock.AsyncMock(side_effect=states),
            ):
                return await logic_engine._read_runtime_energy(
                    "room-x",
                    {
                        "energy_power_entity": "sensor.tuya_power",
                        "energy_kwh_entity": "sensor.room_kwh",
                    },
                    st,
                )

        watts, kwh = asyncio.run(run_case())

        self.assertEqual(watts, 821.8)
        self.assertEqual(kwh, 12.5)
        self.assertEqual(st.energy_watts, 821.8)
        self.assertEqual(st.energy_power_confidence, "metadata")
        self.assertEqual(st.energy_power_validation_reason, "ok")

    def test_telemetry_cache_stales_offline_without_hvac_state_changes(self):
        st = logic_engine.RoomRuntime()
        st.ac_is_on = True
        st.physical_ac_on = True
        st.effective_ac_on = True
        st.ac_state = "on"
        now = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)

        watts, kwh = logic_engine._apply_telemetry_cache(
            st,
            now=now,
            configured=True,
            power_entity="sensor.room_power",
            kwh_entity="sensor.room_kwh",
            parsed_power=700.0,
            parsed_kwh=10.0,
        )
        self.assertEqual((watts, kwh), (700.0, 10.0))
        self.assertEqual(st.telemetry_status, "healthy")
        self.assertFalse(st.telemetry_gap)

        recovering_watts, recovering_kwh = logic_engine._apply_telemetry_cache(
            st,
            now=now + timedelta(seconds=1),
            configured=True,
            power_entity="sensor.room_power",
            kwh_entity="sensor.room_kwh",
            parsed_power=None,
            parsed_kwh=None,
        )
        self.assertEqual((recovering_watts, recovering_kwh), (700.0, 10.0))
        self.assertEqual(st.telemetry_status, "recovering")

        stale_watts, stale_kwh = logic_engine._apply_telemetry_cache(
            st,
            now=now + timedelta(seconds=62),
            configured=True,
            power_entity="sensor.room_power",
            kwh_entity="sensor.room_kwh",
            parsed_power=None,
            parsed_kwh=None,
        )
        self.assertEqual((stale_watts, stale_kwh), (700.0, 10.0))
        self.assertEqual(st.telemetry_status, "stale")
        self.assertTrue(st.telemetry_gap)
        self.assertTrue(st.ac_is_on)
        self.assertTrue(st.physical_ac_on)
        self.assertEqual(st.ac_state, "on")

        offline_watts, offline_kwh = logic_engine._apply_telemetry_cache(
            st,
            now=now + timedelta(seconds=181),
            configured=True,
            power_entity="sensor.room_power",
            kwh_entity="sensor.room_kwh",
            parsed_power=None,
            parsed_kwh=None,
        )
        self.assertEqual((offline_watts, offline_kwh), (None, None))
        self.assertEqual(st.telemetry_status, "offline")
        self.assertTrue(st.ac_is_on)
        self.assertTrue(st.physical_ac_on)

        recovered_watts, recovered_kwh = logic_engine._apply_telemetry_cache(
            st,
            now=now + timedelta(seconds=182),
            configured=True,
            power_entity="sensor.room_power",
            kwh_entity="sensor.room_kwh",
            parsed_power=710.0,
            parsed_kwh=10.2,
        )
        self.assertEqual((recovered_watts, recovered_kwh), (710.0, 10.2))
        self.assertEqual(st.telemetry_status, "healthy")
        self.assertFalse(st.telemetry_gap)
        self.assertEqual(st.last_valid_power_watts, 710.0)
        self.assertEqual(st.last_valid_energy_kwh, 10.2)

    def test_sync_pending_clears_when_decision_not_on_off(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "on"
        st.pending_since = 1.0
        logic_engine._sync_pending_for_action(st, "hold")
        self.assertIsNone(st.pending_action)
        self.assertIsNone(st.pending_since)

    def test_sync_pending_clears_when_flipping_on_to_off(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "on"
        st.pending_since = 1.0
        logic_engine._sync_pending_for_action(st, "off")
        self.assertIsNone(st.pending_action)

    def test_sync_pending_preserves_when_matching_intent(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "on"
        st.pending_since = 1.0
        logic_engine._sync_pending_for_action(st, "on")
        self.assertEqual(st.pending_action, "on")
        self.assertEqual(st.pending_since, 1.0)

    def test_clear_pending_on_satisfied_physically_or_override(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "on"
        st.pending_on_ir_sent = True
        st.pending_on_ir_sent_at = datetime.now(timezone.utc)
        logic_engine._clear_pending_when_physically_satisfied(
            st,
            manual_override_active=False,
            confirmed_ac_on=True,
            physical_ac_on=True,
        )
        self.assertIsNone(st.pending_action)
        self.assertFalse(st.pending_on_ir_sent)
        self.assertIsNone(st.pending_on_ir_sent_at)

        st.pending_action = "on"
        st.pending_since = 2.0
        st.pending_on_ir_sent = True
        st.pending_on_ir_sent_at = datetime.now(timezone.utc)
        logic_engine._clear_pending_when_physically_satisfied(
            st,
            manual_override_active=True,
            confirmed_ac_on=False,
            physical_ac_on=False,
        )
        self.assertIsNone(st.pending_action)
        self.assertFalse(st.pending_on_ir_sent)
        self.assertIsNone(st.pending_on_ir_sent_at)

        # Inferred-only physical ON must NOT clear pending ON until power/IR/HA confirms.
        st.pending_action = "on"
        st.pending_since = 3.0
        st.ac_state_source = "inferred"
        logic_engine._clear_pending_when_physically_satisfied(
            st,
            manual_override_active=False,
            confirmed_ac_on=False,
            physical_ac_on=True,
        )
        self.assertEqual(st.pending_action, "on")
        self.assertEqual(st.pending_since, 3.0)

    def test_pending_on_timeout_clears_lock_state(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.pending_action = "on"
        st.pending_since = now.timestamp() - 120
        st.pending_on_ir_sent = True
        st.pending_on_ir_sent_at = now - timedelta(
            seconds=logic_engine.PENDING_ON_CONFIRM_TIMEOUT_SECS + 1
        )
        st.physical_ac_on = False
        st.soft_start_ui = True

        async def run_case():
            with (
                mock.patch.object(logic_engine.live_broadcast, "broadcast_room_update") as broadcast,
                mock.patch.object(logic_engine, "log_with_room"),
            ):
                cleared = await logic_engine._clear_timed_out_pending_on("room-x", st, now)
            self.assertTrue(cleared)
            broadcast.assert_called_once_with("room-x")

        asyncio.run(run_case())

        self.assertEqual(st.ac_state, "on_failed")
        self.assertEqual(st.last_command, "on_failed")
        self.assertIsNone(st.pending_action)
        self.assertIsNone(st.pending_since)
        self.assertFalse(st.pending_on_ir_sent)
        self.assertIsNone(st.pending_on_ir_sent_at)
        self.assertFalse(st.soft_start_ui)
        self.assertFalse(st.on_failed_retry_used)

    def test_on_failed_retry_allowed_once_after_30s(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.ac_state = "on_failed"
        st.last_command_time = now - timedelta(seconds=31)

        with mock.patch.object(logic_engine, "log_with_room") as log_with_room:
            self.assertTrue(logic_engine._on_failed_retry_allowed("room-x", st, now))
            self.assertFalse(logic_engine._on_failed_retry_allowed("room-x", st, now))

        self.assertTrue(st.on_failed_retry_used)
        self.assertTrue(
            any("[CONTROL] on_retry_allowed" in str(call.args) for call in log_with_room.call_args_list)
        )

    def test_clear_pending_off_when_already_off(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "off"
        logic_engine._clear_pending_when_physically_satisfied(
            st,
            manual_override_active=False,
            confirmed_ac_on=False,
            physical_ac_on=False,
        )
        self.assertIsNone(st.pending_action)

    def test_sync_ac_display_pending_on_masks_effective(self):
        st = logic_engine.RoomRuntime()
        st.physical_ac_on = True
        st.pending_action = "on"
        st.pending_since = 1000.0
        logic_engine._sync_ac_display_fields(st)
        self.assertFalse(st.effective_ac_on)
        self.assertEqual(st.ac_state, "pending_on")

    def test_sync_ac_display_pending_off_vs_on(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "off"
        st.physical_ac_on = True
        logic_engine._sync_ac_display_fields(st)
        self.assertTrue(st.effective_ac_on)
        self.assertEqual(st.ac_state, "pending_off")

        st.pending_action = None
        st.physical_ac_on = True
        logic_engine._sync_ac_display_fields(st)
        self.assertEqual(st.ac_state, "on")

    def test_decision_lock_blocks_delayed_emit_inside_window(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.last_decision_at = now - timedelta(seconds=5)
        self.assertTrue(logic_engine._decision_lock_blocks_delayed_emit(st, now))

    def test_decision_lock_allows_delayed_emit_after_window(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.last_decision_at = now - timedelta(seconds=int(logic_engine.DECISION_LOCK_SECONDS) + 5)
        self.assertFalse(logic_engine._decision_lock_blocks_delayed_emit(st, now))

    def test_effective_mode_auto_clamps_delta_above_base(self):
        cfg = {"effective_mode": "auto", "effective_max_delta_deg": 3.0}
        self.assertAlmostEqual(
            logic_engine.apply_effective_mode_engine_target(
                room_id="r", base_temp=22.0, planned_with_ai=30.0, cfg=cfg, control_log=False,
            ),
            25.0,
        )
        self.assertAlmostEqual(
            logic_engine.apply_effective_mode_engine_target(
                room_id="r", base_temp=22.0, planned_with_ai=21.0, cfg=cfg, control_log=False,
            ),
            22.0,
        )

    def test_effective_mode_manual_respects_floor_and_ceiling(self):
        cfg = {"effective_mode": "manual", "manual_effective_temp": 20.0, "effective_max_delta_deg": 3.0}
        self.assertAlmostEqual(
            logic_engine.apply_effective_mode_engine_target(
                room_id="r", base_temp=22.0, planned_with_ai=25.0, cfg=cfg, control_log=False,
            ),
            22.0,
        )
        cfg["manual_effective_temp"] = 26.0
        self.assertAlmostEqual(
            logic_engine.apply_effective_mode_engine_target(
                room_id="r", base_temp=22.0, planned_with_ai=23.0, cfg=cfg, control_log=False,
            ),
            25.0,
        )

    def test_effective_max_delta_deg_bounded_1_to_5(self):
        self.assertAlmostEqual(logic_engine.effective_max_delta_deg({"effective_max_delta_deg": 99}), 5.0)
        self.assertAlmostEqual(logic_engine.effective_max_delta_deg({"effective_max_delta_deg": 0.25}), 1.0)
        self.assertAlmostEqual(logic_engine.effective_max_delta_deg({"effective_max_delta_deg": "bad"}), 3.0)

    def test_control_decision_uses_defaults_for_invalid_numeric_config(self):
        logic_engine._runtime_by_room.clear()
        action, source, target = logic_engine._resolve_control_decision(
            room_id="invalid-numeric",
            cfg={
                "thermostat_on_delta_deg": "bad",
                "thermostat_off_delta_deg": None,
                "vacancy_timeout_minutes": "bad",
                "user_authority_lock_secs": "bad",
            },
            indoor_temp=25.0,
            effective_target=24.0,
            is_occupied=True,
            ac_on=False,
            now=datetime.now(timezone.utc),
        )
        self.assertEqual((action, source, target), ("on", "thermostat", 24.0))

    def test_sync_effective_mode_transition_clears_pending(self):
        st = logic_engine.RoomRuntime()
        st.last_effective_mode = "auto"
        st.pending_action = "on"
        st.pending_since = 123.0
        logic_engine.sync_effective_mode_transition(st, "room-x", {"effective_mode": "manual"})
        self.assertIsNone(st.pending_action)
        self.assertIsNone(st.pending_since)
        self.assertEqual(st.last_effective_mode, "manual")

    def test_clear_manual_override_releases_all_runtime_latches(self):
        logic_engine._runtime_by_room.clear()
        rid = "override-clear"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.manual_override_config_active = True
        st.manual_override_until = now + timedelta(minutes=5)
        st.manual_override_temp = 26.0
        st.prev_ha_setpoint_seen = 26.0
        st.last_user_command_time = now
        st.last_command_source = "user"
        st.effective_control_source = "manual"

        with mock.patch.object(logic_engine, "log_with_room") as log_with_room:
            cleared = logic_engine.clear_manual_override(rid, reason="manual_override_cleared")

        self.assertTrue(cleared)
        self.assertFalse(st.manual_override_config_active)
        self.assertIsNone(st.manual_override_until)
        self.assertIsNone(st.manual_override_temp)
        self.assertIsNone(st.prev_ha_setpoint_seen)
        self.assertIsNone(st.last_user_command_time)
        self.assertEqual(st.last_command_source, "system")
        self.assertEqual(st.effective_control_source, "none")
        self.assertTrue(
            any("[OVERRIDE] cleared" in str(call.args) for call in log_with_room.call_args_list)
        )
        self.assertTrue(
            any("[OVERRIDE] runtime_resumed" in str(call.args) for call in log_with_room.call_args_list)
        )

    def test_clear_manual_override_resume_broadcasts_and_triggers_tick(self):
        logic_engine._runtime_by_room.clear()
        rid = "override-resume"
        st = logic_engine._rt(rid)
        st.manual_override_config_active = True
        st.manual_override_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        st.manual_override_temp = 25.0

        async def run_case():
            with (
                mock.patch.object(
                    logic_engine.live_broadcast,
                    "broadcast_room_update",
                    new=mock.AsyncMock(),
                ) as broadcast,
                mock.patch.object(logic_engine, "trigger_tick") as trigger_tick,
            ):
                cleared = await logic_engine.clear_manual_override_and_resume(
                    rid,
                    reason="manual_override_cleared",
                )
            return cleared, broadcast, trigger_tick

        cleared, broadcast, trigger_tick = asyncio.run(run_case())

        self.assertTrue(cleared)
        broadcast.assert_awaited_once_with(rid)
        trigger_tick.assert_called_once_with(
            rid,
            reason="manual_override_cleared",
            skip_debounce=True,
        )

    def test_schedule_target_sync_ignores_stale_manual_setpoint(self):
        logic_engine._runtime_by_room.clear()
        rid = "targetsync"
        st = logic_engine._rt(rid)
        st.prev_ha_setpoint_seen = 27.0
        st.manual_override_temp = 27.0
        st.manual_override_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        st.effective_target_temp = 27.0

        cfg = {
            "rooms": [
                {
                    "id": rid,
                    "name": "Target Sync",
                    "climate_entity": "climate.targetsync",
                    "presence_entity": "binary_sensor.targetsync_presence",
                    "indoor_temp_entity": "sensor.targetsync_temp",
                    "energy_power_entity": "sensor.targetsync_power",
                    "settings": {
                        "target_temp": 20,
                        "temperature_mode": "schedule",
                        "schedule": {
                            "morning_temp": 20,
                            "afternoon_temp": 20,
                            "evening_temp": 20,
                            "night_temp": 20,
                        },
                        "smart_temp_adjustment": True,
                        "use_outdoor_temp": True,
                        "effective_mode": "auto",
                        "effective_max_delta_deg": 3.0,
                        "manual_override": False,
                        "use_presence": True,
                        "thermostat_on_delta_deg": 0.7,
                        "thermostat_off_delta_deg": 0.3,
                    },
                }
            ]
        }

        async def fake_get_state(entity_id):
            vals = {
                "sensor.targetsync_temp": "25.4",
                "binary_sensor.targetsync_presence": "on",
                "sensor.targetsync_power": "700",
            }
            return vals.get(entity_id)

        async def run_case():
            with (
                mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg),
                mock.patch.object(
                    logic_engine.ha_client,
                    "get_climate_state",
                    new=mock.AsyncMock(
                        return_value={
                            "state": "cool",
                            "mode": "cool",
                            "current_temp": 25.4,
                            "target_temp": 27.0,
                            "is_on": True,
                        }
                    ),
                ),
                mock.patch.object(logic_engine.ha_client, "get_state", side_effect=fake_get_state),
                mock.patch.object(logic_engine, "resolve_base_target_temp", return_value=(20.0, "night")),
                mock.patch.object(logic_engine, "log_target_resolve"),
                mock.patch.object(
                    logic_engine.weather_api,
                    "get_cached",
                    new=mock.AsyncMock(return_value={"temp": 28.0, "humidity": 50}),
                ),
                mock.patch.object(logic_engine, "_stabilize_presence", return_value=True),
                mock.patch.object(logic_engine, "_maintain_session_lifecycle", new=mock.AsyncMock()),
                mock.patch.object(logic_engine.smart_cooling, "apply_effective_target", new=mock.AsyncMock()),
                mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=None),
            ):
                await logic_engine._tick_impl(rid, rid)

        asyncio.run(run_case())

        self.assertAlmostEqual(st.effective_target_temp, 21.0)
        self.assertEqual(st.effective_target_source, "control_effective")
        self.assertEqual(st.effective_control_source, "thermostat")
        self.assertIsNone(st.manual_override_temp)
        self.assertIsNone(st.manual_override_until)

    def test_manual_mode_can_still_use_fresh_manual_setpoint_lock(self):
        logic_engine._runtime_by_room.clear()
        rid = "manual-sync"
        st = logic_engine._rt(rid)
        st.prev_ha_setpoint_seen = 24.0
        now = datetime.now(timezone.utc)

        active, target = logic_engine._manual_override_resolve(
            rid,
            {"temperature_mode": "manual", "manual_override_duration_minutes": 10},
            {"target_temp": 23.0},
            indoor_temp=26.0,
            now=now,
            engine_planned_target=21.0,
        )

        self.assertTrue(active)
        self.assertAlmostEqual(target, 23.0)
        self.assertAlmostEqual(st.manual_override_temp, 23.0)

    def test_persistent_manual_override_restores_without_expiry(self):
        logic_engine._runtime_by_room.clear()
        rid = "persistent-override"
        st = logic_engine._rt(rid)
        cfg = {
            "manual_override_enabled": True,
            "manual_override": True,
            "override_started_at": "2026-01-01T00:00:00+00:00",
            "override_user_settings": {"target_temp": 23.0},
            "target_temp": 24.0,
        }

        restored = logic_engine._restore_persisted_manual_override(rid, cfg, st)
        with mock.patch.object(
            logic_engine.config_manager,
            "load_config",
            return_value={
                "rooms": [
                    {
                        "id": rid,
                        "name": "Persistent Override",
                        "climate_entity": "climate.override",
                        "settings": dict(cfg),
                    }
                ]
            },
        ):
            runtime = logic_engine.get_runtime_state(rid)

        self.assertTrue(restored)
        self.assertTrue(st.manual_override_config_active)
        self.assertTrue(runtime["manual_override_active"])
        self.assertTrue(runtime["manual_override_persisted"])
        self.assertTrue(runtime["automation_paused_by_user"])
        self.assertIsNone(runtime["manual_override_expires_at"])
        self.assertEqual(runtime["override_started_at"], "2026-01-01T00:00:00+00:00")

    def test_target_context_change_clears_stale_target_state(self):
        st = logic_engine.RoomRuntime()
        st.last_target_context_key = ("manual", "manual", 27.0, False, "auto", None, 3.0)
        st.manual_override_temp = 27.0
        st.manual_override_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        st.prev_ha_setpoint_seen = 27.0
        st.effective_target_temp = 27.0

        cfg = {
            "temperature_mode": "schedule",
            "effective_mode": "auto",
            "ai_enabled": False,
            "effective_max_delta_deg": 3.0,
        }
        logic_engine.sync_target_context_transition(st, "target-context", cfg, "night", 20.0)

        self.assertIsNone(st.manual_override_temp)
        self.assertIsNone(st.manual_override_until)
        self.assertIsNone(st.prev_ha_setpoint_seen)
        self.assertAlmostEqual(st.effective_target_temp, 20.0)

    def test_gate_turn_ac_on_duplicate_physical_on_skips(self):
        """Same fingerprint allowed only when compressor not observed ON — duplicate + ON skips."""
        logic_engine._runtime_by_room.clear()
        rid = "gate-dup-on"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.last_command_time = now - timedelta(seconds=400)
        st.last_command = "on"
        st.last_sent_command_key = logic_engine._fingerprint_turn_on(23.5)
        st.physical_ac_on = True
        st.compressor_off_since = None
        cfg = {"min_command_interval_seconds": 150, "compressor_min_off_seconds": 0}
        self.assertFalse(logic_engine._gate_turn_ac_on(rid, cfg, 23.5, now))

    def test_gate_turn_ac_on_duplicate_physical_off_bypasses_min_interval(self):
        """Missed ACK: fingerprint matches but physically OFF → still allow past min_command_interval."""
        logic_engine._runtime_by_room.clear()
        rid = "gate-dup-off"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.last_command_time = now - timedelta(seconds=70)
        st.last_command = "on"
        st.last_sent_command_key = logic_engine._fingerprint_turn_on(24.0)
        st.physical_ac_on = False
        st.compressor_off_since = None
        cfg = {"min_command_interval_seconds": 150, "compressor_min_off_seconds": 0}
        self.assertTrue(logic_engine._gate_turn_ac_on(rid, cfg, 24.0, now))

    def test_gate_turn_ac_on_duplicate_physical_off_bypasses_ir_cooldown(self):
        """Same fingerprint + physically OFF bypasses IR cooldown so missed commands can retry."""
        logic_engine._runtime_by_room.clear()
        rid = "gate-dup-ir-off"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.last_command_time = now - timedelta(seconds=15)
        st.last_command = "on"
        st.last_sent_command_key = logic_engine._fingerprint_turn_on(23.0)
        st.physical_ac_on = False
        st.compressor_off_since = None
        cfg = {"min_command_interval_seconds": 150, "compressor_min_off_seconds": 0}
        self.assertTrue(logic_engine._gate_turn_ac_on(rid, cfg, 23.0, now))

    def test_delayed_on_single_emit_marks_before_send_and_skips_duplicate(self):
        logic_engine._runtime_by_room.clear()
        rid = "delay-on-once"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        cfg = {"on_delay_seconds": 0, "climate_entity": "climate.test", "ir_backend": "aerostate"}

        async def fake_turn_on(*args, **kwargs):
            self.assertTrue(st.pending_on_ir_sent)
            self.assertEqual(st.pending_on_ir_sent_at, now)
            self.assertTrue(kwargs.get("allow_pending_on_emit"))
            return True

        async def run_case():
            with (
                mock.patch.object(logic_engine, "_turn_ac_on", side_effect=fake_turn_on) as turn_on,
                mock.patch.object(logic_engine.asyncio, "create_task") as create_task,
                mock.patch.object(logic_engine, "log_with_room") as log_with_room,
            ):
                await logic_engine._handle_delayed_on(
                    rid,
                    rid,
                    cfg,
                    indoor_temp=27.0,
                    et_eff=24.0,
                    now=now,
                    st=st,
                    confirmed_ac_on=False,
                )
                await logic_engine._handle_delayed_on(
                    rid,
                    rid,
                    cfg,
                    indoor_temp=27.0,
                    et_eff=24.0,
                    now=now + timedelta(seconds=5),
                    st=st,
                    confirmed_ac_on=False,
                )
                self.assertEqual(turn_on.call_count, 1)
                create_task.assert_not_called()
                self.assertTrue(st.pending_on_ir_sent)
                self.assertTrue(
                    any("Skip duplicate ON" in str(call.args) for call in log_with_room.call_args_list)
                )

        asyncio.run(run_case())

    def test_delayed_on_tuya_schedules_double_emit_after_first_send(self):
        logic_engine._runtime_by_room.clear()
        rid = "delay-on-tuya"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        cfg = {"on_delay_seconds": 0, "climate_entity": "climate.tuya", "ir_backend": "tuya"}

        async def fake_turn_on(*args, **kwargs):
            return True

        def fake_create_task(coro):
            coro.close()
            return mock.Mock()

        async def run_case():
            with (
                mock.patch.object(logic_engine, "_turn_ac_on", side_effect=fake_turn_on) as turn_on,
                mock.patch.object(logic_engine.asyncio, "create_task", side_effect=fake_create_task) as create_task,
                mock.patch.object(logic_engine, "log_with_room"),
            ):
                await logic_engine._handle_delayed_on(
                    rid,
                    rid,
                    cfg,
                    indoor_temp=27.0,
                    et_eff=24.0,
                    now=now,
                    st=st,
                    confirmed_ac_on=False,
                )
            self.assertEqual(turn_on.call_count, 1)
            create_task.assert_called_once()

        asyncio.run(run_case())

    def test_tuya_double_emit_retries_when_physical_off(self):
        logic_engine._runtime_by_room.clear()
        rid = "tuya-double"
        st = logic_engine._rt(rid)
        st.pending_action = "on"
        st.physical_ac_on = False
        now = datetime.now(timezone.utc)

        async def run_case():
            with (
                mock.patch.object(logic_engine.asyncio, "sleep", new=mock.AsyncMock()),
                mock.patch.object(logic_engine, "_turn_ac_on", return_value=True) as turn_on,
                mock.patch.object(logic_engine, "log_with_room") as log_with_room,
            ):
                await logic_engine._tuya_double_emit(
                    rid,
                    {"climate_entity": "climate.tuya", "ir_backend": "tuya"},
                    24.0,
                    now,
                )
            turn_on.assert_awaited_once()
            self.assertTrue(turn_on.await_args.kwargs.get("allow_pending_on_emit"))
            self.assertTrue(
                any("retry ON" in str(call.args) for call in log_with_room.call_args_list)
            )

        asyncio.run(run_case())

    def test_pending_on_decision_lock_blocks_only_after_ir_sent(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "on"
        st.pending_on_ir_sent = False

        action, source = logic_engine._apply_pending_on_decision_lock(
            "room-x", st, "on", "thermostat",
        )
        self.assertEqual((action, source), ("on", "thermostat"))
        self.assertEqual(st.pending_action, "on")

        st.pending_on_ir_sent = True
        with mock.patch.object(logic_engine, "log_with_room") as log_with_room:
            action, source = logic_engine._apply_pending_on_decision_lock(
                "room-x", st, "on", "thermostat",
            )

        self.assertEqual((action, source), ("hold", "pending_on_lock"))
        self.assertEqual(st.pending_action, "on")
        self.assertTrue(
            any("Skip ON" in str(call.args) for call in log_with_room.call_args_list)
        )

    def test_pending_on_off_block_protects_until_timeout(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.pending_action = "on"
        st.pending_on_ir_sent = True
        st.pending_on_ir_sent_at = now - timedelta(seconds=5)

        with mock.patch.object(logic_engine, "log_with_room") as log_with_room:
            action, source = logic_engine._apply_pending_on_off_block(
                "room-x", st, "off", "thermostat_reached", now,
            )

        self.assertEqual((action, source), ("hold", "pending_on_protection"))
        self.assertEqual(st.pending_action, "on")
        self.assertTrue(
            any("Block OFF" in str(call.args) for call in log_with_room.call_args_list)
        )
        self.assertTrue(
            any("thermostat_reached" in str(call.args) for call in log_with_room.call_args_list)
        )

        st.pending_on_ir_sent_at = now - timedelta(
            seconds=logic_engine.PENDING_ON_CONFIRM_TIMEOUT_SECS + 1
        )
        action, source = logic_engine._apply_pending_on_off_block(
            "room-x", st, "off", "thermostat_reached", now,
        )
        self.assertEqual((action, source), ("off", "thermostat_reached"))

    def test_pending_on_off_block_covers_vacancy_and_missing_ir_timestamp(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "on"
        st.pending_on_ir_sent = True
        st.pending_on_ir_sent_at = None

        with mock.patch.object(logic_engine, "log_with_room") as log_with_room:
            action, source = logic_engine._apply_pending_on_off_block(
                "room-x", st, "off", "safety_vacant", datetime.now(timezone.utc),
            )

        self.assertEqual((action, source), ("hold", "pending_on_protection"))
        self.assertTrue(
            any("safety_vacant" in str(call.args) for call in log_with_room.call_args_list)
        )

    def test_running_state_off_block_protects_recent_cooling(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.effective_on_since_ts = now.timestamp() - 30
        st.last_ac_on_at = now.timestamp() - 3600

        with mock.patch.object(logic_engine, "log_with_room") as log_with_room:
            action, source = logic_engine._apply_running_state_off_block(
                "room-x", st, "off", "safety_vacant", now, "cool",
            )

        self.assertEqual((action, source), ("hold", "running_protection"))
        self.assertTrue(
            any("post-ON protection" in str(call.args) for call in log_with_room.call_args_list)
        )
        self.assertTrue(
            any("safety_vacant" in str(call.args) for call in log_with_room.call_args_list)
        )

    def test_running_state_off_block_requires_cool_and_recent_on(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.effective_on_since_ts = now.timestamp() - 30

        action, source = logic_engine._apply_running_state_off_block(
            "room-x", st, "off", "safety_vacant", now, "off",
        )
        self.assertEqual((action, source), ("off", "safety_vacant"))

        st.effective_on_since_ts = now.timestamp() - logic_engine.RUNNING_OFF_BLOCK_SECS - 1
        action, source = logic_engine._apply_running_state_off_block(
            "room-x", st, "off", "safety_vacant", now, "cool",
        )
        self.assertEqual((action, source), ("off", "safety_vacant"))

    def test_running_state_off_block_falls_back_to_recent_on_command(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.last_command = "on"
        st.last_command_time = now - timedelta(seconds=15)

        action, source = logic_engine._apply_running_state_off_block(
            "room-x", st, "off", "safety_vacant", now, "cool",
        )
        self.assertEqual((action, source), ("hold", "running_protection"))

    def test_pending_on_emit_hold_preserves_pending_cycle(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "on"
        st.pending_since = 123.0
        st.pending_on_ir_sent = True

        self.assertTrue(logic_engine._pending_on_emit_hold_in_progress(st, "hold"))
        if not logic_engine._pending_on_emit_hold_in_progress(st, "hold"):
            logic_engine._sync_pending_for_action(st, "hold")
        self.assertEqual(st.pending_action, "on")
        self.assertEqual(st.pending_since, 123.0)

    def test_control_mode_defaults_to_thermostat(self):
        self.assertEqual(logic_engine.normalize_control_mode({}), "thermostat")
        self.assertEqual(logic_engine.normalize_control_mode({"control_mode": "bad"}), "thermostat")
        self.assertEqual(
            logic_engine.normalize_control_mode({"control_mode": "presence_only"}),
            "presence_only",
        )

    def test_use_presence_defaults_to_enabled_for_missing_none_and_malformed(self):
        self.assertTrue(logic_engine.normalize_use_presence({}))
        self.assertTrue(logic_engine.normalize_use_presence({"use_presence": None}))
        self.assertTrue(logic_engine.normalize_use_presence({"use_presence": "maybe"}))

    def test_use_presence_explicit_false_disables_intentionally(self):
        self.assertFalse(logic_engine.normalize_use_presence({"use_presence": False}))

    def test_missing_presence_entity_defaults_to_presence_enabled_and_skips_tick(self):
        logic_engine._runtime_by_room.clear()
        rid = "missing-presence-defaults-enabled"
        st = logic_engine._rt(rid)
        st.occupied = False
        cfg = {
            "rooms": [
                {
                    "id": rid,
                    "climate_entity": "climate.test",
                    "indoor_temp_entity": "sensor.temp",
                    "manual_override": True,
                },
            ],
        }

        async def run_case():
            with (
                mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine, "_load_startup_state", new=mock.AsyncMock()),
                mock.patch.object(logic_engine, "log_with_room") as log_with_room,
            ):
                await logic_engine._tick_impl(rid, rid)
            return log_with_room

        log_with_room = asyncio.run(run_case())
        self.assertFalse(st.occupied)
        self.assertFalse(
            any("presence_control_disabled" in str(call.args) for call in log_with_room.call_args_list)
        )

    def test_explicit_use_presence_false_syncs_occupied_and_logs_once(self):
        logic_engine._runtime_by_room.clear()
        rid = "presence-disabled-explicit"
        st = logic_engine._rt(rid)
        st.occupied = False
        st.stable_occupied = False
        st.last_known_presence = False
        st.vacant_since = datetime.now(timezone.utc) - timedelta(seconds=30)
        cfg = {
            "use_presence": False,
            "rooms": [
                {
                    "id": rid,
                    "climate_entity": "climate.test",
                    "indoor_temp_entity": "sensor.temp",
                    "manual_override": True,
                },
            ],
        }

        async def fake_get_state(entity_id):
            self.assertEqual(entity_id, "sensor.temp")
            return "25"

        async def run_case():
            with (
                mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine, "_load_startup_state", new=mock.AsyncMock()),
                mock.patch.object(
                    logic_engine.ha_client,
                    "get_climate_state",
                    new=mock.AsyncMock(return_value={"state": "off"}),
                ),
                mock.patch.object(logic_engine.ha_client, "get_state", side_effect=fake_get_state),
                mock.patch.object(logic_engine, "log_with_room") as log_with_room,
            ):
                await logic_engine._tick_impl(rid, rid)
                await logic_engine._tick_impl(rid, rid)
            return log_with_room

        log_with_room = asyncio.run(run_case())
        self.assertTrue(st.occupied)
        self.assertTrue(st.stable_occupied)
        self.assertTrue(st.last_known_presence)
        self.assertIsNone(st.vacant_since)
        disabled_logs = [
            call for call in log_with_room.call_args_list
            if "presence_control_disabled" in str(call.args)
        ]
        self.assertEqual(len(disabled_logs), 1)

    def test_presence_only_missing_presence_holds_without_on(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)

        with mock.patch.object(logic_engine, "log_with_room") as log_with_room:
            action, source, occupied = logic_engine._resolve_presence_only_decision(
                "room-x",
                {"control_mode": "presence_only"},
                st,
                "unknown",
                ac_on=False,
                now=now,
            )

        self.assertEqual((action, source, occupied), ("hold", "presence_unavailable", False))
        self.assertIsNone(st.presence_only_present_since)
        self.assertTrue(log_with_room.called)

    def test_presence_stabilization_ignores_false_spike(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)

        self.assertTrue(logic_engine._stabilize_presence(st, "on", now))
        self.assertTrue(
            logic_engine._stabilize_presence(st, "off", now + timedelta(seconds=30))
        )
        self.assertTrue(st.last_known_presence)

    def test_presence_stabilization_allows_stable_vacancy_after_window(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)

        self.assertTrue(logic_engine._stabilize_presence(st, "on", now))
        self.assertTrue(logic_engine._stabilize_presence(st, "off", now))
        self.assertFalse(
            logic_engine._stabilize_presence(
                st,
                "off",
                now + timedelta(seconds=logic_engine.PRESENCE_STABILIZATION_SECS + 1),
            )
        )
        self.assertFalse(st.last_known_presence)

    def test_presence_state_machine_requires_stable_true_after_confirmed_vacancy(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)

        self.assertTrue(logic_engine._stabilize_presence(st, "off", now))
        self.assertFalse(
            logic_engine._stabilize_presence(
                st,
                "off",
                now + timedelta(seconds=logic_engine.VACANCY_CONFIRM_SECS + 1),
            )
        )
        self.assertFalse(logic_engine._stabilize_presence(st, "on", now + timedelta(seconds=70)))
        self.assertTrue(
            logic_engine._stabilize_presence(
                st,
                "on",
                now + timedelta(seconds=70 + logic_engine.PRESENCE_STABILIZATION_SECS + 1),
            )
        )

    def test_presence_only_on_requires_confirmed_dwell_without_temp_sensor(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        cfg = {
            "control_mode": "presence_only",
            "presence_only_on_dwell_seconds": 20,
        }

        action, source, occupied = logic_engine._resolve_presence_only_decision(
            "room-x",
            cfg,
            st,
            "on",
            ac_on=False,
            now=now,
        )
        self.assertEqual((action, source, occupied), ("hold", "presence_dwell", True))

        action, source, occupied = logic_engine._resolve_presence_only_decision(
            "room-x",
            cfg,
            st,
            "on",
            ac_on=False,
            now=now + timedelta(seconds=21),
        )
        self.assertEqual((action, source, occupied), ("on", "presence_only", True))

    def test_presence_only_off_uses_vacancy_grace(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        cfg = {"control_mode": "presence_only", "vacancy_timeout_minutes": 1}

        action, source, occupied = logic_engine._resolve_presence_only_decision(
            "room-x",
            cfg,
            st,
            "off",
            ac_on=True,
            now=now,
        )
        self.assertEqual((action, source, occupied), ("hold", "vacancy_debounce", False))

        action, source, occupied = logic_engine._resolve_presence_only_decision(
            "room-x",
            cfg,
            st,
            "off",
            ac_on=True,
            now=now + timedelta(seconds=61),
        )
        self.assertEqual((action, source, occupied), ("off", "presence_vacant", False))

    def test_presence_only_zone_absence_defers_to_room_presence(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.zone_sensor_usable = True
        st.zone_present = False
        st.occupied = True
        st.stable_occupied = True

        action, source, occupied = logic_engine._resolve_presence_only_decision(
            "room-x",
            {
                "control_mode": "presence_only",
                "zone_entity_id": "binary_sensor.zone",
                "vacancy_timeout_minutes": 1,
            },
            st,
            "on",
            ac_on=True,
            now=now,
        )

        self.assertEqual((action, source, occupied), ("hold", "presence_only", True))
        self.assertIsNone(st.vacant_since)
        self.assertTrue(st.occupied)

    def test_presence_only_vacant_already_off_enters_idle_once(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.pending_action = "on"
        st.pending_since = now.timestamp()
        st.pending_on_ir_sent = True
        st.pending_on_ir_sent_at = now
        st.last_command_time = now
        st.last_command = "on"
        st.last_sent_command_key = "stale-on"
        st.last_decision_at = now
        st.ir_last_sent_ts = now
        st.session_start_time = now - timedelta(minutes=1)
        st.session_state = "provisional"
        st.watts_samples = [0.0]
        cfg = {
            "control_mode": "presence_only",
            "energy_power_entity": "sensor.power",
            "target_temp": 24,
        }

        async def run_once(log_with_room):
            with (
                mock.patch.object(
                    logic_engine.ha_client,
                    "get_state",
                    new=mock.AsyncMock(return_value="0"),
                ),
                mock.patch.object(
                    logic_engine.session_logger,
                    "current_session_id",
                    return_value=None,
                ),
                mock.patch.object(
                    logic_engine,
                    "_maintain_session_lifecycle",
                    new=mock.AsyncMock(),
                ),
                mock.patch.object(logic_engine, "log_with_room", log_with_room),
            ):
                await logic_engine._tick_presence_only_mode(
                    rid_raw="room-x",
                    room_id="room-x",
                    cfg=cfg,
                    climate_data={"target_temp": 24},
                    presence_raw="off",
                    resolved_occupied=False,
                    indoor_temp=24.0,
                    now=now,
                    st=st,
                )

        first_logs = mock.Mock()
        asyncio.run(run_once(first_logs))

        self.assertTrue(st.presence_only_idle)
        self.assertFalse(st.ac_is_on)
        self.assertFalse(st.physical_ac_on)
        self.assertFalse(st.effective_ac_on)
        self.assertEqual(st.ac_state, "off")
        self.assertEqual(st.effective_control_source, "presence_idle")
        self.assertIsNone(st.pending_action)
        self.assertIsNone(st.pending_since)
        self.assertFalse(st.pending_on_ir_sent)
        self.assertIsNone(st.session_start_time)
        self.assertEqual(st.session_state, "idle")
        self.assertIsNone(st.last_command_time)
        self.assertEqual(st.last_command, "")
        self.assertIsNone(st.last_sent_command_key)
        self.assertIsNone(st.last_decision_at)
        self.assertIsNone(st.ir_last_sent_ts)
        self.assertTrue(
            any("[PRESENCE_ONLY] off_finalize" in str(call.args) for call in first_logs.call_args_list)
        )
        self.assertTrue(
            any("[PRESENCE_ONLY] runtime_reset" in str(call.args) for call in first_logs.call_args_list)
        )
        self.assertTrue(
            any("[PRESENCE_ONLY] idle_entered" in str(call.args) for call in first_logs.call_args_list)
        )
        self.assertTrue(
            any(
                "[PRESENCE_ONLY] duplicate_off_block_detected" in str(call.args)
                for call in first_logs.call_args_list
            )
        )
        self.assertTrue(
            any("action=%s source=%s" in str(call.args) and "idle" in str(call.args) for call in first_logs.call_args_list)
        )

        second_logs = mock.Mock()
        asyncio.run(run_once(second_logs))
        self.assertFalse(
            any("[PRESENCE_ONLY] off_finalize" in str(call.args) for call in second_logs.call_args_list)
        )
        self.assertFalse(
            any("action=hold source=presence_only" in str(call.args) for call in second_logs.call_args_list)
        )

    def test_presence_only_vacant_running_sends_off_then_waits_for_power_confirmation(self):
        logic_engine._runtime_by_room.clear()
        rid = "presence-active-off"
        st = logic_engine._rt(rid)
        base = datetime.now(timezone.utc)
        st.ac_is_on = True
        st.physical_ac_on = True
        st.effective_ac_on = True
        st.vacant_since = base - timedelta(minutes=6)
        st.effective_on_since_ts = (base - timedelta(minutes=10)).timestamp()
        st.last_ac_on_at = st.effective_on_since_ts
        st.last_confirmed_on_at = base - timedelta(minutes=10)
        st.session_start_time = base - timedelta(minutes=10)
        st.session_state = "confirmed"
        st.startup_state_loaded = True
        cfg = {
            "control_mode": "presence_only",
            "climate_entity": "climate.test",
            "ir_backend": "aerostate",
            "energy_power_entity": "sensor.power",
            "target_temp": 24,
            "vacancy_timeout_minutes": 5,
            "off_delay_seconds": 3600,
        }

        async def close_session(*_args, **_kwargs):
            st.session_start_time = None
            st.session_state = "idle"

        async def run_once(power, now, log_with_room):
            async def fake_full(entity_id):
                if entity_id == "sensor.power":
                    return {
                        "state": str(power),
                        "attributes": {
                            "device_class": "power",
                            "state_class": "measurement",
                            "unit_of_measurement": "W",
                        },
                    }
                return None

            with (
                mock.patch.object(
                    logic_engine.ha_client,
                    "get_state",
                    new=mock.AsyncMock(return_value=str(power)),
                ),
                mock.patch.object(
                    logic_engine.ha_client,
                    "get_entity_state_full",
                    side_effect=fake_full,
                ),
                mock.patch.object(
                    logic_engine.ac_aerostate_adapter,
                    "turn_off",
                    new=mock.AsyncMock(return_value=True),
                ) as turn_off,
                mock.patch.object(
                    logic_engine,
                    "_maintain_session_lifecycle",
                    new=mock.AsyncMock(),
                ),
                mock.patch.object(
                    logic_engine,
                    "_close_session",
                    new=mock.AsyncMock(side_effect=close_session),
                ) as close_session_mock,
                mock.patch.object(
                    logic_engine.session_logger,
                    "current_session_id",
                    return_value=None,
                ),
                mock.patch.object(logic_engine, "log_with_room", log_with_room),
            ):
                await logic_engine._tick_presence_only_mode(
                    rid_raw=rid,
                    room_id=rid,
                    cfg=cfg,
                    climate_data={"target_temp": 24, "mode": "off"},
                    presence_raw="off",
                    resolved_occupied=False,
                    indoor_temp=24.0,
                    now=now,
                    st=st,
                )
            return turn_off, close_session_mock

        first_logs = mock.Mock()
        turn_off, close_session_mock = asyncio.run(run_once(800, base, first_logs))
        turn_off.assert_awaited_once_with("climate.test")
        close_session_mock.assert_not_called()
        self.assertEqual(st.pending_action, "off")
        self.assertFalse(st.presence_only_idle)
        self.assertEqual(st.ac_state, "pending_off")
        self.assertFalse(
            any("[PRESENCE_ONLY] off_finalize" in str(call.args) for call in first_logs.call_args_list)
        )

        second_logs = mock.Mock()
        turn_off, close_session_mock = asyncio.run(
            run_once(800, datetime.now(timezone.utc) + timedelta(seconds=1), second_logs)
        )
        turn_off.assert_not_awaited()
        close_session_mock.assert_not_called()
        self.assertEqual(st.pending_action, "off")
        self.assertTrue(st.pending_off_confirmation)
        self.assertFalse(st.presence_only_idle)
        self.assertFalse(
            any("[PRESENCE_ONLY] off_finalize" in str(call.args) for call in second_logs.call_args_list)
        )

        third_logs = mock.Mock()
        turn_off, close_session_mock = asyncio.run(
            run_once(0, datetime.now(timezone.utc) + timedelta(seconds=2), third_logs)
        )
        turn_off.assert_not_awaited()
        close_session_mock.assert_not_called()
        self.assertTrue(st.presence_only_idle)
        self.assertFalse(st.physical_ac_on)
        self.assertIsNone(st.pending_action)
        self.assertEqual(st.ac_state, "off")
        self.assertTrue(
            any("[PRESENCE_ONLY] off_finalize" in str(call.args) for call in third_logs.call_args_list)
        )
        self.assertTrue(
            any("[PRESENCE_ONLY] runtime_reset" in str(call.args) for call in third_logs.call_args_list)
        )
        self.assertTrue(
            any("[PRESENCE_ONLY] idle_entered" in str(call.args) for call in third_logs.call_args_list)
        )

    def test_presence_only_tick_uses_single_vacancy_stabilization(self):
        logic_engine._runtime_by_room.clear()
        rid = "presence-dup"
        cfg = {
            "rooms": [
                {
                    "id": rid,
                    "climate_entity": "climate.test",
                    "presence_entity": "binary_sensor.presence",
                    "indoor_temp_entity": "sensor.temp",
                    "energy_power_entity": "sensor.power",
                    "control_mode": "presence_only",
                    "use_presence": True,
                },
            ],
        }

        async def fake_get_state(entity_id):
            if entity_id == "sensor.temp":
                return "24"
            if entity_id == "sensor.power":
                return "0"
            return "off"

        async def run_case():
            with (
                mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine, "_load_startup_state", new=mock.AsyncMock()),
                mock.patch.object(
                    logic_engine.ha_client,
                    "get_climate_state",
                    new=mock.AsyncMock(return_value={"state": "off", "target_temp": 24}),
                ),
                mock.patch.object(logic_engine.ha_client, "get_state", side_effect=fake_get_state),
                mock.patch.object(
                    logic_engine,
                    "_maintain_session_lifecycle",
                    new=mock.AsyncMock(),
                ),
                mock.patch.object(logic_engine, "log_with_room") as log_with_room,
            ):
                await logic_engine._tick_impl(rid, rid)
            return log_with_room

        log_with_room = asyncio.run(run_case())
        block_count = sum(
            "Block OFF" in str(call.args) and "vacancy not stable" in str(call.args)
            for call in log_with_room.call_args_list
        )
        self.assertEqual(block_count, 1)

    def test_presence_only_max_runtime_failsafe_forces_off(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.effective_on_since_ts = (now - timedelta(minutes=31)).timestamp()

        with mock.patch.object(logic_engine, "log_with_room") as log_with_room:
            action, source, occupied = logic_engine._resolve_presence_only_decision(
                "room-x",
                {
                    "control_mode": "presence_only",
                    "presence_only_max_runtime_minutes": 30,
                },
                st,
                "on",
                ac_on=True,
                now=now,
            )

        self.assertEqual((action, source, occupied), ("off", "presence_max_runtime", True))
        self.assertTrue(log_with_room.called)

    def test_thermostat_decision_unchanged_by_control_mode_default(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        action, source, target = logic_engine._resolve_control_decision(
            "room-x",
            {},
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=True,
            ac_on=False,
            now=now,
        )
        self.assertEqual((action, source, target), ("on", "thermostat", 24.0))

    def test_cooldown_does_not_block_pending_initial_on(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        st = logic_engine._rt("room-x")
        st.pending_action = "on"
        st.last_command_time = now - timedelta(seconds=10)

        action, source, target = logic_engine._resolve_control_decision(
            "room-x",
            {},
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=True,
            ac_on=False,
            now=now,
        )

        self.assertEqual((action, source, target), ("on", "thermostat", 24.0))

    def test_vacancy_off_uses_running_protection_from_on_command(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        st = logic_engine._rt("room-x")
        st.vacant_since = now - timedelta(seconds=61)
        st.ac_is_on = True
        st.last_command = "on"
        st.last_command_time = now - timedelta(seconds=30)

        action, source, target = logic_engine._resolve_control_decision(
            "room-x",
            {"vacancy_timeout_minutes": 0},
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=False,
            ac_on=False,
            now=now,
        )

        self.assertEqual((action, source, target), ("hold_vacant", "running_protection", 24.0))

    def test_vacancy_off_has_minimum_presence_exit_confirmation(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        st = logic_engine._rt("room-x")
        st.vacant_since = now - timedelta(seconds=30)
        st.ac_is_on = True

        action, source, target = logic_engine._resolve_control_decision(
            "room-x",
            {"vacancy_timeout_minutes": 0},
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=False,
            ac_on=False,
            now=now,
        )

        self.assertEqual((action, source, target), ("hold", "vacancy_debounce", 24.0))

    def test_brief_vacancy_flicker_after_on_does_not_reach_off(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        st = logic_engine._rt("room-x")
        st.ac_is_on = True
        st.last_confirmed_on_at = now - timedelta(seconds=15)

        self.assertTrue(logic_engine._stabilize_presence(st, "on", now - timedelta(seconds=20)))
        stable = logic_engine._stabilize_presence(st, "off", now)

        action, source, target = logic_engine._resolve_control_decision(
            "room-x",
            {"vacancy_timeout_minutes": 0},
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=stable,
            ac_on=True,
            now=now,
        )

        self.assertTrue(stable)
        self.assertEqual((action, source, target), ("hold", "thermostat", 24.0))

    def test_off_blocked_during_post_on_protection(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        st = logic_engine._rt("room-x")
        st.ac_is_on = True
        st.vacant_since = now - timedelta(seconds=logic_engine.VACANCY_CONFIRM_SECS + 5)
        st.vacancy_confirmed_at = st.vacant_since
        st.stable_occupied = False
        st.last_confirmed_on_at = now - timedelta(seconds=30)

        action, source, target = logic_engine._resolve_control_decision(
            "room-x",
            {"vacancy_timeout_minutes": 0},
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=False,
            ac_on=True,
            now=now,
        )

        self.assertEqual((action, source, target), ("hold_vacant", "running_protection", 24.0))

    def test_stable_vacancy_allows_off_after_post_on_protection(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        st = logic_engine._rt("room-x")
        st.ac_is_on = True
        st.vacant_since = now - timedelta(seconds=logic_engine.VACANCY_CONFIRM_SECS + 5)
        st.vacancy_confirmed_at = st.vacant_since
        st.stable_occupied = False
        st.last_confirmed_on_at = now - timedelta(seconds=logic_engine.RUNNING_OFF_BLOCK_SECS + 5)

        action, source, target = logic_engine._resolve_control_decision(
            "room-x",
            {"vacancy_timeout_minutes": 0},
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=False,
            ac_on=True,
            now=now,
        )

        self.assertEqual((action, source, target), ("off", "safety_vacant", 24.0))

    def test_confirmed_zone_presence_does_not_clear_room_vacancy_runtime(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        rid = "zone-reentry"
        st = logic_engine._rt(rid)
        st.zone_present = True
        st.zone_confirmed = True
        st.zone_sensor_usable = True
        st.zone_confidence = "high"
        st.occupied = False
        st.stable_occupied = False
        st.last_known_presence = False
        st.vacant_since = now - timedelta(minutes=6)
        st.vacancy_confirmed_at = now - timedelta(minutes=5)
        st.vacancy_active = True
        st.vacancy_hold = True
        st.safety_vacant = True
        st.pending_vacancy = False
        st.thermostat_blocked = True
        st.effective_control_source = "safety_vacant"
        st.last_command = "off"
        st.last_command_time = now - timedelta(seconds=5)
        st.off_reason = "vacant"
        st.off_dispatch_pending = True
        st.off_dispatched_at = now - timedelta(seconds=5)

        with mock.patch.object(logic_engine, "log_with_room") as log_with_room:
            action, source, target = logic_engine._resolve_control_decision(
                rid,
                {"zone_entity_id": "binary_sensor.zone", "vacancy_timeout_minutes": 0},
                indoor_temp=24.0,
                effective_target=24.0,
                is_occupied=False,
                ac_on=False,
                now=now,
            )

        self.assertEqual((action, source, target), ("hold_vacant", "safety_vacant", 24.0))
        self.assertFalse(st.occupied)
        self.assertFalse(st.stable_occupied)
        self.assertFalse(st.last_known_presence)
        self.assertIsNotNone(st.vacant_since)
        self.assertTrue(st.vacancy_active)
        self.assertTrue(st.vacancy_hold)
        self.assertTrue(st.safety_vacant)
        self.assertTrue(st.thermostat_blocked)
        self.assertEqual(st.last_command, "off")
        self.assertEqual(st.off_reason, "vacant")
        self.assertTrue(st.off_dispatch_pending)
        self.assertFalse(
            any("[RUNTIME] vacancy_cleared" in str(call.args) for call in log_with_room.call_args_list)
        )

    def test_reoccupancy_cancels_pending_vacancy_shutdown_task(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.occupied = False
        st.stable_occupied = False
        st.last_known_presence = False
        st.vacant_since = now - timedelta(seconds=30)
        st.vacancy_active = True
        st.pending_vacancy = True
        st.pending_action = "off"
        st.off_reason = "vacant"
        st.vacancy_generation = 7

        async def run_case():
            task = asyncio.create_task(asyncio.sleep(60))
            st.pending_vacancy_task = task
            st.pending_delay_wakeup_task = task
            with mock.patch.object(logic_engine, "log_with_room") as log_with_room:
                recovered = logic_engine._clear_vacancy_state(
                    "room-x",
                    st,
                    now,
                    reason="presence_reentry",
                )
                await asyncio.sleep(0)
            return task, recovered, log_with_room

        task, recovered, log_with_room = asyncio.run(run_case())
        self.assertTrue(recovered)
        self.assertTrue(task.cancelled())
        self.assertIsNone(st.pending_vacancy_task)
        self.assertIsNone(st.pending_delay_wakeup_task)
        self.assertIsNone(st.pending_action)
        self.assertIsNone(st.pending_vacancy_deadline)
        self.assertEqual(st.vacancy_generation, 8)
        self.assertTrue(st.occupied)
        self.assertFalse(st.pending_vacancy)
        self.assertTrue(
            any("[VACANCY] cancelled" in str(call.args) for call in log_with_room.call_args_list)
        )

    def test_stale_vacancy_delayed_off_ignored_after_reoccupancy(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.physical_ac_on = True
        st.occupied = True
        st.stable_occupied = True
        st.last_known_presence = True
        st.pending_action = "off"
        st.off_reason = "vacant"
        st.vacancy_generation = 3

        async def run_case():
            with (
                mock.patch.object(
                    logic_engine.ac_aerostate_adapter,
                    "turn_off",
                    new=mock.AsyncMock(return_value=True),
                ) as turn_off,
                mock.patch.object(logic_engine, "log_with_room") as log_with_room,
            ):
                await logic_engine._handle_delayed_off(
                    "room-x",
                    "room-x",
                    {"climate_entity": "climate.x", "off_delay_seconds": 0},
                    24.0,
                    now,
                    st,
                    reason="vacant",
                    force=True,
                )
            return turn_off, log_with_room

        turn_off, log_with_room = asyncio.run(run_case())
        turn_off.assert_not_awaited()
        self.assertIsNone(st.pending_action)
        self.assertTrue(
            any("[VACANCY] stale_timer_ignored" in str(call.args) for call in log_with_room.call_args_list)
        )

    def test_confirmed_zone_presence_does_not_override_room_vacancy(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        rid = "zone-thermostat-reentry"
        st = logic_engine._rt(rid)
        st.zone_present = True
        st.zone_confirmed = True
        st.zone_sensor_usable = True
        st.occupied = False
        st.stable_occupied = False
        st.last_known_presence = False
        st.vacant_since = now - timedelta(minutes=10)
        st.vacancy_active = True
        st.safety_vacant = True
        st.thermostat_blocked = True
        st.ac_is_on = True
        st.effective_on_since_ts = (
            now - timedelta(seconds=logic_engine.RUNNING_OFF_BLOCK_SECS + 5)
        ).timestamp()

        action, source, target = logic_engine._resolve_control_decision(
            rid,
            {
                "zone_entity_id": "binary_sensor.zone",
                "zone_required_for_on": True,
                "vacancy_timeout_minutes": 0,
            },
            indoor_temp=27.0,
            effective_target=24.0,
            is_occupied=False,
            ac_on=True,
            now=now,
        )

        self.assertEqual((action, source, target), ("off", "safety_vacant", 24.0))
        self.assertFalse(st.occupied)
        self.assertFalse(st.stable_occupied)
        self.assertFalse(st.last_known_presence)
        self.assertTrue(st.safety_vacant)
        self.assertTrue(st.thermostat_blocked)

    def test_zone_absence_does_not_start_vacancy_if_room_presence_reports_occupied(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        rid = "zone-absence-room-occupied"
        st = logic_engine._rt(rid)
        st.zone_sensor_usable = True
        st.zone_present = False
        st.occupied = True
        st.stable_occupied = True
        st.last_known_presence = True
        st.ac_is_on = True

        cfg = {"zone_entity_id": "binary_sensor.zone", "vacancy_timeout_minutes": 2}
        logic_engine._sync_runtime_occupancy(
            rid,
            st,
            True,
            now,
            source="test_presence",
        )
        action, source, target = logic_engine._resolve_control_decision(
            rid,
            cfg,
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=True,
            ac_on=True,
            now=now,
        )

        action2, source2, target2 = logic_engine._resolve_control_decision(
            rid,
            cfg,
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=True,
            ac_on=True,
            now=now + timedelta(seconds=30),
        )

        self.assertEqual((action, source, target), ("hold", "thermostat", 24.0))
        self.assertEqual((action2, source2, target2), ("hold", "thermostat", 24.0))
        self.assertIsNone(st.vacant_since)
        self.assertTrue(st.occupied)
        self.assertTrue(st.stable_occupied)

    def test_zone_absent_occupied_running_ac_does_not_enter_zone_gate_deadlock(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        rid = "zone-absent-running-no-deadlock"
        st = logic_engine._rt(rid)
        st.zone_sensor_usable = True
        st.zone_present = False
        st.zone_confirmed = False
        st.occupied = True
        st.stable_occupied = True
        st.last_known_presence = True
        st.ac_is_on = False
        st.physical_ac_on = True
        st.effective_ac_on = True
        st.ac_state = "on"

        cfg = {
            "zone_entity_id": "binary_sensor.zone",
            "zone_required_for_on": True,
            "vacancy_timeout_minutes": 2,
        }
        action, source, target = logic_engine._resolve_control_decision(
            rid,
            cfg,
            indoor_temp=27.0,
            effective_target=24.0,
            is_occupied=True,
            ac_on=False,
            now=now,
        )
        action, source, blocked = logic_engine._fp2_zone_apply_on_gate(
            rid,
            cfg,
            action,
            source,
        )

        self.assertEqual((action, source, blocked), ("on", "thermostat", False))
        self.assertEqual(target, 24.0)
        self.assertTrue(st.occupied)
        self.assertTrue(st.stable_occupied)
        self.assertIsNone(st.vacant_since)

    def test_zone_absence_defers_vacancy_timer_to_room_presence(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        rid = "zone-absence-room-vacant"
        st = logic_engine._rt(rid)
        st.zone_sensor_usable = True
        st.zone_present = False
        st.occupied = True
        st.stable_occupied = True
        st.last_known_presence = True
        st.ac_is_on = True

        cfg = {"zone_entity_id": "binary_sensor.zone", "vacancy_timeout_minutes": 2}
        logic_engine._sync_runtime_occupancy(
            rid,
            st,
            False,
            now,
            source="test_presence",
        )
        action, source, target = logic_engine._resolve_control_decision(
            rid,
            cfg,
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=False,
            ac_on=True,
            now=now,
        )
        first_vacant_since = st.vacant_since

        logic_engine._sync_runtime_occupancy(
            rid,
            st,
            False,
            now + timedelta(seconds=30),
            source="test_presence",
        )
        action2, source2, target2 = logic_engine._resolve_control_decision(
            rid,
            cfg,
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=False,
            ac_on=True,
            now=now + timedelta(seconds=30),
        )

        self.assertEqual((action, source, target), ("hold", "vacancy_debounce", 24.0))
        self.assertEqual((action2, source2, target2), ("hold", "vacancy_debounce", 24.0))
        self.assertIs(st.vacant_since, first_vacant_since)
        self.assertEqual(st.vacant_since, now)
        self.assertFalse(st.occupied)
        self.assertFalse(st.stable_occupied)

    def test_live_presence_false_overrides_stale_stabilized_occupied_state(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        rid = "presence-false-authoritative"
        st = logic_engine._rt(rid)
        logic_engine._stabilize_presence(st, "on", now)

        stabilized = logic_engine._stabilize_presence(
            st,
            "off",
            now + timedelta(seconds=5),
            rid,
        )
        self.assertTrue(stabilized)

        with mock.patch.object(logic_engine.logger, "debug") as debug:
            occupied = logic_engine._sync_runtime_occupancy(
                rid,
                st,
                bool(logic_engine.parse_presence("off")),
                now + timedelta(seconds=5),
                source="ha_presence",
            )

        self.assertFalse(occupied)
        self.assertFalse(st.occupied)
        self.assertFalse(st.stable_occupied)
        self.assertFalse(st.last_known_presence)
        self.assertTrue(
            any("[OCCUPANCY_SYNC]" in str(call.args[0]) for call in debug.call_args_list)
        )

    def test_zone_presence_cannot_reset_monotonic_room_vacancy_timer(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        rid = "zone-present-room-vacant-monotonic"
        st = logic_engine._rt(rid)
        st.zone_sensor_usable = True
        st.zone_present = True
        st.zone_confirmed = True
        st.occupied = True
        st.stable_occupied = True
        st.last_known_presence = True
        st.ac_is_on = True

        cfg = {"zone_entity_id": "binary_sensor.zone", "vacancy_timeout_minutes": 2}
        logic_engine._sync_runtime_occupancy(
            rid,
            st,
            False,
            now,
            source="test_presence",
        )
        logic_engine._resolve_control_decision(
            rid,
            cfg,
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=False,
            ac_on=True,
            now=now,
        )
        first_vacant_since = st.vacant_since

        logic_engine._sync_runtime_occupancy(
            rid,
            st,
            False,
            now + timedelta(seconds=30),
            source="test_presence",
        )
        logic_engine._resolve_control_decision(
            rid,
            cfg,
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=False,
            ac_on=True,
            now=now + timedelta(seconds=30),
        )

        self.assertEqual(st.vacant_since, first_vacant_since)
        self.assertEqual(first_vacant_since, now)
        self.assertFalse(st.occupied)
        self.assertFalse(st.stable_occupied)

    def test_room_vacancy_timeout_off_ignores_zone_absence(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        rid = "room-vacant-zone-absent-off"
        st = logic_engine._rt(rid)
        st.zone_sensor_usable = True
        st.zone_present = False
        st.zone_confirmed = False
        st.occupied = False
        st.stable_occupied = False
        st.last_known_presence = False
        st.vacant_since = now - timedelta(seconds=logic_engine.VACANCY_CONFIRM_SECS + 30)
        st.ac_is_on = True
        st.effective_on_since_ts = (
            now - timedelta(seconds=logic_engine.RUNNING_OFF_BLOCK_SECS + 30)
        ).timestamp()

        cfg = {
            "zone_entity_id": "binary_sensor.zone",
            "zone_required_for_on": True,
            "vacancy_timeout_minutes": 0,
        }
        action, source, target = logic_engine._resolve_control_decision(
            rid,
            cfg,
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=False,
            ac_on=True,
            now=now,
        )
        action, source, blocked = logic_engine._fp2_zone_apply_on_gate(
            rid,
            cfg,
            action,
            source,
        )

        self.assertEqual((action, source, blocked), ("off", "safety_vacant", False))
        self.assertEqual(target, 24.0)
        self.assertFalse(st.occupied)
        self.assertFalse(st.stable_occupied)

    def test_zone_gated_presence_return_waits_for_zone_before_occupying(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        rid = "room-return-zone-absent"
        st = logic_engine._rt(rid)
        st.zone_sensor_usable = True
        st.zone_present = False
        st.zone_confirmed = False
        st.occupied = False
        st.stable_occupied = False
        st.last_known_presence = False
        st.vacant_since = now - timedelta(seconds=30)
        st.vacancy_active = True
        st.pending_vacancy = True
        cfg = {
            "zone_entity_id": "binary_sensor.zone",
            "zone_required_for_on": True,
            "vacancy_timeout_minutes": 2,
        }

        resolved = logic_engine._sync_runtime_occupancy(
            rid,
            st,
            True,
            now,
            cfg=cfg,
            source="test_presence",
        )
        action, source, target = logic_engine._resolve_control_decision(
            rid,
            cfg,
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=resolved,
            ac_on=True,
            now=now,
        )

        self.assertEqual((action, source, target), ("hold", "zone_wait", 24.0))
        self.assertFalse(resolved)
        self.assertFalse(st.occupied)
        self.assertFalse(st.stable_occupied)
        self.assertTrue(st.last_known_presence)
        self.assertEqual(st.vacant_since, now - timedelta(seconds=30))
        self.assertFalse(st.pending_vacancy)

        st.zone_confirmed = True
        resolved2 = logic_engine._sync_runtime_occupancy(
            rid,
            st,
            True,
            now + timedelta(seconds=5),
            cfg=cfg,
            source="test_presence",
        )
        self.assertTrue(resolved2)
        self.assertTrue(st.occupied)
        self.assertTrue(st.stable_occupied)
        self.assertIsNone(st.vacant_since)
        self.assertFalse(st.vacancy_active)

    def test_zone_presence_does_not_cancel_vacancy_without_room_presence(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        rid = "zone-return-before-timeout"
        st = logic_engine._rt(rid)
        st.zone_sensor_usable = True
        st.zone_present = True
        st.zone_confirmed = False
        st.occupied = False
        st.stable_occupied = False
        st.last_known_presence = False
        st.vacant_since = now - timedelta(seconds=30)
        st.vacancy_active = True
        st.pending_vacancy = True

        action, source, target = logic_engine._resolve_control_decision(
            rid,
            {"zone_entity_id": "binary_sensor.zone", "vacancy_timeout_minutes": 2},
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=False,
            ac_on=True,
            now=now,
        )

        self.assertEqual((action, source, target), ("hold", "vacancy_debounce", 24.0))
        self.assertFalse(st.occupied)
        self.assertFalse(st.stable_occupied)
        self.assertEqual(st.vacant_since, now - timedelta(seconds=30))
        self.assertTrue(st.pending_vacancy)

    def test_zone_confirmed_while_occupied_allows_new_on(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        rid = "zone-confirmed-new-on"
        st = logic_engine._rt(rid)
        st.zone_sensor_usable = True
        st.zone_present = True
        st.zone_confirmed = True
        st.occupied = True
        st.stable_occupied = True
        st.last_known_presence = True
        st.ac_is_on = False
        st.physical_ac_on = False
        st.effective_ac_on = False
        st.ac_state = "off"

        cfg = {
            "zone_entity_id": "binary_sensor.zone",
            "zone_required_for_on": True,
            "vacancy_timeout_minutes": 2,
        }
        action, source, target = logic_engine._resolve_control_decision(
            rid,
            cfg,
            indoor_temp=27.0,
            effective_target=24.0,
            is_occupied=True,
            ac_on=False,
            now=now,
        )
        action, source, blocked = logic_engine._fp2_zone_apply_on_gate(
            rid,
            cfg,
            action,
            source,
        )

        self.assertEqual((action, source, blocked), ("on", "thermostat", False))
        self.assertEqual(target, 24.0)
        self.assertTrue(st.occupied)

    def test_zone_absence_does_not_allow_vacancy_off_if_room_presence_is_occupied(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        rid = "zone-absence-timeout"
        st = logic_engine._rt(rid)
        st.zone_sensor_usable = True
        st.zone_present = False
        st.occupied = True
        st.stable_occupied = True
        st.last_known_presence = True
        st.ac_is_on = True
        st.physical_ac_on = True
        st.vacant_since = now - timedelta(seconds=logic_engine.VACANCY_CONFIRM_SECS + 5)
        st.effective_on_since_ts = (
            now - timedelta(seconds=logic_engine.RUNNING_OFF_BLOCK_SECS + 5)
        ).timestamp()

        action, source, target = logic_engine._resolve_control_decision(
            rid,
            {"zone_entity_id": "binary_sensor.zone", "vacancy_timeout_minutes": 0},
            indoor_temp=26.0,
            effective_target=24.0,
            is_occupied=True,
            ac_on=True,
            now=now,
        )

        self.assertEqual((action, source, target), ("hold", "thermostat", 24.0))
        self.assertTrue(st.occupied)
        self.assertTrue(st.stable_occupied)
        self.assertIsNone(st.vacant_since)

    def test_recovery_does_not_clear_recent_decision_lock(self):
        logic_engine._runtime_by_room.clear()
        now = datetime.now(timezone.utc)
        rid = "zone-reentry-no-spam"
        st = logic_engine._rt(rid)
        st.zone_present = True
        st.zone_confirmed = True
        st.occupied = False
        st.vacant_since = now - timedelta(minutes=5)
        st.vacancy_active = True
        st.last_decision_at = now - timedelta(seconds=2)

        logic_engine._clear_vacancy_state(rid, st, now, reason="presence_reentry")

        self.assertEqual(st.last_decision_at, now - timedelta(seconds=2))
        self.assertTrue(st.occupied)
        self.assertFalse(st.vacancy_active)

    def test_ir_backend_default_and_invalid_are_aerostate(self):
        self.assertEqual(logic_engine.normalize_ir_backend({}), "aerostate")
        self.assertEqual(logic_engine.normalize_ir_backend({"ir_backend": "bad"}), "aerostate")
        self.assertEqual(logic_engine.normalize_ir_backend({"ir_backend": "aerostate"}), "aerostate")
        self.assertEqual(logic_engine.normalize_ir_backend({"ir_backend": "tuya"}), "tuya")

    def test_ir_backend_resolve_uses_explicit_config_only(self):
        async def run_case():
            with (
                mock.patch.object(logic_engine.ha_client, "get_entity_state_full") as full,
                mock.patch.object(logic_engine, "log_with_room"),
            ):
                backend = await logic_engine.resolve_ir_backend(
                    "room-x",
                    {"ir_backend": "aerostate"},
                    "climate.tuya",
                )
            self.assertEqual(backend, "aerostate")
            full.assert_not_called()

        asyncio.run(run_case())

    def test_turn_ac_on_aerostate_uses_single_adapter_dispatch(self):
        logic_engine._runtime_by_room.clear()
        rid = "ir-aerostate"
        cfg = {
            "climate_entity": "climate.aerostate",
            "ir_backend": "aerostate",
            "min_command_interval_seconds": 0,
            "compressor_min_off_seconds": 0,
        }

        async def run_case():
            with (
                mock.patch.object(logic_engine.ha_client, "call_service") as call_service,
                mock.patch.object(logic_engine.ac_aerostate_adapter, "turn_on", return_value=True) as turn_on,
                mock.patch.object(logic_engine.ac_tuya_adapter, "turn_on") as turn_on_tuya,
                mock.patch.object(logic_engine, "log_with_room"),
            ):
                ok = await logic_engine._turn_ac_on(rid, cfg, 27.0, 24.0)
            self.assertTrue(ok)
            call_service.assert_not_called()
            turn_on.assert_awaited_once_with("climate.aerostate", 24.0)
            turn_on_tuya.assert_not_called()
            st = logic_engine._rt(rid)
            self.assertIsNotNone(st.ir_last_sent_ts)
            self.assertIsNotNone(st.just_turned_on_until)

        asyncio.run(run_case())

    def test_ir_send_lock_blocks_aerostate_retrigger(self):
        logic_engine._runtime_by_room.clear()
        rid = "ir-lock"
        now = datetime.now(timezone.utc)
        st = logic_engine._rt(rid)
        st.ir_last_sent_ts = now - timedelta(seconds=5)
        cfg = {
            "climate_entity": "climate.aerostate",
            "ir_backend": "aerostate",
            "min_command_interval_seconds": 0,
            "compressor_min_off_seconds": 0,
        }

        async def run_case():
            with mock.patch.object(logic_engine.ac_aerostate_adapter, "turn_on", return_value=True) as turn_on:
                ok = await logic_engine._turn_ac_on(rid, cfg, 27.0, 24.0, now=now)
            self.assertFalse(ok)
            turn_on.assert_not_called()

        asyncio.run(run_case())

    def test_turn_ac_on_tuya_uses_tuya_adapter_not_aerostate_adapter(self):
        logic_engine._runtime_by_room.clear()
        rid = "ir-tuya"
        cfg = {
            "climate_entity": "climate.tuya",
            "ir_backend": "tuya",
            "min_command_interval_seconds": 0,
            "compressor_min_off_seconds": 0,
        }

        async def run_case():
            with (
                mock.patch.object(logic_engine.ac_tuya_adapter, "turn_on", return_value=True) as tuya_turn_on,
                mock.patch.object(logic_engine.ac_aerostate_adapter, "turn_on") as aerostate_turn_on,
                mock.patch.object(logic_engine, "log_with_room"),
            ):
                ok = await logic_engine._turn_ac_on(rid, cfg, 27.0, 24.0)
            self.assertTrue(ok)
            tuya_turn_on.assert_awaited_once_with(
                "climate.tuya",
                24.0,
                fan_mode="auto",
                hvac_mode="cool",
            )
            aerostate_turn_on.assert_not_called()

        asyncio.run(run_case())

    def test_pending_on_duplicate_guard_blocks_all_ir_backends(self):
        logic_engine._runtime_by_room.clear()
        rid = "ir-dup"
        st = logic_engine._rt(rid)
        st.pending_action = "on"
        st.pending_on_ir_sent = True
        st.pending_on_ir_sent_at = datetime.now(timezone.utc)
        st.physical_ac_on = False

        async def run_case():
            with (
                mock.patch.object(logic_engine.ac_aerostate_adapter, "turn_on") as aerostate_turn_on,
                mock.patch.object(logic_engine.ac_tuya_adapter, "turn_on") as tuya_turn_on,
            ):
                ok_aerostate = await logic_engine._turn_ac_on(
                    rid,
                    {"climate_entity": "climate.a", "ir_backend": "aerostate"},
                    27.0,
                    24.0,
                )
                ok_tuya = await logic_engine._turn_ac_on(
                    rid,
                    {"climate_entity": "climate.t", "ir_backend": "tuya"},
                    27.0,
                    24.0,
                )
            self.assertFalse(ok_aerostate)
            self.assertFalse(ok_tuya)
            aerostate_turn_on.assert_not_called()
            tuya_turn_on.assert_not_called()

        asyncio.run(run_case())

    def test_turn_ac_off_routes_by_ir_backend(self):
        async def run_case(ir_backend, adapter_name):
            logic_engine._runtime_by_room.clear()
            rid = f"off-{ir_backend}"
            st = logic_engine._rt(rid)
            st.ac_is_on = True
            st.last_ac_on_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp()
            adapter = getattr(logic_engine, adapter_name)
            with mock.patch.object(adapter, "turn_off", return_value=True) as turn_off:
                await logic_engine._turn_ac_off(
                    rid,
                    {"climate_entity": f"climate.{ir_backend}", "ir_backend": ir_backend},
                    24.0,
                    "manual",
                    force=True,
                )
            turn_off.assert_awaited_once_with(f"climate.{ir_backend}")

        asyncio.run(run_case("aerostate", "ac_aerostate_adapter"))
        asyncio.run(run_case("tuya", "ac_tuya_adapter"))

    def test_turn_ac_off_blocks_for_minimum_on_time_after_on_command(self):
        logic_engine._runtime_by_room.clear()
        rid = "off-min-on"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_is_on = True
        st.last_command = "on"
        st.last_command_time = now - timedelta(seconds=18)

        async def run_case():
            with mock.patch.object(logic_engine.ac_aerostate_adapter, "turn_off", return_value=True) as turn_off:
                await logic_engine._turn_ac_off(
                    rid,
                    {"climate_entity": "climate.test", "ir_backend": "aerostate"},
                    24.0,
                    "vacant",
                    now=now,
                    force=True,
                )
            turn_off.assert_not_called()

        asyncio.run(run_case())

    def test_close_session_preserves_setpoint_tracking_on_provisional_timeout(self):
        logic_engine._runtime_by_room.clear()
        rid = "session-provisional"
        st = logic_engine._rt(rid)
        st.session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        st.last_applied_setpoint = 24.0

        async def run_case():
            with (
                mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=42),
                mock.patch.object(logic_engine.session_logger, "session_start_time", return_value=st.session_start_time),
                mock.patch.object(logic_engine.session_logger, "end_session", new=mock.AsyncMock()),
                mock.patch.object(logic_engine, "clear_setpoint_command_tracking") as clear_tracking,
                mock.patch.object(logic_engine.smart_cooling, "reset"),
            ):
                await logic_engine._close_session(rid, {}, 24.0, "provisional_timeout")
            clear_tracking.assert_not_called()

        asyncio.run(run_case())

    def test_vacancy_off_terminal_reconciliation_clears_idle(self):
        logic_engine._runtime_by_room.clear()
        rid = "runtime-vacant-off"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_state = "on"
        st.ac_state_source = "power"
        st.physical_ac_on = True
        st.effective_ac_on = True
        st.effective_ac_idle = True
        st.last_command = "off"
        st.last_command_time = now - timedelta(seconds=90)
        st.last_confirmed_on_at = now - timedelta(minutes=8)

        with (
            mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=None),
            mock.patch.object(logic_engine, "log_with_room") as log_with_room,
        ):
            cleared = logic_engine._maybe_finalize_terminal_off(
                rid,
                st,
                now,
                climate_data={"mode": "off"},
                in_cooldown=False,
            )

        self.assertTrue(cleared)
        self.assertEqual(st.ac_state, "off")
        self.assertFalse(st.physical_ac_on)
        self.assertFalse(st.effective_ac_idle)
        self.assertIsNone(st.last_confirmed_on_at)
        self.assertTrue(any("[RUNTIME] finalized_off" in str(c.args) for c in log_with_room.call_args_list))

    def test_aerostate_off_reconciliation_has_no_stuck_idle(self):
        logic_engine._runtime_by_room.clear()
        rid = "runtime-aero-off"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_state = "on"
        st.ac_state_source = "power"
        st.effective_power_source = "watts"
        st.physical_ac_on = True
        st.effective_ac_idle = True
        st.last_command = "off"
        st.last_command_time = now - timedelta(seconds=120)
        st.session_state = "idle"

        with mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=None):
            logic_engine._maybe_finalize_terminal_off(
                rid,
                st,
                now,
                climate_data={"state": "off"},
                in_cooldown=False,
            )

        self.assertEqual(st.ac_state, "off")
        self.assertFalse(st.physical_ac_on)
        self.assertFalse(st.effective_ac_idle)
        self.assertEqual(st.effective_power_source, "internal")

    def test_tuya_delayed_power_drop_keeps_idle_until_reconciled(self):
        logic_engine._runtime_by_room.clear()
        rid = "runtime-tuya-lag"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_state = "on"
        st.physical_ac_on = True
        st.effective_ac_idle = True
        st.last_command = "off"
        st.last_command_time = now - timedelta(seconds=15)

        with mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=None):
            early = logic_engine._maybe_finalize_terminal_off(
                rid,
                st,
                now,
                climate_data={"mode": "off"},
                in_cooldown=True,
            )
            late = logic_engine._maybe_finalize_terminal_off(
                rid,
                st,
                now + timedelta(seconds=90),
                climate_data={"mode": "off"},
                in_cooldown=False,
            )

        self.assertFalse(early)
        self.assertTrue(late)
        self.assertEqual(st.ac_state, "off")
        self.assertFalse(st.effective_ac_idle)

    def test_cooldown_expiration_transitions_idle_to_off(self):
        logic_engine._runtime_by_room.clear()
        rid = "runtime-cooldown-expired"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_state = "on"
        st.physical_ac_on = True
        st.effective_ac_idle = True
        st.last_command = "off"
        st.last_command_time = now

        with mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=None):
            self.assertFalse(
                logic_engine._maybe_finalize_terminal_off(
                    rid,
                    st,
                    now + timedelta(seconds=20),
                    climate_data={"mode": "off"},
                    in_cooldown=True,
                )
            )
            self.assertTrue(
                logic_engine._maybe_finalize_terminal_off(
                    rid,
                    st,
                    now + timedelta(seconds=70),
                    climate_data={"mode": "off"},
                    in_cooldown=False,
                )
            )

        self.assertEqual(st.ac_state, "off")
        self.assertFalse(st.physical_ac_on)

    def test_session_finalized_no_lingering_confirmed_state(self):
        logic_engine._runtime_by_room.clear()
        rid = "runtime-no-session"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_state = "on"
        st.ac_state_source = "power"
        st.physical_ac_on = True
        st.effective_ac_on = True
        st.effective_ac_idle = True
        st.session_state = "idle"
        st.session_runtime_confirmed = True
        st.last_confirmed_on_at = now - timedelta(minutes=15)
        st.last_command = "off"
        st.last_command_time = now - timedelta(seconds=90)

        with mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=None):
            logic_engine._maybe_finalize_terminal_off(
                rid,
                st,
                now,
                climate_data={"mode": "off"},
                in_cooldown=False,
            )

        self.assertFalse(st.session_runtime_confirmed)
        self.assertIsNone(st.last_confirmed_on_at)
        self.assertFalse(st.physical_ac_on)

    def test_manual_off_terminal_reconciliation_clears_stale_idle(self):
        logic_engine._runtime_by_room.clear()
        rid = "runtime-manual-off"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_state = "on"
        st.physical_ac_on = True
        st.effective_ac_idle = True
        st.last_command = "off"
        st.last_command_time = now - timedelta(seconds=80)
        st.last_command_source = "user"

        with mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=None):
            cleared = logic_engine._maybe_finalize_terminal_off(
                rid,
                st,
                now,
                climate_data={"mode": "off"},
                in_cooldown=False,
            )

        self.assertTrue(cleared)
        self.assertEqual(st.ac_state, "off")
        self.assertFalse(st.effective_ac_idle)

    def test_off_while_vacant_runtime_reset_is_deterministic(self):
        logic_engine._runtime_by_room.clear()
        rid = "runtime-vacant-reset"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_state = "pending_off"
        st.ac_state_source = "power"
        st.ac_is_on = True
        st.physical_ac_on = True
        st.effective_ac_on = True
        st.effective_ac_idle = True
        st.effective_on_since_ts = (now - timedelta(minutes=20)).timestamp()
        st.possible_on_since = (now - timedelta(minutes=10)).timestamp()
        st.soft_start_ui = True
        st.compressor_on_since = now - timedelta(minutes=20)
        st.last_confirmed_on_at = now - timedelta(minutes=20)
        st.last_command = "off"
        st.last_command_time = now - timedelta(seconds=100)

        with mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=None):
            logic_engine._maybe_finalize_terminal_off(
                rid,
                st,
                now,
                climate_data={"mode": "off"},
                in_cooldown=False,
            )

        self.assertEqual(st.ac_state, "off")
        self.assertFalse(st.ac_is_on)
        self.assertFalse(st.physical_ac_on)
        self.assertFalse(st.effective_ac_on)
        self.assertFalse(st.effective_ac_idle)
        self.assertIsNone(st.effective_on_since_ts)
        self.assertIsNone(st.possible_on_since)
        self.assertFalse(st.soft_start_ui)
        self.assertIsNone(st.compressor_on_since)

    def test_vacancy_off_sends_off_only_once_during_reconciliation(self):
        logic_engine._runtime_by_room.clear()
        rid = "presence-off-once"
        st = logic_engine._rt(rid)
        base = datetime.now(timezone.utc)
        st.ac_is_on = True
        st.physical_ac_on = True
        st.effective_ac_on = True
        st.vacant_since = base - timedelta(minutes=6)
        st.effective_on_since_ts = (base - timedelta(minutes=10)).timestamp()
        st.last_ac_on_at = st.effective_on_since_ts
        st.startup_state_loaded = True
        cfg = {
            "control_mode": "presence_only",
            "climate_entity": "climate.test",
            "ir_backend": "aerostate",
            "energy_power_entity": "sensor.power",
            "target_temp": 24,
            "vacancy_timeout_minutes": 5,
        }

        async def run_once(power, now):
            async def fake_full(entity_id):
                if entity_id == "sensor.power":
                    return {
                        "state": str(power),
                        "attributes": {
                            "device_class": "power",
                            "state_class": "measurement",
                            "unit_of_measurement": "W",
                        },
                    }
                return None

            with (
                mock.patch.object(logic_engine.ha_client, "get_state", new=mock.AsyncMock(return_value=str(power))),
                mock.patch.object(logic_engine.ha_client, "get_entity_state_full", side_effect=fake_full),
                mock.patch.object(logic_engine.ac_aerostate_adapter, "turn_off", new=mock.AsyncMock(return_value=True)) as turn_off,
                mock.patch.object(logic_engine, "_maintain_session_lifecycle", new=mock.AsyncMock()),
                mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=None),
            ):
                await logic_engine._tick_presence_only_mode(
                    rid_raw=rid,
                    room_id=rid,
                    cfg=cfg,
                    climate_data={"target_temp": 24, "mode": "off"},
                    presence_raw="off",
                    resolved_occupied=False,
                    indoor_temp=24.0,
                    now=now,
                    st=st,
                )
            return turn_off

        first = asyncio.run(run_once(800, base))
        second = asyncio.run(run_once(50, base + timedelta(seconds=20)))

        first.assert_awaited_once()
        second.assert_not_awaited()
        self.assertFalse(st.off_dispatch_pending)
        self.assertEqual(st.last_command, "")
        self.assertTrue(st.presence_only_idle)

    def test_vacancy_off_dispatch_enters_pending_confirmation(self):
        logic_engine._runtime_by_room.clear()
        rid = "vacancy-off-pending"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_is_on = True
        st.physical_ac_on = True
        st.effective_ac_on = True
        st.occupied = False
        st.stable_occupied = False
        st.effective_on_since_ts = (now - timedelta(minutes=10)).timestamp()

        async def run_case():
            with (
                mock.patch.object(logic_engine.ac_tuya_adapter, "turn_off", new=mock.AsyncMock(return_value=True)) as turn_off,
                mock.patch.object(logic_engine, "_close_session", new=mock.AsyncMock()) as close_session,
            ):
                sent = await logic_engine._turn_ac_off(
                    rid,
                    {"climate_entity": "climate.test", "ir_backend": "tuya"},
                    24.0,
                    "vacant",
                    now=now,
                    force=True,
                )
            self.assertTrue(sent)
            turn_off.assert_awaited_once()
            close_session.assert_not_awaited()

        asyncio.run(run_case())

        self.assertTrue(st.pending_off_confirmation)
        self.assertEqual(st.pending_action, "off")
        self.assertEqual(st.ac_state, "pending_off")
        self.assertTrue(st.ac_is_on)
        self.assertTrue(st.physical_ac_on)
        self.assertFalse(st.off_finalized)

    def test_pending_off_power_high_retries_without_finalizing(self):
        logic_engine._runtime_by_room.clear()
        rid = "vacancy-off-retry"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_is_on = True
        st.physical_ac_on = True
        st.pending_action = "off"
        st.pending_off_confirmation = True
        st.pending_off_sent_at = now - timedelta(seconds=30)
        st.off_dispatched_at = st.pending_off_sent_at
        st.last_command = "off"
        st.off_reason = "vacant"
        st.occupied = False
        st.stable_occupied = False

        async def run_case():
            with mock.patch.object(logic_engine.ac_tuya_adapter, "turn_off", new=mock.AsyncMock(return_value=True)) as turn_off:
                finalized = await logic_engine._handle_pending_off_confirmation(
                    rid,
                    {"climate_entity": "climate.test", "ir_backend": "tuya"},
                    24.0,
                    st,
                    now,
                    telemetry_power_reading=800.0,
                    climate_data={"mode": "off"},
                )
            self.assertFalse(finalized)
            turn_off.assert_awaited_once()

        asyncio.run(run_case())

        self.assertTrue(st.pending_off_confirmation)
        self.assertEqual(st.pending_off_retry_count, 1)
        self.assertTrue(st.ac_is_on)
        self.assertFalse(st.off_finalized)

    def test_pending_off_power_drop_finalizes_session_and_runtime(self):
        logic_engine._runtime_by_room.clear()
        rid = "vacancy-off-confirmed"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_is_on = True
        st.physical_ac_on = True
        st.effective_ac_on = True
        st.pending_action = "off"
        st.pending_off_confirmation = True
        st.pending_off_sent_at = now - timedelta(seconds=10)
        st.last_command = "off"
        st.off_reason = "vacant"
        st.occupied = False
        st.stable_occupied = False
        st.session_state = "confirmed"
        st.session_start_time = now - timedelta(minutes=10)

        async def run_case():
            with (
                mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=42),
                mock.patch.object(logic_engine, "_close_session", new=mock.AsyncMock()) as close_session,
            ):
                finalized = await logic_engine._handle_pending_off_confirmation(
                    rid,
                    {"climate_entity": "climate.test"},
                    24.0,
                    st,
                    now,
                    telemetry_power_reading=40.0,
                    climate_data={"mode": "cool"},
                )
            self.assertTrue(finalized)
            close_session.assert_awaited_once()

        asyncio.run(run_case())

        self.assertFalse(st.pending_off_confirmation)
        self.assertIsNone(st.pending_action)
        self.assertEqual(st.ac_state, "off")
        self.assertFalse(st.ac_is_on)
        self.assertFalse(st.physical_ac_on)
        self.assertEqual(st.last_confirmed_off_at, now)

    def test_pending_off_reentry_cancels_retry_and_keeps_runtime_on(self):
        logic_engine._runtime_by_room.clear()
        rid = "vacancy-off-reentry"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_is_on = True
        st.physical_ac_on = True
        st.pending_action = "off"
        st.pending_off_confirmation = True
        st.pending_off_sent_at = now - timedelta(seconds=30)
        st.last_command = "off"
        st.off_reason = "vacant"
        st.occupied = True
        st.stable_occupied = True

        async def run_case():
            with mock.patch.object(logic_engine.ac_tuya_adapter, "turn_off", new=mock.AsyncMock(return_value=True)) as turn_off:
                finalized = await logic_engine._handle_pending_off_confirmation(
                    rid,
                    {"climate_entity": "climate.test", "ir_backend": "tuya"},
                    24.0,
                    st,
                    now,
                    telemetry_power_reading=850.0,
                    climate_data={"mode": "cool"},
                )
            self.assertFalse(finalized)
            turn_off.assert_not_awaited()

        asyncio.run(run_case())

        self.assertFalse(st.pending_off_confirmation)
        self.assertIsNone(st.pending_action)
        self.assertTrue(st.ac_is_on)
        self.assertTrue(st.physical_ac_on)
        self.assertEqual(st.last_command, "")

    def test_pending_off_max_retries_marks_failed_without_idle(self):
        logic_engine._runtime_by_room.clear()
        rid = "vacancy-off-failed"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_is_on = True
        st.physical_ac_on = True
        st.pending_action = "off"
        st.pending_off_confirmation = True
        st.pending_off_sent_at = now - timedelta(seconds=30)
        st.pending_off_retry_count = logic_engine.MAX_OFF_CONFIRM_RETRIES
        st.last_command = "off"
        st.off_reason = "vacant"
        st.occupied = False
        st.stable_occupied = False

        async def run_case():
            with mock.patch.object(logic_engine.ac_tuya_adapter, "turn_off", new=mock.AsyncMock(return_value=True)) as turn_off:
                finalized = await logic_engine._handle_pending_off_confirmation(
                    rid,
                    {"climate_entity": "climate.test", "ir_backend": "tuya"},
                    24.0,
                    st,
                    now,
                    telemetry_power_reading=900.0,
                    climate_data={"mode": "off"},
                )
            self.assertFalse(finalized)
            turn_off.assert_not_awaited()

        asyncio.run(run_case())

        self.assertFalse(st.pending_off_confirmation)
        self.assertTrue(st.off_confirmation_failed)
        self.assertTrue(st.ac_is_on)
        self.assertTrue(st.physical_ac_on)
        self.assertEqual(st.ac_state, "on")
        self.assertFalse(st.off_finalized)

    def test_aerostate_duplicate_off_does_not_repeat(self):
        logic_engine._runtime_by_room.clear()
        rid = "aero-no-repeat"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.last_command = "off"
        st.off_dispatch_pending = True
        st.off_dispatched_at = now - timedelta(seconds=10)
        st.physical_ac_on = True

        async def run_case():
            with mock.patch.object(logic_engine.ac_aerostate_adapter, "turn_off", new=mock.AsyncMock(return_value=True)) as turn_off:
                await logic_engine._turn_ac_off(
                    rid,
                    {"climate_entity": "climate.test", "ir_backend": "aerostate"},
                    24.0,
                    "vacant",
                    now=now,
                    force=True,
                )
            turn_off.assert_not_awaited()

        asyncio.run(run_case())

    def test_tuya_duplicate_off_does_not_repeat(self):
        logic_engine._runtime_by_room.clear()
        rid = "tuya-no-repeat"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.last_command = "off"
        st.off_dispatch_pending = True
        st.off_dispatched_at = now - timedelta(seconds=10)
        st.physical_ac_on = True

        async def run_case():
            with mock.patch.object(logic_engine.ac_tuya_adapter, "turn_off", new=mock.AsyncMock(return_value=True)) as turn_off:
                await logic_engine._turn_ac_off(
                    rid,
                    {"climate_entity": "climate.test", "ir_backend": "tuya"},
                    24.0,
                    "vacant",
                    now=now,
                    force=True,
                )
            turn_off.assert_not_awaited()

        asyncio.run(run_case())

    def test_finalized_off_suppresses_duplicate_off_dispatch(self):
        logic_engine._runtime_by_room.clear()
        rid = "finalized-no-repeat"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.last_command = "off"
        st.off_finalized = True
        st.off_settled_at = now - timedelta(seconds=30)
        st.physical_ac_on = True

        async def run_case():
            with mock.patch.object(logic_engine.ac_aerostate_adapter, "turn_off", new=mock.AsyncMock(return_value=True)) as turn_off:
                await logic_engine._turn_ac_off(
                    rid,
                    {"climate_entity": "climate.test", "ir_backend": "aerostate"},
                    24.0,
                    "vacant",
                    now=now,
                    force=True,
                )
            turn_off.assert_not_awaited()

        asyncio.run(run_case())

    def test_elapsed_safety_vacant_duplicate_finalizes_without_dispatch(self):
        logic_engine._runtime_by_room.clear()
        rid = "safety-vacant-finalize"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.last_command = "off"
        st.last_command_time = now - timedelta(seconds=90)
        st.off_dispatch_pending = True
        st.off_dispatched_at = now - timedelta(seconds=90)
        st.ac_state = "on"
        st.ac_state_source = "power"
        st.physical_ac_on = True
        st.effective_ac_idle = True

        with (
            mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=None),
            mock.patch.object(logic_engine, "log_with_room") as log_with_room,
        ):
            suppressed = logic_engine._should_suppress_duplicate_off(
                rid,
            st,
            now,
            climate_data={"mode": "off"},
        )

        self.assertTrue(suppressed)
        self.assertTrue(st.off_finalized)
        self.assertFalse(st.off_dispatch_pending)
        self.assertEqual(st.ac_state, "off")
        self.assertFalse(st.effective_ac_idle)
        self.assertTrue(any("[RUNTIME] finalized_off" in str(c.args) for c in log_with_room.call_args_list))

    def test_entering_idle_logged_once_when_off_is_repeated(self):
        logic_engine._runtime_by_room.clear()
        rid = "idle-once"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.ac_is_on = True
        st.physical_ac_on = True

        async def run_case():
            with (
                mock.patch.object(logic_engine.ac_aerostate_adapter, "turn_off", new=mock.AsyncMock(return_value=True)) as turn_off,
                mock.patch.object(logic_engine, "log_with_room") as log_with_room,
            ):
                await logic_engine._turn_ac_off(
                    rid,
                    {"climate_entity": "climate.test", "ir_backend": "aerostate"},
                    24.0,
                    "vacant",
                    now=now,
                    force=True,
                )
                await logic_engine._turn_ac_off(
                    rid,
                    {"climate_entity": "climate.test", "ir_backend": "aerostate"},
                    24.0,
                    "vacant",
                    now=now + timedelta(seconds=20),
                    force=True,
                )
            turn_off.assert_awaited_once()
            entering = [
                c for c in log_with_room.call_args_list
                if "[RUNTIME] entering_idle" in str(c.args)
            ]
            pending = [
                c for c in log_with_room.call_args_list
                if "[OFF_CONFIRM] pending" in str(c.args)
            ]
            self.assertEqual(len(entering), 0)
            self.assertEqual(len(pending), 1)

        asyncio.run(run_case())

    def test_ui_runtime_state_becomes_off_after_reconciliation(self):
        logic_engine._runtime_by_room.clear()
        rid = "ui-off-runtime"
        now = datetime.now(timezone.utc)
        st = logic_engine._rt(rid)
        st.ac_state = "on"
        st.ac_state_source = "power"
        st.ac_is_on = True
        st.physical_ac_on = True
        st.effective_ac_on = True
        st.effective_ac_idle = True
        st.last_command = "off"
        st.last_command_time = now - timedelta(seconds=90)

        with mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=None):
            logic_engine._maybe_finalize_terminal_off(
                rid,
                st,
                now,
                climate_data={"mode": "off"},
                in_cooldown=False,
            )
        with (
            mock.patch.object(logic_engine.config_manager, "load_config", return_value={"rooms": [{"id": rid}]}),
            mock.patch.object(logic_engine.room_registry, "merge_room_config", return_value={}),
            mock.patch.object(logic_engine.session_logger, "current_session_id", return_value=None),
        ):
            runtime = logic_engine.get_runtime_state(rid)

        self.assertEqual(runtime["ac_state"], "off")
        self.assertFalse(runtime["ac_idle"])
        self.assertFalse(runtime["physical_ac_on"])
        self.assertFalse(runtime["effective_ac_on"])

    def test_cooldown_expiration_does_not_redispatch_off(self):
        logic_engine._runtime_by_room.clear()
        rid = "cooldown-no-repeat"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.last_command = "off"
        st.last_command_time = now - timedelta(seconds=70)
        st.off_finalized = True
        st.physical_ac_on = False

        async def run_case():
            with mock.patch.object(logic_engine.ac_aerostate_adapter, "turn_off", new=mock.AsyncMock(return_value=True)) as turn_off:
                await logic_engine._turn_ac_off(
                    rid,
                    {"climate_entity": "climate.test", "ir_backend": "aerostate"},
                    24.0,
                    "vacant",
                    now=now,
                    force=True,
                )
            turn_off.assert_not_awaited()

        asyncio.run(run_case())

    def test_long_confirmed_cooling_session_stores_valid(self):
        logic_engine._runtime_by_room.clear()
        rid = "session-long-valid"
        st = logic_engine._rt(rid)
        st.session_start_time = datetime.now(timezone.utc) - timedelta(minutes=45)
        st.session_start_temp = 28.0
        st.session_state = "confirmed"
        st.session_runtime_confirmed = True
        st.watts_samples = [720.0, 760.0, 740.0]

        async def run_case():
            end_session = mock.AsyncMock()
            with (
                mock.patch.object(logic_engine.session_logger, "current_session_id", return_value="sid"),
                mock.patch.object(logic_engine.session_logger, "session_start_time", return_value=st.session_start_time),
                mock.patch.object(logic_engine.session_logger, "end_session", new=end_session),
                mock.patch.object(logic_engine, "clear_setpoint_command_tracking"),
                mock.patch.object(logic_engine.smart_cooling, "reset"),
            ):
                await logic_engine._close_session(rid, {}, 24.5, "thermostat_reached")
            payload = end_session.await_args.args[1]
            self.assertEqual(payload["is_record_valid"], 1)
            self.assertGreater(payload["energy_kwh"], 0)
            self.assertGreater(payload["time_to_cool_minutes"], 40)

        asyncio.run(run_case())

    def test_delayed_power_confirmation_promotes_provisional(self):
        logic_engine._runtime_by_room.clear()
        rid = "session-delayed-power"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.session_start_time = now - timedelta(seconds=logic_engine.MAX_PROVISIONAL_SECONDS + 30)
        st.ac_is_on = True
        st.last_ac_on_at = st.session_start_time.timestamp()

        async def run_case():
            with (
                mock.patch.object(logic_engine.session_logger, "current_session_id", return_value="sid"),
                mock.patch.object(logic_engine.session_logger, "current_session_is_provisional", return_value=True),
                mock.patch.object(logic_engine.session_logger, "session_start_time", return_value=st.session_start_time),
                mock.patch.object(logic_engine.session_logger, "upgrade_current_session_to_confirmed", new=mock.AsyncMock()) as upgrade,
                mock.patch.object(logic_engine, "_close_session", new=mock.AsyncMock()) as close_session,
            ):
                await logic_engine._maintain_session_lifecycle(
                    rid,
                    {},
                    25.0,
                    now,
                    24.0,
                    in_cooldown=False,
                    confirmed_ac_on=True,
                    inferred_only_physical=False,
                )
            upgrade.assert_awaited()
            close_session.assert_not_awaited()

        asyncio.run(run_case())

    def test_presence_only_vacancy_shutdown_preserves_valid_session(self):
        logic_engine._runtime_by_room.clear()
        rid = "session-presence-valid"
        st = logic_engine._rt(rid)
        st.session_start_time = datetime.now(timezone.utc) - timedelta(minutes=12)
        st.session_start_temp = 27.0
        st.session_state = "confirmed"
        st.session_runtime_confirmed = True
        st.watts_samples = [650.0, 670.0]

        async def run_case():
            end_session = mock.AsyncMock()
            with (
                mock.patch.object(logic_engine.session_logger, "current_session_id", return_value="sid"),
                mock.patch.object(logic_engine.session_logger, "session_start_time", return_value=st.session_start_time),
                mock.patch.object(logic_engine.session_logger, "end_session", new=end_session),
                mock.patch.object(logic_engine, "clear_setpoint_command_tracking"),
                mock.patch.object(logic_engine.smart_cooling, "reset"),
            ):
                await logic_engine._close_session(rid, {}, 25.0, "vacant")
            payload = end_session.await_args.args[1]
            self.assertEqual(payload["is_record_valid"], 1)
            self.assertEqual(payload["reason_stopped"], "vacant")

        asyncio.run(run_case())

    def test_aerostate_confirmed_runtime_valid_without_power_samples(self):
        logic_engine._runtime_by_room.clear()
        rid = "session-aerostate-valid"
        st = logic_engine._rt(rid)
        st.session_start_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        st.session_start_temp = 28.0
        st.session_state = "confirmed"
        st.session_runtime_confirmed = True

        async def run_case():
            end_session = mock.AsyncMock()
            with (
                mock.patch.object(logic_engine.session_logger, "current_session_id", return_value="sid"),
                mock.patch.object(logic_engine.session_logger, "session_start_time", return_value=st.session_start_time),
                mock.patch.object(logic_engine.session_logger, "end_session", new=end_session),
                mock.patch.object(logic_engine, "clear_setpoint_command_tracking"),
                mock.patch.object(logic_engine.smart_cooling, "reset"),
            ):
                await logic_engine._close_session(rid, {}, 26.0, "manual_off")
            self.assertEqual(end_session.await_args.args[1]["is_record_valid"], 1)

        asyncio.run(run_case())

    def test_tuya_delayed_power_update_does_not_timeout_confirmed_runtime(self):
        logic_engine._runtime_by_room.clear()
        rid = "session-tuya-lag"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.session_start_time = now - timedelta(minutes=4)
        st.ac_is_on = True
        st.physical_ac_on = True

        async def run_case():
            with (
                mock.patch.object(logic_engine.session_logger, "current_session_id", return_value="sid"),
                mock.patch.object(logic_engine.session_logger, "current_session_is_provisional", return_value=True),
                mock.patch.object(logic_engine.session_logger, "session_start_time", return_value=st.session_start_time),
                mock.patch.object(logic_engine.session_logger, "upgrade_current_session_to_confirmed", new=mock.AsyncMock()) as upgrade,
                mock.patch.object(logic_engine, "_close_session", new=mock.AsyncMock()) as close_session,
            ):
                await logic_engine._maintain_session_lifecycle(
                    rid,
                    {},
                    26.0,
                    now,
                    24.0,
                    in_cooldown=False,
                    confirmed_ac_on=True,
                    inferred_only_physical=False,
                )
            upgrade.assert_awaited()
            close_session.assert_not_awaited()

        asyncio.run(run_case())

    def test_short_accidental_on_remains_invalid(self):
        logic_engine._runtime_by_room.clear()
        rid = "session-short-invalid"
        st = logic_engine._rt(rid)
        st.session_start_time = datetime.now(timezone.utc) - timedelta(seconds=12)

        async def run_case():
            end_session = mock.AsyncMock()
            with (
                mock.patch.object(logic_engine.session_logger, "current_session_id", return_value="sid"),
                mock.patch.object(logic_engine.session_logger, "session_start_time", return_value=st.session_start_time),
                mock.patch.object(logic_engine.session_logger, "end_session", new=end_session),
                mock.patch.object(logic_engine, "clear_setpoint_command_tracking"),
                mock.patch.object(logic_engine.smart_cooling, "reset"),
            ):
                await logic_engine._close_session(rid, {}, 26.0, "power_off")
            self.assertEqual(end_session.await_args.args[1]["is_record_valid"], 0)

        asyncio.run(run_case())

    def test_runtime_reconciliation_after_off_uses_logger_start_and_preserves_metrics(self):
        logic_engine._runtime_by_room.clear()
        rid = "session-reset-reconcile"
        st = logic_engine._rt(rid)
        logger_start = datetime.now(timezone.utc) - timedelta(minutes=30)
        st.session_start_time = None
        st.session_start_temp = 28.0
        st.session_state = "confirmed"
        st.session_runtime_confirmed = True
        st.watts_samples = [600.0, 640.0, 620.0]

        async def run_case():
            end_session = mock.AsyncMock()
            with (
                mock.patch.object(logic_engine.session_logger, "current_session_id", return_value="sid"),
                mock.patch.object(logic_engine.session_logger, "session_start_time", return_value=logger_start),
                mock.patch.object(logic_engine.session_logger, "end_session", new=end_session),
                mock.patch.object(logic_engine, "clear_setpoint_command_tracking"),
                mock.patch.object(logic_engine.smart_cooling, "reset"),
            ):
                await logic_engine._close_session(rid, {}, 25.0, "power_off")
            payload = end_session.await_args.args[1]
            self.assertEqual(payload["is_record_valid"], 1)
            self.assertGreater(payload["energy_kwh"], 0)
            self.assertGreater(payload["time_to_cool_minutes"], 25)

        asyncio.run(run_case())

    def test_session_persistence_survives_runtime_reset_after_close(self):
        logic_engine._runtime_by_room.clear()
        rid = "session-reset-after-close"
        st = logic_engine._rt(rid)
        st.session_start_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        st.session_start_temp = 27.0
        st.session_state = "confirmed"
        st.session_runtime_confirmed = True
        st.watts_samples = [700.0]

        async def run_case():
            end_session = mock.AsyncMock()
            with (
                mock.patch.object(logic_engine.session_logger, "current_session_id", return_value="sid"),
                mock.patch.object(logic_engine.session_logger, "session_start_time", return_value=st.session_start_time),
                mock.patch.object(logic_engine.session_logger, "end_session", new=end_session),
                mock.patch.object(logic_engine, "clear_setpoint_command_tracking"),
                mock.patch.object(logic_engine.smart_cooling, "reset"),
            ):
                await logic_engine._close_session(rid, {}, 25.0, "presence_idle")
            payload = end_session.await_args.args[1]
            self.assertEqual(payload["is_record_valid"], 1)
            self.assertIsNone(st.session_start_time)
            self.assertEqual(st.session_state, "idle")

        asyncio.run(run_case())

    def test_fp2_zone_gate_metrics_allow_running_and_block_unconfirmed(self):
        logic_engine._runtime_by_room.clear()
        rid = "z-metrics"
        st = logic_engine._rt(rid)
        cfg = {"zone_entity_id": "binary_sensor.z", "zone_required_for_on": True}

        st.zone_sensor_usable = False
        st.ac_is_on = True
        allow0 = st.zone_allow_count
        logic_engine._fp2_zone_apply_on_gate(rid, cfg, "on", "thermostat")
        self.assertGreater(st.zone_allow_count, allow0)

        st.zone_sensor_usable = True
        st.zone_confirmed = False
        block0 = st.zone_block_count
        st.ac_is_on = False
        st.physical_ac_on = False
        logic_engine._fp2_zone_apply_on_gate(rid, cfg, "on", "thermostat")
        self.assertGreater(st.zone_block_count, block0)

        st.zone_sensor_usable = False
        block1 = st.zone_block_count
        logic_engine._fp2_zone_apply_on_gate(rid, cfg, "on", "thermostat")
        self.assertGreater(st.zone_block_count, block1)

        st.zone_sensor_usable = True
        st.zone_confirmed = True
        allow1 = st.zone_allow_count
        logic_engine._fp2_zone_apply_on_gate(rid, cfg, "on", "thermostat")
        self.assertGreater(st.zone_allow_count, allow1)

    def test_fp2_zone_gate_allows_when_not_required(self):
        logic_engine._runtime_by_room.clear()
        rid = "z-g1"
        st = logic_engine._rt(rid)
        st.zone_sensor_usable = True
        st.zone_confirmed = False
        cfg = {"zone_entity_id": "binary_sensor.z", "zone_required_for_on": False}
        a, s, blocked = logic_engine._fp2_zone_apply_on_gate(rid, cfg, "on", "thermostat")
        self.assertEqual((a, s, blocked), ("on", "thermostat", False))

    def test_fp2_zone_gate_blocks_when_required_sensor_unusable(self):
        logic_engine._runtime_by_room.clear()
        rid = "z-g2"
        st = logic_engine._rt(rid)
        st.zone_sensor_usable = False
        st.zone_confirmed = False
        cfg = {
            "zone_entity_id": "binary_sensor.z",
            "zone_required_for_on": True,
        }
        a, s, blocked = logic_engine._fp2_zone_apply_on_gate(rid, cfg, "on", "thermostat")
        self.assertEqual((a, s, blocked), ("hold", "zone_gate", True))

    def test_fp2_zone_gate_blocks_new_start_until_confirmed(self):
        logic_engine._runtime_by_room.clear()
        rid = "z-g3"
        st = logic_engine._rt(rid)
        st.ac_is_on = False
        st.physical_ac_on = False
        st.zone_sensor_usable = True
        st.zone_confirmed = False
        cfg = {
            "zone_entity_id": "binary_sensor.z",
            "zone_required_for_on": True,
        }
        a, s, blocked = logic_engine._fp2_zone_apply_on_gate(rid, cfg, "on", "thermostat")
        self.assertEqual((a, s, blocked), ("hold", "zone_gate", True))
        st.zone_confirmed = True
        a2, s2, b2 = logic_engine._fp2_zone_apply_on_gate(rid, cfg, "on", "thermostat")
        self.assertEqual((a2, s2, b2), ("on", "thermostat", False))

    def test_fp2_zone_gate_blocks_initial_on_until_zone_confirmed(self):
        logic_engine._runtime_by_room.clear()
        rid = "z-initial"
        st = logic_engine._rt(rid)
        st.ac_is_on = False
        st.physical_ac_on = False
        st.zone_sensor_usable = True
        st.zone_confirmed = False
        cfg = {
            "zone_entity_id": "binary_sensor.z",
            "zone_required_for_on": True,
        }
        a, s, blocked = logic_engine._fp2_zone_apply_on_gate(rid, cfg, "on", "thermostat")
        self.assertEqual((a, s, blocked), ("hold", "zone_gate", True))

    def test_fp2_zone_gate_does_not_block_running_ac_when_zone_absent(self):
        logic_engine._runtime_by_room.clear()
        rid = "z-running"
        st = logic_engine._rt(rid)
        st.ac_is_on = True
        st.physical_ac_on = True
        st.effective_ac_on = True
        st.zone_sensor_usable = True
        st.zone_present = False
        st.zone_confirmed = False
        st.occupied = True
        st.stable_occupied = True
        st.last_known_presence = True
        cfg = {
            "zone_entity_id": "binary_sensor.z",
            "zone_required_for_on": True,
        }

        a, s, blocked = logic_engine._fp2_zone_apply_on_gate(rid, cfg, "on", "thermostat")

        self.assertEqual((a, s, blocked), ("on", "thermostat", False))
        self.assertTrue(st.occupied)
        self.assertTrue(st.stable_occupied)
        self.assertIsNone(st.vacant_since)

    def test_fp2_zone_gate_never_changes_off(self):
        logic_engine._runtime_by_room.clear()
        rid = "z-g4"
        st = logic_engine._rt(rid)
        st.zone_sensor_usable = False
        st.zone_confirmed = False
        cfg = {"zone_entity_id": "binary_sensor.z", "zone_required_for_on": True}
        a, s, blocked = logic_engine._fp2_zone_apply_on_gate(
            rid, cfg, "off", "thermostat_reached",
        )
        self.assertEqual((a, s, blocked), ("off", "thermostat_reached", False))

    def test_merge_room_config_zone_keys(self):
        from backend import room_registry

        g = {"rooms": [], "thermostat_on_delta_deg": 0.7}
        room = {
            "id": "r1",
            "name": "X",
            "climate_entity": "climate.x",
            "zone_entity_id": "binary_sensor.z",
            "zone_dwell_seconds": 45,
            "zone_exit_grace_seconds": 10,
            "zone_required_for_on": True,
        }
        m = room_registry.merge_room_config(g, room)
        self.assertEqual(m["zone_entity_id"], "binary_sensor.z")
        self.assertEqual(m["zone_dwell_seconds"], 45)
        self.assertEqual(m["zone_exit_grace_seconds"], 10)
        self.assertTrue(m["zone_required_for_on"])


if __name__ == "__main__":
    unittest.main()
