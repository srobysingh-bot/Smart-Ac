"""
HawaAI FastAPI application.

Routes:
  GET  /api/status          Live status + readings (?room_id= required)
  GET  /api/weather         Cached outdoor temperature (no room coupling)
  GET  /api/sessions        Paginated session history (?room_id= required)
  GET  /api/sessions/stats  Today + ML quality stats (?room_id= required)
  GET  /api/sessions/today  Today stats only (?room_id= required)
  GET  /api/snapshots       Recent monitoring snapshots (?room_id= required)
  GET  /api/config          Current add-on config
  POST /api/config          Save config to /data/hawaai_config.json
  GET  /api/ai              { ai_enabled, ai_provider, ollama + API fields (key masked) }
  POST /api/ai              Set AI settings (merge persist)
  GET  /api/ai/status       AI worker status (?room_id= required)
  GET  /api/ai/decisions    Recent AI model outputs (?room_id= required)
  GET  /api/entities        HA entity list for Settings dropdowns
  GET  /api/climate/{id}   Live climate entity state + attributes
  POST /api/climate/{id}/set_temperature
  POST /api/climate/{id}/set_hvac_mode
  POST /api/climate/{id}/set_fan_mode
  GET  /api/brands          AC brand+model library
  GET  /api/daily           Daily stats for last N days (?room_id= required)
  GET  /api/export/csv      Download session CSV (?room_id= required)
  GET  /api/export/json     Download session JSON (?room_id= required)
  GET  /api/export/ml_snapshots Clean ML snapshot rows (?room_id= required)
  WS   /ws                  Live push per room (5 s sweep + immediate after each logic tick)
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import aiohttp
from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response

from . import config_manager, database, ha_entity_events, live_broadcast, logic_engine, room_registry, scheduler, session_logger, weather_api
from .room_log_store import room_log_store
from . import ha_client
from .ac_controller import get_brands
from .ai import get_cached
from .ai.ai_worker import get_ai_status, init_ai_worker
from .temperature_schedule import resolve_base_target_temp
from .utils import parse_presence

# Room-scoped WebSocket subscribers: broadcast never crosses room_id boundaries.
_ws_by_room: Dict[str, List[WebSocket]] = defaultdict(list)
_ws_lock = asyncio.Lock()
_ws_log_token_by_room: Dict[str, str] = {}

_api_last_command: Dict[str, float] = defaultdict(float)
_climate_command_state: Dict[str, Dict[str, Any]] = {}
_climate_command_lock = asyncio.Lock()
_CLIMATE_COMMAND_DEBOUNCE_SECS = 1.2
_CLIMATE_DUPLICATE_WINDOW_SECS = 2.0


async def _enqueue_climate_command(
    *,
    room_id: Optional[str],
    entity_id: str,
    service: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Coalesce rapid UI climate commands so HA/Tuya receives only the latest payload.

    Calls are grouped by room/entity/service. Multiple requests that arrive during the
    debounce window all wait for one HA service call using the newest payload.
    """
    key_room = room_id or "_unscoped"
    key = f"{key_room}:{entity_id}:{service}"
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fingerprint = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    async with _climate_command_lock:
        st = _climate_command_state.setdefault(
            key,
            {
                "latest_payload": None,
                "latest_fingerprint": None,
                "updated_at": 0.0,
                "waiters": [],
                "task": None,
                "last_sent_at": 0.0,
                "last_sent_fingerprint": None,
            },
        )
        now_mono = time.monotonic()
        if (
            st.get("last_sent_fingerprint") == fingerprint
            and now_mono - float(st.get("last_sent_at") or 0.0) < _CLIMATE_DUPLICATE_WINDOW_SECS
        ):
            fut.set_result({"success": True, "deduped": True})
            return await fut

        st["latest_payload"] = dict(payload)
        st["latest_fingerprint"] = fingerprint
        st["updated_at"] = now_mono
        st["waiters"].append(fut)
        if st.get("task") is None or st["task"].done():
            st["task"] = asyncio.create_task(_drain_climate_command(key, room_id, entity_id, service))

    return await fut


async def _drain_climate_command(
    key: str,
    room_id: Optional[str],
    entity_id: str,
    service: str,
) -> None:
    while True:
        await asyncio.sleep(_CLIMATE_COMMAND_DEBOUNCE_SECS)
        async with _climate_command_lock:
            st = _climate_command_state.get(key)
            if not st:
                return
            elapsed = time.monotonic() - float(st.get("updated_at") or 0.0)
            if elapsed < _CLIMATE_COMMAND_DEBOUNCE_SECS:
                continue
            payload = dict(st.get("latest_payload") or {})
            fingerprint = st.get("latest_fingerprint")
            waiters = list(st.get("waiters") or [])
            st["waiters"] = []
            st["latest_payload"] = None
            st["latest_fingerprint"] = None
            st["task"] = None
            duplicate = (
                st.get("last_sent_fingerprint") == fingerprint
                and time.monotonic() - float(st.get("last_sent_at") or 0.0) < _CLIMATE_DUPLICATE_WINDOW_SECS
            )
            if duplicate:
                result = {"success": True, "deduped": True}
                for waiter in waiters:
                    if not waiter.done():
                        waiter.set_result(result)
                return

        try:
            ok = await ha_client.call_service("climate", service, payload)
            if ok and room_id:
                logic_engine.record_user_api_command(room_id)
                _api_last_command[room_id] = time.monotonic()
            result = {"success": ok, "queued": True}
        except Exception as exc:
            result = {"success": False, "error": str(exc)}

        async with _climate_command_lock:
            st = _climate_command_state.get(key)
            if st is not None:
                st["last_sent_at"] = time.monotonic()
                st["last_sent_fingerprint"] = fingerprint

        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(result)
        return


def _room_id_for_climate_entity(entity_id: str) -> Optional[str]:
    """Resolve configured room whose climate entity matches HA entity id."""
    eid = (entity_id or "").strip()
    if not eid:
        return None
    base = config_manager.load_config()
    for r in room_registry.list_room_dicts(base):
        eff = room_registry.merge_room_config(base, r)
        ce = (eff.get("climate_entity") or eff.get("ac_entity") or "").strip()
        if ce == eid:
            rid = (r.get("id") or "").strip()
            return rid if rid else None
    return None


def _state_ok(raw: Optional[str]) -> bool:
    if raw is None:
        return False
    s = str(raw).strip().lower()
    return s not in ("unavailable", "unknown", "")


def _sensor_health(entity_id: str, raw: Optional[str]) -> Optional[bool]:
    if not (entity_id or "").strip():
        return None
    return _state_ok(raw)


def _mask_api_key_response(stored: str) -> str:
    """Never expose raw API keys to clients; non-empty keys surface as a single placeholder."""
    return "***" if (stored or "").strip() else ""


def _require_room_query(room_id_raw: str) -> str:
    """Whitespace-stripped room_id — blank after strip → HTTP 400 (no silent default room)."""
    rid = (room_id_raw or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="room_id is required")
    return rid


def _resolve_stored_room_id(cfg: dict, room_id_raw: str) -> Optional[str]:
    """Return persisted `rooms[].id` for this path/query (case-insensitive match), or None."""
    rq = (room_id_raw or "").strip()
    if not rq:
        return None
    nq = logic_engine.normalize_room_id(rq)
    for r in room_registry.list_room_dicts(cfg):
        rid = str(r.get("id") or "").strip()
        if rid and (rid == rq or logic_engine.normalize_room_id(rid) == nq):
            return rid
    return None


