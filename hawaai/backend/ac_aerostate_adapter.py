"""AeroState climate control adapter.

Broadlink-backed ACs should be exposed to Home Assistant as AeroState climate
entities. AeroState owns the IR details, so this adapter sends one climate
command for ON.
"""

import logging

from . import ha_client

logger = logging.getLogger(__name__)


async def turn_on(entity_id: str, temperature: float) -> bool:
    if not entity_id:
        logger.error("[HawaAI] AeroState ON failed: no climate entity configured")
        return False

    state = await ha_client.get_climate_state(entity_id)
    current_hvac = state.get("state") if state else None

    ok_primary = await ha_client.call_service(
        "climate",
        "set_temperature",
        {
            "entity_id": entity_id,
            "hvac_mode": "cool",
            "temperature": float(temperature),
        },
        blocking=True,
    )
    if ok_primary:
        logger.info(
            "[IR][aerostate] primary_on_sent entity=%s temp=%.1f",
            entity_id,
            float(temperature),
        )
    else:
        logger.error("[IR][aerostate] primary_on_failed entity=%s", entity_id)

    if current_hvac in (None, "off"):
        ok_fallback = await ha_client.call_service(
            "climate",
            "set_hvac_mode",
            {
                "entity_id": entity_id,
                "hvac_mode": "cool",
            },
            blocking=True,
        )
        if ok_fallback:
            logger.info("[IR][aerostate] fallback_hvac_mode_sent entity=%s", entity_id)
        else:
            logger.error("[IR][aerostate] fallback_hvac_mode_failed entity=%s", entity_id)
        return bool(ok_primary or ok_fallback)

    return bool(ok_primary)


async def turn_off(entity_id: str) -> bool:
    if not entity_id:
        logger.warning("[HawaAI] AeroState OFF skipped: no climate entity configured")
        return False

    ok = await ha_client.call_service(
        "climate",
        "set_hvac_mode",
        {
            "entity_id": entity_id,
            "hvac_mode": "off",
        },
        blocking=True,
    )
    if ok:
        logger.info("[IR][aerostate] OFF entity=%s", entity_id)
    else:
        logger.error("[IR][aerostate] OFF failed entity=%s", entity_id)
    return ok
