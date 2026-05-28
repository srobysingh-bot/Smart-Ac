"""Deterministic per-room Auto Comfort target resolver.

This module is intentionally pure. It never sends climate commands, never
decides ON/OFF, and never mutates runtime/session state. The logic engine owns
those responsibilities.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


AUTO_COMFORT_MODE = "auto_comfort"
DEFAULT_PROFILE = "comfort"
DEFAULT_MIN_TARGET_C = 16.0
DEFAULT_MAX_TARGET_C = 25.0
DEFAULT_MAX_STEP_C = 0.5
DEFAULT_MAX_TOTAL_OFFSET_C = 2.0
DEFAULT_MIN_CHANGE_SECONDS = 900.0
SUPPORTED_PROFILES = ("comfort", "balanced", "eco")


@dataclass(frozen=True)
class AutoComfortDecision:
    target: float
    base_target: float
    final_target: float
    profile: str
    learned_band: str
    learned_offset: float
    profile_offset: float
    weather_offset: float
    humidity_offset: float
    thermal_load_offset: float
    sleep_offset: float
    cooling_effectiveness_offset: float
    confidence: str
    status: str
    reason: str
    warnings: List[str] = field(default_factory=list)
    held_previous: bool = False
    capped_by_step: bool = False


@dataclass(frozen=True)
class CoolingEffectiveness:
    status: str
    reason: str
    warning: str = ""
    drop_rate_c_per_hour: Optional[float] = None


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


def _round_half(value: float) -> float:
    return round(float(value) * 2.0) / 2.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(float(lo), min(float(hi), float(value)))


def normalize_profile(raw: Any) -> str:
    val = str(raw or DEFAULT_PROFILE).strip().lower().replace("-", "_")
    return val if val in SUPPORTED_PROFILES else DEFAULT_PROFILE


def _profile_offset(profile: str) -> float:
    return {
        "comfort": -0.5,
        "balanced": 0.0,
        "eco": 0.5,
    }.get(normalize_profile(profile), -0.5)


def _weather_offset(outdoor_temp: Optional[float], indoor_temp: float, base_target: float, profile: str) -> float:
    if outdoor_temp is None:
        return 0.0
    outdoor = float(outdoor_temp)
    multiplier = {"comfort": 1.0, "balanced": 0.75, "eco": 0.5}.get(profile, 1.0)
    if outdoor >= 44.0:
        return -1.0 * multiplier
    if outdoor >= 40.0:
        return -0.75 * multiplier
    if outdoor >= 36.0:
        return -0.5 * multiplier
    if outdoor <= 28.0 and indoor_temp <= base_target:
        return 0.25 if profile == "eco" else 0.0
    return 0.0


def _humidity_offset(humidity_percent: Optional[float], profile: str) -> float:
    if humidity_percent is None:
        return 0.0
    rh = float(humidity_percent)
    multiplier = 1.0 if profile == "comfort" else 0.75
    if profile == "eco":
        multiplier = 0.5
    if rh >= 78.0:
        return -0.75 * multiplier
    if rh >= 68.0:
        return -0.5 * multiplier
    if rh >= 60.0:
        return -0.25 * multiplier
    if rh <= 35.0 and profile == "eco":
        return 0.25
    return 0.0


def _thermal_offset(level: str, confidence: str, cooling_saturated: bool, profile: str) -> float:
    if cooling_saturated:
        return 0.0
    lvl = str(level or "low").lower()
    conf = str(confidence or "low").lower()
    if profile == "eco":
        max_high = -0.5
        max_medium = -0.25
    elif profile == "balanced":
        max_high = -0.75
        max_medium = -0.5
    else:
        max_high = -1.0
        max_medium = -0.5
    if lvl == "high" and conf == "high":
        return max_high
    if lvl in ("medium", "high") and conf in ("medium", "high"):
        return max_medium
    return 0.0


def _cooling_effectiveness_offset(effectiveness: str, cooling_saturated: bool, profile: str) -> float:
    if cooling_saturated:
        return 0.0
    eff = str(effectiveness or "unknown").lower()
    if eff == "poor":
        return -0.5 if profile == "comfort" else -0.25
    if eff == "weak":
        return -0.25 if profile != "eco" else 0.0
    return 0.0


def confidence_for_samples(sample_count: int, learned_count: int = 0) -> str:
    if sample_count >= 96 or learned_count >= 12:
        return "high"
    if sample_count >= 24 or learned_count >= 3:
        return "medium"
    return "learning"


def resolve_base_target(
    cfg: Dict[str, Any],
    *,
    learned_target: Optional[float],
    auto_default_target: Optional[float],
    room_target_temp: Optional[float],
    schedule_hint_target: Optional[float],
) -> tuple[float, str]:
    """Resolve Auto Comfort base target using learned/default/user/schedule priority."""
    for source, raw in (
        ("learned", learned_target),
        ("auto_default", auto_default_target),
        ("room_target", room_target_temp),
        ("schedule_hint", schedule_hint_target),
    ):
        try:
            val = float(raw)
            if math.isfinite(val):
                return val, source
        except (TypeError, ValueError):
            continue
    return _cfg_float(cfg, "target_temp", 23.0, lo=16.0, hi=30.0), "fallback"


def evaluate_cooling_effectiveness(
    *,
    ac_on: bool,
    elapsed_seconds: Optional[float],
    start_temp: Optional[float],
    current_temp: Optional[float],
    target_gap: Optional[float],
    outdoor_temp: Optional[float],
    humidity_percent: Optional[float],
    cooling_saturated: bool,
) -> CoolingEffectiveness:
    if not ac_on:
        return CoolingEffectiveness("unknown", "ac_not_running")
    if start_temp is None or current_temp is None or elapsed_seconds is None:
        return CoolingEffectiveness("unknown", "collecting_cooling_window")
    elapsed = float(elapsed_seconds)
    if elapsed < 600.0:
        return CoolingEffectiveness("unknown", "collecting_10_min_window")

    drop = float(start_temp) - float(current_temp)
    rate = (drop / max(elapsed, 1.0)) * 3600.0
    gap = float(target_gap or 0.0)
    outdoor = float(outdoor_temp) if outdoor_temp is not None else None
    humidity = float(humidity_percent) if humidity_percent is not None else None

    if cooling_saturated and gap >= 2.0:
        return CoolingEffectiveness(
            "weak",
            "outdoor_heat_saturation" if outdoor is not None and outdoor >= 42.0 else "max_comfort_cooling_active",
            "outdoor heat saturation" if outdoor is not None and outdoor >= 42.0 else "undersized AC / extreme load",
            round(rate, 2),
        )
    if rate >= 3.0:
        return CoolingEffectiveness("good", "cooling_response_normal", "", round(rate, 2))
    if rate >= 1.0:
        warning = "possible airflow restriction" if humidity is not None and humidity >= 70.0 else ""
        return CoolingEffectiveness("weak", "cooling_response_slow", warning, round(rate, 2))
    if rate >= 0.0:
        warning = "dirty filter suspicion" if elapsed >= 1200.0 else "possible open door/window"
        return CoolingEffectiveness("poor", "cooling_response_poor", warning, round(rate, 2))
    return CoolingEffectiveness("poor", "room_temp_rising_while_ac_on", "possible open door/window", round(rate, 2))


def resolve_auto_comfort_target(
    cfg: Dict[str, Any],
    *,
    now: datetime,
    base_target: float,
    base_source: str,
    indoor_temp: Optional[float],
    outdoor_temp: Optional[float],
    humidity_percent: Optional[float],
    occupied: bool,
    ac_on: bool,
    thermal_load_level: str,
    thermal_load_confidence: str,
    cooling_saturated: bool,
    cooling_effectiveness: str,
    learned_band: str,
    learned_offset: float = 0.0,
    learned_sample_count: int = 0,
    runtime_sample_count: int = 0,
    previous_target: Optional[float] = None,
    previous_target_at: Optional[datetime] = None,
    include_humidity_offset: bool = True,
    include_thermal_load_offset: bool = True,
) -> AutoComfortDecision:
    """Return the Auto Comfort target for one room/tick."""
    warnings: List[str] = []
    profile = normalize_profile(cfg.get("auto_comfort_profile", DEFAULT_PROFILE))
    min_target = _cfg_float(cfg, "auto_comfort_min_target", DEFAULT_MIN_TARGET_C, lo=16.0, hi=30.0)
    max_target = _cfg_float(cfg, "auto_comfort_max_target", DEFAULT_MAX_TARGET_C, lo=min_target, hi=30.0)
    max_step = _cfg_float(cfg, "auto_comfort_max_step_deg", DEFAULT_MAX_STEP_C, lo=0.25, hi=2.0)
    max_total = _cfg_float(cfg, "auto_comfort_max_total_offset_deg", DEFAULT_MAX_TOTAL_OFFSET_C, lo=0.0, hi=3.0)
    min_change_seconds = _cfg_float(cfg, "auto_comfort_min_change_seconds", DEFAULT_MIN_CHANGE_SECONDS, lo=0.0, hi=7200.0)

    if indoor_temp is None:
        fallback = previous_target if previous_target is not None else base_target
        target = _round_half(_clamp(float(fallback), min_target, max_target))
        return AutoComfortDecision(
            target=target,
            base_target=round(float(base_target), 2),
            final_target=target,
            profile=profile,
            learned_band=learned_band,
            learned_offset=0.0,
            profile_offset=0.0,
            weather_offset=0.0,
            humidity_offset=0.0,
            thermal_load_offset=0.0,
            sleep_offset=0.0,
            cooling_effectiveness_offset=0.0,
            confidence="degraded",
            status="degraded",
            reason="room_temp_sensor_required",
            warnings=["room_temp_sensor_required"],
        )

    indoor = float(indoor_temp)
    if humidity_percent is None:
        warnings.append("humidity_unavailable")
    if outdoor_temp is None:
        warnings.append("outdoor_unavailable")

    learned = _clamp(float(learned_offset or 0.0), -2.0, 2.0)
    profile_adj = _profile_offset(profile)
    weather = _weather_offset(outdoor_temp, indoor, float(base_target), profile)
    humidity = _humidity_offset(humidity_percent, profile) if include_humidity_offset else 0.0
    thermal = _thermal_offset(thermal_load_level, thermal_load_confidence, cooling_saturated, profile) if include_thermal_load_offset else 0.0
    cooling = _cooling_effectiveness_offset(cooling_effectiveness, cooling_saturated, profile)
    sleep = 0.0

    if not occupied and indoor <= float(base_target):
        weather = max(weather, 0.0)
        humidity = max(humidity, 0.0)
        thermal = 0.0
        cooling = 0.0

    if cooling_saturated:
        cooling = 0.0
        if float(base_target) <= 17.0 or (previous_target is not None and float(previous_target) <= 17.0):
            warnings.append("cooling_headroom_exhausted")

    raw_offset = learned + profile_adj + weather + humidity + thermal + cooling + sleep
    offset = _clamp(raw_offset, -max_total, max_total)
    desired = _round_half(_clamp(float(base_target) + offset, min_target, max_target))

    held = False
    capped = False
    target = desired
    if previous_target is not None:
        prev = float(previous_target)
        delta = desired - prev
        if abs(delta) < 0.25:
            target = prev
            held = True
        else:
            limited_delta = _clamp(delta, -max_step, max_step)
            if abs(limited_delta - delta) >= 0.01:
                capped = True
            target = _round_half(prev + limited_delta)

        if previous_target_at is not None:
            elapsed = max(0.0, (now - previous_target_at).total_seconds())
            if elapsed < min_change_seconds and abs(target - prev) < 1.0:
                target = prev
                held = True

    target = _round_half(_clamp(target, min_target, max_target))
    confidence = confidence_for_samples(int(runtime_sample_count or 0), int(learned_sample_count or 0))
    if cooling_saturated:
        status = "saturated"
        reason = "max_comfort_cooling_active"
    elif held:
        status = "stable"
        reason = "holding_previous_target"
    elif confidence == "learning":
        status = "learning"
        reason = f"learning_{base_source}_room_profile"
    else:
        status = "active"
        reason = "occupied_hot_room_high_load" if thermal < -0.01 or weather < -0.01 else "comfort_target_resolved"

    return AutoComfortDecision(
        target=round(float(target), 2),
        base_target=round(float(base_target), 2),
        final_target=round(float(target), 2),
        profile=profile,
        learned_band=learned_band,
        learned_offset=round(learned, 2),
        profile_offset=round(profile_adj, 2),
        weather_offset=round(weather, 2),
        humidity_offset=round(humidity, 2),
        thermal_load_offset=round(thermal, 2),
        sleep_offset=round(sleep, 2),
        cooling_effectiveness_offset=round(cooling, 2),
        confidence=confidence,
        status=status,
        reason=reason,
        warnings=warnings,
        held_previous=held,
        capped_by_step=capped,
    )
