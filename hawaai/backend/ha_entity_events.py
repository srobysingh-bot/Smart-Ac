"""
Hybrid control: subscribe to selective HA state_changed events and fire logic_engine.tick
via logic_engine.trigger_tick — scheduler remains authoritative fallback.

Only presence + indoor temp + (delay_elapsed in logic_engine) — not every HA update.
"""

import asyncio
import json
import logging
import math
import os
from collections import defaultdict
from typing import Any, DefaultDict, Dict, List, Optional, Tuple

import aiohttp

from . import config_manager, logic_engine, room_registry
from .utils import parse_presence

logger = logging.getLogger(__name__)

_HA_WS = "ws://supervisor/core/api/websocket"
_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
WatchTarget = Tuple[str, str, str]
_missing_home_location_warned: set[str] = set()


def _entity_watch_index(cfg: dict) -> DefaultDict[str, List[WatchTarget]]:
    """entity_id -> [(stored_room_id, canonical_room_id, trigger_kind), ...]"""
    ix: DefaultDict[str, List[WatchTarget]] = defaultdict(list)
    for r in room_registry.list_room_dicts(cfg):
        if r.get("disabled"):
            continue
        rid_store = (r.get("id") or "").strip()
        if not rid_store:
            continue
        canon = logic_engine.normalize_room_id(rid_store)
        merged = room_registry.merge_room_config(cfg, r)
        pres = (merged.get("presence_entity") or "").strip()
        itemp = (merged.get("indoor_temp_entity") or "").strip()
        if pres:
            ix[pres].append((rid_store, canon, "presence"))
        if itemp:
            ix[itemp].append((rid_store, canon, "temp"))
        if bool(merged.get("pre_cool_geofence_enabled", False)):
            for person in logic_engine._pre_cool_allowed_people(merged):
                ix[person].append((rid_store, canon, "geofence_person"))
    return ix


def _presence_changed(old_so: Optional[dict], new_so: Optional[dict]) -> bool:
    if not old_so:
        return False
    olds = parse_presence((old_so or {}).get("state"))
    news = parse_presence((new_so or {}).get("state"))
    return olds != news


def _float_state(so: Optional[dict]) -> Optional[float]:
    if not so:
        return None
    raw = so.get("state")
    if raw is None or str(raw).lower() in ("unavailable", "unknown", "", "none"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _segment_crosses(prev: float, cur: float, line: float) -> bool:
    return (prev - line) * (cur - line) < 0


def _float_attr(so: Optional[dict], name: str) -> Optional[float]:
    attrs = (so or {}).get("attributes") or {}
    raw = attrs.get(name)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _state_location(so: Optional[dict]) -> Optional[Tuple[float, float]]:
    lat = _float_attr(so, "latitude")
    lon = _float_attr(so, "longitude")
    if lat is None or lon is None:
        return None
    if lat < -90.0 or lat > 90.0 or lon < -180.0 or lon > 180.0:
        return None
    return lat, lon


def _cfg_coord(cfg: dict, key: str, lo: float, hi: float) -> Optional[float]:
    raw = cfg.get(key)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < lo or value > hi:
        return None
    return value


def _home_location(cfg: dict) -> Optional[Tuple[float, float]]:
    lat = _cfg_coord(cfg, "pre_cool_home_latitude", -90.0, 90.0)
    lon = _cfg_coord(cfg, "pre_cool_home_longitude", -180.0, 180.0)
    if lat is None or lon is None:
        return None
    return lat, lon


def _radius_km(cfg: dict) -> float:
    try:
        radius = float(cfg.get("pre_cool_geofence_radius_km", 2.0))
    except (TypeError, ValueError):
        radius = 2.0
    if not math.isfinite(radius):
        radius = 2.0
    return max(0.5, min(radius, 10.0))


def _distance_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    )
    return 6371.0088 * 2.0 * math.asin(min(1.0, math.sqrt(h)))


def _distance_to_home_km(cfg: dict, so: Optional[dict]) -> Optional[float]:
    home = _home_location(cfg)
    current = _state_location(so)
    if home is None or current is None:
        return None
    return _distance_km(home, current)


def _inside_addon_radius(cfg: dict, so: Optional[dict]) -> Optional[bool]:
    distance = _distance_to_home_km(cfg, so)
    if distance is None:
        return None
    return distance <= _radius_km(cfg)


def _geofence_visit_id(person_entity: str, new_so: Optional[dict]) -> str:
    changed = ""
    if isinstance(new_so, dict):
        changed = str(new_so.get("last_updated") or new_so.get("last_changed") or "").strip()
    if not changed:
        changed = str(asyncio.get_running_loop().time())
    return f"{person_entity}:{changed}"


def _warn_missing_home_location_once(rid_store: str) -> None:
    if rid_store in _missing_home_location_warned:
        return
    _missing_home_location_warned.add(rid_store)
    logger.warning(
        "[PRECOOL] room=%s geofence_home_coordinates_missing "
        "pre_cool_home_latitude/pre_cool_home_longitude required",
        rid_store,
    )


async def _maybe_trigger_geofence_pre_cool(
    rid_store: str,
    merged_cfg: dict,
    person_entity: str,
    old_so: Optional[dict],
    new_so: Optional[dict],
) -> None:
    if not bool(merged_cfg.get("pre_cool_geofence_enabled", False)):
        return
    if _home_location(merged_cfg) is None:
        _warn_missing_home_location_once(rid_store)
        return
    old_distance = _distance_to_home_km(merged_cfg, old_so)
    new_distance = _distance_to_home_km(merged_cfg, new_so)
    old_inside = old_distance is not None and old_distance <= _radius_km(merged_cfg)
    new_inside = new_distance is not None and new_distance <= _radius_km(merged_cfg)
    if new_distance is None:
        return
    approaching = bool(
        old_distance is not None
        and new_distance < old_distance
    )

    if old_inside and not new_inside:
        await logic_engine.start_pre_cool(
            rid_store,
            "geofence",
            person_entity,
            visit_id=_geofence_visit_id(person_entity, new_so),
            inside_geofence=False,
            approaching=False,
        )
        return

    if old_inside or not new_inside:
        return

    await logic_engine.start_pre_cool(
        rid_store,
        "geofence",
        person_entity,
        visit_id=_geofence_visit_id(person_entity, new_so),
        inside_geofence=True,
        approaching=approaching,
    )


