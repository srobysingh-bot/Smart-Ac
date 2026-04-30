"""logic_engine: room id normalization, case-insensitive resolve, runtime state keys."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_HAWAAI = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _HAWAAI not in sys.path:
    sys.path.insert(0, _HAWAAI)

from backend import logic_engine  # noqa: E402


class TestLogicEngineHardening(unittest.TestCase):
    def test_normalize_room_id_strip_lower(self):
        self.assertEqual(logic_engine.normalize_room_id("  BedROOM "), "bedroom")
        self.assertEqual(logic_engine.normalize_room_id(""), "")

    def test_resolve_room_definition_case_insensitive(self):
        cfg = {
            "rooms": [
                {
                    "id": "aBc123XYZ789",
                    "name": "Living",
                    "climate_entity": "climate.ac_living",
                },
            ]
        }
        r = logic_engine.resolve_room_definition(cfg, "  abc123xYz789 ")
        self.assertIsNotNone(r)
        self.assertEqual(r["id"], "aBc123XYZ789")

    def test_get_runtime_state_matches_canonical_room(self):
        """API may pass mixed-case URL id — merge config from resolve_room_definition."""
        rid = "cafef00dbabe"
        logic_engine._runtime_by_room.clear()
        st = logic_engine._rt(rid)
        st.ac_is_on = True
        st.effective_ac_on = True
        st.effective_ac_idle = False
        st.effective_power_source = "internal"
        st.effective_control_source = "test"
        st.effective_target_temp = 24.0
        st.last_command_source = "system"
        base = {
            "rooms": [
                {
                    "id": rid,
                    "climate_entity": "climate.x",
                    "min_command_interval_seconds": 150,
                },
            ]
        }
        with mock.patch.object(logic_engine.config_manager, "load_config", return_value=base):
            out = logic_engine.get_runtime_state("  CaFeF00DBabE ")
        self.assertTrue(out["ac_is_on"])
        self.assertTrue(out["effective_ac_on"])
        self.assertIn(out["ac_state_source"], ("power", "inferred", "system"))

    def test_sync_pending_clears_when_decision_not_on_off(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "on"
        st.pending_since = 1.0
        logic_engine._sync_pending_for_action(st, "hold")
        self.assertIsNone(st.pending_action)
        self.assertIsNone(st.pending_since)

    def test_sync_pending_clears_when_flipping_on_to_off(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "on"
        st.pending_since = 1.0
        logic_engine._sync_pending_for_action(st, "off")
        self.assertIsNone(st.pending_action)

    def test_sync_pending_preserves_when_matching_intent(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "on"
        st.pending_since = 1.0
        logic_engine._sync_pending_for_action(st, "on")
        self.assertEqual(st.pending_action, "on")
        self.assertEqual(st.pending_since, 1.0)

    def test_clear_pending_on_satisfied_physically_or_override(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "on"
        st.effective_ac_on = True
        logic_engine._clear_pending_when_physically_satisfied(st, manual_override_active=False)
        self.assertIsNone(st.pending_action)

        st.pending_action = "on"
        st.pending_since = 2.0
        st.effective_ac_on = False
        logic_engine._clear_pending_when_physically_satisfied(st, manual_override_active=True)
        self.assertIsNone(st.pending_action)

        st.pending_action = "on"
        st.pending_since = 3.0
        st.effective_ac_on = False
        st.ac_state_source = "inferred"
        logic_engine._clear_pending_when_physically_satisfied(st, manual_override_active=False)
        self.assertIsNone(st.pending_action)

    def test_clear_pending_off_when_already_off(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "off"
        st.effective_ac_on = False
        logic_engine._clear_pending_when_physically_satisfied(st, manual_override_active=False)
        self.assertIsNone(st.pending_action)

    def test_decision_lock_blocks_delayed_emit_inside_window(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.last_decision_at = now - timedelta(seconds=5)
        self.assertTrue(logic_engine._decision_lock_blocks_delayed_emit(st, now))

    def test_decision_lock_allows_delayed_emit_after_window(self):
        st = logic_engine.RoomRuntime()
        now = datetime.now(timezone.utc)
        st.last_decision_at = now - timedelta(seconds=int(logic_engine.DECISION_LOCK_SECONDS) + 5)
        self.assertFalse(logic_engine._decision_lock_blocks_delayed_emit(st, now))


if __name__ == "__main__":
    unittest.main()
