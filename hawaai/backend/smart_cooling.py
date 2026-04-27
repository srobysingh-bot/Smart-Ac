"""
HawaAI Smart Cooling optimizer — fan-mode-only adjustments.

RULES (hard — never violate):
  ✗  Does NOT control AC ON / OFF
  ✗  Does NOT change session start / stop logic
  ✗  Does NOT send Broadlink IR commands
  ✓  Only adjusts fan mode via climate entity (when configured)
  ✓  Must be explicitly enabled via config key smart_cooling_enabled=true
  ✓  Minimum 120 s between any two fan adjustments
  ✓  Never resends the same fan mode twice in a row
  ✓  Skips silently when: AC off / idle, room vacant, manual override, no climate entity

Why fan speed instead of lowering setpoint temperature?
  AC compressors do not cool faster when setpoint is dropped — their capacity
  is fixed by refrigerant & hardware. What actually makes a room feel cooler
  faster is maximising air movement (higher fan speed disperses cooled air
  through the room more evenly). So this module raises fan to "high" when the
  room is significantly warmer than target (boost mode) and lets it settle to
  "auto" once the gap closes.

Modes:
  boost  — delta ≥ 4.0°C above target → fan = "high"
  normal — 1.5°C < delta < 4.0°C      → fan = "auto"
  hold   — delta ≤ 1.5°C              → no action (room is near comfort zone)
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import ha_client

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
BOOST_DELTA         = 4.0    # °C: delta ≥ this → boost
HOLD_DELTA          = 1.5    # °C: delta ≤ this → hold (no change)
ADJUSTMENT_COOLDOWN = 120    # seconds minimum between fan mode changes

# Logical names used by HawaAI; mapped to entity-specific values at runtime
FAN_BOOST  = "high"
FAN_NORMAL = "auto"

# Map generic → Aerostate / Midea-style (and common aliases). Unknown keys pass through.
FAN_ALIAS_MAP: Dict[str, str] = {
    "high":   "f5",
    "medium": "f3",
    "low":    "f1",
    "auto":   "auto",
}

# ── Fan mode resolution (Aerostate / Midea use f1–f5, not "high"/"low") ────────

def _mode_in_supported(mode: str, supported: List[str]) -> Optional[str]:
    """Return the canonical string from `supported` to use with HA, or None."""
    if not mode or not supported:
        return None
    if mode in supported:
        return mode
    m = mode.lower()
    for s in supported:
        if s is not None and s.lower() == m:
            return s
    return None


def _resolve_fan_mode_for_entity(
    requested_logical: str,
    supported: List[str],
) -> Tuple[Optional[str], str, str]:
    """
    Map logical fan (high/auto/…) to an HA entity value.
    Returns (value_for_ha_or_None, log_fragment, reason_code).
    reason_code: "alias" | "native" | "unsupported" | "empty_supported"
    """
    if not supported:
        return None, requested_logical, "empty_supported"

    mapped = FAN_ALIAS_MAP.get(requested_logical, requested_logical)
    log_frag = (
        f"{requested_logical!r}→{mapped!r}"
        if mapped != requested_logical
        else f"{requested_logical!r}"
    )

    hit = _mode_in_supported(mapped, supported)
    if hit is not None:
        return hit, log_frag, "alias"

    hit2 = _mode_in_supported(requested_logical, supported)
    if hit2 is not None:
        return hit2, f"{requested_logical!r} (no alias)", "native"

    return None, log_frag, "unsupported"


# ── Module-level state (in-memory, never persisted) ───────────────────────────

# Fan-mode optimizer state
_current_mode:         str               = "hold"
_last_adjustment_time: Optional[datetime] = None
_last_fan_mode:        Optional[str]      = None  # last value **sent to HA** (entity-native)

# Effective-target dispatcher state
_APPLY_TARGET_COOLDOWN = 180          # seconds between temperature commands
_last_apply_target_time: Optional[datetime] = None
_last_applied_target:    Optional[float]    = None


# ── Public accessors ──────────────────────────────────────────────────────────

def get_state() -> Dict[str, Any]:
    """Returns current smart cooling state for /api/status and /api/runtime."""
    return {
        "smart_mode":          _current_mode,
        "smart_fan_mode":      _last_fan_mode,
        "last_applied_target": _last_applied_target,
    }


def reset() -> None:
    """Reset all smart cooling state when a session ends or AC turns off."""
    global _current_mode, _last_adjustment_time, _last_fan_mode, \
           _last_apply_target_time, _last_applied_target
    _current_mode           = "hold"
    _last_adjustment_time   = None
    _last_fan_mode          = None
    _last_apply_target_time = None
    _last_applied_target    = None


# ── Core function ─────────────────────────────────────────────────────────────

async def apply_smart_cooling(
    indoor_temp:    float,
    target_temp:    float,
    ac_on:          bool,
    ac_idle:        bool,
    is_occupied:    bool,
    manual_override: bool,
    climate_entity: str,
    enabled:        bool,
) -> Dict[str, Any]:
    """
    Evaluate the current temperature gap and issue a fan-mode adjustment if needed.

    Parameters
    ----------
    indoor_temp     Current indoor temperature (°C)
    target_temp     Effective target temperature after smart-temp-adjustment (°C)
    ac_on           True when compressor is running (from logic_engine)
    ac_idle         True when only fan is running (50–500 W band)
    is_occupied     Room occupancy
    manual_override Config flag — skip all automation
    climate_entity  HA climate entity ID; empty string if not configured
    enabled         config.smart_cooling_enabled gate

    Returns a diagnostic dict (logged / surfaced in /api/status):
      mode     — "boost" | "normal" | "hold"
      delta    — indoor_temp − target_temp (°C)
      fan_mode — target fan mode string, or None if no action
      action   — short string describing what happened this tick
    """
    global _current_mode, _last_adjustment_time, _last_fan_mode

    delta = round(indoor_temp - target_temp, 2)
    result: Dict[str, Any] = {
        "mode":     _current_mode,
        "delta":    delta,
        "fan_mode": None,
        "action":   "no_change",
    }

    # ── Guards ────────────────────────────────────────────────────────────────

    if not enabled:
        result["action"] = "disabled"
        return result

    if manual_override:
        result["action"] = "manual_override"
        return result

    # AC must be actively cooling (compressor ON) — not idle, not off
    if not ac_on or ac_idle:
        _current_mode = "hold"
        result.update(mode="hold", action="ac_off_or_idle")
        return result

    if not is_occupied:
        _current_mode = "hold"
        result.update(mode="hold", action="vacant")
        return result

    # ── Determine target mode ─────────────────────────────────────────────────

    if delta >= BOOST_DELTA:
        target_mode     = "boost"
        target_fan_mode = FAN_BOOST
    elif delta > HOLD_DELTA:
        target_mode     = "normal"
        target_fan_mode = FAN_NORMAL
    else:
        # Comfortable — reset to hold but don't issue a fan command
        _current_mode       = "hold"
        _last_fan_mode      = None   # allow re-applying next time room heats up
        result.update(mode="hold", action="no_change")
        logger.info(
            "[HawaAI] Smart Mode: hold | Delta: %.1f°C (≤ %.1f°C threshold)",
            delta, HOLD_DELTA,
        )
        return result

    result["mode"]     = target_mode
    result["fan_mode"] = target_fan_mode  # logical (boost / normal intent)

    # ── No climate entity — log only, no service call ────────────────────────
    if not climate_entity:
        _current_mode    = target_mode
        result["action"] = "no_climate_entity"
        logger.info(
            "[HawaAI] Smart Mode: %s | Delta: %.1f°C | Fan target: %s "
            "— no climate entity configured, skipping fan command",
            target_mode, delta, target_fan_mode,
        )
        return result

    # ── Runtime: read supported fan_modes, map aliases (high→f5, …) ─────────
    cstate     = await ha_client.get_climate_state(climate_entity)
    supported  = cstate.get("fan_modes")
    if not isinstance(supported, list):
        supported = []
    resolved, _log_frag, resolve_reason = _resolve_fan_mode_for_entity(
        target_fan_mode, supported,
    )
    result["fan_mode_ha"] = resolved

    logger.info(
        "[HawaAI] smart_cooling fan: requested=%r | %s | supported=%s | "
        "translated_ha=%r | detail=%s",
        target_fan_mode,
        _log_frag,
        supported,
        resolved,
        resolve_reason,
    )

    if resolved is None:
        _current_mode    = target_mode
        result["action"]  = "fan_mode_unsupported"
        logger.warning(
            "[HawaAI] Fan mode not supported — skipping (logical=%r, %s) "
            "supported=%s",
            target_fan_mode, _log_frag, supported,
        )
        return result

    cur_fan = cstate.get("fan_mode")
    cur_norm = str(cur_fan).lower() if cur_fan is not None else ""
    res_norm = str(resolved).lower()

    # Skip if AC already reports this *entity-native* mode
    if cur_norm and cur_norm == res_norm:
        _current_mode    = target_mode
        result["action"] = "skip_same"
        _last_fan_mode   = resolved
        logger.info(
            "[HawaAI] smart_cooling fan: skipped (AC already at %r, current=%r)",
            resolved, cur_fan,
        )
        return result

    # Compare to last *applied* entity-native value (HA state may lag one tick)
    if _last_fan_mode is not None and str(_last_fan_mode).lower() == res_norm:
        _current_mode    = target_mode
        result["action"] = "skip_same"
        logger.debug(
            "[HawaAI] Smart Mode: %s — last fan command was already %r, no resend",
            target_mode, resolved,
        )
        return result

    # ── Cooldown guard ────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    if _last_adjustment_time is not None:
        secs_since = (now - _last_adjustment_time).total_seconds()
        if secs_since < ADJUSTMENT_COOLDOWN:
            _current_mode    = target_mode
            result["action"] = "skip_cooldown"
            logger.debug(
                "[HawaAI] Smart Mode: %s — adjustment cooldown %.0fs / %ds",
                target_mode, secs_since, ADJUSTMENT_COOLDOWN,
            )
            return result

    # ── Apply fan mode (isolated: failure does not touch temperature path) ─────
    logger.info(
        "[HawaAI] Smart Mode: %s | Delta: %.1f°C | "
        "Reason: delta %s %.1f°C | applying climate.set_fan_mode → %r",
        target_mode, delta,
        ">=" if target_mode == "boost" else ">",
        BOOST_DELTA if target_mode == "boost" else HOLD_DELTA,
        resolved,
    )

    ok = await ha_client.call_service("climate", "set_fan_mode", {
        "entity_id": climate_entity,
        "fan_mode":  resolved,
    })

    if ok:
        _current_mode         = target_mode
        _last_fan_mode        = resolved
        _last_adjustment_time = now
        result["action"]      = "set_fan"
        logger.info(
            "[HawaAI] smart_cooling fan: applied %r (requested logical=%r) ✓",
            resolved, target_fan_mode,
        )
    else:
        _current_mode    = target_mode
        result["action"] = "set_fan_failed"
        # Do not set _last_fan_mode — will retry; temperature logic unaffected
        logger.error(
            "[HawaAI] smart_cooling fan: climate.set_fan_mode failed for %r on %s "
            "(logical was %r)",
            resolved, climate_entity, target_fan_mode,
        )

    return result


# ── Effective target dispatcher ───────────────────────────────────────────────

async def apply_effective_target(
    climate_entity:   str,
    effective_target: float,
    current_target:   Optional[float],
    ac_on:            bool,
    manual_override:  bool,
) -> str:
    """
    Safely push the smart-adjusted effective target temperature to the
    climate entity.

    This is the bridge between the logic engine's decision (effective_target)
    and the actual AC setpoint — without touching ON/OFF control.

    RULES (hard — never violate):
      - Only runs when AC is ON (compressor confirmed running)
      - Skips if manual_override is True
      - Minimum 180 s between consecutive commands (prevents spam)
      - Dead-band of 0.5°C — ignores tiny/noisy adjustments
      - No climate entity → no-op, returns diagnostic string
      - Never raises — all errors are logged and swallowed

    Returns a short diagnostic string (action label).

    Example log sequence when active:
      [HawaAI] Smart adj: outdoor=41.0°C → effective target 23.0°C (config=24.0°C)
      [HawaAI] Applied smart temp → 23.0°C
    """
    global _last_apply_target_time, _last_applied_target

    # ── Guards ────────────────────────────────────────────────────────────────

    if not climate_entity:
        return "no_climate_entity"

    if manual_override:
        return "manual_override"

    if not ac_on:
        return "ac_off"

    if current_target is None:
        return "no_current_target"

    try:
        current_f  = float(current_target)
        effective_f = round(float(effective_target), 1)
    except (TypeError, ValueError):
        return "parse_error"

    # Dead-band: skip if delta < 0.5°C to avoid hunting / noise
    if abs(current_f - effective_f) < 0.5:
        return "within_deadband"

    # Rate limiter: minimum 180 s between commands
    now = datetime.now(timezone.utc)
    if _last_apply_target_time is not None:
        secs = (now - _last_apply_target_time).total_seconds()
        if secs < _APPLY_TARGET_COOLDOWN:
            logger.debug(
                "[HawaAI] apply_effective_target: cooldown %.0fs / %ds",
                secs, _APPLY_TARGET_COOLDOWN,
            )
            return f"cooldown_{int(secs)}s"

    # ── Send command via HA climate service ───────────────────────────────────
    try:
        ok = await ha_client.set_climate_temperature(climate_entity, effective_f)
        if ok:
            _last_apply_target_time = now
            _last_applied_target    = effective_f
            logger.info(
                "[HawaAI] Applied smart temp → %.1f°C  "
                "(was %.1f°C on %s)",
                effective_f, current_f, climate_entity,
            )
            return "applied"
        else:
            logger.error(
                "[HawaAI] apply_effective_target failed for %s "
                "(%.1f°C → %.1f°C)",
                climate_entity, current_f, effective_f,
            )
            return "failed"
    except Exception as exc:
        logger.error("[HawaAI] apply_effective_target exception: %s", exc)
        return "error"
