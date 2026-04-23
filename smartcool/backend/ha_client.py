"""
Home Assistant REST client for HawaAI.

Inside the HA addon container:
  - HA Core is reachable at http://supervisor/core
  - The auth token is injected by HA Supervisor as SUPERVISOR_TOKEN env var
"""

import os
import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

HA_BASE_URL = "http://supervisor/core"
_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type": "application/json",
    }


async def get_state(entity_id: str) -> Optional[str]:
    """
    Fetch the current state of a HA entity.
    Returns the state string (e.g. "on", "off", "29.4") or None on error.
    """
    if not entity_id:
        return None
    url = f"{HA_BASE_URL}/api/states/{entity_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=_headers(), timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    state = data.get("state")
                    logger.debug("[HawaAI] %s = %s", entity_id, state)
                    return state
                body = await resp.text()
                logger.error("[HawaAI] get_state(%s) HTTP %s: %s", entity_id, resp.status, body)
                return None
    except Exception as e:
        logger.error("[HawaAI] get_state(%s) exception: %s", entity_id, e)
        return None


async def get_all_entities() -> List[Dict[str, Any]]:
    """Returns all HA entity states — used to populate Settings dropdowns."""
    url = f"{HA_BASE_URL}/api/states"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=_headers(), timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error("[HawaAI] get_all_entities HTTP %s", resp.status)
                return []
    except Exception as e:
        logger.error("[HawaAI] get_all_entities exception: %s", e)
        return []


async def call_service(domain: str, service: str, data: Dict[str, Any]) -> bool:
    """Call a HA service (e.g. switch.turn_on, remote.send_command)."""
    url = f"{HA_BASE_URL}/api/services/{domain}/{service}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=_headers(),
                json=data,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                ok = resp.status in (200, 201)
                if not ok:
                    body = await resp.text()
                    logger.error(
                        "[HawaAI] call_service %s.%s HTTP %s: %s",
                        domain, service, resp.status, body,
                    )
                return ok
    except Exception as e:
        logger.error("[HawaAI] call_service %s.%s exception: %s", domain, service, e)
        return False


async def turn_on_ac(switch_entity: str) -> bool:
    domain = switch_entity.split(".")[0] if "." in switch_entity else "switch"
    return await call_service(domain, "turn_on", {"entity_id": switch_entity})


async def turn_off_ac(switch_entity: str) -> bool:
    domain = switch_entity.split(".")[0] if "." in switch_entity else "switch"
    return await call_service(domain, "turn_off", {"entity_id": switch_entity})


async def send_broadlink_command(remote_entity: str, command: str, device_name: str = "") -> bool:
    """
    Send a learned IR command via Broadlink RM device.

    CRITICAL:
      - 'command' must be a LIST — plain string is silently ignored by HA.
      - 'device' must match the device name used when the command was learned.
        Without it, HA searches at root level → not found → HTTP 500.
        Omit only if commands were learned at root (no device name).
    """
    if not remote_entity or not command:
        logger.error("[HawaAI] send_broadlink_command: missing entity=%r or command=%r", remote_entity, command)
        return False

    payload: Dict[str, Any] = {
        "entity_id": remote_entity,
        "command": [command],   # must be a list
        "num_repeats": 1,
        "delay_secs": 0.4,
    }
    if device_name:
        payload["device"] = device_name   # required when commands were learned under a device name

    logger.info(
        "[HawaAI] IR send: entity=%s device=%s command=%s",
        remote_entity, device_name or "none", command,
    )
    success = await call_service("remote", "send_command", payload)
    if success:
        logger.info("[HawaAI] IR '%s' sent OK", command)
    else:
        logger.error(
            "[HawaAI] IR '%s' FAILED — verify device name '%s' matches what was used during learning",
            command, device_name,
        )
    return success


async def get_device_registry() -> List[Dict[str, Any]]:
    """Returns all HA devices from the device registry."""
    url = f"{HA_BASE_URL}/api/config/device_registry/list"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=_headers(), timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error("[HawaAI] get_device_registry HTTP %s", resp.status)
                return []
    except Exception as e:
        logger.error("[HawaAI] get_device_registry exception: %s", e)
        return []


async def get_entity_registry() -> List[Dict[str, Any]]:
    """Returns all HA entity registry entries (includes device_id per entity)."""
    url = f"{HA_BASE_URL}/api/config/entity_registry/list"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=_headers(), timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error("[HawaAI] get_entity_registry HTTP %s", resp.status)
                return []
    except Exception as e:
        logger.error("[HawaAI] get_entity_registry exception: %s", e)
        return []


async def publish_sensor_state(
    entity_id: str, state: Any, attributes: Optional[Dict[str, Any]] = None
) -> bool:
    """Push a synthetic sensor state to HA via the states REST API."""
    url = f"{HA_BASE_URL}/api/states/{entity_id}"
    payload: Dict[str, Any] = {"state": str(state)}
    if attributes:
        payload["attributes"] = attributes
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=_headers(), json=payload, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                return resp.status in (200, 201)
    except Exception as e:
        logger.error("[HawaAI] publish_sensor_state %s failed: %s", entity_id, e)
        return False
