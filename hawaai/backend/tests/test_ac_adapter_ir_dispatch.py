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

    def test_tuya_automatic_on_sends_cool_even_when_ha_already_cool(self):
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
                        "state": "cool",
                        "target_temp": 24.0,
                        "fan_mode": "auto",
                        "fan_modes": ["auto"],
                        "swing_mode": "off",
                        "swing_modes": ["off", "vertical"],
                    },
                ),
                mock.patch.object(
                    ac_tuya_adapter.ha_client,
                    "call_service",
                    side_effect=fake_call_service,
                ),
                mock.patch.object(ac_tuya_adapter.asyncio, "sleep", new=mock.AsyncMock()) as sleep,
            ):
                ok = await ac_tuya_adapter.turn_on(
                    "climate.tuya",
                    24.0,
                    force_physical_on=True,
                    physical_power_watts=0.0,
                    last_commanded_temperature=24.0,
                )
            self.assertTrue(ok)
            sleep.assert_awaited_once_with(ac_tuya_adapter.TUYA_SETTLE_DELAY_SECONDS)

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
            ],
        )

    def test_tuya_normal_on_sends_no_unnecessary_duplicate_services(self):
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
                        "target_temp": 24.0,
                        "fan_mode": "auto",
                        "fan_modes": ["auto"],
                        "swing_mode": "off",
                        "swing_modes": ["off", "vertical"],
                    },
                ),
                mock.patch.object(
                    ac_tuya_adapter.ha_client,
                    "call_service",
                    side_effect=fake_call_service,
                ),
                mock.patch.object(ac_tuya_adapter.asyncio, "sleep", new=mock.AsyncMock()) as sleep,
            ):
                ok = await ac_tuya_adapter.turn_on(
                    "climate.tuya",
                    24.0,
                    last_commanded_temperature=24.0,
                )
            self.assertTrue(ok)
            sleep.assert_awaited_once_with(ac_tuya_adapter.TUYA_SETTLE_DELAY_SECONDS)

        asyncio.run(run_case())

        self.assertEqual(
            [call[1] for call in calls],
            ["set_hvac_mode"],
        )

    def test_tuya_temperature_sent_only_when_target_differs(self):
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
                        "state": "cool",
                        "target_temp": 26.0,
                        "fan_mode": "auto",
                        "fan_modes": ["auto"],
                        "swing_mode": "off",
                        "swing_modes": ["off"],
                    },
                ),
                mock.patch.object(
                    ac_tuya_adapter.ha_client,
                    "call_service",
                    side_effect=fake_call_service,
                ),
            ):
                ok = await ac_tuya_adapter.turn_on("climate.tuya", 24.0)
            self.assertTrue(ok)

        asyncio.run(run_case())

        self.assertEqual(
            [call[1] for call in calls],
            ["set_temperature"],
        )
        self.assertEqual(
            calls[0][2],
            {"entity_id": "climate.tuya", "temperature": 24.0, "hvac_mode": "cool"},
        )

    def test_tuya_fan_and_swing_are_not_sent_when_unchanged(self):
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
                        "state": "cool",
                        "target_temp": 24.0,
                        "fan_mode": "auto",
                        "fan_modes": ["auto", "high"],
                        "swing_mode": "off",
                        "swing_modes": ["off", "vertical"],
                    },
                ),
                mock.patch.object(
                    ac_tuya_adapter.ha_client,
                    "call_service",
                    side_effect=fake_call_service,
                ),
            ):
                ok = await ac_tuya_adapter.turn_on(
                    "climate.tuya",
                    24.0,
                    fan_mode="auto",
                    swing_mode="off",
                    last_commanded_temperature=24.0,
                )
            self.assertTrue(ok)

        asyncio.run(run_case())

        self.assertEqual(calls, [])

    def test_tuya_fan_command_sent_only_when_requested_fan_differs(self):
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
                        "state": "cool",
                        "target_temp": 24.0,
                        "fan_mode": "auto",
                        "fan_modes": ["auto", "high"],
                        "swing_mode": "off",
                        "swing_modes": ["off"],
                    },
                ),
                mock.patch.object(
                    ac_tuya_adapter.ha_client,
                    "call_service",
                    side_effect=fake_call_service,
                ),
            ):
                ok = await ac_tuya_adapter.turn_on(
                    "climate.tuya",
                    24.0,
                    fan_mode="high",
                    last_commanded_temperature=24.0,
                )
            self.assertTrue(ok)

        asyncio.run(run_case())

        self.assertEqual(
            calls,
            [
                (
                    "climate",
                    "set_fan_mode",
                    {"entity_id": "climate.tuya", "fan_mode": "high"},
                    {},
                ),
            ],
        )

    def test_tuya_swing_command_sent_only_when_requested_swing_differs(self):
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
                        "state": "cool",
                        "target_temp": 24.0,
                        "fan_mode": "auto",
                        "fan_modes": ["auto"],
                        "swing_mode": "off",
                        "swing_modes": ["off", "vertical"],
                    },
                ),
                mock.patch.object(
                    ac_tuya_adapter.ha_client,
                    "call_service",
                    side_effect=fake_call_service,
                ),
            ):
                ok = await ac_tuya_adapter.turn_on(
                    "climate.tuya",
                    24.0,
                    swing_mode="vertical",
                    last_commanded_temperature=24.0,
                )
            self.assertTrue(ok)

        asyncio.run(run_case())

        self.assertEqual(
            calls,
            [
                (
                    "climate",
                    "set_swing_mode",
                    {"entity_id": "climate.tuya", "swing_mode": "vertical"},
                    {},
                ),
            ],
        )

    def test_tuya_full_state_pack_uses_single_combined_command(self):
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
                        "target_temp": 26.0,
                        "fan_mode": "auto",
                        "fan_modes": ["auto", "high"],
                        "swing_mode": "off",
                        "swing_modes": ["off", "vertical"],
                        "full_state_on_supported": True,
                    },
                ),
                mock.patch.object(
                    ac_tuya_adapter.ha_client,
                    "call_service",
                    side_effect=fake_call_service,
                ),
            ):
                ok = await ac_tuya_adapter.turn_on(
                    "climate.tuya",
                    24.0,
                    fan_mode="high",
                    swing_mode="vertical",
                )
            self.assertTrue(ok)

        asyncio.run(run_case())

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "set_temperature")
        self.assertEqual(
            calls[0][2],
            {
                "entity_id": "climate.tuya",
                "power_on": True,
                "temperature": 24.0,
                "hvac_mode": "cool",
                "fan_mode": "high",
                "swing_mode": "vertical",
            },
        )


if __name__ == "__main__":
    unittest.main()
