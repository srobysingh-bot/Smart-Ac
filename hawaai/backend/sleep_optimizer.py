"""Passive sleep target relaxation.

This module is deliberately pure: it calculates an additive target offset and
returns diagnostics. It never controls AC hardware, sends commands, mutates
runtime state, opens sessions, or schedules work.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .temperature_schedule import timezone_for_schedule

DEFAULT_SLEEP_ENABLED = True
DEFAULT_SLEEP_START_HOUR = 22
DEFAULT_SLEEP_END_HOUR = 6
DEFAULT_SLEEP_MAX_OFFSET = 1.5
DEFAULT_SLEEP_CURVE_MODE = "gradual"
DEFAULT_EMERGENCY_MARGIN = 4.0

_PHASES = ("settling", "deep_sleep", "late_sleep", "pre_wake")


@dataclass(frozen=True)
class SleepAdjustment:
    """Target adjustment result for one scheduler tick."""

    offset: float
    adjusted_target: float
    active: bool
    phase: str
    suspended: Optional[str] = None


def _cfg_bool(cfg: Dict[str, Any], key: str, default: bool) -> bool:
    raw = cfg.get(key, default)
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def _cfg_float(
    cfg: Dict[str, Any],
    key: str,
    default: float,
    *,
    lo: Optional[float] = None,
    hi: Optional[float] = None,
) -> float:
    try:
        val = float(cfg.get(key, default))
        if not math.isfinite(val):
            raise ValueError("non-finite")
    except (TypeError, ValueError):
        val = float(default)
    if lo is not None:
        val = max(float(lo), val)
    if hi is not None:
        val = min(float(hi), val)
    return val


def _cfg_hour(cfg: Dict[str, Any], key: str, default: int) -> int:
    try:
        val = int(float(cfg.get(key, default)))
    except (TypeError, ValueError):
        val = int(default)
    return max(0, min(23, val))


def _sleep_window_minutes(start_hour: int, end_hour: int) -> int:
    start = start_hour * 60
    end = end_hour * 60
    if end <= start:
        end += 24 * 60
    return max(0, end - start)


def _elapsed_sleep_minutes(local_time: datetime, start_hour: int, end_hour: int) -> Optional[int]:
    minutes_now = local_time.hour * 60 + local_time.minute
    start = start_hour * 60
    end = end_hour * 60

    if start == end:
        return None

    if end > start:
        if start <= minutes_now < end:
            return minutes_now - start
        return None

    if minutes_now >= start:
        return minutes_now - start
    if minutes_now < end:
        return (24 * 60 - start) + minutes_now
    return None


def _gradual_offset(elapsed_minutes: int, window_minutes: int, max_offset: float) -> tuple[float, str]:
    if window_minutes <= 0 or max_offset <= 0:
        return 0.0, "inactive"

    phase_minutes = window_minutes / 4.0
    phase_index = int(elapsed_minutes // phase_minutes) if phase_minutes > 0 else 0
    phase_index = max(0, min(3, phase_index))
    step = max_offset / 3.0
    offset = round(step * phase_index, 2)
    return offset, _PHASES[phase_index]


def _schedule_timezone(cfg: Dict[str, Any]):
    try:
        return timezone_for_schedule(cfg)
    except Exception:
        return timezone.utc


def calculate_sleep_adjustment(
    cfg: Dict[str, Any],
    *,
    current_time: datetime,
    target_temp: float,
    indoor_temp: Optional[float],
    user_manual_target: Optional[float] = None,
) -> SleepAdjustment:
    """
    Return the sleep offset and adjusted target for this tick.

    ``target_temp`` is the already-composed schedule + weather + AI target.
    The returned ``offset`` is additive and non-negative. Emergency suspension
    compares indoor temperature against the pre-sleep target so extreme heat
    never receives a relaxed cooling target.
    """
    target = float(target_temp)
    if not _cfg_bool(cfg, "sleep_optimization_enabled", DEFAULT_SLEEP_ENABLED):
        return SleepAdjustment(0.0, target, False, "disabled")

    curve_mode = str(cfg.get("sleep_curve_mode") or DEFAULT_SLEEP_CURVE_MODE).strip().lower()
    if curve_mode != "gradual":
        return SleepAdjustment(0.0, target, False, "unsupported_curve")

    start_hour = _cfg_hour(cfg, "sleep_start_hour", DEFAULT_SLEEP_START_HOUR)
    end_hour = _cfg_hour(cfg, "sleep_end_hour", DEFAULT_SLEEP_END_HOUR)
    max_offset = _cfg_float(
        cfg,
        "sleep_max_offset",
        DEFAULT_SLEEP_MAX_OFFSET,
        lo=0.0,
        hi=5.0,
    )

    tz = _schedule_timezone(cfg)
    local_time = current_time
    if local_time.tzinfo is None:
        local_time = local_time.replace(tzinfo=tz)
    else:
        local_time = local_time.astimezone(tz)

    elapsed = _elapsed_sleep_minutes(local_time, start_hour, end_hour)
    if elapsed is None:
        return SleepAdjustment(0.0, target, False, "outside_sleep")

    window = _sleep_window_minutes(start_hour, end_hour)
    offset, phase = _gradual_offset(elapsed, window, max_offset)

    emergency_margin = _cfg_float(
        cfg,
        "sleep_emergency_margin",
        DEFAULT_EMERGENCY_MARGIN,
        lo=0.0,
        hi=10.0,
    )
    if indoor_temp is not None:
        try:
            indoor = float(indoor_temp)
        except (TypeError, ValueError):
            indoor = None
        if indoor is not None and indoor > target + emergency_margin:
            return SleepAdjustment(0.0, target, False, phase, "high_heat")

    if user_manual_target is not None:
        try:
            manual_cap = float(user_manual_target)
        except (TypeError, ValueError):
            manual_cap = None
        if manual_cap is not None:
            allowed_offset = max(0.0, manual_cap - target)
            offset = min(offset, allowed_offset)

    offset = round(max(0.0, min(offset, max_offset)), 2)
    return SleepAdjustment(offset, round(target + offset, 2), True, phase)
