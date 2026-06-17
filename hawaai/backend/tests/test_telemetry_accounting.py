import os
import aiosqlite
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from hawaai.backend import database, ha_entity_events, logic_engine, session_logger


class TelemetryRuntimeTests(unittest.TestCase):
    def test_invalid_live_power_does_not_fall_back_to_cached_value(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)

        power, kwh = logic_engine._apply_telemetry_cache(
            st,
            now=now,
            configured=True,
            power_entity="sensor.ac_power",
            kwh_entity="sensor.ac_energy",
            parsed_power=425.0,
            parsed_kwh=10.25,
        )
        self.assertEqual(power, 425.0)
        self.assertEqual(kwh, 10.25)
        self.assertEqual(st.telemetry_status, "healthy")

        power, kwh = logic_engine._apply_telemetry_cache(
            st,
            now=now + timedelta(seconds=10),
            configured=True,
            power_entity="sensor.ac_power",
            kwh_entity="sensor.ac_energy",
            parsed_power=None,
            parsed_kwh=None,
        )
        self.assertIsNone(power)
        self.assertIsNone(kwh)
        self.assertIsNone(st.energy_watts)
        self.assertIsNone(st.energy_kwh)
        self.assertEqual(st.last_valid_power_watts, 425.0)
        self.assertEqual(st.last_valid_energy_kwh, 10.25)
        self.assertEqual(st.telemetry_status, "recovering")

    def test_numeric_zero_is_valid_healthy_power(self):
        st = logic_engine.RoomRuntime()
        power, _ = logic_engine._apply_telemetry_cache(
            st,
            now=datetime.now(timezone.utc),
            configured=True,
            power_entity="sensor.ac_power",
            kwh_entity="",
            parsed_power=0.0,
            parsed_kwh=None,
        )
        self.assertEqual(power, 0.0)
        self.assertEqual(st.energy_watts, 0.0)
        self.assertEqual(st.telemetry_status, "healthy")

    def test_not_configured_status_is_explicit(self):
        st = logic_engine.RoomRuntime()
        power, kwh = logic_engine._apply_telemetry_cache(
            st,
            now=datetime.now(timezone.utc),
            configured=False,
            power_entity="",
            kwh_entity="",
            parsed_power=None,
            parsed_kwh=None,
        )
        self.assertIsNone(power)
        self.assertIsNone(kwh)
        self.assertEqual(st.telemetry_status, "not_configured")

    def test_power_off_reconciliation_keeps_pending_confirmation_for_session_close(self):
        st = logic_engine.RoomRuntime(
            energy_configured=True,
            energy_power_entity="sensor.ac_power",
            telemetry_power_live_valid=True,
            pending_action="off",
            pending_off_confirmation=True,
            physical_ac_on=True,
            ac_is_on=True,
        )
        applied = logic_engine._reconcile_physical_ac_from_power(
            "bedroom",
            {"physical_on_watts": 100.0, "physical_off_watts": 30.0, "physical_state_confirm_seconds": 0},
            st,
            datetime.now(timezone.utc),
            0.0,
        )
        self.assertTrue(applied)
        self.assertFalse(st.physical_ac_on)
        self.assertTrue(st.pending_off_confirmation)

    def test_timestamp_power_integration_and_gap_survives_recovery(self):
        st = logic_engine.RoomRuntime()
        t0 = datetime.now(timezone.utc)
        logic_engine._record_session_power_sample(st, now=t0, power_watts=360.0, live_valid=True)
        logic_engine._record_session_power_sample(st, now=t0 + timedelta(seconds=10), power_watts=None, live_valid=False)
        logic_engine._record_session_power_sample(st, now=t0 + timedelta(seconds=40), power_watts=180.0, live_valid=True)
        logic_engine._record_session_power_sample(st, now=t0 + timedelta(seconds=50), power_watts=180.0, live_valid=True)
        self.assertAlmostEqual(st.session_energy_wh, 1.5, places=3)
        self.assertEqual(st.session_telemetry_gap_seconds, 30.0)
        self.assertIsNone(st.session_telemetry_gap_started_at)


class HaTelemetryEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_power_event_refreshes_runtime_and_triggers_immediate_tick(self):
        cfg = {
            "rooms": [{
                "id": "Bedroom",
                "energy_power_entity": "sensor.ac_power",
                "energy_kwh_entity": "sensor.ac_energy",
            }]
        }
        ix = ha_entity_events._entity_watch_index(cfg)
        with (
            mock.patch.object(ha_entity_events.config_manager, "load_config", return_value=cfg),
            mock.patch.object(ha_entity_events.logic_engine, "refresh_runtime_energy", new=mock.AsyncMock()) as refresh,
            mock.patch.object(ha_entity_events.logic_engine, "trigger_tick") as trigger,
        ):
            await ha_entity_events._handle_state_changed(
                {
                    "entity_id": "sensor.ac_power",
                    "old_state": {"state": "0"},
                    "new_state": {"state": "420"},
                },
                ix,
            )
        refresh.assert_awaited_once()
        trigger.assert_called_once_with("Bedroom", reason="power_event", skip_debounce=True)

    async def test_reconnect_snapshot_refreshes_each_room_once(self):
        cfg = {
            "rooms": [
                {"id": "Bedroom", "energy_power_entity": "sensor.ac_power", "energy_kwh_entity": "sensor.ac_energy"},
                {"id": "Bedroom", "energy_power_entity": "sensor.ac_power", "energy_kwh_entity": "sensor.ac_energy"},
            ]
        }
        ix = ha_entity_events._entity_watch_index(cfg)
        with (
            mock.patch.object(ha_entity_events.config_manager, "load_config", return_value=cfg),
            mock.patch.object(ha_entity_events.logic_engine, "refresh_runtime_energy", new=mock.AsyncMock()) as refresh,
        ):
            await ha_entity_events._refresh_energy_snapshots(ix)
        refresh.assert_awaited_once()


class SessionStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.old_db_path = database.DB_PATH
        database.DB_PATH = self.tmp.name
        await database.init_db()

    async def asyncTearDown(self):
        session_logger.clear_room_buffers("bedroom")
        logic_engine._runtime_by_room.pop("bedroom", None)
        database.DB_PATH = self.old_db_path
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    async def test_today_stats_exclude_invalid_and_do_not_zero_missing_energy(self):
        now = datetime.now(timezone.utc)
        valid_missing = {
            "session_id": "valid-missing",
            "room_id": "bedroom",
            "start_time": now.isoformat(),
            "indoor_temp_start": 28.0,
            "day_of_week": now.weekday(),
            "hour_of_day": now.hour,
            "is_record_valid": 1,
            "provisional": 0,
        }
        invalid_energy = {
            **valid_missing,
            "session_id": "invalid-energy",
            "is_record_valid": 0,
        }
        await database.insert_session_start(valid_missing)
        await database.update_session_end("valid-missing", {
            "end_time": (now + timedelta(minutes=20)).isoformat(),
            "time_to_cool_minutes": 20.0,
            "energy_consumed_kwh": None,
            "is_record_valid": 1,
        })
        await database.insert_session_start(invalid_energy)
        await database.update_session_end("invalid-energy", {
            "end_time": (now + timedelta(minutes=20)).isoformat(),
            "time_to_cool_minutes": 20.0,
            "energy_consumed_kwh": 5.0,
            "is_record_valid": 0,
        })

        stats = await database.get_today_stats("bedroom", timezone_name="UTC")
        self.assertEqual(stats["session_count"], 1)
        self.assertIsNone(stats["total_kwh"])
        self.assertIsNone(stats["total_cost"])
        self.assertEqual(stats["energy_session_count"], 0)

    async def test_today_stats_sum_only_valid_energy_sessions(self):
        now = datetime.now(timezone.utc)
        session = {
            "session_id": "valid-energy",
            "room_id": "bedroom",
            "start_time": now.isoformat(),
            "indoor_temp_start": 28.0,
            "day_of_week": now.weekday(),
            "hour_of_day": now.hour,
            "is_record_valid": 1,
            "provisional": 0,
        }
        await database.insert_session_start(session)
        await database.update_session_end("valid-energy", {
            "end_time": (now + timedelta(minutes=30)).isoformat(),
            "time_to_cool_minutes": 30.0,
            "energy_consumed_kwh": 0.42,
            "cost_estimate": 3.36,
            "is_record_valid": 1,
        })

        stats = await database.get_today_stats("bedroom", tariff_per_kwh=8.0, timezone_name="UTC")
        self.assertEqual(stats["session_count"], 1)
        self.assertEqual(stats["energy_session_count"], 1)
        self.assertEqual(stats["total_kwh"], 0.42)
        self.assertEqual(stats["total_cost"], 3.36)

    async def test_physical_on_off_run_closes_one_session_and_counts_today(self):
        room_id = "bedroom"
        cfg = {"target_temp": 24.0, "energy_tariff_per_kwh": 8.0}
        st = logic_engine._rt(room_id)
        st.physical_ac_on = True
        st.ac_is_on = True
        st.energy_kwh_entity = "sensor.ac_energy"
        st.energy_kwh = 10.0
        with mock.patch.object(logic_engine.weather_api, "get_cached", new=mock.AsyncMock(return_value={})):
            await logic_engine._start_provisional_session(room_id, cfg, 28.0, datetime.now(timezone.utc), 24.0)
        sid = session_logger.current_session_id(room_id)
        self.assertIsNotNone(sid)
        open_row = await database.get_open_session(room_id)
        self.assertEqual(open_row["provisional"], 1)

        st.energy_kwh = 10.42
        with mock.patch.object(logic_engine, "MIN_SESSION_SECONDS", 0):
            await logic_engine._close_session(room_id, cfg, 27.5, "power_off")
        self.assertIsNone(session_logger.current_session_id(room_id))

        sessions = await database.get_sessions(room_id)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], sid)
        self.assertEqual(sessions[0]["provisional"], 0)
        self.assertTrue(sessions[0]["valid"])
        stats = await database.get_today_stats(room_id, tariff_per_kwh=8.0, timezone_name="UTC")
        self.assertEqual(stats["session_count"], 1)
        self.assertEqual(stats["energy_session_count"], 1)
        self.assertEqual(stats["total_kwh"], 0.42)

    async def test_restart_while_power_on_resumes_open_session(self):
        room_id = "bedroom"
        now = datetime.now(timezone.utc)
        row = {
            "session_id": "resume-me",
            "room_id": room_id,
            "start_time": now.isoformat(),
            "indoor_temp_start": 28.0,
            "day_of_week": now.weekday(),
            "hour_of_day": now.hour,
            "is_record_valid": 1,
            "provisional": 1,
            "meter_start_kwh": 3.0,
            "active_session_started_at_utc": now.isoformat(),
            "accumulated_energy_wh": 12.5,
            "session_telemetry_gap_seconds": 7.0,
            "last_valid_power_sample_at": now.isoformat(),
            "last_valid_power_watts": 420.0,
            "last_confirmed_physical_on_at": now.timestamp(),
        }
        await database.insert_session_start(row)
        st = logic_engine._rt(room_id)
        st.energy_configured = True
        st.energy_power_entity = "sensor.ac_power"
        st.telemetry_power_live_valid = True
        cfg = {"climate_entity": "climate.ac", "energy_power_entity": "sensor.ac_power", "physical_on_watts": 100.0}
        with (
            mock.patch.object(logic_engine.ha_client, "get_climate_state", new=mock.AsyncMock(return_value={"state": "cool"})),
            mock.patch.object(logic_engine, "_read_runtime_energy", new=mock.AsyncMock(return_value=(420.0, None))),
        ):
            await logic_engine._load_startup_state(room_id, cfg)
        self.assertEqual(session_logger.current_session_id(room_id), "resume-me")
        self.assertEqual(st.session_energy_wh, 12.5)
        self.assertEqual(st.session_telemetry_gap_seconds, 7.0)

    async def test_restart_does_not_restore_stale_or_future_session_timestamp(self):
        room_id = "bedroom"
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        row = {
            "session_id": "future-session",
            "room_id": room_id,
            "start_time": future.isoformat(),
            "indoor_temp_start": 28.0,
            "day_of_week": future.weekday(),
            "hour_of_day": future.hour,
            "is_record_valid": 1,
            "provisional": 1,
            "active_session_started_at_utc": future.isoformat(),
        }
        await database.insert_session_start(row)
        st = logic_engine._rt(room_id)
        st.energy_configured = True
        st.energy_power_entity = "sensor.ac_power"
        st.telemetry_power_live_valid = True
        cfg = {"climate_entity": "climate.ac", "energy_power_entity": "sensor.ac_power", "physical_on_watts": 100.0}
        with (
            mock.patch.object(logic_engine.ha_client, "get_climate_state", new=mock.AsyncMock(return_value={"state": "cool"})),
            mock.patch.object(logic_engine, "_read_runtime_energy", new=mock.AsyncMock(return_value=(420.0, None))),
        ):
            await logic_engine._load_startup_state(room_id, cfg)
        self.assertIsNone(session_logger.current_session_id(room_id))
        async with aiosqlite.connect(database.DB_PATH) as db:
            async with db.execute(
                "SELECT is_record_valid, end_time FROM sessions WHERE session_id = ?",
                ("future-session",),
            ) as cur:
                stored = await cur.fetchone()
        self.assertIsNotNone(stored)
        self.assertEqual(stored[0], 0)
        self.assertIsNotNone(stored[1])

    async def test_old_open_session_with_long_gap_and_power_on_does_not_resume_old_timer(self):
        room_id = "bedroom"
        old = datetime.now(timezone.utc) - timedelta(hours=10)
        row = {
            "session_id": "old-powered",
            "room_id": room_id,
            "start_time": old.isoformat(),
            "indoor_temp_start": 28.0,
            "day_of_week": old.weekday(),
            "hour_of_day": old.hour,
            "is_record_valid": 1,
            "provisional": 1,
            "active_session_started_at_utc": old.isoformat(),
            "last_valid_power_sample_at": old.isoformat(),
            "last_valid_power_watts": 450.0,
        }
        await database.insert_session_start(row)
        st = logic_engine._rt(room_id)
        st.energy_configured = True
        st.energy_power_entity = "sensor.ac_power"
        st.telemetry_power_live_valid = True
        cfg = {"climate_entity": "climate.ac", "energy_power_entity": "sensor.ac_power", "physical_on_watts": 100.0}
        with (
            mock.patch.object(logic_engine.ha_client, "get_climate_state", new=mock.AsyncMock(return_value={"state": "cool"})),
            mock.patch.object(logic_engine, "_read_runtime_energy", new=mock.AsyncMock(return_value=(430.0, None))),
        ):
            await logic_engine._load_startup_state(room_id, cfg)
        self.assertIsNone(session_logger.current_session_id(room_id))
        self.assertFalse(st.active_session_continuity_confirmed)
        self.assertEqual(st.active_session_recovery_state, "recovery_gap")

    async def test_open_session_power_off_clears_timer(self):
        room_id = "bedroom"
        now = datetime.now(timezone.utc)
        row = {
            "session_id": "off-open",
            "room_id": room_id,
            "start_time": now.isoformat(),
            "indoor_temp_start": 28.0,
            "day_of_week": now.weekday(),
            "hour_of_day": now.hour,
            "is_record_valid": 1,
            "provisional": 1,
            "active_session_started_at_utc": now.isoformat(),
            "last_valid_power_sample_at": now.isoformat(),
            "last_valid_power_watts": 0.0,
        }
        await database.insert_session_start(row)
        st = logic_engine._rt(room_id)
        st.energy_configured = True
        st.energy_power_entity = "sensor.ac_power"
        st.telemetry_power_live_valid = True
        cfg = {"climate_entity": "climate.ac", "energy_power_entity": "sensor.ac_power", "physical_on_watts": 100.0}
        with (
            mock.patch.object(logic_engine.ha_client, "get_climate_state", new=mock.AsyncMock(return_value={"state": "off"})),
            mock.patch.object(logic_engine, "_read_runtime_energy", new=mock.AsyncMock(return_value=(0.0, None))),
        ):
            await logic_engine._load_startup_state(room_id, cfg)
        self.assertIsNone(session_logger.current_session_id(room_id))
        self.assertFalse(st.active_session_continuity_confirmed)
        self.assertEqual(st.active_session_recovery_state, "power_off")

    async def test_invalid_sessions_do_not_affect_ml_stats(self):
        now = datetime.now(timezone.utc)
        valid = {
            "session_id": "valid-ml",
            "room_id": "bedroom",
            "start_time": now.isoformat(),
            "day_of_week": now.weekday(),
            "hour_of_day": now.hour,
            "is_record_valid": 1,
            "provisional": 0,
        }
        invalid = {**valid, "session_id": "invalid-ml", "is_record_valid": 0}
        await database.insert_session_start(valid)
        await database.update_session_end("valid-ml", {
            "end_time": (now + timedelta(minutes=20)).isoformat(),
            "time_to_cool_minutes": 20,
            "is_record_valid": 1,
        })
        await database.insert_session_start(invalid)
        await database.update_session_end("invalid-ml", {
            "end_time": (now + timedelta(minutes=90)).isoformat(),
            "time_to_cool_minutes": 90,
            "is_record_valid": 0,
        })
        ml = await database.get_ml_stats("bedroom")
        self.assertEqual(ml["total_sessions"], 1)
        self.assertEqual(ml["avg_cool_time"], 20.0)


if __name__ == "__main__":
    unittest.main()
