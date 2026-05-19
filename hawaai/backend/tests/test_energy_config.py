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


if __name__ == "__main__":
    unittest.main()
