"""Room id normalization and config load recovery (no crash, stable ids)."""

import json
import os
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
