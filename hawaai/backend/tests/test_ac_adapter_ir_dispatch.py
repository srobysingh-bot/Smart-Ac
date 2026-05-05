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
    def test_turn_on_sends_single_combined_temperature_payload(self):
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
            ):
                ok = await ac_adapter.turn_on("climate.broadlink", 24.0)
            self.assertTrue(ok)

        asyncio.run(run_case())

        self.assertEqual(
            calls,
            [
                (
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": "climate.broadlink",
                        "hvac_mode": "cool",
                        "temperature": 24.0,
                        "fan_mode": "auto",
                    },
                    {"blocking": True},
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
