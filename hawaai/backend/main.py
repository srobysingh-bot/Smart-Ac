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
  WS   /ws                  Live push per subscribed room_id every 5 s
"""

import asyncio
import json
import logging
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

from . import config_manager, database, logic_engine, room_registry, scheduler, session_logger, weather_api
from . import ha_client
from .ac_controller import get_brands
from .ai import get_cached
from .ai.ai_worker import get_ai_status, init_ai_worker
from .temperature_schedule import resolve_base_target_temp
from .utils import parse_presence

# Room-scoped WebSocket subscribers: broadcast never crosses room_id boundaries.
_ws_by_room: Dict[str, List[WebSocket]] = defaultdict(list)
_ws_lock = asyncio.Lock()


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




@asynccontextmanager
async def lifespan(app: FastAPI):
    database.backup_db("startup")
    await database.init_db()
    cfg = config_manager.load_config()
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
    asyncio.create_task(scheduler.start())
    asyncio.create_task(_broadcast_loop())
    logger.info("[HawaAI] Add-on started")
    yield
    database.backup_db("shutdown")
    logger.info("[HawaAI] Add-on stopped")


app = FastAPI(title="HawaAI API", version="1.4.2", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
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
    room_def = room_registry.get_room(base, rid)
    if not room_def:
        raise HTTPException(status_code=404, detail=f"Unknown room: {rid}")
    cfg = room_registry.merge_room_config(base, room_def)
    runtime = logic_engine.get_runtime_state(rid)

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

    # ── Determine ac_on + ac_idle from power sensor (mirrors logic_engine) ────
    cooldown_active = runtime.get("cooldown_active", False)
    watts_on_thr    = runtime.get("watts_on_threshold",   500.0)
    watts_idle_thr  = runtime.get("watts_idle_threshold",  50.0)

    ac_idle: bool = False
    power_source: str

    if energy_watts is not None and not cooldown_active:
        # Power sensor available and cooldown expired — use watts as truth
        if energy_watts > watts_on_thr:
            ac_on        = True
            ac_idle      = False
            power_source = "watts"
        elif energy_watts >= watts_idle_thr:
            ac_on        = runtime.get("ac_is_on", False)
            ac_idle      = True
            power_source = "watts_idle"
        else:
            ac_on        = False
            ac_idle      = False
            power_source = "watts"
    elif cooldown_active:
        # Just sent a command — trust internal flag until AC responds
        ac_on        = runtime.get("ac_is_on", False)
        ac_idle      = False
        power_source = "cooldown"
    else:
        # No power sensor configured
        ac_on        = runtime.get("ac_is_on", False)
        ac_idle      = False
        power_source = "internal"

    # Aerostate — single source of truth for displayed AC state.
    # HawaAI now commands via Aerostate; this reads live state back.
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
    effective_target, ai_adjust_applied = logic_engine.bounded_effective_from_ai_cache(
        rid,
        cfg,
        effective_after_weather,
        is_occupied,
    )

    rt = _runtime_block(runtime)
    reason = _smart_adjustment_reason(
        smart_curve, outdoor_temp_val, base_target, effective_after_weather,
    )

    ai_snap = get_ai_status(rid)
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
        "ac_on":            ac_on,
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
        # ── Engine diagnostics ────────────────────────────────────────────────
        "cooldown_active":  cooldown_active,
        "last_command":     runtime.get("last_command"),
        "secs_since_cmd":   runtime.get("secs_since_cmd"),
        # ── Config ────────────────────────────────────────────────────────────
        "manual_override":  cfg.get("manual_override", False),
        "config_complete":  bool(
            cfg.get("presence_entity") and cfg.get("indoor_temp_entity")
        ),
        "target_temp": base_target,
        "schedule_base_temp": base_target,
        "effective_target": effective_target,
        "effective_after_weather": effective_after_weather,
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
    room_def = room_registry.get_room(base, rid)
    if not room_def:
        raise HTTPException(status_code=404, detail="room not found")
    eff = room_registry.merge_room_config(base, room_def)
    return {
        "room": room_registry.public_room_view(room_def),
        "effective": _mask_effective_room_settings(eff),
    }


@app.post("/api/rooms")
async def api_create_room(body: Dict[str, Any] = Body(...)):
    import uuid

    base = config_manager.load_config()
    rooms = [dict(r) for r in room_registry.list_room_dicts(base)]
    rid = (str(body.get("id") or "")).strip() or str(uuid.uuid4())[:12]
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
            cur_s = dict(r.get("settings") or {})
            for sk, sv in inc.items():
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
    rooms[idx] = r
    if not config_manager.save_config({"rooms": rooms}):
        raise HTTPException(status_code=500, detail="failed to save rooms")
    return room_registry.public_room_view(r)


@app.delete("/api/rooms/{room_id}")
async def api_delete_room(room_id: str):
    rid = _require_room_query(room_id)
    base = config_manager.load_config()
    rooms = [dict(r) for r in room_registry.list_room_dicts(base)]
    new_rooms = [r for r in rooms if r.get("id") != rid]
    if len(new_rooms) == len(rooms):
        raise HTTPException(status_code=404, detail="room not found")
    if not config_manager.save_config({"rooms": new_rooms}):
        raise HTTPException(status_code=500, detail="failed to save rooms")
    return {"ok": True}


@app.get("/api/rooms/{room_id}/status")
async def api_room_status(room_id: str):
    return await get_status(room_id=_require_room_query(room_id))


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
    temperature = data.get("temperature")
    if temperature is None:
        return {"success": False, "error": "temperature field required"}
    ok = await ha_client.call_service("climate", "set_temperature", {
        "entity_id":   entity_id,
        "temperature": float(temperature),
    })
    return {"success": ok}


@app.post("/api/climate/{entity_id:path}/set_hvac_mode")
async def climate_set_hvac_mode(entity_id: str, data: Dict[str, Any] = Body(...)):
    """Set HVAC mode. Body: {"hvac_mode": "cool"}"""
    hvac_mode = data.get("hvac_mode")
    if not hvac_mode:
        return {"success": False, "error": "hvac_mode field required"}
    ok = await ha_client.call_service("climate", "set_hvac_mode", {
        "entity_id": entity_id,
        "hvac_mode": hvac_mode,
    })
    return {"success": ok}


@app.post("/api/climate/{entity_id:path}/set_fan_mode")
async def climate_set_fan_mode(entity_id: str, data: Dict[str, Any] = Body(...)):
    """Set fan mode. Body: {"fan_mode": "auto"}"""
    fan_mode = data.get("fan_mode")
    if not fan_mode:
        return {"success": False, "error": "fan_mode field required"}
    ok = await ha_client.call_service("climate", "set_fan_mode", {
        "entity_id": entity_id,
        "fan_mode":  fan_mode,
    })
    return {"success": ok}


@app.post("/api/climate/{entity_id:path}/set_swing_mode")
async def climate_set_swing_mode(entity_id: str, data: Dict[str, Any] = Body(...)):
    """Set swing mode. Body: {"swing_mode": "auto"}"""
    swing_mode = data.get("swing_mode")
    if not swing_mode:
        return {"success": False, "error": "swing_mode field required"}
    ok = await ha_client.call_service("climate", "set_swing_mode", {
        "entity_id":  entity_id,
        "swing_mode": swing_mode,
    })
    return {"success": ok}


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
        result.append({
            "entity_id": entity_id,
            "friendly_name": friendly_name,
            "domain": entity_domain,
            "state": e.get("state"),
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


# ── WebSocket ─────────────────────────────────────────────────────────────────

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
            if not room_registry.get_room(base, rid):
                await websocket.send_json({"type": "error", "detail": "unknown_room", "room_id": rid})
                await websocket.close(code=4404)
                return
            async with _ws_lock:
                _ws_by_room[rid].append(websocket)
            subscribed = rid
            await websocket.send_json({"type": "subscribed", "room_id": rid})

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
    """Push compact runtime ticks only to WebSocket clients subscribed to each room_id."""
    while True:
        await asyncio.sleep(5)
        async with _ws_lock:
            snapshot: List[tuple] = [(rid, list(wss)) for rid, wss in _ws_by_room.items() if wss]
        for rid, clients in snapshot:
            if not clients:
                continue
            try:
                base = config_manager.load_config()
                room_def = room_registry.get_room(base, rid)
                if not room_def:
                    payload = json.dumps({"type": "error", "room_id": rid, "detail": "unknown_room"})
                else:
                    merged = room_registry.merge_room_config(base, room_def)
                    runtime = logic_engine.get_runtime_state(rid)
                    sched_bt, sched_slot = resolve_base_target_temp(merged)
                    payload = json.dumps(
                        {
                            "type": "tick",
                            **runtime,
                            "target_temp": sched_bt,
                            "schedule_slot": sched_slot,
                            "temperature_mode": merged.get("temperature_mode") or "manual",
                            "room_id": rid,
                        },
                        default=str,
                    )
            except Exception:
                continue

            dead: List[WebSocket] = []
            for ws in clients:
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            if not dead:
                continue
            async with _ws_lock:
                bucket = _ws_by_room.get(rid)
                if not bucket:
                    continue
                for ws in dead:
                    try:
                        bucket.remove(ws)
                    except ValueError:
                        pass


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
