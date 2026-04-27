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
_session_start_temp: Optional[float] = None   # for cooling_rate / efficiency calculation
_cooled_at: Optional[datetime] = None


async def start_session(data: Dict[str, Any]) -> str:
    """Insert a session start record. Returns the new session_id (UUID)."""
    global _current_session_id, _session_start_time, _session_start_temp, _cooled_at

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
    _session_start_temp = data.get("indoor_temp_start")
    _cooled_at = None

    logger.info(
        "[HawaAI] Session started: %s (%.1f°C indoor)",
        session_id,
        data.get("indoor_temp_start") or 0,
    )
    return session_id


async def end_session(data: Dict[str, Any]) -> None:
    """
    Update the current open session with end data and compute analytics.

    Analytics rules (read-only, never affect control logic):
      - cooling_rate = (start_temp − end_temp) / duration_minutes
        → only computed when duration > ANALYTICS_WARMUP_MINUTES (5 min)
        → must be > 0 (room actually cooled)
      - cooling_type = fast (>0.5°C/min) / normal (0.2–0.5) / slow (<0.2)
      - efficiency   = delta_temp / energy_kwh
        → only computed when energy_consumed_kwh > 0
    """
    global _current_session_id, _session_start_time, _session_start_temp, _cooled_at

    # Minimum session duration before computing cooling analytics.
    # The first 5 minutes are ignored because the AC needs time to reach
    # stable operation after the IR command is processed.
    ANALYTICS_WARMUP_MINUTES = 5.0

    if not _current_session_id:
        logger.warning("[HawaAI] end_session called but no active session")
        return

    cool_minutes: Optional[float] = None
    if _cooled_at and _session_start_time:
        cool_minutes = (_cooled_at - _session_start_time).total_seconds() / 60.0

    end_time     = data.get("end_time", datetime.now(timezone.utc).isoformat())
    indoor_start = _session_start_temp   # set by start_session via _store_start_temp
    indoor_end   = data.get("indoor_temp_end")
    duration_min = data.get("time_to_cool_minutes") or (
        round(cool_minutes, 1) if cool_minutes else None
    )
    energy_kwh   = data.get("energy_kwh")

    # ── Compute cooling analytics (never raises — errors logged and skipped) ───
    cooling_rate: Optional[float] = None
    cooling_type: Optional[str]   = None
    efficiency:   Optional[float] = None

    try:
        # Safe coercion — treat None/non-numeric as 0 for guard comparisons only
        _start  = float(indoor_start) if indoor_start is not None else None
        _end    = float(indoor_end)   if indoor_end   is not None else None
        _dur    = float(duration_min) if duration_min is not None else 0.0
        _energy = float(energy_kwh)   if energy_kwh   is not None else 0.0

        if (
            _start is not None
            and _end    is not None
            and _dur    > ANALYTICS_WARMUP_MINUTES      # skip first 5 min
        ):
            delta = _start - _end
            if delta > 0 and _dur > 0:                  # room cooled + no div/0
                cooling_rate = round(delta / _dur, 4)
                if cooling_rate > 0.5:
                    cooling_type = "fast"
                elif cooling_rate >= 0.2:
                    cooling_type = "normal"
                else:
                    cooling_type = "slow"

        if (
            _start  is not None
            and _end    is not None
            and _energy > 0                             # guard against div/0
        ):
            delta = _start - _end
            if delta > 0:
                efficiency = round(delta / _energy, 2)

        if cooling_rate is not None:
            logger.info(
                "[HawaAI] Analytics — cooling_rate=%.4f°C/min (%s) | efficiency=%s°C/kWh",
                cooling_rate, cooling_type,
                f"{efficiency:.2f}" if efficiency is not None else "N/A",
            )
    except Exception as exc:
        logger.warning("[HawaAI] Analytics calculation skipped: %s", exc)
        cooling_rate = cooling_type = efficiency = None

    end_data = {
        "end_time":             end_time,
        "indoor_temp_end":      indoor_end,
        "time_to_cool_minutes": duration_min,
        "energy_consumed_kwh":  energy_kwh,
        "cost_estimate":        data.get("cost"),
        "reason_stopped":       data.get("reason_stopped"),
        "peak_watt_draw":       data.get("peak_watts"),
        "avg_watt_draw":        data.get("avg_watts"),
        # Analytics (None if session too short or data missing)
        "cooling_rate":         cooling_rate,
        "cooling_type":         cooling_type,
        "efficiency":           efficiency,
    }

    await database.update_session_end(_current_session_id, end_data)
    logger.info(
        "[HawaAI] Session ended: %s | reason=%s",
        _current_session_id,
        data.get("reason_stopped"),
    )

    _current_session_id = None
    _session_start_time = None
    _session_start_temp = None
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
