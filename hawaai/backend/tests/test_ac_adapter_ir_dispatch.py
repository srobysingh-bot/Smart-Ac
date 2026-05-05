"""AC adapter dispatch behavior."""

import asyncio
import os
import sys
import unittest
from unittest import mock

_HAWAAI = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _HAWAAI not in sys.path:
    sys.path.insert(0, _HAWAAI)

from backend import ac_aerostate_adapter, ac_tuya_adapter  # noqa: E402


class TestAcAdapterIrDispatch(unittest.TestCase):
    def test_aerostate_turn_on_sends_single_temperature_payload_when_already_cool(self):
        calls = []

        async def fake_call_service(domain, service, payload, **kwargs):
            calls.append((domain, service, dict(payload), dict(kwargs)))
            return True

        async def run_case():
            with (
                mock.patch.object(
                    ac_aerostate_adapter.ha_client,
                    "get_climate_state",
                    return_value={"state": "cool"},
                ),
                mock.patch.object(
                    ac_aerostate_adapter.ha_client,
                    "call_service",
                    side_effect=fake_call_service,
                ),
            ):
                ok = await ac_aerostate_adapter.turn_on("climate.aerostate", 24.0)
            self.assertTrue(ok)

        asyncio.run(run_case())

        self.assertEqual(
            calls,
            [
                (
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": "climate.aerostate",
                        "hvac_mode": "cool",
                        "temperature": 24.0,
                    },
                    {"blocking": True},
                ),
            ],
        )

    def test_aerostate_turn_on_adds_fallback_hvac_mode_only_when_off(self):
        calls = []

        async def fake_call_service(domain, service, payload, **kwargs):
            calls.append((domain, service, dict(payload), dict(kwargs)))
            return True

        async def run_case():
            with (
                mock.patch.object(
                    ac_aerostate_adapter.ha_client,
                    "get_climate_state",
                    return_value={"state": "off"},
                ),
                mock.patch.object(
                    ac_aerostate_adapter.ha_client,
                    "call_service",
                    side_effect=fake_call_service,
                ),
            ):
                ok = await ac_aerostate_adapter.turn_on("climate.aerostate", 24.0)
            self.assertTrue(ok)

        asyncio.run(run_case())

        self.assertEqual(
            calls,
            [
                (
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": "climate.aerostate",
                        "hvac_mode": "cool",
                        "temperature": 24.0,
                    },
                    {"blocking": True},
                ),
                (
                    "climate",
                    "set_hvac_mode",
                    {
                        "entity_id": "climate.aerostate",
                        "hvac_mode": "cool",
                    },
                    {"blocking": True},
                ),
            ],
        )

    def test_tuya_turn_on_keeps_staged_mode_then_temperature_payload(self):
        calls = []

        async def fake_call_service(domain, service, payload, **kwargs):
            calls.append((domain, service, dict(payload), dict(kwargs)))
            return True

        async def run_case():
            with (
                mock.patch.object(
                    ac_tuya_adapter.ha_client,
                    "get_climate_state",
                    return_value={
                        "state": "off",
                        "target_temp": None,
                        "fan_mode": None,
                        "fan_modes": ["auto"],
                    },
                ),
                mock.patch.object(
                    ac_tuya_adapter.ha_client,
                    "call_service",
                    side_effect=fake_call_service,
                ),
                mock.patch.object(ac_tuya_adapter.asyncio, "sleep", new=mock.AsyncMock()) as sleep,
            ):
                ok = await ac_tuya_adapter.turn_on("climate.tuya", 24.0)
            self.assertTrue(ok)
            sleep.assert_awaited_once_with(2.0)

        asyncio.run(run_case())

        self.assertEqual(
            calls,
            [
                (
                    "climate",
                    "set_hvac_mode",
                    {
                        "entity_id": "climate.tuya",
                        "hvac_mode": "cool",
                    },
                    {},
                ),
                (
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": "climate.tuya",
                        "temperature": 24.0,
                        "hvac_mode": "cool",
                    },
                    {},
                ),
                (
                    "climate",
                    "set_fan_mode",
                    {
                        "entity_id": "climate.tuya",
                        "fan_mode": "auto",
                    },
                    {},
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