async def _maybe_trigger_temp_cross(
    rid_store: str,
    room_canon: str,
    merged_cfg: dict,
    old_so: Optional[dict],
    new_so: Optional[dict],
) -> None:
    new_t = _float_state(new_so)
    if new_t is None:
        return
    st = logic_engine._rt(room_canon)
    prev = st.last_event_probe_indoor_temp
    st.last_event_probe_indoor_temp = new_t
    if prev is None:
        return

    et = await logic_engine.effective_target_for_temp_cross(
        room_canon, merged_cfg, indoor_temp=new_t
    )
    if et is None:
        return
    try:
        on_d = float(merged_cfg.get("thermostat_on_delta_deg", 0.7))
        off_d = float(merged_cfg.get("thermostat_off_delta_deg", 0.3))
    except (TypeError, ValueError):
        on_d, off_d = 0.7, 0.3

    hi = et + on_d
    lo = et - off_d
    old_t = _float_state(old_so)
    o = old_t if old_t is not None else prev
    if _segment_crosses(o, new_t, hi) or _segment_crosses(o, new_t, lo):
        logic_engine.trigger_tick(rid_store, reason="temp_cross")


async def _handle_state_changed(
    data: Dict[str, Any],
    ix: DefaultDict[str, List[WatchTarget]],
) -> None:
    entity_id = (data.get("entity_id") or "").strip()
    if not entity_id:
        return
    targets = ix.get(entity_id)
    if not targets:
        return

    old_s = data.get("old_state") or {}
    new_s = data.get("new_state") or {}

    cfg = config_manager.load_config()

    seen: set[str] = set()
    for rid_store, canon, kind in targets:
        seen_key = f"{canon}:{kind}"
        if seen_key in seen:
            continue
        seen.add(seen_key)

        room_def = logic_engine.resolve_room_definition(cfg, rid_store)
        if not room_def or room_def.get("disabled"):
            continue
        merged = room_registry.merge_room_config(cfg, room_def)

        if kind == "presence" and _presence_changed(old_s, new_s):
            logic_engine.trigger_tick(rid_store, reason="presence_change")
            continue

        if kind == "temp":
            await _maybe_trigger_temp_cross(rid_store, canon, merged, old_s, new_s)
            continue

        if kind == "geofence_person":
            await _maybe_trigger_geofence_pre_cool(rid_store, merged, entity_id, old_s, new_s)


async def run_forever(reconnect_pause: float = 5.0) -> None:
    """
    Persistent HA Core WebSocket: auth + subscribe_events(state_changed).
    Maps entity updates to trigger_tick only for subscribed presence/indoor sensors.
    """
    if not _TOKEN:
        logger.warning("[HA_EVENTS] SUPERVISOR_TOKEN missing — hybrid triggers disabled")
        return

    sub_msg_id = 1
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    _HA_WS,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as ws:
                    hello = await ws.receive_json(timeout=15)
                    if hello.get("type") != "auth_required":
                        logger.error("[HA_EVENTS] expected auth_required — got %s", hello.get("type"))
                        await asyncio.sleep(reconnect_pause)
                        continue

                    await ws.send_json({"type": "auth", "access_token": _TOKEN})
                    auth_ok = await ws.receive_json(timeout=15)
                    if auth_ok.get("type") != "auth_ok":
                        logger.error("[HA_EVENTS] auth failed: %s", auth_ok)
                        await asyncio.sleep(reconnect_pause)
                        continue

                    cfg0 = config_manager.load_config()
                    ix = _entity_watch_index(cfg0)
                    if not ix:
                        logger.info("[HA_EVENTS] No presence/indoor entities configured — listener idle")

                    sid = sub_msg_id + 1
                    sub_msg_id = sid + 1
                    await ws.send_json(
                        {
                            "id": sid,
                            "type": "subscribe_events",
                            "event_type": "state_changed",
                        }
                    )
                    res = await ws.receive_json(timeout=15)
                    if not res.get("success"):
                        logger.error("[HA_EVENTS] subscribe_events failed: %s", res)
                        await asyncio.sleep(reconnect_pause)
                        continue

                    logger.info("[HA_EVENTS] state_changed subscription active (%d watched entities)", len(ix))

                    while True:
                        msg_raw = await ws.receive(timeout=None)
                        if msg_raw.type == aiohttp.WSMsgType.TEXT:
                            try:
                                msg = json.loads(msg_raw.data)
                            except json.JSONDecodeError:
                                continue
                            if msg.get("type") != "event":
                                continue
                            ev = msg.get("event") or {}
                            if ev.get("event_type") != "state_changed":
                                continue
                            evt_data = ev.get("data") or {}

                            cfg = config_manager.load_config()
                            ix = _entity_watch_index(cfg)

                            await _handle_state_changed(evt_data, ix)
                        elif msg_raw.type == aiohttp.WSMsgType.CLOSE:
                            break
                        elif msg_raw.type == aiohttp.WSMsgType.ERROR:
                            break

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[HA_EVENTS] websocket loop error — reconnect in %ss: %s", reconnect_pause, e)

        await asyncio.sleep(reconnect_pause)
