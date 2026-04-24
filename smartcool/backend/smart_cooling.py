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
from typing import Any, Dict, Optional

from . import ha_client

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
BOOST_DELTA         = 4.0    # °C: delta ≥ this → boost
HOLD_DELTA          = 1.5    # °C: delta ≤ this → hold (no change)
ADJUSTMENT_COOLDOWN = 120    # seconds minimum between fan mode changes

FAN_BOOST  = "high"
FAN_NORMAL = "auto"

# ── Module-level state (in-memory, never persisted) ───────────────────────────
_current_mode:        str               = "hold"
_last_adjustment_time: Optional[datetime] = None
_last_fan_mode:        Optional[str]     = None


# ── Public accessors ──────────────────────────────────────────────────────────

def get_state() -> Dict[str, Any]:
    """Returns current smart cooling state for /api/status and /api/runtime."""
    return {
        "smart_mode":     _current_mode,
        "smart_fan_mode": _last_fan_mode,
    }


def reset() -> None:
    """Reset state when a session ends or AC is turned off."""
    global _current_mode, _last_adjustment_time, _last_fan_mode
    _current_mode         = "hold"
    _last_adjustment_time = None
    _last_fan_mode        = None


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
    result["fan_mode"] = target_fan_mode

    # ── Skip-same guard ───────────────────────────────────────────────────────
    if _last_fan_mode == target_fan_mode:
        _current_mode    = target_mode
        result["action"] = "skip_same"
        logger.debug(
            "[HawaAI] Smart Mode: %s — fan already '%s', no resend",
            target_mode, target_fan_mode,
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

    # ── Apply fan mode via HA climate service ─────────────────────────────────
    logger.info(
        "[HawaAI] Smart Mode: %s | Delta: %.1f°C | Fan set to: %s | "
        "Reason: delta %s %.1f°C threshold",
        target_mode, delta, target_fan_mode,
        ">=" if target_mode == "boost" else ">",
        BOOST_DELTA if target_mode == "boost" else HOLD_DELTA,
    )

    ok = await ha_client.call_service("climate", "set_fan_mode", {
        "entity_id": climate_entity,
        "fan_mode":  target_fan_mode,
    })

    if ok:
        _current_mode         = target_mode
        _last_fan_mode        = target_fan_mode
        _last_adjustment_time = now
        result["action"]      = "set_fan"
        logger.info("[HawaAI] Fan set to: %s ✓", target_fan_mode)
    else:
        result["action"] = "set_fan_failed"
        logger.error(
            "[HawaAI] Smart Mode: failed to set fan mode '%s' on %s",
            target_fan_mode, climate_entity,
        )

    return result
