"""Passive humidity-aware comfort intelligence.

The functions here are pure calculations. They never control AC hardware, send
IR commands, alter runtime/session state, schedule work, or change HVAC mode.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

DEFAULT_HUMIDITY_COMFORT_ENABLED = True
DEFAULT_IDEAL_MIN = 40.0
DEFAULT_IDEAL_MAX = 60.0
DEFAULT_WARNING_THRESHOLD = 65.0
DEFAULT_CRITICAL_THRESHOLD = 75.0
DEFAULT_MIN_OFFSET = -1.0
DEFAULT_MAX_OFFSET = 0.5


@dataclass(frozen=True)
class HumidityComfort:
    """Humidity comfort metrics and passive target adjustment for one tick."""

    humidity_percent: Optional[float]
    feels_like_temp: Optional[float]
    dew_point: Optional[float]
    humidity_offset: float
    adjusted_target: float
    comfort_score: float
    comfort_level: str
    humidity_band: str
    dry_mode_recommended: bool
    active: bool
    reason: str = "ok"


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


def valid_humidity_percent(raw: Any) -> Optional[float]:
    """Return humidity percent in [0, 100], or None for unavailable/invalid input."""
    if raw in (None, "", "unavailable", "unknown"):
        return None
    try:
        rh = float(raw)
        if not math.isfinite(rh) or rh < 0.0 or rh > 100.0:
            return None
    except (TypeError, ValueError):
        return None
    return round(rh, 1)


def dew_point_celsius(temp_c: float, humidity_percent: float) -> float:
    """Magnus dew point estimate in degrees Celsius."""
    rh = max(1e-6, min(100.0, float(humidity_percent)))
    temp = float(temp_c)
    a = 17.27
    b = 237.7
    gamma = (a * temp) / (b + temp) + math.log(rh / 100.0)
    return round((b * gamma) / (a - gamma), 1)


def feels_like_celsius(temp_c: float, humidity_percent: float, dew_point: float) -> float:
    """
    Indoor apparent comfort estimate.

    This intentionally stays conservative for thermostat use: humidity raises
    the comfort index in small increments, while very dry air can slightly lower
    perceived warmth.
    """
    temp = float(temp_c)
    rh = float(humidity_percent)
    feels = temp
    feels += max(0.0, rh - 55.0) * 0.06
    feels += max(0.0, float(dew_point) - 18.0) * 0.15
    feels -= max(0.0, 40.0 - rh) * 0.03
    return round(feels, 1)


def humidity_band_for(cfg: Dict[str, Any], humidity_percent: float) -> str:
    ideal_min = _cfg_float(cfg, "humidity_ideal_min", DEFAULT_IDEAL_MIN, lo=0.0, hi=100.0)
    ideal_max = _cfg_float(cfg, "humidity_ideal_max", DEFAULT_IDEAL_MAX, lo=0.0, hi=100.0)
    warning = _cfg_float(cfg, "humidity_warning_threshold", DEFAULT_WARNING_THRESHOLD, lo=0.0, hi=100.0)
    critical = _cfg_float(cfg, "humidity_critical_threshold", DEFAULT_CRITICAL_THRESHOLD, lo=0.0, hi=100.0)
    rh = float(humidity_percent)

    if rh < ideal_min:
        return "low"
    if rh <= ideal_max:
        return "ideal"
    if rh < warning:
        return "elevated"
    if rh < critical:
        return "warning"
    return "critical"


def discomfort_score(
    cfg: Dict[str, Any],
    *,
    indoor_temp: float,
    target_temp: float,
    humidity_percent: float,
    feels_like_temp: float,
    dew_point: float,
) -> float:
    ideal_min = _cfg_float(cfg, "humidity_ideal_min", DEFAULT_IDEAL_MIN, lo=0.0, hi=100.0)
    ideal_max = _cfg_float(cfg, "humidity_ideal_max", DEFAULT_IDEAL_MAX, lo=0.0, hi=100.0)
    rh = float(humidity_percent)

    humid_discomfort = max(0.0, rh - ideal_max) * 1.7
    dry_discomfort = max(0.0, ideal_min - rh) * 1.2
    dew_discomfort = max(0.0, float(dew_point) - 16.0) * 3.0
    feels_discomfort = max(0.0, float(feels_like_temp) - float(indoor_temp)) * 5.0
    temp_discomfort = max(0.0, float(indoor_temp) - float(target_temp)) * 3.0

    score = (
        min(50.0, humid_discomfort)
        + min(35.0, dry_discomfort)
        + min(35.0, dew_discomfort)
        + min(20.0, feels_discomfort)
        + min(15.0, temp_discomfort)
    )
    return round(min(100.0, score), 1)


def _comfort_level(band: str, score: float, dew_point: float) -> str:
    if band == "low":
        return "dry"
    if band == "critical" or score >= 65.0 or dew_point >= 24.0:
        return "sticky"
    if band == "ideal" and score < 25.0:
        return "comfortable"
    if band in ("elevated", "warning") or score >= 35.0:
        return "humid"
    return "comfortable"


def _stepped_offset(
    cfg: Dict[str, Any],
    *,
    humidity_percent: float,
    band: str,
    dew_point: float,
) -> float:
    min_offset = _cfg_float(cfg, "humidity_min_offset", DEFAULT_MIN_OFFSET, lo=-3.0, hi=0.0)
    max_offset = _cfg_float(cfg, "humidity_max_offset", DEFAULT_MAX_OFFSET, lo=0.0, hi=3.0)

    if band == "low":
        raw = 0.5
    elif band == "ideal":
        raw = 0.5 if humidity_percent < 50.0 else 0.0
    elif band == "elevated":
        raw = -0.25
    elif band == "warning":
        raw = -0.5
    else:
        raw = -0.5
        if dew_point >= 24.0 or humidity_percent >= 85.0:
            raw = -0.75
        if dew_point >= 26.0 or humidity_percent >= 90.0:
            raw = -1.0

    stepped = round(raw * 4.0) / 4.0
    return round(max(min_offset, min(max_offset, stepped)), 2)


def calculate_humidity_comfort(
    cfg: Dict[str, Any],
    *,
    indoor_temp: float,
    target_temp: float,
    humidity_percent: Optional[float],
    ac_on: bool = False,
) -> HumidityComfort:
    """
    Return comfort metrics and a bounded additive target offset.

    ``target_temp`` is the already-composed target before humidity. The result's
    ``adjusted_target`` is ``target_temp + humidity_offset``.
    """
    target = float(target_temp)
    if not _cfg_bool(cfg, "humidity_comfort_enabled", DEFAULT_HUMIDITY_COMFORT_ENABLED):
        return HumidityComfort(
            None, None, None, 0.0, target, 0.0, "disabled", "disabled", False, False, "disabled",
        )

    rh = valid_humidity_percent(humidity_percent)
    if rh is None:
        return HumidityComfort(
            None, None, None, 0.0, target, 0.0, "unknown", "unavailable", False, False, "no_valid_humidity",
        )

    temp = float(indoor_temp)
    dew = dew_point_celsius(temp, rh)
    feels = feels_like_celsius(temp, rh, dew)
    band = humidity_band_for(cfg, rh)
    score = discomfort_score(
        cfg,
        indoor_temp=temp,
        target_temp=target,
        humidity_percent=rh,
        feels_like_temp=feels,
        dew_point=dew,
    )
    level = _comfort_level(band, score, dew)
    offset = _stepped_offset(cfg, humidity_percent=rh, band=band, dew_point=dew)

    critical = _cfg_float(
        cfg, "humidity_critical_threshold", DEFAULT_CRITICAL_THRESHOLD, lo=0.0, hi=100.0,
    )
    near_target = abs(temp - target) <= 1.0
    dry_mode = bool(
        ac_on
        and rh >= critical
        and near_target
        and score >= 55.0
        and feels >= target + 2.0
    )

    return HumidityComfort(
        humidity_percent=rh,
        feels_like_temp=feels,
        dew_point=dew,
        humidity_offset=offset,
        adjusted_target=round(target + offset, 2),
        comfort_score=score,
        comfort_level=level,
        humidity_band=band,
        dry_mode_recommended=dry_mode,
        active=True,
    )
