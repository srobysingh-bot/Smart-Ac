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
    session_provisional: bool = False


_rs: Dict[str, _RoomSession] = defaultdict(_RoomSession)
# Latest SQLite snapshot row id per room (for AI ↔ snapshot linkage).
_last_snapshot_id: Dict[str, int] = {}


def _room(room_id: str) -> _RoomSession:
    return _rs[room_id]


def _require_room(room_id: str) -> str:
    rid = (room_id or "").strip()
    if not rid:
        raise ValueError("room_id is required")
    return rid


async def start_session(room_id: str, data: Dict[str, Any]) -> str:
    """Insert a session start record. Returns the new session_id (UUID)."""
    rid = _require_room(room_id)
    s = _room(rid)

    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    record = {
        "session_id": session_id,
        "room_id": rid,
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
        "provisional": int(1 if data.get("provisional") else 0),
        "is_record_valid": int(data["is_record_valid"]) if data.get("is_record_valid") is not None else 1,
    }

    await database.insert_session_start(record)

    s.current_session_id = session_id
    start_iso = str(record["start_time"])
    try:
        s.session_start_time = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except ValueError:
        try:
            s.session_start_time = datetime.strptime(start_iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            s.session_start_time = now
    s.session_start_temp = data.get("indoor_temp_start")
    s.cooled_at = None
    s.session_provisional = bool(data.get("provisional"))

    logger.debug(
        "[SESSION_START][db] [%s]: %s provisional=%s (%.1f°C indoor)",
        rid,
        session_id,
        s.session_provisional,
        data.get("indoor_temp_start") or 0,
    )
    return session_id


async def end_session(room_id: str, data: Dict[str, Any]) -> None:
    """Update the current open session with end data and compute analytics."""
    try:
        rid = _require_room(room_id)
    except ValueError:
        logger.error("[HawaAI] end_session rejected — missing room_id")
        return
    s = _room(rid)
    ANALYTICS_WARMUP_MINUTES = 5.0

    if not s.current_session_id:
        logger.warning("[HawaAI] end_session[%s] called but no active session", rid)
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
    time_to_target_minutes: Optional[float] = None
    temp_drop_rate_snap: Optional[float] = None

    try:
        _start = float(indoor_start) if indoor_start is not None else None
        _end = float(indoor_end) if indoor_end is not None else None
        snap_metrics = await database.compute_cooling_snapshot_metrics(s.current_session_id)
        time_to_target_minutes = snap_metrics.get("time_to_target_minutes")
        temp_drop_rate_snap = snap_metrics.get("temp_drop_rate")
        tt_target = time_to_target_minutes
        tdrop = temp_drop_rate_snap

        _dur = float(duration_min) if duration_min is not None else 0.0
        _energy = float(energy_kwh) if energy_kwh is not None else 0.0

        rate_for_type: Optional[float] = tdrop
        dur_for_type: Optional[float] = tt_target
        if rate_for_type is None and _start is not None and _end is not None and _dur > ANALYTICS_WARMUP_MINUTES:
            delta = _start - _end
            if delta > 0 and _dur > 0:
                rate_for_type = delta / _dur
            dur_for_type = _dur

        if rate_for_type is not None and (dur_for_type or 0) > ANALYTICS_WARMUP_MINUTES:
            cooling_rate = round(float(rate_for_type), 4)
            if cooling_rate > 0.5:
                cooling_type = "fast"
            elif cooling_rate >= 0.2:
                cooling_type = "normal"
            else:
                cooling_type = "slow"
        else:
            cooling_rate = None
            cooling_type = None

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
                "[HawaAI] Analytics [%s] — cooling_rate=%.4f°C/min (%s) | efficiency=%s°C/kWh | "
                "snap_t_target=%s min | snap_drop_rate=%s",
                rid,
                cooling_rate,
                cooling_type,
                f"{efficiency:.2f}" if efficiency is not None else "N/A",
                tt_target,
                tdrop,
            )
    except Exception as exc:
        logger.warning("[HawaAI] Analytics calculation skipped [%s]: %s", rid, exc)
        cooling_rate = cooling_type = efficiency = None
        time_to_target_minutes = None
        temp_drop_rate_snap = None

    rs = str(data.get("reason_stopped") or "")
    uo = data.get("user_override")
    if uo is None:
        uo = 1 if rs in ("power_off", "manual", "manual_off") else 0
    else:
        uo = int(uo)

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
        "cooling_time": duration_min,
        "energy_used": energy_kwh,
        "user_override": uo,
        "time_to_target_minutes": time_to_target_minutes,
        "temp_drop_rate": temp_drop_rate_snap,
    }
    if data.get("is_record_valid") is not None:
        end_data["is_record_valid"] = int(data["is_record_valid"])

    await database.update_session_end(s.current_session_id, end_data)
    logger.info(
        "[SESSION_END] [%s]: session=%s | reason=%s | record_valid_out=%s",
        rid,
        s.current_session_id,
        data.get("reason_stopped"),
        end_data.get("is_record_valid", "unchanged"),
    )

    s.current_session_id = None
    s.session_start_time = None
    s.session_start_temp = None
    s.cooled_at = None
    s.session_provisional = False