async def _disconnect_room_websockets(rid_canon: str) -> None:
    """Close subscriber sockets when a room is removed from configuration."""
    async with _ws_lock:
        bucket = _ws_by_room.pop(rid_canon, None)
    if not bucket:
        return
    for ws in list(bucket):
        try:
            await ws.close(code=4404)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.backup_db("startup")
    await database.init_db()
    cfg = config_manager.load_config()
    room_log_store.set_max_lines_per_room(int(cfg.get("log_buffer_size", 300)))
    ac_ent = (cfg.get("ac_entity") or cfg.get("climate_entity") or "").strip() or "(not set)"
    smart_on = logic_engine.smart_temp_adjustment_enabled(cfg)
    logger.info(
        "[HawaAI] Startup configuration: AC entity=%s | Control=climate_adapter | Target=%s°C | Smart=%s",
        ac_ent,
        cfg.get("target_temp", 24),
        "enabled" if smart_on else "disabled",
    )
    if bool(cfg.get("ai_enabled", False)):
        ap = (str(cfg.get("ai_provider") or "ollama")).strip().lower()
        prov_label = "API" if ap == "api" else "Ollama"
        logger.info("[AI] Enabled (provider=%s)", prov_label)
    else:
        logger.info("[AI] Disabled (default)")
    try:
        init_ai_worker()
    except Exception:
        logger.exception("[AI] AI worker startup hook failed — continuing without AI bootstrap")
    live_broadcast.register_room_broadcast(_broadcast_to_room_subscribers)
    asyncio.create_task(scheduler.start())
    asyncio.create_task(ha_entity_events.run_forever())
    asyncio.create_task(_broadcast_loop())
    logger.info("[HawaAI] Add-on started")
    yield
    database.backup_db("shutdown")
    logger.info("[HawaAI] Add-on stopped")


