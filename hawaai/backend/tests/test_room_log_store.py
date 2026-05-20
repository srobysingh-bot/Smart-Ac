import os
import sys
import unittest

_HAWAAI = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _HAWAAI not in sys.path:
    sys.path.insert(0, _HAWAAI)

from backend.room_log_store import (  # noqa: E402
    LOG_SCOPE_CONFIG,
    LOG_SCOPE_RUNTIME,
    LOG_SCOPE_SYSTEM,
    RoomLogStore,
)


class TestRoomLogStore(unittest.TestCase):
    def test_append_get_clear_room_logs(self):
        s = RoomLogStore()
        s.append("Living", "[TICK] hello", level="info")
        s.append("living", "[CONTROL] world", level="warning")
        logs = s.get_logs("LIVING", 10)
        self.assertEqual(len(logs), 2)
        self.assertGreater(logs[0]["ts"], 1_000_000_000_000)
        self.assertEqual(logs[0]["room_id"], "living")
        self.assertEqual(logs[0]["scope"], LOG_SCOPE_RUNTIME)
        self.assertEqual(logs[0]["level"], "INFO")
        self.assertEqual(logs[1]["level"], "WARNING")
        self.assertIn("[TICK] hello", logs[0]["message"])
        self.assertIn("[CONTROL] world", logs[1]["message"])
        s.clear("living")
        self.assertEqual(s.get_logs("living", 10), [])

    def test_limit_applies_to_tail(self):
        s = RoomLogStore()
        for i in range(5):
            s.append("room-a", f"m{i}")
        tail = s.get_logs("room-a", 2)
        self.assertEqual([x["message"] for x in tail], ["m3", "m4"])

    def test_latest_key_and_resize(self):
        s = RoomLogStore()
        s.append("r", "a", level="info")
        self.assertIsNotNone(s.latest_key("r"))
        s.set_max_lines_per_room(50)
        for i in range(70):
            s.append("r", f"n{i}", level="error")
        logs = s.get_logs("r", 1000)
        self.assertEqual(len(logs), 50)
        self.assertEqual(logs[-1]["level"], "ERROR")

    def test_runtime_logs_exclude_config_and_system_scopes(self):
        s = RoomLogStore()
        s.append("study", "[TICK] runtime", scope=LOG_SCOPE_RUNTIME)
        s.append("study", "[ENERGY_CONFIG] loaded nested blob", scope=LOG_SCOPE_CONFIG)
        s.append("study", "HTTP GET /api/status", scope=LOG_SCOPE_SYSTEM)

        logs = s.get_logs("study", 10)

        self.assertEqual([x["message"] for x in logs], ["[TICK] runtime"])
        self.assertEqual(s.latest_key("study"), logs[-1]["ts"].__str__() + "-runtime-INFO-[TICK] runtime")

    def test_room_logs_are_strictly_room_scoped(self):
        s = RoomLogStore()
        s.append("dining", "[TICK] dining")
        s.append("study", "[TICK] study")

        self.assertEqual([x["message"] for x in s.get_logs("dining", 10)], ["[TICK] dining"])
        self.assertEqual([x["message"] for x in s.get_logs("study", 10)], ["[TICK] study"])


if __name__ == "__main__":
    unittest.main()
