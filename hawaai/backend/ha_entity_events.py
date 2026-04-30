"""
Hybrid control: subscribe to selective HA state_changed events and fire logic_engine.tick
via logic_engine.trigger_tick — scheduler remains authoritative fallback.

Only presence + indoor temp + (delay_elapsed in logic_engine) — not every HA update.
"""

import asyncio
import json
import logging
import os
from collections import defaultdict
from typing import Any, DefaultDict, Dict, List, Optional, Tuple

import aiohttp

from . import config_manager, logic_engine, room_registry
from .utils import parse_presence

logger = logging.getLogger(__name__)

_HA_WS = "ws://supervisor/core/api/websocket"
_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


def _entity_watch_index(cfg: dict) -> DefaultDict[str, List[Tuple[str, str]]]:
    """entity_id → [(stored_room_id, canonical_room_id), ...]"""
    ix: DefaultDict[str, List[Tuple[str, str]]] = defaultdict(list)
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
            ix[pres].append((rid_store, canon))
        if itemp:
            ix[itemp].append((rid_store, canon))
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
    ix: DefaultDict[str, List[Tuple[str, str]]],
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
    for rid_store, canon in targets:
        if canon in seen:
            continue
        seen.add(canon)

        room_def = logic_engine.resolve_room_definition(cfg, rid_store)
        if not room_def or room_def.get("disabled"):
            continue
        merged = room_registry.merge_room_config(cfg, room_def)

        pres_e = (merged.get("presence_entity") or "").strip()
        tmp_e = (merged.get("indoor_temp_entity") or "").strip()

        if entity_id == pres_e and _presence_changed(old_s, new_s):
            logic_engine.trigger_tick(rid_store, reason="presence_change")
            continue

        if entity_id == tmp_e:
            await _maybe_trigger_temp_cross(rid_store, canon, merged, old_s, new_s)


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