app = FastAPI(title="HawaAI API", version="1.4.58", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# ── CONFIG ────────────────────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    """Frontend calls this on Settings page load to pre-fill all fields."""
    cfg = config_manager.load_config()
    # Mask secrets in response
    masked = dict(cfg)
    if masked.get("weather_api_key"):
        masked["weather_api_key"] = "***"
    mk = (masked.get("ai_api_key") or "").strip()
    masked["ai_api_key"] = _mask_api_key_response(mk)
    return masked


@app.post("/api/config")
async def save_config(data: Dict[str, Any] = Body(...)):
    """Frontend POSTs full config on Save. Persists to /data/hawaai_config.json."""
    # Don't overwrite secrets with masked placeholder
    for secret_key in ("weather_api_key", "ai_api_key"):
        if data.get(secret_key) == "***" or data.get(secret_key) == "":
            data.pop(secret_key, None)

    ok = config_manager.save_config(data)
    if ok:
        logger.info("[HawaAI] Config updated: %s", list(data.keys()))
        return {"success": True, "message": "Settings saved — logic engine will use new values on next tick."}
    return {"success": False, "message": "Failed to save config"}, 500


@app.post("/api/config/reload")
async def reload_config():
    config_manager.load_config()
    return {"ok": True}


@app.get("/api/ai")
async def get_ai_flag():
    """Expose AI layer flags; API key is never returned raw (see /api/config)."""
    cfg = config_manager.load_config()
    prov = (str(cfg.get("ai_provider") or "ollama")).strip().lower()
    prov_norm = "api" if prov == "api" else "ollama"
    try:
        tout = int(cfg.get("ai_api_timeout", 60))
    except (TypeError, ValueError):
        tout = 60
    return {
        "ai_enabled": bool(cfg.get("ai_enabled", False)),
        "ai_provider": prov_norm,
        "ai_ollama_url": (str(cfg.get("ai_ollama_url") or "")).strip(),
        "ai_ollama_model": (str(cfg.get("ai_ollama_model") or "")).strip(),
        "ai_api_base_url": (str(cfg.get("ai_api_base_url") or "")).strip(),
        "ai_api_model": (str(cfg.get("ai_api_model") or "")).strip(),
        "ai_api_timeout": tout,
        "ai_api_json_object_format": bool(cfg.get("ai_api_json_object_format", False)),
        "ai_api_key_set": bool((str(cfg.get("ai_api_key") or "")).strip()),
        "default_ollama_model": config_manager.DEFAULT_OLLAMA_MODEL,
    }


@app.post("/api/ai")
async def set_ai_flag(data: Dict[str, Any] = Body(...)):
    """Update AI-related settings (merges into /data/hawaai_config.json). At least one field required."""
    patch: Dict[str, Any] = {}
    if "ai_enabled" in data:
        patch["ai_enabled"] = bool(data["ai_enabled"])
    if "ai_provider" in data and data.get("ai_provider") is not None:
        p = str(data["ai_provider"] or "ollama").strip().lower()
        patch["ai_provider"] = "api" if p == "api" else "ollama"
    if "ai_ollama_url" in data and data.get("ai_ollama_url") is not None:
        patch["ai_ollama_url"] = str(data["ai_ollama_url"] or "").strip()
    if "ai_ollama_model" in data and data.get("ai_ollama_model") is not None:
        patch["ai_ollama_model"] = str(data["ai_ollama_model"] or "").strip()
    if "ai_api_base_url" in data and data.get("ai_api_base_url") is not None:
        patch["ai_api_base_url"] = str(data["ai_api_base_url"] or "").strip().rstrip("/")
    if "ai_api_model" in data and data.get("ai_api_model") is not None:
        patch["ai_api_model"] = str(data["ai_api_model"] or "").strip()
    if "ai_api_timeout" in data and data.get("ai_api_timeout") is not None:
        try:
            patch["ai_api_timeout"] = int(data["ai_api_timeout"])
        except (TypeError, ValueError):
            patch["ai_api_timeout"] = 60
    if "ai_api_key" in data and data.get("ai_api_key") is not None:
        k = str(data["ai_api_key"] or "").strip()
        if k and k != "***":
            patch["ai_api_key"] = k
    if "ai_api_json_object_format" in data and data.get("ai_api_json_object_format") is not None:
        patch["ai_api_json_object_format"] = bool(data["ai_api_json_object_format"])
    if not patch:
        raise HTTPException(
            status_code=400,
            detail="At least one AI field required (e.g. ai_enabled, ai_provider, ai_ollama_url)",
        )
    ok = config_manager.save_config(patch)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save config")
    out = config_manager.load_config()
    prov = (str(out.get("ai_provider") or "ollama")).strip().lower()
    prov_norm = "api" if prov == "api" else "ollama"
    try:
        tout_o = int(out.get("ai_api_timeout", 60))
    except (TypeError, ValueError):
        tout_o = 60
    return {
        "ai_enabled": bool(out.get("ai_enabled", False)),
        "ai_provider": prov_norm,
        "ai_ollama_url": (str(out.get("ai_ollama_url") or "")).strip(),
        "ai_ollama_model": (str(out.get("ai_ollama_model") or "")).strip(),
        "ai_api_base_url": (str(out.get("ai_api_base_url") or "")).strip(),
        "ai_api_model": (str(out.get("ai_api_model") or "")).strip(),
        "ai_api_timeout": tout_o,
        "ai_api_json_object_format": bool(out.get("ai_api_json_object_format", False)),
        "ai_api_key_set": bool((str(out.get("ai_api_key") or "")).strip()),
        "default_ollama_model": config_manager.DEFAULT_OLLAMA_MODEL,
    }


@app.get("/api/ai/status")
async def get_ai_runtime_status(room_id: str = Query(..., min_length=1)):
    """Last AI inference attempt for an explicit room_id."""
    return get_ai_status(_require_room_query(room_id))


@app.get("/api/ai/decisions")
async def get_ai_decisions(
    room_id: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
):
    """Persisted AI outputs for ML / audit (scoped per room_id)."""
    rid = _require_room_query(room_id)
    return {"decisions": await database.get_ai_decisions_recent(rid, limit)}


@app.get("/api/weather")
async def get_weather_cached():
    """
    Outdoor temperature/humidity from the weather cache.
    Does not require room_id — use for Settings preview and other non-room UI.
    """
    weather = await weather_api.get_cached()
    if not weather:
        return {"outdoor_temp": None, "outdoor_humidity": None, "available": False}
    return {
        "outdoor_temp": weather.get("temp"),
        "outdoor_humidity": weather.get("humidity"),
        "available": True,
    }


# ── LIVE STATUS ───────────────────────────────────────────────────────────────

def _runtime_block(runtime: Dict[str, Any]) -> Dict[str, Any]:
    """Session runtime for UI timer (minutes + formatted)."""
    now = datetime.now(timezone.utc)
    start_iso = runtime.get("session_start_time")
    sid = runtime.get("session_id")
    active = bool(sid and start_iso)
    minutes = 0
    if active and start_iso:
        try:
            iso = start_iso.replace("Z", "+00:00")
            st = datetime.fromisoformat(iso)
            minutes = max(0, int((now - st).total_seconds() // 60))
        except (TypeError, ValueError):
            minutes = 0
    return {
        "active":      active,
        "minutes":     minutes,
        "formatted":   f"{minutes} min" if active else "—",
        "session_start": start_iso,
    }


def _smart_adjustment_reason(
    enabled: bool,
    outdoor: Optional[float],
    base_t: float,
    eff_t: float,
) -> str:
    if not enabled:
        return "Smart target adjustment is off in Settings."
    if outdoor is None:
        return "Waiting for outdoor temperature from the weather API."
    if eff_t > base_t:
        if outdoor < 30:
            return "Cooler outside — relaxed setpoint (+1 °C)."
        if outdoor < 35:
            return "Warm outside — slight relaxation (+0.5 °C)."
        return "Outdoor conditions raised the effective target."
    if eff_t < base_t:
        return "Very hot outside — stronger cooling (−1 °C)."
    return "Effective target matches config (outdoor in 30–40 °C band)."


@app.get("/api/status")
async def get_status(room_id: str = Query(..., min_length=1)):
    """Dashboard status for one room. `room_id` is required — no default room fallback."""
    rid = _require_room_query(room_id)
    logger.info("[ROOM] /api/status room_id=%s", rid)
    try:
        return await _dashboard_status_payload(rid)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[ROOM] /api/status failed room_id=%s: %s", rid, exc)
        raise HTTPException(status_code=500, detail="failed to build status") from exc


async def _dashboard_status_payload(rid: str) -> Dict[str, Any]:
    """Build /api/status JSON. `rid` must be non-empty; unknown room raises 404."""
    base = config_manager.load_config()
    room_def = logic_engine.resolve_room_definition(base, rid)
    if not room_def:
        raise HTTPException(status_code=404, detail=f"Unknown room: {rid}")
    cfg = room_registry.merge_room_config(base, room_def)

    indoor_temp_raw  = await ha_client.get_state(cfg.get("indoor_temp_entity", ""))
    presence_raw     = await ha_client.get_state(cfg.get("presence_entity", ""))
    energy_power_raw = await ha_client.get_state(cfg.get("energy_power_entity", ""))
    energy_kwh_raw   = await ha_client.get_state(cfg.get("energy_kwh_entity", ""))

    is_occupied = parse_presence(presence_raw)
    weather     = await weather_api.get_cached()

    def safe_float(val):
        try:
            return float(val) if val not in (None, "unavailable", "unknown") else None
        except (ValueError, TypeError):
            return None

    energy_watts = safe_float(energy_power_raw)
    energy_kwh   = safe_float(energy_kwh_raw)

    runtime = logic_engine.get_runtime_state(rid)
    ac_state = str(runtime.get("ac_state") or "off")
    physical_ac_on = bool(runtime.get("physical_ac_on", runtime.get("ac_is_on", False)))
    effective_ac_on = bool(runtime.get("effective_ac_on", False))
    ac_on_compat = physical_ac_on
    ac_idle = bool(runtime.get("ac_idle", False))
    power_source = str(runtime.get("power_source", "internal"))
    cooldown_active = bool(runtime.get("cooldown_active", False))

    # Aerostate — live climate read for UI labels (ON/OFF from logic runtime)
    climate_entity = (cfg.get("ac_entity") or cfg.get("climate_entity") or "").strip()
    climate_data: dict = {}
    if climate_entity:
        climate_data = await ha_client.get_climate_state(climate_entity)

    # Indoor temp: prefer dedicated sensor; fall back to climate entity thermistor
    indoor_temp = safe_float(indoor_temp_raw)
    if indoor_temp is None and climate_data:
        indoor_temp = climate_data.get("current_temp")

    # Effective target — same pipeline as logic_engine.tick (manual / schedule × weather ± optional AI clamp)
    base_target, schedule_slot = resolve_base_target_temp(cfg)
    smart_curve = logic_engine.smart_temp_adjustment_enabled(cfg) and bool(cfg.get("use_outdoor_temp", True))
    outdoor_temp_val = weather.get("temp") if weather else None
    effective_after_weather = logic_engine.compute_effective_target(
        base_target, outdoor_temp_val, smart_curve,
    )
    tm = str(cfg.get("temperature_mode") or "manual")
    try:
        effective_target = float(runtime.get("target_temp"))
    except (TypeError, ValueError):
        effective_target = effective_after_weather
    try:
        sleep_offset = float(runtime.get("sleep_offset") or 0.0)
    except (TypeError, ValueError):
        sleep_offset = 0.0
    try:
        humidity_offset = float(runtime.get("humidity_offset") or 0.0)
    except (TypeError, ValueError):
        humidity_offset = 0.0
    effective_without_comfort_layers = float(effective_target) - sleep_offset - humidity_offset
    ai_adjust_applied = (
        tm == "schedule_ai"
        and bool(cfg.get("ai_enabled", False))
        and abs(float(effective_without_comfort_layers) - float(effective_after_weather)) >= 0.01
    )

    rt = _runtime_block(runtime)
    reason = _smart_adjustment_reason(
        smart_curve, outdoor_temp_val, base_target, effective_after_weather,
    )

    ai_snap = get_ai_status(rid)
    humidity_entity = (
        str(cfg.get("humidity_entity_id") or "").strip()
        or str(cfg.get("indoor_humidity_entity") or "").strip()
    )
    health = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "climate": {
            "entity_id": climate_entity,
            "available": bool(climate_entity) and _state_ok(climate_data.get("state") if climate_data else None),
            "state": climate_data.get("state") if climate_data else None,
            "last_changed": climate_data.get("last_changed"),
            "last_updated": climate_data.get("last_updated"),
        },
        "sensors": {
            "indoor_temp": _sensor_health(cfg.get("indoor_temp_entity", ""), indoor_temp_raw),
            "presence": _sensor_health(cfg.get("presence_entity", ""), presence_raw),
            "energy_power": _sensor_health(cfg.get("energy_power_entity", ""), energy_power_raw),
            "energy_kwh": _sensor_health(cfg.get("energy_kwh_entity", ""), energy_kwh_raw),
            "humidity": (
                None if not humidity_entity else runtime.get("humidity_percent") is not None
            ),
        },
        "ai": {
            "status": ai_snap.get("status"),
            "last_call": ai_snap.get("last_call"),
            "last_error": ai_snap.get("last_error"),
            "circuit_open": ai_snap.get("circuit_open"),
        },
    }

    return {
        # ── Optional AI layer ──────────────────────────────────────────────────
        "ai_enabled":       bool(cfg.get("ai_enabled", False)),
        "ai_cached":        bool(runtime.get("ai_cached")),
        # ── Core state ────────────────────────────────────────────────────────
        "ac_on":             ac_on_compat,
        "physical_ac_on":    physical_ac_on,
        "ac_state":          ac_state,
        "effective_ac_on":   effective_ac_on,
        "ac_state_source":  runtime.get("ac_state_source", "system"),
        "ac_idle":          ac_idle,       # fan running, compressor off (50–500 W)
        "power_source":     power_source,  # "watts" | "watts_idle" | "cooldown" | "internal"
        "indoor_temp":      indoor_temp,
        "outdoor_temp":     outdoor_temp_val,
        "outdoor_humidity": weather.get("humidity") if weather else None,
        "presence":         is_occupied,
        # ── Energy ────────────────────────────────────────────────────────────
        "watt_draw":        energy_watts or 0.0,
        "energy_watts":     energy_watts,
        "energy_kwh_total": energy_kwh,
        # ── Session ───────────────────────────────────────────────────────────
        "session_kwh":      runtime.get("session_start_kwh"),
        "session_id":       runtime.get("session_id"),
        "session_start":    runtime.get("session_start_time"),
        "runtime":          rt,
        "zone_status": {
            "phase": runtime.get("zone_ui_phase") or "inactive",
            "dwell_target_seconds": runtime.get("zone_dwell_seconds"),
            "dwell_elapsed_seconds": runtime.get("zone_dwell_elapsed_seconds"),
            "dwell_remaining_seconds": runtime.get("zone_dwell_remaining_seconds"),
        },
        # ── Engine diagnostics ────────────────────────────────────────────────
        "cooldown_active":  cooldown_active,
        "last_command":     runtime.get("last_command"),
        "secs_since_cmd":   runtime.get("secs_since_cmd"),
        "last_ac_on_at":    runtime.get("last_ac_on_at"),
        "last_ac_off_at":   runtime.get("last_ac_off_at"),
        "last_power_confirmed_on":  runtime.get("last_power_confirmed_on"),
        "last_power_confirmed_off": runtime.get("last_power_confirmed_off"),
        "control_source":           runtime.get("control_source", "none"),
        "last_command_source":      runtime.get("last_command_source", "system"),
        "on_delay_seconds":         runtime.get("on_delay_seconds", 0),
        "off_delay_seconds":        runtime.get("off_delay_seconds", 0),
        "pending_action":           runtime.get("pending_action"),
        "pending_since_ts":         runtime.get("pending_since_ts"),
        "pending_remaining_seconds": runtime.get("pending_remaining_seconds"),
        # ── Config ────────────────────────────────────────────────────────────
        "manual_override":  cfg.get("manual_override", False),
        "control_mode": logic_engine.normalize_control_mode(cfg),
        "ir_backend": logic_engine.normalize_ir_backend(cfg),
        "config_complete":  bool(
            cfg.get("presence_entity")
            and (
                cfg.get("indoor_temp_entity")
                or logic_engine.normalize_control_mode(cfg) == "presence_only"
            )
        ),
        "target_temp": base_target,
        "schedule_base_temp": base_target,
        "effective_target": effective_target,
        "effective_mode": str(cfg.get("effective_mode") or "auto"),
        "manual_effective_temp": cfg.get("manual_effective_temp"),
        "effective_max_delta_deg": logic_engine.effective_max_delta_deg(cfg),
        "effective_after_weather": effective_after_weather,
        "sleep_offset": runtime.get("sleep_offset", 0.0),
        "sleep_phase": runtime.get("sleep_phase", "inactive"),
        "sleep_optimization_active": runtime.get("sleep_optimization_active", False),
        "sleep_suspended_reason": runtime.get("sleep_suspended_reason"),
        "humidity_percent": runtime.get("humidity_percent"),
        "feels_like_temp": runtime.get("feels_like_temp"),
        "dew_point": runtime.get("dew_point"),
        "humidity_offset": runtime.get("humidity_offset", 0.0),
        "comfort_score": runtime.get("comfort_score", 0.0),
        "comfort_level": runtime.get("comfort_level", "unknown"),
        "humidity_band": runtime.get("humidity_band", "unavailable"),
        "dry_mode_recommended": runtime.get("dry_mode_recommended", False),
        "temperature_mode": tm,
        "schedule_slot": schedule_slot,
        "ai_adjust_applied": ai_adjust_applied,
        "climate_entity":   climate_entity,
        "ac_entity":        climate_entity,
        "smart_adjustment":           smart_curve,
        "smart_adjustment_reason":    reason,
        # ── Aerostate — live state read back from the climate entity ───────────
        # This is the single UI truth for what the AC is currently doing.
        "aerostate": {
            "entity_id":    climate_entity,
            "mode":         climate_data.get("mode"),         # hvac_mode
            "current_temp": climate_data.get("current_temp"), # measured room temp
            "target_temp":  climate_data.get("target_temp"),  # setpoint
            "fan_mode":     climate_data.get("fan_mode"),
            "swing_mode":   climate_data.get("swing_mode"),
            "is_on":        climate_data.get("is_on", False),
        },
        # Flattened aliases kept for backward compat with existing frontend components
        "ac_current_temp":  climate_data.get("current_temp"),
        "ac_target_temp":   climate_data.get("target_temp"),
        "ac_mode":          climate_data.get("mode"),
        "ac_fan_mode":      climate_data.get("fan_mode"),
        "ac_swing_mode":    climate_data.get("swing_mode"),
        # ── Smart cooling (read-only, NEVER changes AC ON/OFF) ─────────────────
        "smart_cooling_enabled": cfg.get("smart_cooling_enabled", False),
        "smart_temp_adjustment": cfg.get(
            "smart_temp_adjustment",
            logic_engine.smart_temp_adjustment_enabled(cfg),
        ),
        "smart_mode":            runtime.get("smart_mode"),
        "smart_fan_mode":        runtime.get("smart_fan_mode"),
        "smart_delta": (
            round(indoor_temp - effective_target, 2)
            if indoor_temp is not None else None
        ),
        "last_applied_target":   runtime.get("last_applied_target"),
        "room_id":               rid,
        "room_name":             room_def.get("name"),
        "health":                health,
    }


@app.get("/api/rooms")
async def api_list_rooms():
    cfg = config_manager.load_config()
    return {
        "rooms": [room_registry.public_room_view(r) for r in room_registry.list_room_dicts(cfg)],
    }


def _mask_effective_room_settings(eff: Dict[str, Any]) -> Dict[str, Any]:
    """Mask secrets in merged effective config for GET /api/rooms/{id}."""
    masked = dict(eff)
    if (masked.get("weather_api_key") or "").strip():
        masked["weather_api_key"] = "***"
    mk = (masked.get("ai_api_key") or "").strip()
    masked["ai_api_key"] = _mask_api_key_response(mk)
    return masked


@app.get("/api/rooms/{room_id}")
async def api_get_room(room_id: str):
    """Room row + merged effective config (same shape as legacy global /config for the form)."""
    rid = _require_room_query(room_id)
    base = config_manager.load_config()
    room_def = logic_engine.resolve_room_definition(base, rid)
    if not room_def:
        raise HTTPException(status_code=404, detail="room not found")
    eff = room_registry.merge_room_config(base, room_def)
    return {
        "room": room_registry.public_room_view(room_def),
        "effective": _mask_effective_room_settings(eff),
    }


@app.post("/api/rooms")
async def api_create_room(body: Dict[str, Any] = Body(...)):
    base = config_manager.load_config()
    rooms = [dict(r) for r in room_registry.list_room_dicts(base)]
    rid = (str(body.get("id") or "")).strip() or room_registry._new_room_id()
    if rid.lower() == "default":
        raise HTTPException(status_code=400, detail="room id 'default' is reserved — choose another id")
    if any(r.get("id") == rid for r in rooms):
        raise HTTPException(status_code=400, detail="room id already exists")
    name = (str(body.get("name") or "Room")).strip() or "Room"
    climate_entity = (str(body.get("climate_entity") or "")).strip()
    if not climate_entity:
        raise HTTPException(status_code=400, detail="climate_entity is required")
    row: Dict[str, Any] = {"id": rid, "name": name, "climate_entity": climate_entity}
    for k in (
        "presence_entity",
        "indoor_temp_entity",
        "indoor_humidity_entity",
        "humidity_entity_id",
        "energy_power_entity",
        "energy_kwh_entity",
    ):
        v = body.get(k)
        if v and str(v).strip():
            row[k] = str(v).strip()
    if isinstance(body.get("ai_config"), dict):
        row["ai_config"] = body["ai_config"]
    rooms.append(row)
    if not config_manager.save_config({"rooms": rooms}):
        raise HTTPException(status_code=500, detail="failed to save rooms")
    return room_registry.public_room_view(row)


def _sanitize_zone_room_settings(incoming_settings: Dict[str, Any]) -> None:
    """Clamp ``zone_dwell_seconds`` server-side (never rely on UI-only validation)."""
    if not isinstance(incoming_settings, dict):
        return
    if "zone_dwell_seconds" not in incoming_settings:
        return
    raw = incoming_settings.get("zone_dwell_seconds")
    if raw is None:
        return
    try:
        z = int(round(float(raw)))
    except (TypeError, ValueError):
        incoming_settings.pop("zone_dwell_seconds", None)
        return
    incoming_settings["zone_dwell_seconds"] = max(0, min(z, 3600))


def _sanitize_control_mode_room_settings(incoming_settings: Dict[str, Any]) -> None:
    if not isinstance(incoming_settings, dict):
        return
    if "control_mode" in incoming_settings:
        mode = str(incoming_settings.get("control_mode") or "thermostat").strip().lower()
        incoming_settings["control_mode"] = (
            mode if mode in ("thermostat", "presence_only") else "thermostat"
        )
    if "ir_backend" in incoming_settings:
        backend = str(incoming_settings.get("ir_backend") or "aerostate").strip().lower()
        incoming_settings["ir_backend"] = (
            backend if backend in ("aerostate", "tuya") else "aerostate"
        )
    for key, default, lo, hi in (
        ("presence_only_on_dwell_seconds", 20.0, 0.0, 3600.0),
        ("presence_only_max_runtime_minutes", 240.0, 1.0, 1440.0),
    ):
        if key not in incoming_settings or incoming_settings[key] is None:
            continue
        try:
            incoming_settings[key] = max(lo, min(float(incoming_settings[key]), hi))
        except (TypeError, ValueError):
            incoming_settings[key] = default


def _sanitize_effective_target_room_settings(
    base_cfg: Dict[str, Any],
    room_row: Dict[str, Any],
    incoming_settings: Dict[str, Any],
) -> None:
    """
    Clamp effective_mode / manual_effective_temp / effective_max_delta_deg on save.

    Uses schedule-resolved base + merged settings preview (same model as runtime tick).
    Mutates ``incoming_settings`` for those keys.
    """
    touch_keys = frozenset(
        {"effective_mode", "manual_effective_temp", "effective_max_delta_deg"}
    )
    if not isinstance(incoming_settings, dict):
        return
    if not any(k in incoming_settings for k in touch_keys):
        return

    if "effective_mode" in incoming_settings and incoming_settings["effective_mode"] is not None:
        em = str(incoming_settings["effective_mode"]).strip().lower()
        incoming_settings["effective_mode"] = em if em in ("auto", "manual") else "auto"

    if "effective_max_delta_deg" in incoming_settings and incoming_settings["effective_max_delta_deg"] is not None:
        try:
            raw_md = float(incoming_settings["effective_max_delta_deg"])
            incoming_settings["effective_max_delta_deg"] = logic_engine.effective_max_delta_deg(
                {"effective_max_delta_deg": raw_md}
            )
        except (TypeError, ValueError):
            incoming_settings.pop("effective_max_delta_deg", None)

    orig_s = dict(room_row.get("settings") or {})
    proposed = dict(orig_s)
    for kk, vv in incoming_settings.items():
        if vv is None:
            proposed.pop(kk, None)
        else:
            proposed[kk] = vv
    draft = dict(room_row)
    draft["settings"] = proposed
    try:
        merged_eff = room_registry.merge_room_config(base_cfg, draft)
        base_t, _ = resolve_base_target_temp(merged_eff)
        max_d = logic_engine.effective_max_delta_deg(merged_eff)
    except Exception:
        return

    if "manual_effective_temp" in incoming_settings and incoming_settings["manual_effective_temp"] is not None:
        try:
            mv = float(incoming_settings["manual_effective_temp"])
            incoming_settings["manual_effective_temp"] = max(
                float(base_t), min(mv, float(base_t) + float(max_d))
            )
        except (TypeError, ValueError):
            incoming_settings.pop("manual_effective_temp", None)


@app.put("/api/rooms/{room_id}")
async def api_update_room(room_id: str, body: Dict[str, Any] = Body(...)):
    rid = _require_room_query(room_id)
    base = config_manager.load_config()
    rooms = [dict(r) for r in room_registry.list_room_dicts(base)]
    idx = next((i for i, re in enumerate(rooms) if re.get("id") == rid), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="room not found")
    r = rooms[idx]
    if "name" in body and body["name"] is not None:
        r["name"] = str(body["name"]).strip() or r.get("name", "Room")
    if "climate_entity" in body and body["climate_entity"] is not None:
        ce = str(body["climate_entity"]).strip()
        if ce:
            r["climate_entity"] = ce
    for k in (
        "presence_entity",
        "indoor_temp_entity",
        "indoor_humidity_entity",
        "humidity_entity_id",
        "energy_power_entity",
        "energy_kwh_entity",
    ):
        if k in body:
            v = body[k]
            if v is None or str(v).strip() == "":
                r.pop(k, None)
            else:
                r[k] = str(v).strip()
    if "settings" in body:
        inc = body["settings"]
        if isinstance(inc, dict):
            inc_applied = dict(inc)
            _sanitize_zone_room_settings(inc_applied)
            _sanitize_control_mode_room_settings(inc_applied)
            _sanitize_effective_target_room_settings(base, r, inc_applied)
            cur_s = dict(r.get("settings") or {})
            for sk, sv in inc_applied.items():
                if sk in ("weather_api_key", "ai_api_key") and sv in (None, "", "***"):
                    continue
                if sv is None:
                    cur_s.pop(sk, None)
                else:
                    cur_s[sk] = sv
            r["settings"] = cur_s
        elif inc is None:
            r.pop("settings", None)
    if "ai_config" in body:
        ac = body["ai_config"]
        if isinstance(ac, dict):
            cur_ai = dict(r.get("ai_config") or {})
            for ak, av in ac.items():
                if ak == "ai_api_key" and av in (None, "", "***"):
                    continue
                if av is None:
                    cur_ai.pop(ak, None)
                else:
                    cur_ai[ak] = av
            r["ai_config"] = cur_ai
        elif ac is None:
            r.pop("ai_config", None)
    if "disabled" in body and body["disabled"] is not None:
        r["disabled"] = bool(body["disabled"])
    rooms[idx] = r
    if not config_manager.save_config({"rooms": rooms}):
        raise HTTPException(status_code=500, detail="failed to save rooms")
    return room_registry.public_room_view(r)


@app.post("/api/rooms/{room_id}/disable")
async def api_disable_room(room_id: str):
    """Pause automation; keep room in config and DB history."""
    rq = _require_room_query(room_id)
    base = config_manager.load_config()
    stored = _resolve_stored_room_id(base, rq)
    if not stored:
        raise HTTPException(status_code=404, detail="room not found")
    rooms = [dict(r) for r in room_registry.list_room_dicts(base)]
    idx = next((i for i, re in enumerate(rooms) if re.get("id") == stored), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="room not found")
    if rooms[idx].get("disabled"):
        rid_canon = logic_engine.normalize_room_id(stored)
        return {
            "status": "already_disabled",
            "room_id": stored,
            "room_id_canonical": rid_canon,
        }
    rooms[idx]["disabled"] = True
    if not config_manager.save_config({"rooms": rooms}):
        raise HTTPException(status_code=500, detail="failed to save rooms")
    await logic_engine.stop_room(stored, shutdown_reason="room_disabled")
    rid_canon = logic_engine.normalize_room_id(stored)
    return {"status": "disabled", "room_id": stored, "room_id_canonical": rid_canon}


@app.post("/api/rooms/{room_id}/enable")
async def api_enable_room(room_id: str):
    """Resume scheduler ticks for this room."""
    rq = _require_room_query(room_id)
    base = config_manager.load_config()
    stored = _resolve_stored_room_id(base, rq)
    if not stored:
        raise HTTPException(status_code=404, detail="room not found")
    rooms = [dict(r) for r in room_registry.list_room_dicts(base)]
    idx = next((i for i, re in enumerate(rooms) if re.get("id") == stored), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="room not found")
    if not rooms[idx].get("disabled"):
        rid_canon = logic_engine.normalize_room_id(stored)
        return {
            "status": "already_enabled",
            "room_id": stored,
            "room_id_canonical": rid_canon,
        }
    rooms[idx]["disabled"] = False
    if not config_manager.save_config({"rooms": rooms}):
        raise HTTPException(status_code=500, detail="failed to save rooms")
    rid_canon = logic_engine.normalize_room_id(stored)
    return {"status": "enabled", "room_id": stored, "room_id_canonical": rid_canon}


@app.delete("/api/rooms/{room_id}")
async def api_delete_room(room_id: str, purge: bool = Query(False)):
    """
    Remove room from configuration after a safe stop.
    When purge=true, delete sessions/snapshots/AI rows for this room (irreversible).
    """
    rq = _require_room_query(room_id)
    base = config_manager.load_config()
    stored = _resolve_stored_room_id(base, rq)
    if not stored:
        return {"status": "already_deleted", "purged": bool(purge)}
    rooms = [dict(r) for r in room_registry.list_room_dicts(base)]
    idx = next((i for i, re in enumerate(rooms) if re.get("id") == stored), None)
    if idx is None:
        return {"status": "already_deleted", "purged": bool(purge)}

    rid_canon = logic_engine.normalize_room_id(stored)

    rooms[idx]["disabled"] = True
    if not config_manager.save_config({"rooms": rooms}):
        raise HTTPException(status_code=500, detail="failed to save rooms")

    await logic_engine.stop_room(stored, shutdown_reason="room_deleted")

    if purge:
        await database.delete_room_data(stored)

    new_rooms = [r for r in rooms if r.get("id") != stored]
    if not config_manager.save_config({"rooms": new_rooms}):
        raise HTTPException(status_code=500, detail="failed to save rooms")

    session_logger.clear_room_buffers(stored)
    await _disconnect_room_websockets(rid_canon)

    return {"status": "deleted", "purged": bool(purge), "room_id": stored}


@app.get("/api/rooms/{room_id}/status")
async def api_room_status(room_id: str):
    return await get_status(room_id=_require_room_query(room_id))


@app.get("/api/rooms/{room_id}/logs")
async def api_room_logs(
    room_id: str,
    limit: int = Query(200, ge=1, le=500),
):
    rid = logic_engine.normalize_room_id(_require_room_query(room_id))
    return {"room_id": rid, "logs": room_log_store.get_logs(rid, limit)}


@app.delete("/api/rooms/{room_id}/logs")
async def api_room_logs_clear(room_id: str):
    rid = logic_engine.normalize_room_id(_require_room_query(room_id))
    room_log_store.clear(rid)
    _ws_log_token_by_room.pop(rid, None)
    return {"ok": True, "room_id": rid}


@app.get("/api/rooms/{room_id}/ai/status")
async def api_room_ai_status(room_id: str):
    return get_ai_status(_require_room_query(room_id))


# ── SESSIONS ──────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def get_sessions(
    room_id: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    rid = _require_room_query(room_id)
    sessions = await database.get_sessions(rid, limit, offset, date_from, date_to)
    total = await database.get_session_count(rid, date_from, date_to)
    return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}


@app.get("/api/sessions/stats")
async def get_stats(room_id: str = Query(..., min_length=1)):
    """Today + ML quality stats (used by Dashboard and Analytics pages)."""
    rid = _require_room_query(room_id)
    today = await database.get_today_stats(rid)
    ml = await database.get_ml_stats(rid)
    return {"today": today, "ml": ml}


@app.get("/api/sessions/today")
async def get_today_stats_route(room_id: str = Query(..., min_length=1)):
    """Today stats only."""
    return await database.get_today_stats(_require_room_query(room_id))


# ── INSIGHTS ──────────────────────────────────────────────────────────────────

@app.get("/api/insights")
async def get_insights(room_id: str = Query(..., min_length=1)):
    """
    Read-only analytics derived from completed sessions for one room.
    Does not affect any control logic.

    Returns cooling_rate, efficiency, best target temperature, cooling type
    distribution, and a recent performance trend.
    Always returns valid JSON — never a 500 error.
    """
    try:
        return await database.get_insights(_require_room_query(room_id))
    except Exception as exc:
        logger.error("[HawaAI] /api/insights error: %s", exc, exc_info=True)
        return {
            "sessions_analyzed":   0,
            "avg_cooling_rate":    0.0,
            "avg_efficiency":      0.0,
            "best_target_temp":    None,
            "best_outdoor_range":  None,
            "cooling_type_counts": {"fast": 0, "normal": 0, "slow": 0},
            "trend":               None,
            "error":               str(exc),
        }


# ── SNAPSHOTS ─────────────────────────────────────────────────────────────────

@app.get("/api/snapshots")
async def get_snapshots(
    minutes: int = Query(120, ge=5, le=1440),
    room_id: str = Query(..., min_length=1),
):
    return await database.get_snapshots_recent(minutes, _require_room_query(room_id))


# ── DAILY STATS ───────────────────────────────────────────────────────────────

@app.get("/api/daily")
async def get_daily(
    days: int = Query(7, ge=1, le=90),
    room_id: str = Query(..., min_length=1),
):
    return await database.get_daily_stats(days, _require_room_query(room_id))


# ── CLIMATE ENTITY ────────────────────────────────────────────────────────────

@app.get("/api/climate/{entity_id:path}")
async def get_climate_state(entity_id: str):
    """
    Fetch live state of a HA climate entity.
    Returns hvac_mode, current_temperature, temperature (setpoint),
    fan_mode, swing_mode, and all available mode lists.
    """
    full = await ha_client.get_entity_state_full(entity_id)
    if full is None:
        return {"error": f"Entity {entity_id!r} not found or unavailable"}

    attrs = full.get("attributes", {})

    def _safe_float(v):
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    return {
        "entity_id":          entity_id,
        "hvac_mode":          full.get("state"),               # "cool" / "heat" / "off" / "fan_only" / "dry" / "auto"
        "current_temperature": _safe_float(attrs.get("current_temperature")),
        "temperature":         _safe_float(attrs.get("temperature")),   # setpoint
        "fan_mode":           attrs.get("fan_mode"),
        "swing_mode":         attrs.get("swing_mode"),
        "hvac_modes":         attrs.get("hvac_modes", []),
        "fan_modes":          attrs.get("fan_modes", []),
        "swing_modes":        attrs.get("swing_modes", []),
        "min_temp":           _safe_float(attrs.get("min_temp")),
        "max_temp":           _safe_float(attrs.get("max_temp")),
        "target_temp_step":   _safe_float(attrs.get("target_temp_step")) or 1.0,
        "friendly_name":      attrs.get("friendly_name", entity_id),
    }


@app.post("/api/climate/{entity_id:path}/set_temperature")
async def climate_set_temperature(entity_id: str, data: Dict[str, Any] = Body(...)):
    """Set climate setpoint. Body: {"temperature": 24}"""
    rid_room = _room_id_for_climate_entity(entity_id)
    temperature = data.get("temperature")
    if temperature is None:
        return {"success": False, "error": "temperature field required"}
    return await _enqueue_climate_command(
        room_id=rid_room,
        entity_id=entity_id,
        service="set_temperature",
        payload={
        "entity_id":   entity_id,
        "temperature": float(temperature),
        },
    )


@app.post("/api/climate/{entity_id:path}/set_hvac_mode")
async def climate_set_hvac_mode(entity_id: str, data: Dict[str, Any] = Body(...)):
    """Set HVAC mode. Body: {"hvac_mode": "cool"}"""
    rid_room = _room_id_for_climate_entity(entity_id)
    hvac_mode = data.get("hvac_mode")
    if not hvac_mode:
        return {"success": False, "error": "hvac_mode field required"}
    return await _enqueue_climate_command(
        room_id=rid_room,
        entity_id=entity_id,
        service="set_hvac_mode",
        payload={
            "entity_id": entity_id,
            "hvac_mode": hvac_mode,
        },
    )


@app.post("/api/climate/{entity_id:path}/set_fan_mode")
async def climate_set_fan_mode(entity_id: str, data: Dict[str, Any] = Body(...)):
    """Set fan mode. Body: {"fan_mode": "auto"}"""
    rid_room = _room_id_for_climate_entity(entity_id)
    fan_mode = data.get("fan_mode")
    if not fan_mode:
        return {"success": False, "error": "fan_mode field required"}
    return await _enqueue_climate_command(
        room_id=rid_room,
        entity_id=entity_id,
        service="set_fan_mode",
        payload={
            "entity_id": entity_id,
            "fan_mode":  fan_mode,
        },
    )


@app.post("/api/climate/{entity_id:path}/set_swing_mode")
async def climate_set_swing_mode(entity_id: str, data: Dict[str, Any] = Body(...)):
    """Set swing mode. Body: {"swing_mode": "auto"}"""
    rid_room = _room_id_for_climate_entity(entity_id)
    swing_mode = data.get("swing_mode")
    if not swing_mode:
        return {"success": False, "error": "swing_mode field required"}
    return await _enqueue_climate_command(
        room_id=rid_room,
        entity_id=entity_id,
        service="set_swing_mode",
        payload={
            "entity_id":  entity_id,
            "swing_mode": swing_mode,
        },
    )


# ── HA ENTITIES (for Settings dropdowns) ─────────────────────────────────────

@app.get("/api/entities")
async def list_entities(filter: Optional[str] = None, domain: Optional[str] = None):
    """
    Returns all HA entities for Settings dropdowns.
    Optional ?filter=binary_sensor or ?domain=binary_sensor to filter by domain.
    """
    all_entities = await ha_client.get_all_entities()
    domain_filter = filter or domain
    result = []
    for e in all_entities:
        entity_id = e.get("entity_id", "")
        friendly_name = e.get("attributes", {}).get("friendly_name", entity_id)
        entity_domain = entity_id.split(".")[0] if "." in entity_id else ""
        if domain_filter and entity_domain != domain_filter:
            continue
        attrs = e.get("attributes") or {}
        dc = attrs.get("device_class")
        result.append({
            "entity_id": entity_id,
            "friendly_name": friendly_name,
            "domain": entity_domain,
            "state": e.get("state"),
            "device_class": dc if dc is None else str(dc),
        })
    result.sort(key=lambda x: x["entity_id"])
    return result


# ── HA DEVICE REGISTRY ────────────────────────────────────────────────────────

@app.get("/api/devices")
async def get_devices():
    """
    Returns all HA devices from the device registry, sorted by name.
    Used by Settings Energy section so user can pick their circuit breaker / plug.
    """
    devices = await ha_client.get_device_registry()
    result = [
        {
            "device_id":    d.get("id", ""),
            "name":         d.get("name_by_user") or d.get("name") or "",
            "manufacturer": d.get("manufacturer") or "",
            "model":        d.get("model") or "",
        }
        for d in devices
        if d.get("id")
    ]
    result.sort(key=lambda d: d["name"].lower())
    return result


@app.get("/api/devices/{device_id}/entities")
async def get_device_entities(device_id: str):
    """
    Returns all sensor entities that belong to a specific HA device.
    Queries the entity registry for device_id match, then enriches with live state.
    """
    # Get entity registry to find which entities belong to this device
    registry = await ha_client.get_entity_registry()
    device_entity_ids = {
        r["entity_id"]
        for r in registry
        if r.get("device_id") == device_id
    }

    if not device_entity_ids:
        return []

    # Enrich with live states
    all_states = await ha_client.get_all_entities()
    state_map = {e.get("entity_id"): e for e in all_states}

    result = []
    for eid in sorted(device_entity_ids):
        state_obj = state_map.get(eid, {})
        attrs = state_obj.get("attributes", {})
        result.append({
            "entity_id":     eid,
            "friendly_name": attrs.get("friendly_name", eid),
            "domain":        eid.split(".")[0] if "." in eid else "",
            "unit":          attrs.get("unit_of_measurement", ""),
            "state":         state_obj.get("state"),
        })
    return result


# ── AC BRANDS ─────────────────────────────────────────────────────────────────

@app.get("/api/brands")
async def list_brands():
    return get_brands()


# ── EXPORT ────────────────────────────────────────────────────────────────────

@app.get("/api/export/csv")
async def export_csv(room_id: str = Query(..., min_length=1)):
    import io
    import csv
    rid = _require_room_query(room_id)
    sessions = await database.get_all_sessions_for_export(rid)
    output = io.StringIO()
    if sessions:
        writer = csv.DictWriter(output, fieldnames=sessions[0].keys())
        writer.writeheader()
        writer.writerows(sessions)
    output.seek(0)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="hawaai_data.csv"'},
    )


