"""Climate-card command dispatch fast lane."""

import asyncio
import os
import sys
import time
import unittest
from unittest import mock

_HAWAAI = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _HAWAAI not in sys.path:
    sys.path.insert(0, _HAWAAI)

from backend import main  # noqa: E402


class TestClimateCommandLane(unittest.TestCase):
    def setUp(self):
        main._climate_command_state.clear()
        main._api_last_command.clear()

    def tearDown(self):
        main._climate_command_state.clear()
        main._api_last_command.clear()

    def test_temperature_command_starts_service_without_pre_dispatch_debounce(self):
        calls = []

        async def fake_call_service(domain, service, payload):
            calls.append((time.monotonic(), domain, service, dict(payload)))
            return True

        async def run_case():
            received = time.monotonic()
            with (
                mock.patch.object(main.ha_client, "call_service", side_effect=fake_call_service),
                mock.patch.object(main.logic_engine, "record_user_temperature_command"),
                mock.patch.object(main.logic_engine, "trigger_tick"),
            ):
                result = await asyncio.wait_for(
                    main._enqueue_climate_command(
                        room_id="dining",
                        entity_id="climate.dining",
                        service="set_temperature",
                        payload={"entity_id": "climate.dining", "temperature": 22.0},
                        api_received_mono=received,
                    ),
                    timeout=0.2,
                )
            return received, result

        received, result = asyncio.run(run_case())

        self.assertTrue(result["success"])
        self.assertEqual(len(calls), 1)
        self.assertLess(calls[0][0] - received, 0.1)
        self.assertEqual(calls[0][1:], (
            "climate",
            "set_temperature",
            {"entity_id": "climate.dining", "temperature": 22.0},
        ))

    def test_rapid_temperature_taps_keep_first_and_latest_only(self):
        calls = []
        first_call_started = asyncio.Event()

        async def fake_call_service(domain, service, payload):
            calls.append(dict(payload))
            first_call_started.set()
            await asyncio.sleep(0.03)
            return True

        async def run_case():
            with (
                mock.patch.object(main.ha_client, "call_service", side_effect=fake_call_service),
                mock.patch.object(main.logic_engine, "record_user_temperature_command"),
                mock.patch.object(main.logic_engine, "trigger_tick"),
                mock.patch.object(main, "_CLIMATE_COMMAND_AEROSTATE_TRAILING_LOCK_SECS", 0.0),
            ):
                first = asyncio.create_task(main._enqueue_climate_command(
                    room_id="dining",
                    entity_id="climate.dining",
                    service="set_temperature",
                    payload={"entity_id": "climate.dining", "temperature": 24.0},
                ))
                await first_call_started.wait()
                middle = asyncio.create_task(main._enqueue_climate_command(
                    room_id="dining",
                    entity_id="climate.dining",
                    service="set_temperature",
                    payload={"entity_id": "climate.dining", "temperature": 23.0},
                ))
                latest = asyncio.create_task(main._enqueue_climate_command(
                    room_id="dining",
                    entity_id="climate.dining",
                    service="set_temperature",
                    payload={"entity_id": "climate.dining", "temperature": 22.0},
                ))
                return await asyncio.gather(first, middle, latest)

        results = asyncio.run(run_case())

        self.assertEqual(
            [payload["temperature"] for payload in calls],
            [24.0, 22.0],
        )
        self.assertTrue(results[1]["dropped"])
        self.assertTrue(results[2]["success"])

    def test_user_temperature_command_records_learning_but_mode_command_does_not(self):
        async def fake_call_service(domain, service, payload):
            return True

        async def run_case():
            with (
                mock.patch.object(main.ha_client, "call_service", side_effect=fake_call_service),
                mock.patch.object(main.logic_engine, "record_user_temperature_command") as record_temp,
                mock.patch.object(main.logic_engine, "record_user_api_command") as record_api,
                mock.patch.object(main.logic_engine, "trigger_tick"),
            ):
                await main._enqueue_climate_command(
                    room_id="dining",
                    entity_id="climate.dining",
                    service="set_temperature",
                    payload={"entity_id": "climate.dining", "temperature": 21.0},
                )
                await main._enqueue_climate_command(
                    room_id="dining",
                    entity_id="climate.dining",
                    service="set_hvac_mode",
                    payload={"entity_id": "climate.dining", "hvac_mode": "cool"},
                )
            return record_temp, record_api

        record_temp, record_api = asyncio.run(run_case())

        record_temp.assert_called_once_with("dining", 21.0)
        record_api.assert_called_once_with("dining")


if __name__ == "__main__":
    unittest.main()
