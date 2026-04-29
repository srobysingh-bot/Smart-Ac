"""
Time-based schedule helpers: fixed slots (local wall clock) per room configuration.

Morning:   06:00–12:00
Afternoon: 12:00–17:00
Evening:   17:00–22:00
Night:     22:00–06:00
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, Literal, Optional, Tuple

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def validate_timezone_optional(raw: Any) -> str:
    """
    Normalize to a valid IANA zone name. Empty string means “use downstream default (UTC / TZ)”.

    Common mistakes (e.g. ``kolkata``) map to canonical names (``Asia/Kolkata``).
    Unrecognized values fall back to ``Asia/Kolkata`` so schedules never silently use wrong local time from "".
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    upper = s.upper()
    if upper == "UTC":
        return "UTC"
    aliases = {
        "kolkata": "Asia/Kolkata",
        "calcutta": "Asia/Kolkata",
        "bombay": "Asia/Kolkata",
        "mumbai": "Asia/Kolkata",
        "delhi": "Asia/Kolkata",
        "bangalore": "Asia/Kolkata",
        "bengaluru": "Asia/Kolkata",
    }
    key = s.lower().replace(" ", "_")
    if key in aliases:
        return aliases[key]
    try:
        ZoneInfo(s)
        return s
    except Exception:
        logger.error(
            "[HawaAI] Invalid IANA timezone %r — using Asia/Kolkata. Use e.g. Asia/Kolkata, Europe/London, UTC.",
            s,
        )
        return "Asia/Kolkata"

TemperatureMode = Literal["manual", "schedule", "schedule_ai"]

SCHEDULE_SLOTS = ("morning", "afternoon", "evening", "night")  # informational

# AI may nudge effective (after weather), not replace schedule — symmetric clamp (°C)
AI_SCHEDULE_MAX_DELTA_C = 1.0


def timezone_for_schedule(cfg: Dict[str, Any]) -> ZoneInfo:
    """IANA tz name from merged config / env; falls back to UTC."""
    tzs = (str(cfg.get("timezone") or "")).strip()
    if not tzs:
        tzs = (os.environ.get("TZ") or "").strip()
    if not tzs:
        tzs = "UTC"
    try:
        return ZoneInfo(tzs)
    except Exception:
        logger.warning(
            "[HawaAI] Invalid timezone %r — using UTC for schedule slots", tzs,
        )
        return ZoneInfo("UTC")


def now_local_for_schedule(cfg: Dict[str, Any]) -> datetime:
    """Current time in schedule timezone (DST-aware via ZoneInfo)."""
    tz = timezone_for_schedule(cfg)
    return datetime.now(tz=tz)


def get_time_slot(local_time: datetime) -> str:
    """
    Return slot id: morning | afternoon | evening | night.

    local_time must be timezone-aware for correct semantics.
    """
    if local_time.tzinfo is None:
        raise ValueError("get_time_slot requires timezone-aware datetime")
    minutes = local_time.hour * 60 + local_time.minute
    # Night 22:00–06:00
    if minutes >= 22 * 60 or minutes < 6 * 60:
        return "night"
    if minutes < 12 * 60:
        return "morning"
    if minutes < 17 * 60:
        return "afternoon"
    return "evening"


def _coerce_schedule_temps(schedule: Any, fallback: float) -> Dict[str, float]:
    keys = ("morning_temp", "afternoon_temp", "evening_temp", "night_temp")
    raw = schedule if isinstance(schedule, dict) else {}
    out: Dict[str, float] = {}
    for k in keys:
        v = raw.get(k)
        try:
            if v is None or (isinstance(v, str) and not str(v).strip()):
                raise TypeError("missing")
            out[k] = float(v)
        except (TypeError, ValueError):
            out[k] = fallback
    return out


def normalize_temperature_mode(raw: Any) -> str:
    m = str(raw or "manual").strip().lower().replace("+", "_")
    if m == "manual":
        return "manual"
    if m == "schedule":
        return "schedule"
    if m in ("schedule_ai", "schedule+ai"):
        return "schedule_ai"
    return "manual"


def ensure_temperature_schedule_defaults(merged_room_cfg: Dict[str, Any]) -> None:
    """
    Populate temperature_mode + schedule temps on merged per-room effective config.

    Mutates merged_room_cfg in place.
    """
    base_t = merged_room_cfg.get("target_temp", 24)
    try:
        fb = float(base_t)
    except (TypeError, ValueError):
        fb = 24.0

    merged_room_cfg["temperature_mode"] = normalize_temperature_mode(
        merged_room_cfg.get("temperature_mode"),
    )
    merged_room_cfg["schedule"] = _coerce_schedule_temps(
        merged_room_cfg.get("schedule"),
        fb,
    )


def resolve_base_target_temp(
    cfg: Dict[str, Any],
    now_local: Optional[datetime] = None,
) -> Tuple[float, str]:
    """
    Manual path: user_setpoint (target_temp).
    Schedule paths: mapped schedule temp for current slot.

    Returns (degrees_celsius, slot_name_or_manual).
    """
    ensure_temperature_schedule_defaults(cfg)
    mode: str = cfg["temperature_mode"]
    base_fb = float(cfg.get("target_temp", 24) or 24)

    if mode == "manual":
        try:
            t = float(cfg.get("target_temp", base_fb))
        except (TypeError, ValueError):
            t = base_fb
        return t, "manual"

    ln = now_local or now_local_for_schedule(cfg)
    slot = get_time_slot(ln)
    key = f"{slot}_temp"
    sch = cfg.get("schedule") or {}
    raw = sch.get(key)
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return base_fb, slot
    try:
        t = float(raw)
    except (TypeError, ValueError):
        t = base_fb
    return t, slot


def apply_ai_bounded_adjustment(effective_after_weather: float, ai_target: Optional[float]) -> float:
    """
    Clamp model target to ± AI_SCHEDULE_MAX_DELTA_C °C around the weather-adjusted baseline.

    Schedule + weather define the envelope; AI may only adjust within ±1 °C per product spec.
    """
    if ai_target is None:
        return effective_after_weather
    try:
        t = float(ai_target)
    except (TypeError, ValueError):
        return effective_after_weather
    lo = effective_after_weather - AI_SCHEDULE_MAX_DELTA_C
    hi = effective_after_weather + AI_SCHEDULE_MAX_DELTA_C
    return max(lo, min(hi, t))