async def upgrade_current_session_to_confirmed(room_id: str) -> None:
    """Clear provisional flag in DB for the room's open session (same session_id)."""
    rid = _require_room(room_id)
    s = _room(rid)
    if not s.current_session_id or not s.session_provisional:
        return
    sid = s.current_session_id
    await database.clear_session_provisional_flag(sid)
    s.session_provisional = False
    logger.info("[SESSION_UPGRADE] [%s] session=%s → confirmed (provisional=0)", rid, sid)


def mark_cooled(room_id: str) -> None:
    try:
        rid = _require_room(room_id)
    except ValueError:
        logger.error("[HawaAI] mark_cooled rejected — missing room_id")
        return
    s = _room(rid)
    if s.cooled_at is None:
        s.cooled_at = datetime.now(timezone.utc)


async def add_snapshot(room_id: str, session_id: Optional[str], data: Dict[str, Any]) -> int:
    """Insert a monitoring snapshot (called every tick while AC is on). Returns row id."""
    rid = _require_room(room_id)
    if not session_id:
        logger.debug("[session_logger] add_snapshot skipped — no session_id (room=%s)", rid)
        return 0
    snap = {
        "session_id": session_id,
        "room_id": rid,
        "timestamp": data.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "indoor_temp": data.get("indoor_temp"),
        "outdoor_temp": data.get("outdoor_temp"),
        "outdoor_humidity": data.get("outdoor_humidity"),
        "indoor_humidity": data.get("indoor_humidity"),
        "ac_state": data.get("ac_state", False),
        "watt_draw": data.get("watt_draw", 0.0),
        "presence": data.get("presence", True),
        "setpoint": data.get("setpoint"),
        "fan_mode": data.get("fan_mode"),
        "energy_kwh": data.get("energy_kwh"),
        "ai_target_temp": data.get("ai_target_temp"),
        "ai_fan_mode": data.get("ai_fan_mode"),
        "ai_confidence": data.get("ai_confidence"),
        "schedule_slot": data.get("schedule_slot"),
        "schedule_base_temp": data.get("schedule_base_temp"),
        "effective_after_weather": data.get("effective_after_weather"),
        "effective_final_temp": data.get("effective_final_temp"),
        "ai_adjust_applied": data.get("ai_adjust_applied"),
        "target_temp": data.get("target_temp"),
        "control_source": data.get("control_source"),
        "hvac_mode": data.get("hvac_mode"),
    }
    row_id = await database.insert_snapshot(snap)
    _last_snapshot_id[rid] = row_id
    return row_id


def last_snapshot_id(room_id: str) -> Optional[int]:
    return _last_snapshot_id.get(room_id)


async def ensure_snapshot_id_for_ai(room_id: str) -> int:
    """
    Latest snapshot for the room, or insert a minimal row so ai_decisions.snapshot_id
    is always populated.
    """
    rid = _require_room(room_id)
    sid = _last_snapshot_id.get(rid)
    if sid is not None and sid != 0:
        return sid
    csid = current_session_id(rid)
    if not csid:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    row_id = await database.insert_snapshot(
        {
            "session_id": csid,
            "room_id": rid,
            "timestamp": now,
            "indoor_temp": None,
            "outdoor_temp": None,
            "outdoor_humidity": None,
            "indoor_humidity": None,
            "ac_state": False,
            "watt_draw": 0.0,
            "presence": True,
            "setpoint": None,
            "fan_mode": None,
            "energy_kwh": None,
            "ai_target_temp": None,
            "ai_fan_mode": None,
            "ai_confidence": None,
            "schedule_slot": None,
            "schedule_base_temp": None,
            "effective_after_weather": None,
            "effective_final_temp": None,
            "ai_adjust_applied": None,
        },
    )
    if row_id:
        _last_snapshot_id[rid] = row_id
    return row_id


async def get_sessions(room_id: str, limit: int = 50, offset: int = 0) -> List[Dict]:
    return await database.get_sessions(room_id, limit, offset)


async def get_session_count(room_id: str) -> int:
    return await database.get_session_count(room_id)


async def get_today_stats(room_id: str) -> Dict[str, Any]:
    return await database.get_today_stats(room_id)


async def get_snapshots(hours: int = 2, room_id: str = "") -> List[Dict]:
    return await database.get_snapshots_recent(hours * 60, room_id)


def current_session_id(room_id: str) -> Optional[str]:
    rid = (room_id or "").strip()
    if not rid:
        return None
    return _room(rid).current_session_id


def current_session_is_provisional(room_id: str) -> bool:
    """True if runtime shows an open session marked provisional."""
    rid = (room_id or "").strip()
    if not rid:
        return False
    s = _room(rid)
    return bool(s.current_session_id and s.session_provisional)


def session_start_time(room_id: str) -> Optional[datetime]:
    rid = (room_id or "").strip()
    if not rid:
        return None
    return _room(rid).session_start_time
