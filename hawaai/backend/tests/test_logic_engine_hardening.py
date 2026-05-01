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
        st.physical_ac_on = True
        st.effective_ac_on = True
        st.ac_state = "on"
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
        self.assertTrue(out["physical_ac_on"])
        self.assertTrue(out["effective_ac_on"])
        self.assertEqual(out["ac_state"], "on")
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
        logic_engine._clear_pending_when_physically_satisfied(
            st,
            manual_override_active=False,
            confirmed_ac_on=True,
            physical_ac_on=True,
        )
        self.assertIsNone(st.pending_action)

        st.pending_action = "on"
        st.pending_since = 2.0
        logic_engine._clear_pending_when_physically_satisfied(
            st,
            manual_override_active=True,
            confirmed_ac_on=False,
            physical_ac_on=False,
        )
        self.assertIsNone(st.pending_action)

        # Inferred-only physical ON must NOT clear pending ON until power/IR/HA confirms.
        st.pending_action = "on"
        st.pending_since = 3.0
        st.ac_state_source = "inferred"
        logic_engine._clear_pending_when_physically_satisfied(
            st,
            manual_override_active=False,
            confirmed_ac_on=False,
            physical_ac_on=True,
        )
        self.assertEqual(st.pending_action, "on")
        self.assertEqual(st.pending_since, 3.0)

    def test_clear_pending_off_when_already_off(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "off"
        logic_engine._clear_pending_when_physically_satisfied(
            st,
            manual_override_active=False,
            confirmed_ac_on=False,
            physical_ac_on=False,
        )
        self.assertIsNone(st.pending_action)

    def test_sync_ac_display_pending_on_masks_effective(self):
        st = logic_engine.RoomRuntime()
        st.physical_ac_on = True
        st.pending_action = "on"
        st.pending_since = 1000.0
        logic_engine._sync_ac_display_fields(st)
        self.assertFalse(st.effective_ac_on)
        self.assertEqual(st.ac_state, "pending_on")

    def test_sync_ac_display_pending_off_vs_on(self):
        st = logic_engine.RoomRuntime()
        st.pending_action = "off"
        st.physical_ac_on = True
        logic_engine._sync_ac_display_fields(st)
        self.assertTrue(st.effective_ac_on)
        self.assertEqual(st.ac_state, "pending_off")

        st.pending_action = None
        st.physical_ac_on = True
        logic_engine._sync_ac_display_fields(st)
        self.assertEqual(st.ac_state, "on")

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

    def test_effective_mode_auto_clamps_delta_above_base(self):
        cfg = {"effective_mode": "auto", "effective_max_delta_deg": 3.0}
        self.assertAlmostEqual(
            logic_engine.apply_effective_mode_engine_target(
                room_id="r", base_temp=22.0, planned_with_ai=30.0, cfg=cfg, control_log=False,
            ),
            25.0,
        )
        self.assertAlmostEqual(
            logic_engine.apply_effective_mode_engine_target(
                room_id="r", base_temp=22.0, planned_with_ai=21.0, cfg=cfg, control_log=False,
            ),
            22.0,
        )

    def test_effective_mode_manual_respects_floor_and_ceiling(self):
        cfg = {"effective_mode": "manual", "manual_effective_temp": 20.0, "effective_max_delta_deg": 3.0}
        self.assertAlmostEqual(
            logic_engine.apply_effective_mode_engine_target(
                room_id="r", base_temp=22.0, planned_with_ai=25.0, cfg=cfg, control_log=False,
            ),
            22.0,
        )
        cfg["manual_effective_temp"] = 26.0
        self.assertAlmostEqual(
            logic_engine.apply_effective_mode_engine_target(
                room_id="r", base_temp=22.0, planned_with_ai=23.0, cfg=cfg, control_log=False,
            ),
            25.0,
        )

    def test_effective_max_delta_deg_bounded_1_to_5(self):
        self.assertAlmostEqual(logic_engine.effective_max_delta_deg({"effective_max_delta_deg": 99}), 5.0)
        self.assertAlmostEqual(logic_engine.effective_max_delta_deg({"effective_max_delta_deg": 0.25}), 1.0)

    def test_sync_effective_mode_transition_clears_pending(self):
        st = logic_engine.RoomRuntime()
        st.last_effective_mode = "auto"
        st.pending_action = "on"
        st.pending_since = 123.0
        logic_engine.sync_effective_mode_transition(st, "room-x", {"effective_mode": "manual"})
        self.assertIsNone(st.pending_action)
        self.assertIsNone(st.pending_since)
        self.assertEqual(st.last_effective_mode, "manual")

    def test_gate_turn_ac_on_duplicate_physical_on_skips(self):
        """Same fingerprint allowed only when compressor not observed ON — duplicate + ON skips."""
        logic_engine._runtime_by_room.clear()
        rid = "gate-dup-on"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.last_command_time = now - timedelta(seconds=400)
        st.last_command = "on"
        st.last_sent_command_key = logic_engine._fingerprint_turn_on(23.5)
        st.physical_ac_on = True
        st.compressor_off_since = None
        cfg = {"min_command_interval_seconds": 150, "compressor_min_off_seconds": 0}
        self.assertFalse(logic_engine._gate_turn_ac_on(rid, cfg, 23.5, now))

    def test_gate_turn_ac_on_duplicate_physical_off_bypasses_min_interval(self):
        """Missed ACK: fingerprint matches but physically OFF → still allow past min_command_interval."""
        logic_engine._runtime_by_room.clear()
        rid = "gate-dup-off"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.last_command_time = now - timedelta(seconds=70)
        st.last_command = "on"
        st.last_sent_command_key = logic_engine._fingerprint_turn_on(24.0)
        st.physical_ac_on = False
        st.compressor_off_since = None
        cfg = {"min_command_interval_seconds": 150, "compressor_min_off_seconds": 0}
        self.assertTrue(logic_engine._gate_turn_ac_on(rid, cfg, 24.0, now))

    def test_gate_turn_ac_on_duplicate_physical_off_bypasses_ir_cooldown(self):
        """Same fingerprint + physically OFF bypasses IR cooldown so missed commands can retry."""
        logic_engine._runtime_by_room.clear()
        rid = "gate-dup-ir-off"
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.last_command_time = now - timedelta(seconds=15)
        st.last_command = "on"
        st.last_sent_command_key = logic_engine._fingerprint_turn_on(23.0)
        st.physical_ac_on = False
        st.compressor_off_since = None
        cfg = {"min_command_interval_seconds": 150, "compressor_min_off_seconds": 0}
        self.assertTrue(logic_engine._gate_turn_ac_on(rid, cfg, 23.0, now))


if __name__ == "__main__":
    unittest.main()
