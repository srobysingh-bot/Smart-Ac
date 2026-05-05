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

    ok = await ha_client.call_service(
        "climate",
        "set_temperature",
        {
            "entity_id": entity_id,
            "hvac_mode": "cool",
            "temperature": float(temperature),
        },
        blocking=True,
    )
    if ok:
        logger.info("[IR][aerostate] ON entity=%s temp=%.1f", entity_id, float(temperature))
    else:
        logger.error("[IR][aerostate] ON failed entity=%s", entity_id)
    return ok


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
