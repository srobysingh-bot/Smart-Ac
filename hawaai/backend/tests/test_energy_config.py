import asyncio
import unittest
from unittest import mock

from backend import energy_config


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
                                "attributes": {
                                    "device_class": "power",
                                    "state_class": "measurement",
                                    "unit_of_measurement": "W",
                                },
                            },
                            {
                                "entity_id": "sensor.ac_energy",
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
                "sensor.energy_power": "611",
                "sensor.energy_total": "42.75",
            }.get(entity_id)

        async def run_case():
            logic_engine._runtime_by_room.clear()
            with (
                mock.patch.object(main.config_manager, "load_config", return_value=cfg),
                mock.patch.object(main.ha_client, "get_state", side_effect=fake_get_state),
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


if __name__ == "__main__":
    unittest.main()
