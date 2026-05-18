"""Room id normalization and config load recovery (no crash, stable ids)."""

import json
import os
import sys
import tempfile
import unittest
import asyncio
from unittest import mock

_HAWAAI = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _HAWAAI not in sys.path:
    sys.path.insert(0, _HAWAAI)

from backend import room_registry  # noqa: E402


class TestRoomIds(unittest.TestCase):
    def test_new_room_id_is_hex_length_12(self):
        for _ in range(8):
            rid = room_registry._new_room_id()
            self.assertEqual(len(rid), 12)
            self.assertTrue(all(c in "0123456789abcdef" for c in rid))

    def test_short_id_regenerated(self):
        fixed = room_registry._ensure_stable_room_id("ab")
        self.assertGreaterEqual(len(fixed), room_registry._MIN_ROOM_ID_LEN)

    def test_normalize_room_list_rejects_short_ids(self):
        rooms = [{"id": "xx", "name": "A", "climate_entity": "climate.a"}]
        room_registry._normalize_room_list(rooms)
        self.assertGreaterEqual(len(rooms[0]["id"]), room_registry._MIN_ROOM_ID_LEN)


class TestConfigLoad(unittest.TestCase):
    def test_load_uses_backup_when_primary_read_fails(self):
        import backend.config_manager as cm

        real_read = cm._read_json_dict
        merged_room_id = {"rooms": [{"id": "abc123def456", "name": "R", "climate_entity": "climate.x"}]}

        with tempfile.TemporaryDirectory() as td:
            primary = os.path.join(td, "hawaai_config.json")
            backup = os.path.join(td, "hawaai_backup1.json")

            with open(backup, "w", encoding="utf-8") as f:
                json.dump(merged_room_id, f)

            cm._last_known_good_config = None

            def read_side(path: str):
                if os.path.abspath(path) == os.path.abspath(primary):
                    raise RuntimeError("simulated primary read failure")
                return real_read(path)

            with mock.patch.object(cm, "CONFIG_PATH", primary):
                with mock.patch.object(cm, "_read_json_dict", side_effect=read_side):
                    with mock.patch.object(cm, "_backup_config_paths_newest_first", return_value=[backup]):
                        merged = cm.load_config()

            self.assertIsInstance(merged.get("rooms"), list)
            self.assertGreaterEqual(len(merged["rooms"]), 1)
            self.assertEqual(merged["rooms"][0]["id"], "abc123def456")

    def test_room_energy_config_persists_through_update_reload_and_merge(self):
        import backend.config_manager as cm
        import backend.main as main

        initial = {
            "rooms": [
                {
                    "id": "roomabc12345",
                    "name": "Study",
                    "climate_entity": "climate.study",
                    "settings": {"target_temp": 25},
                }
            ]
        }

        with tempfile.TemporaryDirectory() as td:
            primary = os.path.join(td, "hawaai_config.json")
            with open(primary, "w", encoding="utf-8") as f:
                json.dump(initial, f)

            cm._last_known_good_config = None
            with (
                mock.patch.object(cm, "CONFIG_PATH", primary),
                mock.patch.object(main.logic_engine, "trigger_tick") as trigger_tick,
            ):
                asyncio.run(
                    main.api_update_room(
                        "roomabc12345",
                        {
                            "energy_device_id": "dev-breaker-1",
                            "energy_device_name": "Study Breaker",
                            "energy_power_entity": "sensor.study_power",
                            "energy_kwh_entity": "sensor.study_energy",
                            "settings": {"vacancy_timeout_minutes": 7},
                        },
                    )
                )
                reloaded = cm.load_config()
                trigger_tick.assert_called_once_with(
                    "roomabc12345",
                    reason="config_updated",
                    skip_debounce=True,
                )

            room = reloaded["rooms"][0]
            self.assertEqual(room["energy_device_id"], "dev-breaker-1")
            self.assertEqual(room["energy_device_name"], "Study Breaker")
            self.assertEqual(room["energy_power_entity"], "sensor.study_power")
            self.assertEqual(room["energy_kwh_entity"], "sensor.study_energy")
            self.assertEqual(room["settings"]["target_temp"], 25)
            self.assertEqual(room["settings"]["vacancy_timeout_minutes"], 7)

            effective = room_registry.merge_room_config(reloaded, room)
            self.assertEqual(effective["energy_power_entity"], "sensor.study_power")
            self.assertEqual(effective["energy_kwh_entity"], "sensor.study_energy")

    def test_room_energy_aliases_are_migrated_without_silent_loss(self):
        import backend.config_manager as cm
        import backend.main as main

        initial = {
            "rooms": [
                {
                    "id": "roomdef12345",
                    "name": "Bedroom",
                    "climate_entity": "climate.bedroom",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as td:
            primary = os.path.join(td, "hawaai_config.json")
            with open(primary, "w", encoding="utf-8") as f:
                json.dump(initial, f)

            cm._last_known_good_config = None
            with (
                mock.patch.object(cm, "CONFIG_PATH", primary),
                mock.patch.object(main.logic_engine, "trigger_tick") as trigger_tick,
            ):
                asyncio.run(
                    main.api_update_room(
                        "roomdef12345",
                        {
                            "breaker_device_id": "dev-legacy",
                            "settings": {
                                "live_power_sensor": "sensor.bedroom_power",
                                "energy_usage_sensor": "sensor.bedroom_kwh",
                            },
                        },
                    )
                )
                reloaded = cm.load_config()
                trigger_tick.assert_called_once_with(
                    "roomdef12345",
                    reason="config_updated",
                    skip_debounce=True,
                )

        room = reloaded["rooms"][0]
        self.assertEqual(room["energy_device_id"], "dev-legacy")
        self.assertEqual(room["energy_power_entity"], "sensor.bedroom_power")
        self.assertEqual(room["energy_kwh_entity"], "sensor.bedroom_kwh")
        self.assertNotIn("live_power_sensor", room.get("settings", {}))
        self.assertNotIn("energy_usage_sensor", room.get("settings", {}))


if __name__ == "__main__":
    unittest.main()
