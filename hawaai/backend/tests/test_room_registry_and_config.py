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
    def test_settings_ui_allows_zero_vacancy_timeout(self):
        settings_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "src", "pages", "Settings.jsx")
        )
        with open(settings_path, "r", encoding="utf-8") as f:
            src = f.read()

        self.assertIn('label="Vacancy Timeout"', src)
        self.assertIn('min={0} max={60} step={1} unit=" min"', src)
        self.assertIn("0 min = turn OFF as soon as vacancy is confirmed.", src)

    def test_frontend_build_script_and_precool_card_cancel_regression(self):
        frontend_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
        )
        with open(os.path.join(frontend_root, "package.json"), "r", encoding="utf-8") as f:
            pkg = json.load(f)
        with open(
            os.path.join(frontend_root, "src", "components", "ACStatusCard.jsx"),
            "r",
            encoding="utf-8",
        ) as f:
            src = f.read()

        self.assertEqual(pkg.get("scripts", {}).get("build"), "vite build")
        self.assertEqual(src.count("\n              Cancel\n"), 1)
        self.assertEqual(src.count("\n              Snooze today\n"), 1)

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

    def test_load_config_logs_energy_summary_only_when_changed(self):
        import backend.config_manager as cm

        initial = {
            "rooms": [
                {
                    "id": "roomlog12345",
                    "name": "Study",
                    "climate_entity": "climate.study",
                    "settings": {"large_nested_blob": {"not": "for logs"}},
                }
            ]
        }

        with tempfile.TemporaryDirectory() as td:
            primary = os.path.join(td, "hawaai_config.json")
            with open(primary, "w", encoding="utf-8") as f:
                json.dump(initial, f)

            cm._last_known_good_config = None
            cm._last_logged_config_load_sig = None
            with (
                mock.patch.object(cm, "CONFIG_PATH", primary),
                mock.patch.object(cm.logger, "info") as info,
            ):
                cm.load_config()
                cm.load_config()

            loaded_calls = [
                call for call in info.call_args_list
                if call.args and call.args[0] == "[ENERGY_CONFIG] loaded %s"
            ]

        cm._last_logged_config_load_sig = None

        self.assertEqual(len(loaded_calls), 1)
        rendered = str(loaded_calls[0].args[1])
        self.assertIn("'rooms': 1", rendered)
        self.assertNotIn("climate.study", rendered)
        self.assertNotIn("large_nested_blob", rendered)

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

    def test_backend_accepts_zero_vacancy_timeout(self):
        import backend.config_manager as cm
        import backend.main as main

        initial = {
            "rooms": [
                {
                    "id": "roomzero12345",
                    "name": "Study",
                    "climate_entity": "climate.study",
                    "settings": {"vacancy_timeout_minutes": 5},
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
                mock.patch.object(main.logic_engine, "trigger_tick"),
            ):
                asyncio.run(
                    main.api_update_room(
                        "roomzero12345",
                        {"settings": {"vacancy_timeout_minutes": 0}},
                    )
                )
                reloaded = cm.load_config()

        room = reloaded["rooms"][0]
        self.assertEqual(room["settings"]["vacancy_timeout_minutes"], 0)
        effective = room_registry.merge_room_config(reloaded, room)
        self.assertEqual(effective["vacancy_timeout_minutes"], 0)

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

    def test_temperature_mode_transition_clears_override_once(self):
        import backend.config_manager as cm
        import backend.main as main

        initial = {
            "rooms": [
                {
                    "id": "roommode1234",
                    "name": "Dining",
                    "climate_entity": "climate.dining",
                    "settings": {
                        "temperature_mode": "manual",
                        "manual_override_enabled": True,
                        "manual_override": True,
                    },
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
                mock.patch.object(main.logic_engine, "clear_manual_override_and_resume", new=mock.AsyncMock()) as clear_resume,
                mock.patch.object(main.logic_engine, "trigger_tick") as trigger_tick,
            ):
                asyncio.run(
                    main.api_update_room(
                        "roommode1234",
                        {"settings": {"temperature_mode": "auto_comfort"}},
                    )
                )

        clear_resume.assert_awaited_once_with("roommode1234", reason="temperature_mode_changed")
        trigger_tick.assert_not_called()

    def test_same_temperature_mode_payload_does_not_clear_override(self):
        import backend.config_manager as cm
        import backend.main as main

        initial = {
            "rooms": [
                {
                    "id": "roommode5678",
                    "name": "Dining",
                    "climate_entity": "climate.dining",
                    "settings": {
                        "temperature_mode": "auto_comfort",
                        "manual_override_enabled": False,
                        "manual_override": False,
                    },
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
                mock.patch.object(main.logic_engine, "clear_manual_override_and_resume", new=mock.AsyncMock()) as clear_resume,
                mock.patch.object(main.logic_engine, "trigger_tick") as trigger_tick,
            ):
                asyncio.run(
                    main.api_update_room(
                        "roommode5678",
                        {"settings": {"temperature_mode": "auto_comfort"}},
                    )
                )

        clear_resume.assert_not_awaited()
        trigger_tick.assert_called_once_with(
            "roommode5678",
            reason="config_updated",
            skip_debounce=True,
        )

    def test_config_migration_preserves_entities_and_drops_runtime_state(self):
        import backend.config_manager as cm

        migrated = cm.migrate_config(
            {
                "rooms": [
                    {
                        "id": "roommig12345",
                        "name": "Study",
                        "climate_entity": "climate.study",
                        "energy_power_entity": "sensor.study_power",
                        "effective_on_since_ts": 12345,
                        "pending_off_confirmation": True,
                        "settings": {
                            "energy_usage_sensor": "sensor.study_kwh",
                            "watt_draw": 900,
                            "manual_override": True,
                            "target_temp": 23,
                        },
                    }
                ],
                "runtime": {"roommig12345": {"ac_state": "on"}},
            }
        )

        room = migrated["rooms"][0]
        self.assertEqual(migrated["schema_version"], cm.CONFIG_SCHEMA_VERSION)
        self.assertEqual(room["energy_power_entity"], "sensor.study_power")
        self.assertEqual(room["energy_kwh_entity"], "sensor.study_kwh")
        self.assertNotIn("runtime", migrated)
        self.assertNotIn("effective_on_since_ts", room)
        self.assertNotIn("pending_off_confirmation", room)
        self.assertNotIn("watt_draw", room.get("settings", {}))
        self.assertTrue(room["settings"]["manual_override_enabled"])
        self.assertTrue(room["settings"]["manual_override"])
        self.assertIsInstance(room["settings"]["override_started_at"], str)
        self.assertIsInstance(room["settings"]["override_user_settings"], dict)

    def test_migrated_config_is_persisted_and_not_logged_again(self):
        import backend.config_manager as cm

        initial = {
            "rooms": [
                {
                    "id": "persist12345",
                    "name": "Study",
                    "climate_entity": "climate.study",
                    "energy_power_entity": "sensor.study_power",
                    "effective_on_since_ts": 12345,
                    "settings": {
                        "energy_usage_sensor": "sensor.study_kwh",
                        "watt_draw": 900,
                    },
                }
            ]
        }

        with tempfile.TemporaryDirectory() as td:
            primary = os.path.join(td, "hawaai_config.json")
            with open(primary, "w", encoding="utf-8") as f:
                json.dump(initial, f)

            cm._last_known_good_config = None
            cm._last_logged_config_load_sig = None
            cm._logged_migration_steps.clear()
            with mock.patch.object(cm, "CONFIG_PATH", primary):
                first = cm.load_config()
                self.assertEqual(first["schema_version"], cm.CONFIG_SCHEMA_VERSION)
                self.assertTrue(cm.persist_migrated_config_if_needed())
                with open(primary, "r", encoding="utf-8") as f:
                    saved = json.load(f)

                self.assertEqual(saved["schema_version"], cm.CONFIG_SCHEMA_VERSION)
                room = saved["rooms"][0]
                self.assertEqual(room["energy_power_entity"], "sensor.study_power")
                self.assertEqual(room["energy_kwh_entity"], "sensor.study_kwh")
                self.assertNotIn("effective_on_since_ts", room)
                self.assertNotIn("watt_draw", room.get("settings", {}))

                cm._logged_migration_steps.clear()
                with mock.patch.object(cm.logger, "info") as info:
                    cm.load_config()

            migration_calls = [
                call for call in info.call_args_list
                if call.args and str(call.args[0]).startswith("[CONFIG] migration_applied")
            ]
            self.assertEqual(migration_calls, [])


if __name__ == "__main__":
    unittest.main()
