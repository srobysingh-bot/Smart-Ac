import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from hawaai.backend import database, logic_engine


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


class SessionStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.old_db_path = database.DB_PATH
        database.DB_PATH = self.tmp.name
        await database.init_db()

    async def asyncTearDown(self):
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


if __name__ == "__main__":
    unittest.main()
