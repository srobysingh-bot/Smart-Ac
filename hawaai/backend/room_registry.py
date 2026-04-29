"""Multi-room registry: persisted `rooms` list in config + merge helpers.

Each room has its own climate entity and optional entity/AI overrides.
Global config (weather, API keys, intervals) is shared; control state is not.
"""

from __future__ import annotations

import copy
import logging
import uuid
from typing import Any, Dict, List, Optional

from . import temperature_schedule

logger = logging.getLogger(__name__)

DEFAULT_ROOM_ID = "default"

# Keys copied from room dict onto merged config when non-empty (string fields).
_ROOM_ENTITY_KEYS = (
    "presence_entity",
    "indoor_temp_entity",
    "indoor_humidity_entity",
    "energy_power_entity",
    "energy_kwh_entity",
)

# Optional AI-related keys allowed inside room["ai_config"].
_AI_OVERRIDE_KEYS = frozenset({
    "ai_enabled",
    "ai_provider",
    "ai_ollama_url",
    "ai_ollama_model",
    "ai_api_key",
    "ai_api_base_url",
    "ai_api_model",
    "ai_api_timeout",
    "ai_api_json_object_format",
})


def ensure_migrated(cfg: Dict[str, Any]) -> None:
    """If `rooms` is empty but legacy climate_entity is set, create one default room."""
    raw = cfg.get("rooms")
    if not isinstance(raw, list):
        cfg["rooms"] = []
        raw = cfg["rooms"]
    rooms: List[Dict[str, Any]] = raw
    if rooms:
        _normalize_room_list(rooms)
        return
    ce = (cfg.get("climate_entity") or cfg.get("ac_entity") or "").strip()
    if not ce:
        cfg["rooms"] = []
        return
    name = (cfg.get("room_name") or "Living Room").strip() or "Living Room"
    rooms.append(
        {
            "id": DEFAULT_ROOM_ID,
            "name": name,
            "climate_entity": ce,
        }
    )
    cfg["rooms"] = rooms
    logger.info("[HawaAI] Migrated single-room config → rooms[0] id=%s", DEFAULT_ROOM_ID)


def _normalize_room_list(rooms: List[Dict[str, Any]]) -> None:
    for r in rooms:
        if not isinstance(r, dict):
            continue
        if not (r.get("id") or "").strip():
            r["id"] = str(uuid.uuid4())[:12]
        r["id"] = str(r["id"]).strip()
        r["name"] = (str(r.get("name") or "Room")).strip() or "Room"
        r["climate_entity"] = (str(r.get("climate_entity") or "")).strip()


def list_room_dicts(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return normalized room dicts from config (may include entries without climate yet)."""
    raw = cfg.get("rooms")
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = [dict(r) for r in raw if isinstance(r, dict)]
    _normalize_room_list(out)
    return out


def get_room(cfg: Dict[str, Any], room_id: str) -> Optional[Dict[str, Any]]:
    rid = (room_id or "").strip()
    if not rid:
        return None
    for r in list_room_dicts(cfg):
        if r.get("id") == rid:
            return r
    return None


def merge_room_config(global_cfg: Dict[str, Any], room: Dict[str, Any]) -> Dict[str, Any]:
    """
    Per-room effective config: deep copy of global (excluding the `rooms` list) plus
    room climate/entity fields and explicit ai_config overrides.

    AI: only keys present in room["ai_config"] replace the corresponding global keys;
    there is no implicit merge of unspecified AI fields into a partial dict.
    """
    out: Dict[str, Any] = {
        k: copy.deepcopy(v) for k, v in global_cfg.items() if k != "rooms"
    }
    ce = (room.get("climate_entity") or "").strip()
    out["climate_entity"] = ce
    out["ac_entity"] = ce
    for k in _ROOM_ENTITY_KEYS:
        v = (str(room.get(k) or "")).strip()
        if v:
            out[k] = v
    rname = (room.get("name") or "").strip()
    if rname:
        out["room_name"] = rname
    ai = room.get("ai_config")
    if isinstance(ai, dict):
        for k, v in ai.items():
            if k in _AI_OVERRIDE_KEYS:
                out[k] = v
    sett = room.get("settings")
    if isinstance(sett, dict):
        for k, v in sett.items():
            if v is None:
                continue
            out[k] = copy.deepcopy(v) if isinstance(v, (dict, list)) else v
    temperature_schedule.ensure_temperature_schedule_defaults(out)
    return out


def public_room_view(room: Dict[str, Any]) -> Dict[str, Any]:
    """Strip secrets for API list/detail (ai_config may hold api key)."""
    r = dict(room)
    sett = r.get("settings")
    if isinstance(sett, dict):
        sett = dict(sett)
        if sett.get("weather_api_key"):
            sett["weather_api_key"] = "***" if str(sett.get("weather_api_key")).strip() else ""
        r["settings"] = sett
    ac = r.get("ai_config")
    if isinstance(ac, dict):
        ac = dict(ac)
        if ac.get("ai_api_key"):
            ac["ai_api_key"] = "***" if str(ac.get("ai_api_key")).strip() else ""
        r["ai_config"] = ac
    return r
