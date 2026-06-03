import asyncio
from datetime import datetime, timedelta, timezone
from unittest import TestCase, mock

from backend import ha_entity_events, logic_engine, main


def _cfg(rid="precool-room", **settings):
    base_settings = {
        "pre_cool_enabled": True,
        "pre_cool_duration_minutes": 25,
        "pre_cool_min_temp_gap_deg": 1.0,
        "pre_cool_target_offset_deg": 1.0,
        "pre_cool_arrival_grace_seconds": 120,
        "pre_cool_geofence_enabled": False,
        "pre_cool_geofence_mode": "suggest_only",
        "pre_cool_geofence_radius_km": 2,
        "pre_cool_home_latitude": 28.6139,
        "pre_cool_home_longitude": 77.2090,
        "pre_cool_allowed_people": [],
        "pre_cool_geofence_cooldown_minutes": 30,
        "pre_cool_one_shot_per_window": True,
        "pre_cool_allow_extension": True,
        "pre_cool_extension_minutes": 10,
        "pre_cool_max_total_minutes": 45,
        "pre_cool_stop_if_user_leaves_geofence": True,
        "target_temp": 24,
        "temperature_mode": "manual",
        "use_presence": True,
    }
    base_settings.update(settings)
    return {
        "target_temp": 24,
        "temperature_mode": "manual",
        "rooms": [
            {
                "id": rid,
                "name": "Room",
                "climate_entity": "climate.room",
                "presence_entity": "binary_sensor.room_presence",
                "indoor_temp_entity": "sensor.room_temp",
                "settings": base_settings,
            }
        ],
    }


