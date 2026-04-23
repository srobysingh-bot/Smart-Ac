"""
HawaAI FastAPI application.

Routes:
  GET  /api/status          Live status + current readings
  GET  /api/sessions        Paginated session history
  GET  /api/sessions/stats  Today + ML quality stats (for Analytics/Dashboard)
  GET  /api/sessions/today  Today stats only
  GET  /api/snapshots       Recent monitoring snapshots (last 2h)
  GET  /api/config          Current add-on config
  POST /api/config          Save config to /data/hawaai_config.json
  GET  /api/entities        HA entity list for Settings dropdowns
  GET  /api/climate/{id}   Live climate entity state + attributes
  POST /api/climate/{id}/set_temperature
  POST /api/climate/{id}/set_hvac_mode
  POST /api/climate/{id}/set_fan_mode
  GET  /api/brands          AC brand+model library
  GET  /api/daily           Daily stats for last N days
  GET  /api/export/csv      Download session CSV
  GET  /api/export/json     Download session JSON
  WS   /ws                  Live push of status every 5 s
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from fastapi import Body, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response

from . import config_manager, database, logic_engine, scheduler, session_logger, weather_api
from . import ha_client
from .ac_controller import get_brands
from .utils import parse_presence

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

_ws_clients: List[WebSocket] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_db()
    config_manager.load_config()
    asyncio.create_task(scheduler.start())
    asyncio.create_task(_broadcast_loop())
    logger.info("[HawaAI] Add-on started")
    yield
    logger.info("[HawaAI] Add-on stopped")


app = FastAPI(title="HawaAI API", version="1.1.14", lifespan=lifespan)

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
    return masked


@app.post("/api/config")
async def save_config(data: Dict[str, Any] = Body(...)):
    """Frontend POSTs full config on Save. Persists to /data/hawaai_config.json."""
    # Don't overwrite secrets with masked placeholder
    for secret_key in ("weather_api_key",):
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


# ── LIVE STATUS ───────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    """
    Called by Dashboard every 5 seconds. Single source of truth for all UI state.

    AC state rule (v1.1.13+):
      ac_on = logic_engine._ac_is_on (internal flag, set only by Broadlink IR commands)
      Climate entity is NEVER used to determine ac_on — display only.
    """
    cfg     = config_manager.load_config()
    runtime = logic_engine.get_runtime_state()

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

    # AC state = internal flag ONLY. Never override with climate entity or power.
    # This mirrors the logic_engine architecture: Broadlink IR commands set the flag.
    ac_on = runtime["ac_is_on"]

    # Climate entity — display data only (temp, mode, fan, swing for ClimateCard)
    climate_entity = cfg.get("climate_entity", "")
    climate_data: dict = {}
    if climate_entity:
        climate_data = await ha_client.get_climate_state(climate_entity)

    # Indoor temp: prefer dedicated sensor; fall back to climate entity thermistor
    indoor_temp = safe_float(indoor_temp_raw)
    if indoor_temp is None and climate_data:
        indoor_temp = climate_data.get("current_temp")

    return {
        # ── Core state (always from internal engine flag) ─────────────────────
        "ac_on":            ac_on,
        "indoor_temp":      indoor_temp,
        "outdoor_temp":     weather.get("temp")      if weather else None,
        "outdoor_humidity": weather.get("humidity")  if weather else None,
        "presence":         is_occupied,
        # ── Energy (from power sensor — display only, not used for ac_on) ──────
        "watt_draw":        energy_watts or 0.0,
        "energy_watts":     energy_watts,
        "energy_kwh_total": energy_kwh,
        # ── Session ───────────────────────────────────────────────────────────
        "session_kwh":      runtime.get("session_start_kwh"),
        "session_id":       runtime["session_id"],
        "session_start":    runtime["session_start_time"],
        # ── Engine diagnostics ────────────────────────────────────────────────
        "cooldown_active":  runtime.get("cooldown_active", False),
        "last_command":     runtime.get("last_command"),
        "secs_since_cmd":   runtime.get("secs_since_cmd"),
        # ── Config ────────────────────────────────────────────────────────────
        "manual_override":  cfg.get("manual_override", False),
        "config_complete":  bool(
            cfg.get("presence_entity") and cfg.get("indoor_temp_entity")
        ),
        "target_temp":      cfg.get("target_temp", 24),
        "climate_entity":   climate_entity,
        # ── Climate display data (read-only, not used for ac_on) ──────────────
        "ac_current_temp":  climate_data.get("current_temp"),
        "ac_target_temp":   climate_data.get("target_temp"),
        "ac_mode":          climate_data.get("mode"),
        "ac_fan_mode":      climate_data.get("fan_mode"),
        "ac_swing_mode":    climate_data.get("swing_mode"),
    }


# ── SESSIONS ──────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def get_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    sessions = await database.get_sessions(limit, offset, date_from, date_to)
    total = await database.get_session_count(date_from, date_to)
    return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}


@app.get("/api/sessions/stats")
async def get_stats():
    """Today + ML quality stats (used by Dashboard and Analytics pages)."""
    today = await database.get_today_stats()
    ml = await database.get_ml_stats()
    return {"today": today, "ml": ml}


@app.get("/api/sessions/today")
async def get_today_stats():
    """Today stats only."""
    return await database.get_today_stats()


# ── SNAPSHOTS ─────────────────────────────────────────────────────────────────

@app.get("/api/snapshots")
async def get_snapshots(minutes: int = Query(120, ge=5, le=1440)):
    return await database.get_snapshots_recent(minutes)


# ── DAILY STATS ───────────────────────────────────────────────────────────────

@app.get("/api/daily")
async def get_daily(days: int = Query(7, ge=1, le=90)):
    return await database.get_daily_stats(days)


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
async def export_csv():
    import io
    import csv
    sessions = await database.get_all_sessions_for_export()
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
async def export_json_route():
    sessions = await database.get_all_sessions_for_export()
    return Response(
        content=json.dumps(sessions, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="hawaai_data.json"'},
    )


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)


async def _broadcast_loop():
    """Push live status to all connected WebSocket clients every 5 seconds."""
    while True:
        await asyncio.sleep(5)
        if not _ws_clients:
            continue
        try:
            cfg = config_manager.load_config()
            runtime = logic_engine.get_runtime_state()
            payload = json.dumps({**runtime, "target_temp": cfg.get("target_temp", 24)}, default=str)
        except Exception:
            continue
        dead = []
        for ws in list(_ws_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in _ws_clients:
                _ws_clients.remove(ws)


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
