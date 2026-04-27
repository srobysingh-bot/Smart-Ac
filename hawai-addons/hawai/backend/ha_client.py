"""
Home Assistant REST + WebSocket client for HawaAI.

Inside the HA addon container:
  - HA Core REST API:  http://supervisor/core/api/...
  - HA Core WebSocket: ws://supervisor/core/api/websocket
  - Auth token injected by HA Supervisor as SUPERVISOR_TOKEN env var

The device registry and entity registry are ONLY accessible via WebSocket
(config/device_registry/list, config/entity_registry/list).
They do NOT have REST equivalents — any REST call to those paths returns 404.
"""

import asyncio
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


async def get_entity_state_full(entity_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch the full HA state object for an entity, including all attributes.
    Returns {"state": "...", "attributes": {...}} or None on error.
    Needed for climate entities where attributes carry temperature, modes, etc.
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
                    return {
                        "state":      data.get("state"),
                        "attributes": data.get("attributes", {}),
                    }
                body = await resp.text()
                logger.error("[HawaAI] get_entity_state_full(%s) HTTP %s: %s", entity_id, resp.status, body)
                return None
    except Exception as e:
        logger.error("[HawaAI] get_entity_state_full(%s) exception: %s", entity_id, e)
        return None


async def get_climate_state(entity_id: str) -> Dict[str, Any]:
    """
    Fetch live state of a HA climate entity, returning a flat dict ready for
    the logic engine and API status response.

    Returns {} (empty dict) on error so callers can safely call .get() on it.
    """
    full = await get_entity_state_full(entity_id)
    if not full:
        return {}
    attrs  = full.get("attributes", {})
    state  = full.get("state") or "off"
    is_on  = state not in ("off", "unavailable", "unknown")
    raw_fan_modes = attrs.get("fan_modes") or attrs.get("fan_mode_list")
    if isinstance(raw_fan_modes, (list, tuple)):
        fan_modes: List[str] = [str(x) for x in raw_fan_modes if x is not None]
    else:
        fan_modes = []

    return {
        "state":        state,
        "current_temp": attrs.get("current_temperature"),
        "target_temp":  attrs.get("temperature"),
        "mode":         state,                  # for climate entities state == hvac_mode
        "fan_mode":     attrs.get("fan_mode"),
        "fan_modes":    fan_modes,             # supported fan speeds for smart_cooling mapping
        "swing_mode":   attrs.get("swing_mode"),
        "is_on":        is_on,
    }


async def set_climate_temperature(
    entity_id: str, temperature: float, mode: str = "cool"
) -> bool:
    """Set AC target temperature via HA climate.set_temperature service."""
    return await call_service("climate", "set_temperature", {
        "entity_id":  entity_id,
        "temperature": temperature,
        "hvac_mode":   mode,
    })


async def set_climate_mode(entity_id: str, mode: str) -> bool:
    """Turn AC on/off or switch HVAC mode via HA climate services."""
    if mode == "off":
        return await call_service("climate", "turn_off", {"entity_id": entity_id})
    return await call_service("climate", "set_hvac_mode", {
        "entity_id": entity_id,
        "hvac_mode": mode,
    })


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


_WS_URL = "ws://supervisor/core/api/websocket"


async def _ws_command(command_type: str) -> Optional[Any]:
    """
    Execute a single command against the HA Core WebSocket API and return
    the result payload, or None on failure.

    Protocol:
      1. Connect to ws://supervisor/core/api/websocket
      2. Receive {"type": "auth_required"}
      3. Send    {"type": "auth", "access_token": TOKEN}
      4. Receive {"type": "auth_ok"}
      5. Send    {"id": 1, "type": command_type}
      6. Receive {"id": 1, "type": "result", "success": true, "result": [...]}
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                _WS_URL,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as ws:
                # 1 — auth_required
                msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
                if msg.get("type") != "auth_required":
                    logger.error("[HawaAI] WS: expected auth_required, got %r", msg.get("type"))
                    return None

                # 2 — authenticate
                await ws.send_json({"type": "auth", "access_token": _TOKEN})
                msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
                if msg.get("type") != "auth_ok":
                    logger.error("[HawaAI] WS: auth failed — %s", msg.get("message", msg))
                    return None

                # 3 — send command
                await ws.send_json({"id": 1, "type": command_type})

                # 4 — receive result
                msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
                if not msg.get("success"):
                    err = msg.get("error", {})
                    logger.error(
                        "[HawaAI] WS %s failed: %s %s",
                        command_type, err.get("code"), err.get("message"),
                    )
                    return None

                return msg.get("result")

    except asyncio.TimeoutError:
        logger.error("[HawaAI] WS command %s timed out", command_type)
        return None
    except Exception as e:
        logger.error("[HawaAI] WS command %s exception: %s", command_type, e)
        return None


async def get_device_registry() -> List[Dict[str, Any]]:
    """
    Returns all HA devices from the device registry via WebSocket.

    REST /api/config/device_registry/list does NOT exist — this MUST go
    through the Core WebSocket API (config/device_registry/list).
    """
    result = await _ws_command("config/device_registry/list")
    if result is None:
        return []
    # HA returns the list directly as result
    if isinstance(result, list):
        return result
    # Some HA versions wrap it in {"devices": [...]}
    if isinstance(result, dict):
        return result.get("devices", [])
    return []


async def get_entity_registry() -> List[Dict[str, Any]]:
    """
    Returns all HA entity registry entries (includes device_id) via WebSocket.

    REST /api/config/entity_registry/list does NOT exist — this MUST go
    through the Core WebSocket API (config/entity_registry/list).
    """
    result = await _ws_command("config/entity_registry/list")
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("entities", [])
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