class PreCoolTriggerTests(TestCase):
    def setUp(self):
        logic_engine._runtime_by_room.clear()

    async def _start(self, cfg, rid="precool-room", source="manual_button", **kwargs):
        with (
            mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg),
            mock.patch.object(logic_engine.weather_api, "get_cached", new=mock.AsyncMock(return_value={})),
            mock.patch.object(logic_engine.ha_client, "get_state", new=mock.AsyncMock(side_effect=["off", "29"])),
            mock.patch.object(logic_engine.ha_client, "call_service", new=mock.AsyncMock(return_value=True)),
            mock.patch.object(logic_engine, "tick", new=mock.AsyncMock()) as tick_mock,
        ):
            result = await logic_engine.start_pre_cool(rid, source, **kwargs)
        return result, tick_mock

    def test_geofence_disabled_does_nothing(self):
        async def run_case():
            result, tick_mock = await self._start(_cfg(), source="geofence", person="person.amit")
            self.assertFalse(result["success"])
            self.assertEqual(result["pre_cool_result"], "blocked_geofence_disabled")
            tick_mock.assert_not_awaited()

        asyncio.run(run_case())

    def test_person_not_allowed_does_nothing(self):
        async def run_case():
            cfg = _cfg(pre_cool_geofence_enabled=True, pre_cool_allowed_people=["person.allowed"])
            result, tick_mock = await self._start(cfg, source="geofence", person="person.other")
            self.assertFalse(result["success"])
            self.assertEqual(result["pre_cool_result"], "blocked_person_not_allowed")
            tick_mock.assert_not_awaited()

        asyncio.run(run_case())

    def test_suggest_only_creates_suggestion_only(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="suggest_only",
                pre_cool_allowed_people=["person.amit"],
            )
            result, tick_mock = await self._start(cfg, source="geofence", person="person.amit")
            self.assertTrue(result["success"])
            self.assertFalse(result["pre_cool_active"])
            self.assertEqual(result["pre_cool_result"], "suggestion_created")
            self.assertEqual(result["pre_cool_trigger_source"], "geofence")
            tick_mock.assert_not_awaited()

        asyncio.run(run_case())

    def test_suggest_only_creates_persistent_notification_with_addon_radius_context(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="suggest_only",
                pre_cool_allowed_people=["person.amit"],
                pre_cool_geofence_radius_km=3,
            )
            with (
                mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine.weather_api, "get_cached", new=mock.AsyncMock(return_value={})),
                mock.patch.object(logic_engine.ha_client, "get_state", new=mock.AsyncMock(side_effect=["off", "29"])),
                mock.patch.object(logic_engine.ha_client, "call_service", new=mock.AsyncMock(return_value=True)) as notify_mock,
                mock.patch.object(logic_engine, "tick", new=mock.AsyncMock()) as tick_mock,
            ):
                result = await logic_engine.start_pre_cool("precool-room", "geofence", "person.amit")

            self.assertEqual(result["pre_cool_result"], "suggestion_created")
            tick_mock.assert_not_awaited()
            notify_mock.assert_awaited_once()
            args = notify_mock.await_args.args
            self.assertEqual(args[:2], ("persistent_notification", "create"))
            self.assertIn("add-on pre-cool radius", args[2]["message"])
            self.assertIn("3.0 km", args[2]["message"])

        asyncio.run(run_case())

    def test_auto_start_starts_pre_cool(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="auto_start",
                pre_cool_allowed_people=["person.amit"],
            )
            result, tick_mock = await self._start(cfg, source="geofence", person="person.amit")
            self.assertTrue(result["pre_cool_active"])
            self.assertEqual(result["pre_cool_result"], "started")
            self.assertEqual(result["pre_cool_geofence_trigger_person"], "person.amit")
            tick_mock.assert_awaited_once()

        asyncio.run(run_case())

    def test_ha_person_location_enters_addon_radius_triggers_geofence_pre_cool(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="auto_start",
                pre_cool_allowed_people=["person.amit"],
            )
            ix = ha_entity_events._entity_watch_index(cfg)
            self.assertIn("person.amit", ix)
            self.assertIn(("precool-room", "precool-room", "geofence_person"), ix["person.amit"])

            with (
                mock.patch.object(ha_entity_events.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine, "start_pre_cool", new=mock.AsyncMock()) as start_mock,
            ):
                await ha_entity_events._handle_state_changed(
                    {
                        "entity_id": "person.amit",
                        "old_state": {
                            "state": "not_home",
                            "attributes": {"latitude": 28.6500, "longitude": 77.2090},
                        },
                        "new_state": {
                            "state": "not_home",
                            "attributes": {"latitude": 28.6145, "longitude": 77.2090},
                            "last_changed": "2026-06-03T06:45:00+00:00",
                            "last_updated": "2026-06-03T06:45:02+00:00",
                        },
                    },
                    ix,
                )

            start_mock.assert_awaited_once_with(
                "precool-room",
                "geofence",
                "person.amit",
                visit_id="person.amit:2026-06-03T06:45:02+00:00",
                inside_geofence=True,
                approaching=True,
            )

        asyncio.run(run_case())

    def test_missing_home_coordinates_warns_and_does_not_trigger_geofence(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="auto_start",
                pre_cool_allowed_people=["person.amit"],
                pre_cool_home_latitude=None,
                pre_cool_home_longitude=None,
            )
            ix = ha_entity_events._entity_watch_index(cfg)
            ha_entity_events._missing_home_location_warned.clear()
            with (
                mock.patch.object(ha_entity_events.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine, "start_pre_cool", new=mock.AsyncMock()) as start_mock,
                mock.patch.object(ha_entity_events.logger, "warning") as warning_mock,
            ):
                await ha_entity_events._handle_state_changed(
                    {
                        "entity_id": "person.amit",
                        "old_state": {
                            "state": "not_home",
                            "attributes": {"latitude": 28.6500, "longitude": 77.2090},
                        },
                        "new_state": {
                            "state": "not_home",
                            "attributes": {"latitude": 28.6145, "longitude": 77.2090},
                        },
                    },
                    ix,
                )

            start_mock.assert_not_awaited()
            warning_mock.assert_called_once()
            self.assertIn("geofence_home_coordinates_missing", warning_mock.call_args.args[0])

        asyncio.run(run_case())

    def test_geofence_auto_start_blocked_without_home_coordinates(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="auto_start",
                pre_cool_allowed_people=["person.amit"],
                pre_cool_home_latitude=None,
                pre_cool_home_longitude=None,
            )
            result, tick_mock = await self._start(cfg, source="geofence", person="person.amit")
            self.assertFalse(result["success"])
            self.assertEqual(result["pre_cool_result"], "blocked_geofence_home_location_required")
            tick_mock.assert_not_awaited()

        asyncio.run(run_case())

    def test_api_ha_persons_returns_only_person_entities(self):
        async def run_case():
            states = [
                {"entity_id": "person.amit", "attributes": {"friendly_name": "Amit"}},
                {"entity_id": "sensor.temp", "attributes": {"friendly_name": "Temp"}},
                {"entity_id": "person.mother", "attributes": {"friendly_name": "Mother"}},
                {"entity_id": "device_tracker.phone", "attributes": {"friendly_name": "Phone"}},
            ]
            with mock.patch.object(main.ha_client, "get_all_entities", new=mock.AsyncMock(return_value=states)):
                result = await main.list_ha_persons()
            self.assertEqual(
                result,
                [
                    {"entity_id": "person.amit", "name": "Amit"},
                    {"entity_id": "person.mother", "name": "Mother"},
                ],
            )

        asyncio.run(run_case())

    def test_api_ha_home_location_reads_ha_config(self):
        async def run_case():
            with mock.patch.object(
                main.ha_client,
                "get_ha_config",
                new=mock.AsyncMock(return_value={"latitude": 28.6139, "longitude": 77.2090}),
            ):
                result = await main.get_ha_home_location()
            self.assertEqual(result, {"latitude": 28.6139, "longitude": 77.209})

        asyncio.run(run_case())

    def test_ha_person_location_outside_addon_radius_does_not_trigger_pre_cool(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="auto_start",
                pre_cool_allowed_people=["person.amit"],
            )
            ix = ha_entity_events._entity_watch_index(cfg)
            with (
                mock.patch.object(ha_entity_events.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine, "start_pre_cool", new=mock.AsyncMock()) as start_mock,
            ):
                await ha_entity_events._handle_state_changed(
                    {
                        "entity_id": "person.amit",
                        "old_state": {
                            "state": "not_home",
                            "attributes": {"latitude": 28.6600, "longitude": 77.2090},
                        },
                        "new_state": {
                            "state": "not_home",
                            "attributes": {"latitude": 28.6500, "longitude": 77.2090},
                        },
                    },
                    ix,
                )

            start_mock.assert_not_awaited()

        asyncio.run(run_case())

    def test_ha_person_location_leaves_addon_radius_updates_active_pre_cool(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="auto_start",
                pre_cool_allowed_people=["person.amit"],
            )
            ix = ha_entity_events._entity_watch_index(cfg)
            with (
                mock.patch.object(ha_entity_events.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine, "start_pre_cool", new=mock.AsyncMock()) as start_mock,
            ):
                await ha_entity_events._handle_state_changed(
                    {
                        "entity_id": "person.amit",
                        "old_state": {
                            "state": "not_home",
                            "attributes": {"latitude": 28.6145, "longitude": 77.2090},
                        },
                        "new_state": {
                            "state": "not_home",
                            "attributes": {"latitude": 28.6500, "longitude": 77.2090},
                            "last_changed": "2026-06-03T07:00:00+00:00",
                        },
                    },
                    ix,
                )

            start_mock.assert_awaited_once_with(
                "precool-room",
                "geofence",
                "person.amit",
                visit_id="person.amit:2026-06-03T07:00:00+00:00",
                inside_geofence=False,
                approaching=False,
            )

        asyncio.run(run_case())

    def test_geofence_leave_without_active_precool_does_not_start(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="auto_start",
                pre_cool_allowed_people=["person.amit"],
            )
            result, tick_mock = await self._start(
                cfg,
                source="geofence",
                person="person.amit",
                inside_geofence=False,
                approaching=False,
            )
            self.assertEqual(result["pre_cool_result"], "skipped_outside_geofence")
            self.assertFalse(result["pre_cool_active"])
            tick_mock.assert_not_awaited()

        asyncio.run(run_case())

    def test_active_geofence_precool_cancels_when_triggering_person_leaves_radius(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="auto_start",
                pre_cool_allowed_people=["person.amit"],
                pre_cool_stop_if_user_leaves_geofence=True,
            )
            await self._start(cfg, source="geofence", person="person.amit", approaching=True)
            with (
                mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine, "tick", new=mock.AsyncMock()) as tick_mock,
            ):
                result = await logic_engine.start_pre_cool(
                    "precool-room",
                    "geofence",
                    "person.amit",
                    inside_geofence=False,
                    approaching=False,
                )

            self.assertEqual(result["pre_cool_result"], "geofence_left")
            self.assertFalse(result["pre_cool_active"])
            tick_mock.assert_awaited_once()

        asyncio.run(run_case())

    def test_active_geofence_precool_does_not_cancel_when_other_person_leaves_radius(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="auto_start",
                pre_cool_allowed_people=["person.amit", "person.other"],
                pre_cool_stop_if_user_leaves_geofence=True,
            )
            await self._start(cfg, source="geofence", person="person.amit", approaching=True)
            st = logic_engine._rt("precool-room")
            self.assertTrue(st.pre_cool_active)
            self.assertTrue(st.pre_cool_geofence_inside)
            with (
                mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine, "tick", new=mock.AsyncMock()) as tick_mock,
            ):
                result = await logic_engine.start_pre_cool(
                    "precool-room",
                    "geofence",
                    "person.other",
                    inside_geofence=False,
                    approaching=False,
                )

            self.assertEqual(result["pre_cool_result"], "skipped_non_triggering_person_left")
            self.assertTrue(result["pre_cool_active"])
            self.assertTrue(st.pre_cool_geofence_inside)
            tick_mock.assert_not_awaited()

        asyncio.run(run_case())

    def test_disable_geofence_endpoint_updates_room_settings_once(self):
        async def run_case():
            cfg = _cfg(pre_cool_geofence_enabled=True)
            saved = {}

            def save_config(payload):
                saved.update(payload)
                return True

            with (
                mock.patch.object(main.config_manager, "load_config", return_value=cfg),
                mock.patch.object(main.config_manager, "save_config", side_effect=save_config) as save_mock,
            ):
                result = await main.api_disable_geofence_pre_cool("precool-room")

            self.assertTrue(result["success"])
            self.assertFalse(result["pre_cool_geofence_enabled"])
            save_mock.assert_called_once()
            self.assertFalse(saved["rooms"][0]["settings"]["pre_cool_geofence_enabled"])

        asyncio.run(run_case())

    def test_room_update_saves_multiple_allowed_people_from_multiselect(self):
        async def run_case():
            cfg = _cfg()
            saved = {}

            def save_config(payload):
                saved.update(payload)
                return True

            with (
                mock.patch.object(main.config_manager, "load_config", return_value=cfg),
                mock.patch.object(main.config_manager, "save_config", side_effect=save_config),
                mock.patch.object(main.logic_engine, "trigger_tick"),
            ):
                await main.api_update_room(
                    "precool-room",
                    {
                        "settings": {
                            "pre_cool_allowed_people": ["person.amit", "person.mother"],
                        },
                    },
                )

            self.assertEqual(
                saved["rooms"][0]["settings"]["pre_cool_allowed_people"],
                ["person.amit", "person.mother"],
            )

        asyncio.run(run_case())

    def test_manual_button_works_without_geofence(self):
        async def run_case():
            result, tick_mock = await self._start(_cfg())
            self.assertTrue(result["pre_cool_active"])
            self.assertEqual(result["pre_cool_trigger_source"], "manual_button")
            tick_mock.assert_awaited_once()

        asyncio.run(run_case())

    def test_occupied_room_skips(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="auto_start",
                pre_cool_allowed_people=["person.amit"],
            )
            with (
                mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine.weather_api, "get_cached", new=mock.AsyncMock(return_value={})),
                mock.patch.object(logic_engine.ha_client, "get_state", new=mock.AsyncMock(side_effect=["on", "29"])),
                mock.patch.object(logic_engine, "tick", new=mock.AsyncMock()) as tick_mock,
            ):
                result = await logic_engine.start_pre_cool("precool-room", "geofence", "person.amit")
            self.assertEqual(result["pre_cool_result"], "skipped_already_occupied")
            self.assertFalse(result["pre_cool_active"])
            tick_mock.assert_not_awaited()

        asyncio.run(run_case())

    def test_already_cool_and_manual_override_block(self):
        async def run_case():
            cool, _tick = await self._start(_cfg(), source="manual_button")
            self.assertEqual(cool["pre_cool_result"], "started")

            logic_engine._runtime_by_room.clear()
            cfg_cool = _cfg()
            with (
                mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg_cool),
                mock.patch.object(logic_engine.weather_api, "get_cached", new=mock.AsyncMock(return_value={})),
                mock.patch.object(logic_engine.ha_client, "get_state", new=mock.AsyncMock(side_effect=["off", "25.5"])),
                mock.patch.object(logic_engine, "tick", new=mock.AsyncMock()),
            ):
                result_cool = await logic_engine.start_pre_cool("precool-room", "manual_button")
            self.assertEqual(result_cool["pre_cool_result"], "skipped_already_cool")

            cfg_override = _cfg(manual_override_enabled=True)
            result_override, tick_mock = await self._start(cfg_override, source="manual_button")
            self.assertEqual(result_override["pre_cool_result"], "blocked_by_manual_override")
            tick_mock.assert_not_awaited()

        asyncio.run(run_case())

    def test_active_pre_cool_prevents_duplicate_and_second_user_does_not_reset_timer(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="auto_start",
                pre_cool_allowed_people=["person.one", "person.two"],
            )
            started, _tick = await self._start(cfg, source="geofence", person="person.one")
            until = logic_engine._rt("precool-room").pre_cool_until
            with (
                mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine.ha_client, "get_state", new=mock.AsyncMock(side_effect=["off", "29"])),
                mock.patch.object(logic_engine, "tick", new=mock.AsyncMock()) as tick_mock,
            ):
                duplicate = await logic_engine.start_pre_cool("precool-room", "geofence", "person.two")
            self.assertEqual(started["pre_cool_result"], "started")
            self.assertEqual(duplicate["pre_cool_result"], "skipped_already_active")
            self.assertEqual(logic_engine._rt("precool-room").pre_cool_until, until)
            tick_mock.assert_not_awaited()

        asyncio.run(run_case())

    def test_cancel_suppresses_restart_and_snooze_blocks_until_expiry(self):
        async def run_case():
            cfg = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="auto_start",
                pre_cool_allowed_people=["person.amit"],
            )
            await self._start(cfg, source="geofence", person="person.amit", visit_id="visit-1")
            with mock.patch.object(logic_engine, "tick", new=mock.AsyncMock()):
                await logic_engine.cancel_pre_cool("precool-room")
            with (
                mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg),
                mock.patch.object(logic_engine.ha_client, "get_state", new=mock.AsyncMock(side_effect=["off", "29"])),
            ):
                blocked = await logic_engine.start_pre_cool("precool-room", "geofence", "person.amit", visit_id="visit-1")
            self.assertEqual(blocked["pre_cool_result"], "blocked_visit_suppressed")

            logic_engine._runtime_by_room.clear()
            with mock.patch.object(logic_engine, "tick", new=mock.AsyncMock()):
                await logic_engine.snooze_pre_cool("precool-room", minutes=60)
            cfg_snooze = _cfg(
                pre_cool_geofence_enabled=True,
                pre_cool_geofence_mode="auto_start",
                pre_cool_allowed_people=["person.amit"],
            )
            with mock.patch.object(logic_engine.config_manager, "load_config", return_value=cfg_snooze):
                snoozed = await logic_engine.start_pre_cool("precool-room", "geofence", "person.amit")
            self.assertEqual(snoozed["pre_cool_result"], "blocked_snoozed")

        asyncio.run(run_case())

    def test_room_presence_handoff_and_no_show_expiry(self):
        rid = "precool-room"
        cfg = _cfg()["rooms"][0]["settings"]
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.pre_cool_active = True
        st.pre_cool_until = now + timedelta(minutes=5)
        self.assertTrue(logic_engine._reconcile_pre_cool_state(rid, cfg, st, now, True, 29.0))
        self.assertEqual(st.pre_cool_result, "handoff_presence_detected")

        st.pre_cool_active = True
        st.pre_cool_until = now - timedelta(seconds=1)
        self.assertFalse(logic_engine._reconcile_pre_cool_state(rid, cfg, st, now, False, 29.0))
        self.assertEqual(st.pre_cool_result, "expired_no_show")

    def test_traffic_extension_and_max_total_expiry(self):
        rid = "precool-room"
        cfg = _cfg(
            pre_cool_allow_extension=True,
            pre_cool_extension_minutes=10,
            pre_cool_max_total_minutes=45,
        )["rooms"][0]["settings"]
        st = logic_engine._rt(rid)
        now = datetime.now(timezone.utc)
        st.pre_cool_active = True
        st.pre_cool_trigger_source = "geofence"
        st.pre_cool_requested_at = now - timedelta(minutes=25)
        st.pre_cool_until = now - timedelta(seconds=1)
        st.pre_cool_target = 25.0
        st.pre_cool_geofence_inside = True
        st.pre_cool_geofence_approaching = True
        self.assertFalse(logic_engine._reconcile_pre_cool_state(rid, cfg, st, now, False, 29.0))
        self.assertTrue(st.pre_cool_active)
        self.assertEqual(st.pre_cool_extension_count, 1)

        st.pre_cool_until = now - timedelta(seconds=1)
        st.pre_cool_requested_at = now - timedelta(minutes=46)
        self.assertFalse(logic_engine._reconcile_pre_cool_state(rid, cfg, st, now, False, 29.0))
        self.assertFalse(st.pre_cool_active)
        self.assertEqual(st.pre_cool_result, "expired_no_show")
