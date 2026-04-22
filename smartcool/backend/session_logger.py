"""
Session logger — writes start/end records and snapshots to SQLite.
Module-level functions; runtime session state held in module variables.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import database

logger = logging.getLogger(__name__)

# Runtime state (in-memory, not persisted)
_current_session_id: Optional[str] = None
_session_start_time: Optional[datetime] = None
_cooled_at: Optional[datetime] = None


async def start_session(data: Dict[str, Any]) -> str:
    """Insert a session start record. Returns the new session_id (UUID)."""
    global _current_session_id, _session_start_time, _cooled_at

    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    record = {
        "session_id": session_id,
        "start_time": data.get("start_time", now.isoformat()),
        "indoor_temp_start": data.get("indoor_temp_start"),
        "outdoor_temp_start": data.get("outdoor_temp_start"),
        "outdoor_humidity_start": data.get("outdoor_humidity_start"),
        "target_temp": data.get("target_temp"),
        "ac_entity_id": data.get("ac_entity_id"),
        "ac_brand": data.get("ac_brand"),
        "ac_model": data.get("ac_model"),
        "room_name": data.get("room_name"),
        "presence_trigger": "occupied",
        "energy_start_kwh": 0.0,
        "day_of_week": now.weekday(),
        "hour_of_day": now.hour,
    }

    await database.insert_session_start(record)

    _current_session_id = session_id
    _session_start_time = now
    _cooled_at = None

    logger.info(
        "[HawaAI] Session started: %s (%.1f°C indoor)",
        session_id,
        data.get("indoor_temp_start") or 0,
    )
    return session_id


async def end_session(data: Dict[str, Any]) -> None:
    """Update the current open session with end data."""
    global _current_session_id, _session_start_time, _cooled_at

    if not _current_session_id:
        logger.warning("[HawaAI] end_session called but no active session")
        return

    cool_minutes: Optional[float] = None
    if _cooled_at and _session_start_time:
        cool_minutes = (_cooled_at - _session_start_time).total_seconds() / 60.0

    end_data = {
        "end_time": data.get("end_time", datetime.now(timezone.utc).isoformat()),
        "indoor_temp_end": data.get("indoor_temp_end"),
        "time_to_cool_minutes": data.get("time_to_cool_minutes") or (
            round(cool_minutes, 1) if cool_minutes else None
        ),
        "energy_consumed_kwh": data.get("energy_kwh"),
        "cost_estimate": data.get("cost"),
        "reason_stopped": data.get("reason_stopped"),
        "peak_watt_draw": data.get("peak_watts"),
        "avg_watt_draw": data.get("avg_watts"),
    }

    await database.update_session_end(_current_session_id, end_data)
    logger.info(
        "[HawaAI] Session ended: %s | reason=%s",
        _current_session_id,
        data.get("reason_stopped"),
    )

    _current_session_id = None
    _session_start_time = None
    _cooled_at = None


def mark_cooled() -> None:
    """Mark first time target temp was reached — used for time-to-cool metric."""
    global _cooled_at
    if _cooled_at is None:
        _cooled_at = datetime.now(timezone.utc)


async def add_snapshot(session_id: Optional[str], data: Dict[str, Any]) -> None:
    """Insert a monitoring snapshot (called every tick while AC is on)."""
    snap = {
        "session_id": session_id,
        "timestamp": data.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "indoor_temp": data.get("indoor_temp"),
        "outdoor_temp": data.get("outdoor_temp"),
        "ac_state": data.get("ac_state", False),
        "watt_draw": data.get("watt_draw", 0.0),
        "presence": data.get("presence", True),
    }
    await database.insert_snapshot(snap)


async def get_sessions(limit: int = 50, offset: int = 0) -> List[Dict]:
    """Return sessions ordered by start_time DESC."""
    return await database.get_sessions(limit, offset)


async def get_session_count() -> int:
    return await database.get_session_count()


async def get_today_stats() -> Dict[str, Any]:
    return await database.get_today_stats()


async def get_snapshots(hours: int = 2) -> List[Dict]:
    return await database.get_snapshots_recent(hours * 60)


# Expose current session id for logic_engine
def current_session_id() -> Optional[str]:
    return _current_session_id


def session_start_time() -> Optional[datetime]:
    return _session_start_time
