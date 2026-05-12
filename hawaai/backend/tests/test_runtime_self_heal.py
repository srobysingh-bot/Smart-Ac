import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

_HAWAAI = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _HAWAAI not in sys.path:
    sys.path.insert(0, _HAWAAI)

from backend import runtime_self_heal as sh  # noqa: E402


def _actions(report):
    return {rec.action for rec in report.recommendations}


class TestRuntimeSelfHeal(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        self.cfg = sh.SelfHealConfig(
            pending_stale_seconds=60,
            pending_off_stale_seconds=60,
            mismatch_grace_seconds=30,
            orphan_session_grace_seconds=30,
            sensor_stale_seconds=60,
            failed_on_retry_release_seconds=60,
        )
        self.state = sh.SelfHealState()

    def test_stale_pending_on_recommends_clear(self):
        rt = sh.RuntimeSnapshot(
            room_id="bed",
            pending_action="on",
            pending_since=self.now.timestamp() - 90,
        )
        report = sh.evaluate(rt, sh.ObservationSnapshot(), now=self.now, cfg=self.cfg, state=self.state)

        self.assertIn(sh.RecoveryAction.CLEAR_STALE_PENDING_ON, _actions(report))
        self.assertEqual(report.status, sh.HealthStatus.DEGRADED)

    def test_ha_unavailable_degrades_but_uses_short_lived_cache(self):
        ok_obs = sh.ObservationSnapshot(
            climate_entity="climate.bed",
            climate_state="cool",
            climate_available=True,
        )
        rt = sh.RuntimeSnapshot(room_id="bed", ac_is_on=True, physical_ac_on=True)
        sh.evaluate(rt, ok_obs, now=self.now, cfg=self.cfg, state=self.state)

        report = sh.evaluate(
            rt,
            sh.ObservationSnapshot(
                climate_entity="climate.bed",
                climate_state=None,
                climate_available=False,
            ),
            now=self.now + timedelta(seconds=10),
            cfg=self.cfg,
            state=self.state,
        )

        self.assertIn("climate", report.used_cached_values)
        self.assertLess(report.confidence.climate, 0.75)
        self.assertIn(sh.RecoveryAction.RESTORE_CACHED_STATE, _actions(report))

    def test_power_climate_mismatch_rebuilds_after_delay(self):
        rt = sh.RuntimeSnapshot(room_id="bed", ac_is_on=False, physical_ac_on=False)
        obs = sh.ObservationSnapshot(
            climate_entity="climate.bed",
            climate_state="off",
            power_entity="sensor.power",
            power_watts=900,
        )
        sh.evaluate(rt, obs, now=self.now, cfg=self.cfg, state=self.state)
        report = sh.evaluate(rt, obs, now=self.now + timedelta(seconds=31), cfg=self.cfg, state=self.state)

        self.assertIn(sh.RecoveryAction.REBUILD_RUNTIME, _actions(report))
        rec = next(r for r in report.recommendations if r.action == sh.RecoveryAction.REBUILD_RUNTIME)
        self.assertTrue(rec.metadata["observed_on"])
        self.assertEqual(report.status, sh.HealthStatus.DESYNCED)

    def test_orphan_session_recommends_close_after_delay(self):
        rt = sh.RuntimeSnapshot(room_id="bed", session_id="sid-1")
        obs = sh.ObservationSnapshot(
            climate_entity="climate.bed",
            climate_state="off",
            power_entity="sensor.power",
            power_watts=0,
        )
        sh.evaluate(rt, obs, now=self.now, cfg=self.cfg, state=self.state)
        report = sh.evaluate(rt, obs, now=self.now + timedelta(seconds=31), cfg=self.cfg, state=self.state)

        self.assertIn(sh.RecoveryAction.CLOSE_ORPHAN_SESSION, _actions(report))

    def test_restart_recovery_rebuilds_runtime_truth(self):
        rt = sh.RuntimeSnapshot(room_id="bed", startup_state_loaded=False)
        obs = sh.ObservationSnapshot(
            climate_entity="climate.bed",
            climate_state="cool",
            power_entity="sensor.power",
            power_watts=650,
        )
        report = sh.evaluate(rt, obs, now=self.now, cfg=self.cfg, state=self.state)

        self.assertIn("startup_unreconciled", {i.code for i in report.issues})
        self.assertIn(sh.RecoveryAction.REBUILD_RUNTIME, _actions(report))

    def test_cached_power_fallback_degrades_confidence(self):
        rt = sh.RuntimeSnapshot(room_id="bed")
        obs_ok = sh.ObservationSnapshot(power_entity="sensor.power", power_watts=700, power_available=True)
        sh.evaluate(rt, obs_ok, now=self.now, cfg=self.cfg, state=self.state)

        report = sh.evaluate(
            rt,
            sh.ObservationSnapshot(power_entity="sensor.power", power_watts=None, power_available=False),
            now=self.now + timedelta(seconds=10),
            cfg=self.cfg,
            state=self.state,
        )

        self.assertIn("power", report.used_cached_values)
        self.assertLess(report.confidence.power, 0.75)

    def test_stale_sensor_detection(self):
        rt = sh.RuntimeSnapshot(room_id="bed")
        obs = sh.ObservationSnapshot(
            sensors=(sh.SensorSnapshot("sensor.temp", 25.0, True, "temperature"),),
        )
        sh.evaluate(rt, obs, now=self.now, cfg=self.cfg, state=self.state)
        report = sh.evaluate(rt, obs, now=self.now + timedelta(seconds=61), cfg=self.cfg, state=self.state)

        self.assertIn("sensor_frozen_temperature", {issue.code for issue in report.issues})
        self.assertLess(report.confidence.sensor, 1.0)

    def test_confidence_degradation_for_multiple_faults(self):
        rt = sh.RuntimeSnapshot(
            room_id="bed",
            ac_is_on=True,
            physical_ac_on=True,
            pending_action="on",
            pending_since=self.now.timestamp() - 90,
        )
        obs = sh.ObservationSnapshot(
            climate_entity="climate.bed",
            climate_state="unavailable",
            climate_available=False,
            power_entity="sensor.power",
            power_watts=0,
        )
        report = sh.evaluate(rt, obs, now=self.now, cfg=self.cfg, state=self.state)

        self.assertEqual(report.confidence.label, "low")
        self.assertEqual(report.status, sh.HealthStatus.DEGRADED)

    def test_retry_release_after_failed_on(self):
        rt = sh.RuntimeSnapshot(
            room_id="bed",
            ac_state="on_failed",
            last_command_time=self.now - timedelta(seconds=90),
            on_failed_retry_used=True,
        )
        report = sh.evaluate(rt, sh.ObservationSnapshot(), now=self.now, cfg=self.cfg, state=self.state)

        self.assertIn(sh.RecoveryAction.RELEASE_FAILED_ON_RETRY, _actions(report))

    def test_invalid_humidity_is_sensor_issue(self):
        rt = sh.RuntimeSnapshot(room_id="bed")
        obs = sh.ObservationSnapshot(
            sensors=(sh.SensorSnapshot("sensor.humidity", 150, True, "humidity"),),
        )
        report = sh.evaluate(rt, obs, now=self.now, cfg=self.cfg, state=self.state)

        self.assertIn("sensor_invalid_humidity", {issue.code for issue in report.issues})
        self.assertLess(report.confidence.sensor, 1.0)

    def test_runtime_rebuild_off_from_power_low(self):
        rt = sh.RuntimeSnapshot(room_id="bed", ac_is_on=True, physical_ac_on=True)
        obs = sh.ObservationSnapshot(
            climate_entity="climate.bed",
            climate_state="off",
            power_entity="sensor.power",
            power_watts=0,
        )
        sh.evaluate(rt, obs, now=self.now, cfg=self.cfg, state=self.state)
        report = sh.evaluate(rt, obs, now=self.now + timedelta(seconds=31), cfg=self.cfg, state=self.state)

        rec = next(r for r in report.recommendations if r.action == sh.RecoveryAction.REBUILD_RUNTIME)
        self.assertFalse(rec.metadata["observed_on"])


if __name__ == "__main__":
    unittest.main()
