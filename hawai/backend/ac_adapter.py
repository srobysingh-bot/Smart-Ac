"""
HawaAI AC Adapter — Aerostate (climate entity) control layer.

ALL AC on/off commands from HawaAI flow through this module.
It calls HA climate services, which drive Broadlink → physical AC.

Pipeline:
  HawaAI Logic Engine → ac_adapter → Aerostate (climate.xxx) → Broadlink → AC

Rules:
  - Never sends IR directly; that is Aerostate's responsibility.
  - Reads current entity state before every call to prevent command spam.
  - Returns True on success or no-op, False on error.
  - Always logs at INFO level so every command is traceable.
"""

import logging
from typing import Optional

from . import ha_client

logger = logging.getLogger(__name__)

# Minimum temperature delta to warrant a set_temperature call (°C).
# Wider band avoids duplicate ON / setpoint spam when Aerostate already matches.
_TEMP_DEAD_BAND: float = 1.0


async def turn_on(
    entity_id: str,
    temperature: float,
    fan_mode: str = "auto",
    hvac_mode: str = "cool",
) -> bool:
    """
    Turn AC ON via the Aerostate climate entity.

    Sequence:
      1. Read current state — skip if already at desired state (spam prevention)
      2. set_hvac_mode → hvac_mode   (if mode differs)
      3. set_temperature → temperature  (if delta ≥ 0.5°C)
      4. set_fan_mode → fan_mode        (if fan differs, and fan_mode is given)

    Returns True if all needed service calls succeeded (or were skipped as no-ops).
    Returns False if any call failed; caller should NOT mark AC as ON.
    """
    if not entity_id:
        logger.error(
            "[HawaAI] ac_adapter.turn_on: no climate entity configured — "
            "set 'climate_entity' in Settings"
        )
        return False

    # ── Read current state (spam prevention) ─────────────────────────────────
    state = await ha_client.get_climate_state(entity_id)
    current_mode = state.get("state", "off")
    current_temp = state.get("target_temp")   # setpoint, not measured temp
    current_fan  = state.get("fan_mode")

    # Strong guard: already in desired mode and setpoint within deadband → no HA calls
    if current_mode == hvac_mode:
        if current_temp is not None and abs(current_temp - temperature) < _TEMP_DEAD_BAND:
            fan_ok_early = (current_fan == fan_mode) if fan_mode else True
            if fan_ok_early:
                logger.info(
                    "[HawaAI] Aerostate skip — mode=%s setpoint within %.1f°C (no command)",
                    hvac_mode, _TEMP_DEAD_BAND,
                )
                return True

    already_on = current_mode not in ("off", "unavailable", "unknown", "")
    temp_ok    = (
        current_temp is not None
        and abs(current_temp - temperature) < _TEMP_DEAD_BAND
    )
    fan_ok = (current_fan == fan_mode) if fan_mode else True

    if already_on and temp_ok and fan_ok:
        logger.debug(
            "[HawaAI] Aerostate already mode=%s temp=%.1f fan=%s — no command needed",
            current_mode, current_temp, current_fan,
        )
        return True

    logger.info(
        "[HawaAI] Control → Aerostate | mode=%s | temp=%.1f°C | fan=%s",
        hvac_mode, temperature, fan_mode,
    )

    ok = True

    # Step A — set HVAC mode (turns the unit on if it was off)
    if current_mode != hvac_mode:
        r = await ha_client.call_service("climate", "set_hvac_mode", {
            "entity_id": entity_id,
            "hvac_mode": hvac_mode,
        })
        if not r:
            logger.error("[HawaAI] Aerostate set_hvac_mode=%s FAILED", hvac_mode)
        ok = ok and r

    # Step B — set temperature setpoint
    if not temp_ok:
        r = await ha_client.set_climate_temperature(entity_id, temperature, mode=hvac_mode)
        if not r:
            logger.error("[HawaAI] Aerostate set_temperature=%.1f FAILED", temperature)
        ok = ok and r

    # Step C — set fan mode
    if fan_mode and not fan_ok:
        r = await ha_client.call_service("climate", "set_fan_mode", {
            "entity_id": entity_id,
            "fan_mode":  fan_mode,
        })
        if not r:
            logger.error("[HawaAI] Aerostate set_fan_mode=%s FAILED", fan_mode)
        ok = ok and r

    if ok:
        logger.info(
            "[HawaAI] Aerostate ON ✓ | mode=%s | temp=%.1f°C | fan=%s",
            hvac_mode, temperature, fan_mode,
        )
    else:
        logger.error(
            "[HawaAI] Aerostate ON FAILED | mode=%s | temp=%.1f°C | fan=%s",
            hvac_mode, temperature, fan_mode,
        )

    return ok


async def turn_off(entity_id: str) -> bool:
    """
    Turn AC OFF via the Aerostate climate entity.

    Reads current state first — skips call if already off.
    Returns True on success or no-op, False on error.
    Even on failure, the caller (_turn_ac_off) will still mark the
    internal flag as OFF to prevent a stuck-ON state.
    """
    if not entity_id:
        logger.warning(
            "[HawaAI] ac_adapter.turn_off: no climate entity configured — "
            "internal state will be marked OFF without sending a command"
        )
        return False

    # ── Read current state (spam prevention) ─────────────────────────────────
    state = await ha_client.get_climate_state(entity_id)
    current_mode = state.get("state", "off")

    if current_mode in ("off", "unavailable", "unknown", ""):
        logger.debug("[HawaAI] Aerostate already OFF — no command needed")
        return True

    logger.info("[HawaAI] Control → Aerostate OFF (current mode=%s)", current_mode)

    ok = await ha_client.call_service("climate", "set_hvac_mode", {
        "entity_id": entity_id,
        "hvac_mode": "off",
    })

    if ok:
        logger.info("[HawaAI] Aerostate OFF ✓")
    else:
        logger.error("[HawaAI] Aerostate OFF FAILED — marking OFF internally anyway")

    return ok