@app.get("/api/export/json")
async def export_json_route(room_id: str = Query(..., min_length=1)):
    rid = _require_room_query(room_id)
    sessions = await database.get_all_sessions_for_export(rid)
    return Response(
        content=json.dumps(sessions, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="hawaai_data.json"'},
    )


@app.get("/api/export/ml_snapshots")
async def export_ml_snapshots(room_id: str = Query(..., min_length=1), limit: int = 10000):
    """
    Export clean ML training data.
    Filters out any row with null in critical columns.
    """
    rid = _require_room_query(room_id)
    rows = await database.get_snapshots_for_ml(room_id=rid, limit=limit)

    required = [
        "session_id", "room_id", "indoor_temp", "ac_state",
        "presence", "control_source", "effective_final_temp",
    ]
    clean = [
        r for r in rows
        if all(r.get(col) is not None for col in required)
    ]

    if not clean:
        return {"rows": 0, "data": []}

    return {"rows": len(clean), "data": clean}


# ── WebSocket ─────────────────────────────────────────────────────────────────


def _encode_room_tick_ws_payload_json(rid_canon: str) -> Optional[str]:
    """
    Compact live tick JSON for one room (runtime + schedule slot context).
    Mirrors get_runtime_state and must include pending / AC display fields for the dashboard.
    """
    rk = (rid_canon or "").strip().lower()
    if not rk:
        return None
    try:
        base = config_manager.load_config()
        room_def = logic_engine.resolve_room_definition(base, rk)
        if not room_def:
            return json.dumps({"type": "error", "room_id": rk, "detail": "unknown_room"})
        merged = room_registry.merge_room_config(base, room_def)
        runtime = logic_engine.get_runtime_state(rk)
        sched_bt, sched_slot = resolve_base_target_temp(merged)
        payload: Dict[str, Any] = {
            "type": "tick",
            **runtime,
            "control_source": runtime.get("control_source", "none"),
            "target_temp": sched_bt,
            "schedule_slot": sched_slot,
            "temperature_mode": merged.get("temperature_mode") or "manual",
            "room_id": rk,
        }
        latest = room_log_store.latest_key(rk)
        prev = _ws_log_token_by_room.get(rk)
        if latest and latest != prev:
            payload["recent_logs"] = room_log_store.get_logs(rk, 20)
            _ws_log_token_by_room[rk] = latest
        return json.dumps(
            payload,
            default=str,
        )
    except Exception:
        logger.debug("[WS] encode tick payload failed rid=%s", rk, exc_info=True)
        return None


async def _push_ws_payload_to_room_clients(rid_canon: str, payload: str) -> None:
    rk = (rid_canon or "").strip().lower()
    async with _ws_lock:
        clients = list(_ws_by_room.get(rk, []))
    if not clients:
        return
    dead: List[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    if not dead:
        return
    async with _ws_lock:
        bucket = _ws_by_room.get(rk)
        if not bucket:
            return
        for ws in dead:
            try:
                bucket.remove(ws)
            except ValueError:
                pass


async def _broadcast_to_room_subscribers(rid_canon: str) -> None:
    payload = _encode_room_tick_ws_payload_json(rid_canon)
    if not payload:
        return
    await _push_ws_payload_to_room_clients(rid_canon, payload)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    subscribed: Optional[str] = None
    try:
        while subscribed is None:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                return
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "invalid_json"})
                continue
            if msg.get("type") != "subscribe":
                await websocket.send_json({"type": "error", "detail": "subscribe_required"})
                continue
            rid = str(msg.get("room_id") or "").strip()
            if not rid:
                logger.error("[WS] subscribe rejected — missing room_id")
                await websocket.send_json({"type": "error", "detail": "room_id_required"})
                continue
            base = config_manager.load_config()
            if not logic_engine.resolve_room_definition(base, rid):
                await websocket.send_json({"type": "error", "detail": "unknown_room", "room_id": rid})
                await websocket.close(code=4404)
                return
            rid_canon = logic_engine.normalize_room_id(rid)
            async with _ws_lock:
                _ws_by_room[rid_canon].append(websocket)
            subscribed = rid_canon
            await websocket.send_json({"type": "subscribed", "room_id": rid_canon})

        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if subscribed:
            async with _ws_lock:
                bucket = _ws_by_room.get(subscribed)
                if bucket and websocket in bucket:
                    bucket.remove(websocket)
                if bucket is not None and len(bucket) == 0:
                    _ws_by_room.pop(subscribed, None)


