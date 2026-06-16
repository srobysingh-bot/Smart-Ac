"""Tuya climate control adapter."""

import asyncio
import logging
from typing import Optional

from . import ha_client

logger = logging.getLogger(__name__)

TUYA_SETTLE_DELAY_SECONDS = 2.0


def _resolve_supported_mode(requested: str, supported: object) -> Optional[str]:
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


def _resolve_supported_fan_mode(requested: str, supported: object) -> Optional[str]:
    return _resolve_supported_mode(requested, supported)


def _resolve_supported_swing_mode(requested: object, supported: object) -> Optional[str]:
    return _resolve_supported_mode(str(requested or ""), supported)


def _supports_full_state_on(state: dict) -> bool:
    return bool(
        state.get("full_state_on_supported")
        or state.get("supports_full_state_on")
        or state.get("tuya_full_state_on_supported")
    )


def _same_temperature(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    try:
        return abs(round(float(left), 1) - round(float(right), 1)) < 0.05
    except (TypeError, ValueError):
        return False


def _temperature_command_needed(
    desired: float,
    current_temperature: object,
    last_commanded_temperature: object,
) -> bool:
    if _same_temperature(desired, current_temperature):
        return False
    if _same_temperature(desired, last_commanded_temperature):
        return False
    return True


async def _send_explicit_cool_power_on(entity_id: str, hvac_mode: str) -> bool:
    return await ha_client.call_service(
        "climate",
        "set_hvac_mode",
        {
            "entity_id": entity_id,
            "hvac_mode": hvac_mode,
        },
    )


async def turn_on(
    entity_id: str,
    temperature: float,
    *,
    fan_mode: Optional[str] = None,
    hvac_mode: str = "cool",
    swing_mode: Optional[str] = None,
    last_commanded_temperature: Optional[float] = None,
    force_physical_on: bool = False,
    physical_power_watts: Optional[float] = None,
) -> bool:
    if not entity_id:
        logger.error("[HawaAI] Tuya ON failed: no climate entity configured")
        return False

    state = await ha_client.get_climate_state(entity_id)
    supported_fan = (
        _resolve_supported_fan_mode(fan_mode, state.get("fan_modes"))
        if fan_mode is not None
        else None
    )
    supported_swing = (
        _resolve_supported_swing_mode(swing_mode, state.get("swing_modes"))
        if swing_mode is not None
        else None
    )
    current_fan = str(state.get("fan_mode") or "").strip().lower()
    current_hvac = str(state.get("state") or "").strip().lower()
    current_swing = str(state.get("swing_mode") or "").strip().lower()
    current_temperature = state.get("target_temp")
    desired_temperature = float(temperature)
    full_state_on = _supports_full_state_on(state)
    fan_changed = bool(supported_fan and current_fan != supported_fan.lower())
    swing_changed = bool(supported_swing and current_swing != supported_swing.lower())
    temp_changed = _temperature_command_needed(
        desired_temperature,
        current_temperature,
        last_commanded_temperature,
    )

    if force_physical_on:
        logger.warning(
            "[CONTROL][tuya] force_physical_on ha_mode=%s power=%s",
            current_hvac or "unknown",
            f"{float(physical_power_watts):.0f}" if physical_power_watts is not None else "n/a",
        )

    should_send_power_on = force_physical_on or current_hvac != hvac_mode.lower()
    if full_state_on:
        selected_fan = supported_fan or state.get("fan_mode")
        selected_swing = supported_swing or state.get("swing_mode")
        combined_payload = {
            "entity_id": entity_id,
            "power_on": True,
            "temperature": desired_temperature,
            "hvac_mode": hvac_mode,
        }
        if selected_fan:
            combined_payload["fan_mode"] = selected_fan
        if selected_swing:
            combined_payload["swing_mode"] = selected_swing
        logger.info(
            "[CONTROL][tuya] send combined mode=%s temp=%s fan=%s swing=%s",
            hvac_mode,
            desired_temperature,
            selected_fan or "preserve",
            selected_swing or "preserve",
        )
        return await ha_client.call_service("climate", "set_temperature", combined_payload)

    if should_send_power_on:
        logger.info(
            "[IR][tuya] step=set_hvac_mode entity=%s hvac=%s (was=%s)",
            entity_id,
            hvac_mode,
            current_hvac,
        )
        logger.info("[CONTROL][tuya] send power_on")
        ok_mode = await _send_explicit_cool_power_on(entity_id, hvac_mode)
        if not ok_mode:
            logger.warning("[IR][tuya] step=set_hvac_mode failed entity=%s", entity_id)
            return False
        await asyncio.sleep(TUYA_SETTLE_DELAY_SECONDS)
    else:
        logger.info("[IR][tuya] step=set_hvac_mode skipped_already=%s", hvac_mode)

    if temp_changed:
        logger.info("[CONTROL][tuya] send temperature temp=%s", desired_temperature)
        ok_temp = await ha_client.call_service(
            "climate",
            "set_temperature",
            {
                "entity_id": entity_id,
                "temperature": desired_temperature,
                "hvac_mode": hvac_mode,
            },
        )
        if not ok_temp:
            logger.warning("[IR][tuya] set_temperature failed before fan step")
            return False
    else:
        logger.info(
            "[IR][tuya] step=set_temperature skipped_unchanged current=%s last=%s desired=%s",
            current_temperature,
            last_commanded_temperature,
            desired_temperature,
        )

    if supported_fan:
        if fan_changed:
            logger.info("[IR][tuya] step=set_fan_mode fan=%s", supported_fan)
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
                return False
        else:
            logger.info("[IR][tuya] step=set_fan_mode skipped_unchanged=%s", supported_fan)
    elif fan_mode is not None:
        logger.info(
            "[IR][tuya] step=skip_fan_mode_unsupported requested=%s supported=%s",
            fan_mode,
            state.get("fan_modes") or [],
        )
    if supported_swing:
        if swing_changed:
            logger.info("[IR][tuya] step=set_swing_mode swing=%s", supported_swing)
            ok_swing = await ha_client.call_service(
                "climate",
                "set_swing_mode",
                {
                    "entity_id": entity_id,
                    "swing_mode": supported_swing,
                },
            )
            if not ok_swing:
                logger.warning("[IR][tuya] step=set_swing_mode failed swing=%s", supported_swing)
                return False
        else:
            logger.info("[IR][tuya] step=set_swing_mode skipped_unchanged=%s", supported_swing)
    elif swing_mode is not None:
        logger.info(
            "[IR][tuya] step=skip_swing_mode_unsupported requested=%s supported=%s",
            swing_mode,
            state.get("swing_modes") or [],
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
