import asyncio
import unittest
from unittest import mock

from backend import config_manager, energy_config


class EnergyConfigResolverTests(unittest.TestCase):
    def test_legacy_device_config_resolves_as_auto_discovery(self):
        resolved = energy_config.resolve_energy_config(
            {
                "energy_device_id": "abc123",
                "energy_device_name": "Breaker",
            }
        )

        self.assertEqual(resolved.mode, energy_config.EnergyConfigMode.AUTO_DISCOVERY)
        self.assertTrue(resolved.configured)
        self.assertEqual(resolved.device_id, "abc123")
        self.assertFalse(resolved.device_lookup_skipped)

    def test_manual_override_only_resolves_without_device_lookup(self):
        resolved = energy_config.resolve_energy_config(
            {
                "energy_power_entity": "sensor.ac_power",
                "energy_kwh_entity": "sensor.ac_total",
            }
        )

        self.assertEqual(resolved.mode, energy_config.EnergyConfigMode.MANUAL_OVERRIDE)
        self.assertTrue(resolved.configured)
        self.assertEqual(resolved.power_entity, "sensor.ac_power")
        self.assertEqual(resolved.kwh_entity, "sensor.ac_total")
        self.assertEqual(resolved.device_id, "")
        self.assertTrue(resolved.device_lookup_skipped)

    def test_empty_config_resolves_as_unconfigured(self):
        resolved = energy_config.resolve_energy_config({})

        self.assertEqual(resolved.mode, energy_config.EnergyConfigMode.UNCONFIGURED)
        self.assertFalse(resolved.configured)
        self.assertTrue(resolved.device_lookup_skipped)

    def test_power_normalization_passes_valid_watts_through(self):
        result = energy_config.normalize_power_value(
            "sensor.ac_power",
            "611.5",
            {
                "device_class": "power",
                "state_class": "measurement",
                "unit_of_measurement": "W",
            },
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.watts, 611.5)
        self.assertEqual(result.confidence, "unit")
        self.assertFalse(result.suspicious)

    def test_power_normalization_uses_tuya_scale_metadata(self):
        result = energy_config.normalize_power_value(
            "sensor.tuya_power",
            "8218",
            {
                "device_class": "power",
                "state_class": "measurement",
                "unit_of_measurement": "W",
                "scale": 1,
                "suggested_display_precision": 1,
            },
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.watts, 821.8)
        self.assertEqual(result.confidence, "metadata")
        self.assertEqual(result.scale_source, "scale")

    def test_power_normalization_uses_tuya_scaling_multiplier_metadata(self):
        result = energy_config.normalize_power_value(
            "sensor.tuya_power",
            "8218",
            {
                "device_class": "power",
                "state_class": "measurement",
                "unit_of_measurement": "W",
                "scaling": 0.1,
            },
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.watts, 821.8)
        self.assertEqual(result.confidence, "metadata")
        self.assertEqual(result.scale_source, "scaling")

    def test_power_normalization_safely_infers_scaled_integer_telemetry(self):
        result = energy_config.normalize_power_value(
            "sensor.tuya_power",
            "16355",
            {
                "device_class": "power",
                "state_class": "measurement",
                "unit_of_measurement": "W",
            },
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.watts, 1635.5)
        self.assertEqual(result.confidence, "inferred")
        self.assertEqual(result.reason, "inferred_decimal_scale_1")

    def test_power_normalization_converts_kw_without_decimal_inference(self):
        result = energy_config.normalize_power_value(
            "sensor.ac_power_kw",
            "1.2",
            {
                "device_class": "power",
                "state_class": "measurement",
                "unit_of_measurement": "kW",
            },
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.watts, 1200.0)
        self.assertEqual(result.confidence, "unit")

    def test_power_normalization_rejects_unrecoverable_suspicious_power(self):
        result = energy_config.normalize_power_value(
            "sensor.bad_power",
            "9999999",
            {
                "device_class": "power",
                "state_class": "measurement",
                "unit_of_measurement": "W",
            },
        )

        self.assertFalse(result.valid)
        self.assertTrue(result.suspicious)
        self.assertEqual(result.reason, "suspicious_power")

    def test_config_energy_log_summary_does_not_dump_nested_room_config(self):
        summary = config_manager._energy_config_log_summary(
            {
                "energy_power_entity": "sensor.global_power",
                "rooms": [
                    {
                        "id": "study-room",
                        "energy_power_entity": "sensor.study_power",
                        "settings": {"large_nested_blob": {"secret": "should-not-log"}},
                    }
                ],
            }
        )

        rendered = str(summary)
        self.assertEqual(summary["rooms"], 1)
        self.assertEqual(summary["room_power_configured"], 1)
        self.assertNotIn("sensor.study_power", rendered)
        self.assertNotIn("large_nested_blob", rendered)

    def test_manual_runtime_resolution_never_calls_device_registry(self):
        async def run_case():
            with (
                mock.patch.object(
                    energy_config.ha_client,
                    "get_entity_registry",
                    new=mock.AsyncMock(side_effect=AssertionError("registry lookup")),
                ),
                mock.patch.object(
                    energy_config.ha_client,
                    "get_all_entities",
                    new=mock.AsyncMock(side_effect=AssertionError("state discovery")),
                ),
            ):
                return await energy_config.resolve_runtime_energy_config(
                    {"energy_power_entity": "sensor.ac_power"}
                )

        resolved = asyncio.run(run_case())
        self.assertEqual(resolved.mode, energy_config.EnergyConfigMode.MANUAL_OVERRIDE)
        self.assertEqual(resolved.power_entity, "sensor.ac_power")
        self.assertTrue(resolved.device_lookup_skipped)

    def test_auto_discovery_uses_ha_metadata(self):
        async def run_case():
            with (
                mock.patch.object(
                    energy_config.ha_client,
                    "get_entity_registry",
                    new=mock.AsyncMock(
                        return_value=[
                            {"entity_id": "sensor.ac_power", "device_id": "abc123"},
                            {"entity_id": "sensor.ac_energy", "device_id": "abc123"},
                            {"entity_id": "sensor.other_power", "device_id": "other"},
                        ]
                    ),
                ),
                mock.patch.object(
                    energy_config.ha_client,
                    "get_all_entities",
                    new=mock.AsyncMock(
                        return_value=[
                            {
                                "entity_id": "sensor.ac_power",
                                "state": "611",
                                "attributes": {
                                    "device_class": "power",
                                    "state_class": "measurement",
                                    "unit_of_measurement": "W",
                                },
                            },
                            {
                                "entity_id": "sensor.ac_energy",
                                "state": "42.5",
                                "attributes": {
                                    "device_class": "energy",
                                    "state_class": "total_increasing",
                                    "unit_of_measurement": "kWh",
                                },
                            },
                        ]
                    ),
                ),
            ):
                return await energy_config.resolve_runtime_energy_config(
                    {"energy_device_id": "abc123"}
                )

        resolved = asyncio.run(run_case())
        self.assertEqual(resolved.mode, energy_config.EnergyConfigMode.AUTO_DISCOVERY)
        self.assertEqual(resolved.power_entity, "sensor.ac_power")
        self.assertEqual(resolved.kwh_entity, "sensor.ac_energy")
        self.assertFalse(resolved.device_lookup_skipped)

    def test_auto_discovery_rejects_select_power_behaviour(self):
        async def run_case():
            with (
                mock.patch.object(
                    energy_config.ha_client,
                    "get_entity_registry",
                    new=mock.AsyncMock(
                        return_value=[
                            {
                                "entity_id": "select.breaker_power_on_behaviour",
                                "device_id": "abc123",
                            },
                            {"entity_id": "sensor.breaker_power", "device_id": "abc123"},
                        ]
                    ),
                ),
                mock.patch.object(
                    energy_config.ha_client,
                    "get_all_entities",
                    new=mock.AsyncMock(
                        return_value=[
                            {
                                "entity_id": "select.breaker_power_on_behaviour",
                                "state": "previous",
                                "attributes": {},
                            },
                            {
                                "entity_id": "sensor.breaker_power",
                                "state": "611",
                                "attributes": {
                                    "device_class": "power",
                                    "state_class": "measurement",
                                    "unit_of_measurement": "W",
                                },
                            },
                        ]
                    ),
                ),
            ):
                return await energy_config.resolve_runtime_energy_config(
                    {"energy_device_id": "abc123"},
                    room_id="study",
                )

        resolved = asyncio.run(run_case())
        self.assertEqual(resolved.power_entity, "sensor.breaker_power")

    def test_tuya_suffix_and_unit_fallback_accepts_missing_device_class(self):
        async def run_case():
            with (
                mock.patch.object(
                    energy_config.ha_client,
                    "get_entity_registry",
                    new=mock.AsyncMock(
                        return_value=[
                            {
                                "entity_id": "sensor.30a_smart_circuit_breaker_energy_meter_8_power",
                                "device_id": "abc123",
                            },
                            {
                                "entity_id": "sensor.30a_smart_circuit_breaker_energy_meter_8_total_energy",
                                "device_id": "abc123",
                            },
                        ]
                    ),
                ),
                mock.patch.object(
                    energy_config.ha_client,
                    "get_all_entities",
                    new=mock.AsyncMock(
                        return_value=[
                            {
                                "entity_id": "sensor.30a_smart_circuit_breaker_energy_meter_8_power",
                                "state": "611",
                                "attributes": {"unit_of_measurement": "W"},
                            },
                            {
                                "entity_id": "sensor.30a_smart_circuit_breaker_energy_meter_8_total_energy",
                                "state": "42.5",
                                "attributes": {"unit_of_measurement": "kWh"},
                            },
                        ]
                    ),
                ),
            ):
                return await energy_config.resolve_runtime_energy_config(
                    {"energy_device_id": "abc123"},
                    room_id="study",
                )

        resolved = asyncio.run(run_case())
        self.assertEqual(
            resolved.power_entity,
            "sensor.30a_smart_circuit_breaker_energy_meter_8_power",
        )
        self.assertEqual(
            resolved.kwh_entity,
            "sensor.30a_smart_circuit_breaker_energy_meter_8_total_energy",
        )

    def test_auto_discovery_does_not_assign_invalid_power_entity(self):
        async def run_case():
            with (
                mock.patch.object(
                    energy_config.ha_client,
                    "get_entity_registry",
                    new=mock.AsyncMock(
                        return_value=[
                            {
                                "entity_id": "select.breaker_power_on_behaviour",
                                "device_id": "abc123",
                            },
                        ]
                    ),
                ),
                mock.patch.object(
                    energy_config.ha_client,
                    "get_all_entities",
                    new=mock.AsyncMock(
                        return_value=[
                            {
                                "entity_id": "select.breaker_power_on_behaviour",
                                "state": "previous",
                                "attributes": {},
                            },
                        ]
                    ),
                ),
            ):
                return await energy_config.resolve_runtime_energy_config(
                    {"energy_device_id": "abc123"},
                    room_id="study",
                )

        resolved = asyncio.run(run_case())
        self.assertEqual(resolved.power_entity, "")

    def test_dashboard_status_hydrates_live_energy_from_latest_room_config(self):
        from backend import logic_engine, main

        rid = "roomenergy01"
        cfg = {
            "rooms": [
                {
                    "id": rid,
                    "name": "Energy Room",
                    "presence_entity": "binary_sensor.energy_presence",
                    "indoor_temp_entity": "sensor.energy_temp",
                    "climate_entity": "climate.energy_room",
                    "energy_power_entity": "sensor.energy_power",
                    "energy_kwh_entity": "sensor.energy_total",
                }
            ]
        }

        async def fake_get_state(entity_id):
            return {
                "sensor.energy_temp": "24.5",
                "binary_sensor.energy_presence": "on",
            }.get(entity_id)

        async def fake_get_entity_state_full(entity_id):
            state = {
                "sensor.energy_power": {
                    "state": "611",
                    "attributes": {
                        "device_class": "power",
                        "state_class": "measurement",
                        "unit_of_measurement": "W",
                    },
                },
                "sensor.energy_total": {
                    "state": "42.75",
                    "attributes": {
                        "device_class": "energy",
                        "state_class": "total_increasing",
                        "unit_of_measurement": "kWh",
                    },
                },
            }
            return state.get(entity_id)

        async def run_case():
            logic_engine._runtime_by_room.clear()
            with (
                mock.patch.object(main.config_manager, "load_config", return_value=cfg),
                mock.patch.object(main.ha_client, "get_state", side_effect=fake_get_state),
                mock.patch.object(
                    main.ha_client,
                    "get_entity_state_full",
                    side_effect=fake_get_entity_state_full,
                ),
                mock.patch.object(
                    main.ha_client,
                    "get_climate_state",
                    new=mock.AsyncMock(return_value={}),
                ),
                mock.patch.object(
                    main.weather_api,
                    "get_cached",
                    new=mock.AsyncMock(return_value={"temp": 34, "humidity": 55}),
                ),
                mock.patch.object(main, "get_ai_status", return_value={}),
            ):
                return await main._dashboard_status_payload(rid)

        payload = asyncio.run(run_case())
        self.assertTrue(payload["energy_configured"])
        self.assertTrue(payload["energy_live_available"])
        self.assertEqual(payload["energy_status"], "ok")
        self.assertEqual(payload["energy_power_entity"], "sensor.energy_power")
        self.assertEqual(payload["energy_kwh_entity"], "sensor.energy_total")
        self.assertEqual(payload["energy_watts"], 611.0)
        self.assertEqual(payload["energy_kwh_total"], 42.75)

    def test_invalid_room_energy_does_not_clear_another_room_runtime(self):
        from backend import logic_engine, main

        cfg = {
            "rooms": [
                {
                    "id": "diningroom01",
                    "name": "Dining",
                    "presence_entity": "binary_sensor.dining_presence",
                    "indoor_temp_entity": "sensor.dining_temp",
                    "climate_entity": "climate.dining",
                    "energy_power_entity": "sensor.dining_power",
                },
                {
                    "id": "studyroom001",
                    "name": "Study",
                    "presence_entity": "binary_sensor.study_presence",
                    "indoor_temp_entity": "sensor.study_temp",
                    "climate_entity": "climate.study",
                    "energy_power_entity": "select.study_power_on_behaviour",
                },
            ]
        }

        async def fake_get_state(entity_id):
            return {
                "sensor.dining_temp": "24",
                "binary_sensor.dining_presence": "on",
                "sensor.study_temp": "24",
                "binary_sensor.study_presence": "on",
            }.get(entity_id)

        async def fake_get_entity_state_full(entity_id):
            state = {
                "sensor.dining_power": {
                    "state": "611",
                    "attributes": {
                        "device_class": "power",
                        "state_class": "measurement",
                        "unit_of_measurement": "W",
                    },
                },
                "select.study_power_on_behaviour": {
                    "state": "previous",
                    "attributes": {},
                },
            }
            return state.get(entity_id)

        async def run_case():
            logic_engine._runtime_by_room.clear()
            with (
                mock.patch.object(main.config_manager, "load_config", return_value=cfg),
                mock.patch.object(main.ha_client, "get_state", side_effect=fake_get_state),
                mock.patch.object(
                    main.ha_client,
                    "get_entity_state_full",
                    side_effect=fake_get_entity_state_full,
                ),
                mock.patch.object(
                    main.ha_client,
                    "get_climate_state",
                    new=mock.AsyncMock(return_value={}),
                ),
                mock.patch.object(
                    main.weather_api,
                    "get_cached",
                    new=mock.AsyncMock(return_value={"temp": 34, "humidity": 55}),
                ),
                mock.patch.object(main, "get_ai_status", return_value={}),
            ):
                dining_before = await main._dashboard_status_payload("diningroom01")
                study = await main._dashboard_status_payload("studyroom001")
                dining_after = await main._dashboard_status_payload("diningroom01")
            return dining_before, study, dining_after

        dining_before, study, dining_after = asyncio.run(run_case())
        self.assertEqual(dining_before["energy_watts"], 611.0)
        self.assertEqual(dining_after["energy_watts"], 611.0)
        self.assertEqual(dining_after["energy_power_entity"], "sensor.dining_power")
        self.assertFalse(study["energy_live_available"])
        self.assertEqual(study["energy_status"], "unavailable")

    def test_load_time_sanitizer_preserves_saved_energy_fields(self):
        from backend import config_manager

        cfg = {
            "rooms": [
                {
                    "id": "study",
                    "name": "Study",
                    "climate_entity": "climate.study",
                    "energy_power_entity": "select.study_power_on_behaviour",
                    "energy_kwh_entity": "sensor.study_total_energy",
                    "settings": {"target_temp": 25},
                },
                {
                    "id": "dining",
                    "name": "Dining",
                    "climate_entity": "climate.dining",
                    "energy_power_entity": "sensor.dining_power",
                },
            ]
        }

        cleaned = config_manager.sanitize_energy_entities(cfg)
        study = cleaned["rooms"][0]
        dining = cleaned["rooms"][1]
        self.assertEqual(study["energy_power_entity"], "select.study_power_on_behaviour")
        self.assertEqual(study["energy_kwh_entity"], "sensor.study_total_energy")
        self.assertEqual(study["settings"]["target_temp"], 25)
        self.assertEqual(dining["energy_power_entity"], "sensor.dining_power")


if __name__ == "__main__":
    unittest.main()