async def _broadcast_loop():
    """Fallback sweep: push tick envelope every 5s (event ticks also broadcast immediately)."""
    while True:
        await asyncio.sleep(5)
        async with _ws_lock:
            snapshot: List[tuple] = [(rid, list(wss)) for rid, wss in _ws_by_room.items() if wss]
        for rid, clients in snapshot:
            if not clients:
                continue
            payload = _encode_room_tick_ws_payload_json(rid)
            if not payload:
                continue
            await _push_ws_payload_to_room_clients(rid, payload)


# ── Serve React frontend ──────────────────────────────────────────────────────

_FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str, request: Request):
    """
    Serve React SPA.
    Injects window.__INGRESS_PATH__ into index.html so the frontend
    constructs correct absolute API URLs through the HA ingress proxy.
    Real static assets are served directly; all other routes fall through
    to index.html for client-side routing.
    """
    if not _FRONTEND_DIST.exists():
        return HTMLResponse("<h1>Frontend not built</h1>", status_code=503)

    asset = _FRONTEND_DIST / full_path
    if asset.is_file():
        return FileResponse(asset)

    index = _FRONTEND_DIST / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>index.html not found</h1>", status_code=503)

    ingress_path = request.headers.get("X-Ingress-Path", "")
    html = index.read_text(encoding="utf-8")
    snippet = f'<script>window.__INGRESS_PATH__="{ingress_path}";</script>'
    html = html.replace("</head>", snippet + "\n</head>")
    return HTMLResponse(html)
