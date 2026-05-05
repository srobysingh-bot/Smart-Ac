"""AC adapter IR dispatch behavior."""

import asyncio
import os
import sys
import unittest
from unittest import mock

_HAWAAI = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _HAWAAI not in sys.path:
    sys.path.insert(0, _HAWAAI)

from backend import ac_adapter  # noqa: E402


class TestAcAdapterIrDispatch(unittest.TestCase):
    def test_turn_on_sends_staged_mode_then_temperature_payload(self):
        calls = []

        async def fake_call_service(domain, service, payload, **kwargs):
            calls.append((domain, service, dict(payload), dict(kwargs)))
            return True

        async def run_case():
            with (
                mock.patch.object(
                    ac_adapter.ha_client,
                    "get_climate_state",
                    return_value={
                        "state": "off",
                        "target_temp": None,
                        "fan_mode": None,
                        "fan_modes": ["auto"],
                    },
                ),
                mock.patch.object(
                    ac_adapter.ha_client,
                    "call_service",
                    side_effect=fake_call_service,
                ),
                mock.patch.object(ac_adapter.asyncio, "sleep", new=mock.AsyncMock()) as sleep,
            ):
                ok = await ac_adapter.turn_on("climate.broadlink", 24.0)
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
                        "entity_id": "climate.broadlink",
                        "hvac_mode": "cool",
                    },
                    {"blocking": True},
                ),
                (
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": "climate.broadlink",
                        "temperature": 24.0,
                        "fan_mode": "auto",
                    },
                    {"blocking": True},
                ),
            ],
        )

    def test_turn_on_skips_temperature_when_on_and_temp_unchanged(self):
        async def run_case():
            with (
                mock.patch.object(
                    ac_adapter.ha_client,
                    "get_climate_state",
                    return_value={
                        "state": "cool",
                        "target_temp": 24.1,
                        "fan_mode": "auto",
                        "fan_modes": ["auto"],
                    },
                ),
                mock.patch.object(ac_adapter.ha_client, "call_service") as call_service,
            ):
                ok = await ac_adapter.turn_on("climate.broadlink", 24.0)
            self.assertTrue(ok)
            call_service.assert_not_called()

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()
