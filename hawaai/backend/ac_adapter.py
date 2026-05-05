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

import asyncio
import logging
from typing import Optional

from . import ha_client

logger = logging.getLogger(__name__)

# Minimum temperature delta to warrant a set_temperature call (°C).
# Matches logic_engine meaningful_setpoint_delta_deg / anti-chatter.
_TEMP_DEAD_BAND: float = 0.5


async def turn_on(
    entity_id: str,
    temperature: float,
    fan_mode: str = "auto",
    hvac_mode: str = "cool",
) -> bool:
    """
    Turn AC ON via the Aerostate climate entity.

    Broadlink/AeroState ON requires staged commands:
      1) set_hvac_mode("cool")
      2) wait 2s so the AC accepts the ON packet
      3) set_temperature(target)

    Returns True if a service call succeeded (or if it was a no-op due to current state).
    Returns False if either staged call failed.
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
        "[AC_ADAPTER] Sending ON → entity=%s temp=%.1f fan=%s hvac=%s",
        entity_id,
        float(temperature),
        (fan_mode or "auto"),
        hvac_mode,
    )

    # Fan mode can break Tuya IR profiles if unsupported or wrong casing.
    # Only include fan_mode when it appears supported by the entity.
    use_fan: Optional[str] = None
    try:
        supported = state.get("fan_modes") or []
        supported = [str(x) for x in supported if x is not None]
        req = (fan_mode or "").strip()
        if req:
            if req in supported:
                use_fan = req
            else:
                low_map = {str(x).strip().lower(): str(x) for x in supported if str(x).strip()}
                if req.lower() in low_map:
                    use_fan = low_map[req.lower()]
    except Exception:
        use_fan = None

    payload_mode = {
        "entity_id": entity_id,
        "hvac_mode": hvac_mode,
    }
    payload_temp = {
        "entity_id": entity_id,
        "temperature": float(temperature),
    }
    if use_fan:
        payload_temp["fan_mode"] = use_fan

    if already_on:
        ok = await ha_client.call_service(
            "climate",
            "set_temperature",
            payload_temp,
            blocking=True,
        )
    else:
        ok_mode = await ha_client.call_service(
            "climate",
            "set_hvac_mode",
            payload_mode,
            blocking=True,
        )
        if not ok_mode:
            ok = False
        else:
            await asyncio.sleep(2.0)
            ok = await ha_client.call_service(
                "climate",
                "set_temperature",
                payload_temp,
                blocking=True,
            )
    if ok:
        logger.info(
            "[HawaAI] Aerostate ON ✓ | mode=%s | temp=%.1f°C | fan=%s",
            hvac_mode,
            float(temperature),
            use_fan or fan_mode,
        )
    else:
        logger.error(
            "[HawaAI] Aerostate ON FAILED | mode=%s | temp=%.1f°C | fan=%s",
            hvac_mode,
            float(temperature),
            use_fan or fan_mode,
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
