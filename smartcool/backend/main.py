"""
SmartCool FastAPI application.

Routes:
  GET  /api/status          Live status + current readings
  GET  /api/sessions        Paginated session history
  GET  /api/sessions/stats  Today stats + ML quality stats
  GET  /api/snapshots       Recent monitoring snapshots (last 2h)
  GET  /api/config          Current add-on config
  POST /api/config          Patch config at runtime
  GET  /api/brands          AC brand+model library
  GET  /api/entities        HA entity lists for Settings dropdowns
  POST /api/config/reload   Reload config from disk
  GET  /api/export/csv      Download session CSV
  GET  /api/export/json     Download session JSON
  GET  /api/daily           Daily stats for last N days
  WS   /ws                  Live push of status every 5 s
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel

from . import config_manager, database, export_manager, scheduler
from .ac_controller import ACController, get_brands
from .energy_monitor import EnergyMonitor
from .ha_client import HAClient
from .logic_engine import LogicEngine
from .presence_handler import PresenceHandler
from .session_logger import SessionLogger
from .temperature_handler import TemperatureHandler

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ── Singleton service objects ─────────────────────────────────────────────────
ha: Optional[HAClient] = None
engine: Optional[LogicEngine] = None
_ws_clients: List[WebSocket] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown logic."""
    global ha, engine

    config_manager.load()
    await database.init_db()

    ha = HAClient(config_manager.get("ha_token"))

    presence = PresenceHandler(ha)
    temp_handler = TemperatureHandler(ha)
    energy = EnergyMonitor(ha)
    ac = ACController(ha)
    session_log = SessionLogger(ha)
    engine = LogicEngine(ha, presence, temp_handler, energy, ac, session_log)

    await ha.start()
    scheduler.start(engine)

    # Kick off WebSocket broadcast loop
    asyncio.create_task(_broadcast_loop())

    logger.info("SmartCool started")
    yield

    scheduler.stop()
    await ha.stop()
    logger.info("SmartCool stopped")


app = FastAPI(title="SmartCool API", version="1.0.5", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ── REST Routes ───────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    if engine is None:
        raise HTTPException(503, "Engine not ready")
    return engine.status


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
    today = await database.get_today_stats()
    ml = await database.get_ml_stats()
    return {"today": today, "ml": ml}


@app.get("/api/snapshots")
async def get_snapshots(minutes: int = Query(120, ge=5, le=1440)):
    return await database.get_snapshots_recent(minutes)


@app.get("/api/daily")
async def get_daily(days: int = Query(7, ge=1, le=90)):
    return await database.get_daily_stats(days)


@app.get("/api/config")
async def read_config():
    cfg = config_manager.get_all()
    # Mask secret tokens in response
    masked = {**cfg}
    if masked.get("ha_token"):
        masked["ha_token"] = "***"
    if masked.get("weather_api_key"):
        masked["weather_api_key"] = "***"
    return masked


class ConfigPatch(BaseModel):
    class Config:
        extra = "allow"


@app.post("/api/config")
async def update_config(patch: ConfigPatch):
    data = patch.dict(exclude_none=True)
    # Prevent clearing secret fields via masked values or empty strings
    # Don't send masked "***" or empty strings for secrets
    current = config_manager.get_all()
    for secret_key in ("ha_token", "weather_api_key"):
        val = data.get(secret_key, "")
        # Remove if masked, empty, or not provided
        if not val or val == "***":
            if secret_key in data:
                del data[secret_key]
    
    updated = config_manager.update(data)
    logger.info("Config updated with keys: %s", list(data.keys()))
    return {"ok": True, "config": {k: v for k, v in updated.items() if k not in ("ha_token", "weather_api_key")}}


@app.post("/api/config/reload")
async def reload_config():
    config_manager.reload()
    return {"ok": True}


@app.get("/api/brands")
async def list_brands():
    return get_brands()


@app.get("/api/entities")
async def list_entities(domain: Optional[str] = None):
    """List HA entities for Settings dropdowns."""
    if ha is None:
        return []
    entities = await ha.list_entities(domain)
    return [
        {
            "entity_id": e["entity_id"],
            "friendly_name": e.get("attributes", {}).get("friendly_name", e["entity_id"]),
            "state": e.get("state"),
        }
        for e in entities
    ]


@app.get("/api/export/csv", response_class=PlainTextResponse)
async def export_csv():
    content = await export_manager.export_csv()
    filename = export_manager.export_filename("csv")
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/export/json")
async def export_json_route():
    content = await export_manager.export_json()
    filename = export_manager.export_filename("json")
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.append(websocket)
    try:
        while True:
            # Keep connection alive; server pushes data via broadcast loop
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.remove(websocket)


async def _broadcast_loop():
    """Push live status to all connected WebSocket clients every 5 seconds."""
    while True:
        await asyncio.sleep(5)
        if not _ws_clients or engine is None:
            continue
        payload = json.dumps(engine.status, default=str)
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
    """Serve the React SPA, injecting the HA ingress base path into index.html
    so the frontend can construct correct absolute API + WebSocket URLs.
    Real static assets (JS/CSS/images) are served directly by FileResponse.
    All other paths fall through to index.html for client-side routing.
    """
    if not _FRONTEND_DIST.exists():
        return HTMLResponse("<h1>Frontend not built</h1>", status_code=503)

    # Serve real static assets directly
    asset = _FRONTEND_DIST / full_path
    if asset.is_file():
        return FileResponse(asset)

    # For SPA routes (/, /history, /analytics, /settings …) serve index.html
    # and inject the HA ingress base path so the frontend knows its URL prefix.
    index = _FRONTEND_DIST / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>index.html not found</h1>", status_code=503)

    # HA Supervisor sets X-Ingress-Path header, e.g. /api/hassio_ingress/TOKEN
    ingress_path = request.headers.get("X-Ingress-Path", "")
    html = index.read_text(encoding="utf-8")
    snippet = f'<script>window.__INGRESS_PATH__="{ingress_path}";</script>'
    html = html.replace("</head>", snippet + "\n</head>")
    return HTMLResponse(html)
