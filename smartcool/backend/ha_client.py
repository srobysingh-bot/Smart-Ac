"""
Home Assistant WebSocket + REST client for SmartCool.

Maintains a persistent WebSocket connection to the HA supervisor,
subscribes to entity state changes, and exposes helpers for REST service calls.
"""

import asyncio
import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

HA_BASE_URL = os.environ.get("HA_BASE_URL", "http://supervisor/core")
_SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Emitted when an entity's state changes — (entity_id, new_state_str, attributes)
StateChangeCallback = Callable[[str, str, Dict[str, Any]], None]


class HAClient:
    """
    Async Home Assistant client.

    Usage:
        client = HAClient(token)
        await client.start()
        # ... subscribe / call services ...
        await client.stop()
    """

    def __init__(self, token: Optional[str] = None) -> None:
        # SUPERVISOR_TOKEN is injected by HA Supervisor into every add-on container
        # and is always valid for add-on → HA Core communication through the proxy.
        # A user-provided LLAT (ha_token) is used ONLY as a fallback for local dev
        # outside of the HA add-on context (where SUPERVISOR_TOKEN is absent).
        self._token = _SUPERVISOR_TOKEN or token or ""
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._msg_id: int = 1
        self._pending: Dict[int, asyncio.Future] = {}
        self._state_listeners: List[StateChangeCallback] = []
        self._entity_states: Dict[str, Dict[str, Any]] = {}
        self._running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._reconnect_delay = 5  # seconds

    # ── Public API ────────────────────────────────────────────────────────────

    def on_state_change(self, callback: StateChangeCallback) -> None:
        """Register a callback invoked whenever any entity state changes."""
        self._state_listeners.append(callback)

    async def start(self) -> None:
        """Begin WebSocket connection (auto-reconnects on disconnect)."""
        self._running = True
        self._ws_task = asyncio.create_task(self._connection_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Fetch latest known state (from cache or REST fallback)."""
        if entity_id in self._entity_states:
            return self._entity_states[entity_id]
        return await self._rest_get_state(entity_id)

    async def get_state_value(self, entity_id: str) -> Optional[str]:
        """Return just the state string for an entity."""
        state = await self.get_state(entity_id)
        return state.get("state") if state else None

    async def get_state_attribute(
        self, entity_id: str, attribute: str
    ) -> Optional[Any]:
        state = await self.get_state(entity_id)
        if state:
            return state.get("attributes", {}).get(attribute)
        return None

    async def list_entities(self, domain: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return all entities (optionally filtered by domain) via REST."""
        url = f"{HA_BASE_URL}/api/states"
        try:
            async with self._get_session().get(url, headers=self._headers()) as resp:
                resp.raise_for_status()
                states = await resp.json()
                if domain:
                    states = [s for s in states if s["entity_id"].startswith(f"{domain}.")]
                return states
        except Exception as exc:
            logger.error("list_entities failed: %s", exc)
            return []

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Call a HA service via REST. Returns True on success."""
        url = f"{HA_BASE_URL}/api/services/{domain}/{service}"
        payload = service_data or {}
        try:
            async with self._get_session().post(
                url, json=payload, headers=self._headers()
            ) as resp:
                if resp.status in (200, 201):
                    return True
                body = await resp.text()
                logger.error(
                    "Service %s.%s failed [%s]: %s", domain, service, resp.status, body
                )
                return False
        except Exception as exc:
            logger.error("call_service %s.%s exception: %s", domain, service, exc)
            return False

    async def turn_on(self, entity_id: str) -> bool:
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "turn_on", {"entity_id": entity_id})

    async def turn_off(self, entity_id: str) -> bool:
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "turn_off", {"entity_id": entity_id})

    async def send_ir_command(
        self, remote_entity: str, device: str, command: str
    ) -> bool:
        return await self.call_service(
            "remote",
            "send_command",
            {
                "entity_id": remote_entity,
                "device": device,
                "command": command,
            },
        )

    async def publish_sensor_state(
        self,
        entity_id: str,
        state: Any,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Push a synthetic sensor state to HA via the states REST API."""
        url = f"{HA_BASE_URL}/api/states/{entity_id}"
        payload: Dict[str, Any] = {"state": str(state)}
        if attributes:
            payload["attributes"] = attributes
        try:
            async with self._get_session().post(
                url, json=payload, headers=self._headers()
            ) as resp:
                return resp.status in (200, 201)
        except Exception as exc:
            logger.error("publish_sensor_state %s failed: %s", entity_id, exc)
            return False

    # ── WebSocket internals ───────────────────────────────────────────────────

    async def _connection_loop(self) -> None:
        while self._running:
            try:
                await self._connect_and_run()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "WebSocket disconnected (%s). Reconnecting in %ds…",
                    exc,
                    self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)

    async def _connect_and_run(self) -> None:
        ws_url = HA_BASE_URL.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/api/websocket"
        logger.info("Connecting to HA WebSocket at %s", ws_url)

        session = self._get_session()
        async with session.ws_connect(ws_url) as ws:
            self._ws = ws
            logger.info("WebSocket connected")

            # Auth handshake
            msg = await ws.receive_json()
            if msg.get("type") != "auth_required":
                raise RuntimeError(f"Unexpected WS greeting: {msg}")

            await ws.send_json({"type": "auth", "access_token": self._token})
            msg = await ws.receive_json()
            if msg.get("type") != "auth_ok":
                raise RuntimeError(f"WS auth failed: {msg}")

            logger.info("WebSocket authenticated")

            # Subscribe to state_changed events
            sub_id = self._next_id()
            await ws.send_json(
                {
                    "id": sub_id,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }
            )

            async for raw in ws:
                if raw.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(json.loads(raw.data))
                elif raw.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    logger.warning("WebSocket closed/error: %s", raw.type)
                    break

    async def _handle_message(self, msg: Dict[str, Any]) -> None:
        msg_type = msg.get("type")

        if msg_type == "event":
            event_data = msg.get("event", {}).get("data", {})
            entity_id = event_data.get("entity_id", "")
            new_state = event_data.get("new_state") or {}
            state_str = new_state.get("state", "")
            attributes = new_state.get("attributes", {})

            # Update cache
            self._entity_states[entity_id] = new_state

            # Notify listeners
            for cb in self._state_listeners:
                try:
                    cb(entity_id, state_str, attributes)
                except Exception as exc:
                    logger.error("State listener error for %s: %s", entity_id, exc)

        elif msg_type == "result":
            msg_id = msg.get("id")
            if msg_id in self._pending:
                fut = self._pending.pop(msg_id)
                if msg.get("success"):
                    fut.set_result(msg.get("result"))
                else:
                    fut.set_exception(
                        RuntimeError(msg.get("error", {}).get("message", "unknown"))
                    )

    # ── REST fallback ─────────────────────────────────────────────────────────

    async def _rest_get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        url = f"{HA_BASE_URL}/api/states/{entity_id}"
        try:
            async with self._get_session().get(url, headers=self._headers()) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._entity_states[entity_id] = data
                    return data
                return None
        except Exception as exc:
            logger.error("REST get_state %s failed: %s", entity_id, exc)
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id
