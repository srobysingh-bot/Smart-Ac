"""Tuya climate control adapter."""

import asyncio
import logging
from typing import Optional

from . import ha_client

logger = logging.getLogger(__name__)


def _resolve_supported_fan_mode(requested: str, supported: object) -> Optional[str]:
    modes = [str(x).strip() for x in (supported or []) if str(x).strip()]
    if not modes:
        return None
    req = str(requested or "").strip()
    if not req:
        return None
    exact = next((m for m in modes if m == req), None)
    if exact:
        return exact
    low_map = {m.lower(): m for m in modes}
    return low_map.get(req.lower())


async def turn_on(
    entity_id: str,
    temperature: float,
    *,
    fan_mode: str = "auto",
    hvac_mode: str = "cool",
) -> bool:
    if not entity_id:
        logger.error("[HawaAI] Tuya ON failed: no climate entity configured")
        return False

    state = await ha_client.get_climate_state(entity_id)
    supported_fan = _resolve_supported_fan_mode(fan_mode, state.get("fan_modes"))
    current_fan = str(state.get("fan_mode") or "").strip().lower()
    current_hvac = str(state.get("state") or "").strip().lower()

    if current_hvac != hvac_mode.lower():
        logger.info(
            "[IR][tuya] step=set_hvac_mode entity=%s hvac=%s (was=%s)",
            entity_id,
            hvac_mode,
            current_hvac,
        )
        ok_mode = await ha_client.call_service(
            "climate",
            "set_hvac_mode",
            {
                "entity_id": entity_id,
                "hvac_mode": hvac_mode,
            },
        )
        if not ok_mode:
            logger.warning("[IR][tuya] step=set_hvac_mode failed entity=%s", entity_id)
            return False
        await asyncio.sleep(2.0)
    else:
        logger.info("[IR][tuya] step=set_hvac_mode skipped_already=%s", hvac_mode)

    ok_temp = await ha_client.call_service(
        "climate",
        "set_temperature",
        {
            "entity_id": entity_id,
            "temperature": float(temperature),
            "hvac_mode": hvac_mode,
        },
    )
    if not ok_temp:
        logger.warning("[IR][tuya] set_temperature failed before fan step")
        return False

    if supported_fan:
        if current_fan == supported_fan.lower():
            logger.info("[IR][tuya] step=set_fan_mode skipped_already=%s", supported_fan)
        else:
            ok_fan = await ha_client.call_service(
                "climate",
                "set_fan_mode",
                {
                    "entity_id": entity_id,
                    "fan_mode": supported_fan,
                },
            )
            if not ok_fan:
                logger.warning("[IR][tuya] step=set_fan_mode failed fan=%s", supported_fan)
    else:
        logger.info(
            "[IR][tuya] step=skip_fan_mode_unsupported requested=%s supported=%s",
            fan_mode,
            state.get("fan_modes") or [],
        )

    return True


async def turn_off(entity_id: str) -> bool:
    if not entity_id:
        logger.warning("[HawaAI] Tuya OFF skipped: no climate entity configured")
        return False

    return await ha_client.call_service(
        "climate",
        "set_hvac_mode",
        {
            "entity_id": entity_id,
            "hvac_mode": "off",
        },
    )
