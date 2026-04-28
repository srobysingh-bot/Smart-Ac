"""
Session logger — writes start/end records and snapshots to SQLite.
Runtime state is isolated per room_id.
"""

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import database

logger = logging.getLogger(__name__)


@dataclass
class _RoomSession:
    current_session_id: Optional[str] = None
    session_start_time: Optional[datetime] = None
    session_start_temp: Optional[float] = None
    cooled_at: Optional[datetime] = None


_rs: Dict[str, _RoomSession] = defaultdict(_RoomSession)


def _room(room_id: str) -> _RoomSession:
    return _rs[room_id]


async def start_session(room_id: str, data: Dict[str, Any]) -> str:
    """Insert a session start record. Returns the new session_id (UUID)."""
    s = _room(room_id)

    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    record = {
        "session_id": session_id,
        "room_id": room_id,
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
        "energy_start_kwh": data.get("energy_kwh_start"),
        "day_of_week": now.weekday(),
        "hour_of_day": now.hour,
    }

    await database.insert_session_start(record)

    s.current_session_id = session_id
    s.session_start_time = now
    s.session_start_temp = data.get("indoor_temp_start")
    s.cooled_at = None

    logger.info(
        "[HawaAI] Session started [%s]: %s (%.1f°C indoor)",
        room_id,
        session_id,
        data.get("indoor_temp_start") or 0,
    )
    return session_id


async def end_session(room_id: str, data: Dict[str, Any]) -> None:
    """Update the current open session with end data and compute analytics."""
    s = _room(room_id)
    ANALYTICS_WARMUP_MINUTES = 5.0

    if not s.current_session_id:
        logger.warning("[HawaAI] end_session[%s] called but no active session", room_id)
        return

    cool_minutes: Optional[float] = None
    if s.cooled_at and s.session_start_time:
        cool_minutes = (s.cooled_at - s.session_start_time).total_seconds() / 60.0

    end_time = data.get("end_time", datetime.now(timezone.utc).isoformat())
    indoor_start = s.session_start_temp
    indoor_end = data.get("indoor_temp_end")
    duration_min = data.get("time_to_cool_minutes") or (
        round(cool_minutes, 1) if cool_minutes else None
    )
    energy_kwh = data.get("energy_kwh")

    cooling_rate: Optional[float] = None
    cooling_type: Optional[str] = None
    efficiency: Optional[float] = None

    try:
        _start = float(indoor_start) if indoor_start is not None else None
        _end = float(indoor_end) if indoor_end is not None else None
        _dur = float(duration_min) if duration_min is not None else 0.0
        _energy = float(energy_kwh) if energy_kwh is not None else 0.0

        if (
            _start is not None
            and _end is not None
            and _dur > ANALYTICS_WARMUP_MINUTES
        ):
            delta = _start - _end
            if delta > 0 and _dur > 0:
                cooling_rate = round(delta / _dur, 4)
                if cooling_rate > 0.5:
                    cooling_type = "fast"
                elif cooling_rate >= 0.2:
                    cooling_type = "normal"
                else:
                    cooling_type = "slow"

        if (
            _start is not None
            and _end is not None
            and _energy > 0
        ):
            delta = _start - _end
            if delta > 0:
                efficiency = round(delta / _energy, 2)

        if cooling_rate is not None:
            logger.info(
                "[HawaAI] Analytics [%s] — cooling_rate=%.4f°C/min (%s) | efficiency=%s°C/kWh",
                room_id,
                cooling_rate,
                cooling_type,
                f"{efficiency:.2f}" if efficiency is not None else "N/A",
            )
    except Exception as exc:
        logger.warning("[HawaAI] Analytics calculation skipped [%s]: %s", room_id, exc)
        cooling_rate = cooling_type = efficiency = None

    end_data = {
        "end_time": end_time,
        "indoor_temp_end": indoor_end,
        "time_to_cool_minutes": duration_min,
        "energy_consumed_kwh": energy_kwh,
        "cost_estimate": data.get("cost"),
        "reason_stopped": data.get("reason_stopped"),
        "peak_watt_draw": data.get("peak_watts"),
        "avg_watt_draw": data.get("avg_watts"),
        "cooling_rate": cooling_rate,
        "cooling_type": cooling_type,
        "efficiency": efficiency,
    }

    await database.update_session_end(s.current_session_id, end_data)
    logger.info(
        "[HawaAI] Session ended [%s]: %s | reason=%s",
        room_id,
        s.current_session_id,
        data.get("reason_stopped"),
    )

    s.current_session_id = None
    s.session_start_time = None
    s.session_start_temp = None
    s.cooled_at = None


def mark_cooled(room_id: str) -> None:
    s = _room(room_id)
    if s.cooled_at is None:
        s.cooled_at = datetime.now(timezone.utc)


async def add_snapshot(room_id: str, session_id: Optional[str], data: Dict[str, Any]) -> None:
    """Insert a monitoring snapshot (called every tick while AC is on)."""
    snap = {
        "session_id": session_id,
        "room_id": room_id,
        "timestamp": data.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "indoor_temp": data.get("indoor_temp"),
        "outdoor_temp": data.get("outdoor_temp"),
        "ac_state": data.get("ac_state", False),
        "watt_draw": data.get("watt_draw", 0.0),
        "presence": data.get("presence", True),
    }
    await database.insert_snapshot(snap)


async def get_sessions(room_id: str, limit: int = 50, offset: int = 0) -> List[Dict]:
    return await database.get_sessions(room_id, limit, offset)


async def get_session_count(room_id: str) -> int:
    return await database.get_session_count(room_id)


async def get_today_stats(room_id: str) -> Dict[str, Any]:
    return await database.get_today_stats(room_id)


async def get_snapshots(hours: int = 2, room_id: str = "") -> List[Dict]:
    return await database.get_snapshots_recent(hours * 60, room_id)


def current_session_id(room_id: str) -> Optional[str]:
    return _room(room_id).current_session_id


def session_start_time(room_id: str) -> Optional[datetime]:
    return _room(room_id).session_start_time
