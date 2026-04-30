"""
Room-scoped WebSocket push hooks. Implementation is registered from ``main`` lifespan.

``logic_engine.tick`` calls :func:`broadcast_room_update` so event-triggered ticks
wake the dashboard without waiting for the periodic 5s broadcast loop.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_broadcast_room: Optional[Callable[[str], Awaitable[None]]] = None


def register_room_broadcast(impl: Callable[[str], Awaitable[None]]) -> None:
    global _broadcast_room
    _broadcast_room = impl


async def broadcast_room_update(room_id_canonical: str) -> None:
    canon = (room_id_canonical or "").strip().lower()
    if not canon:
        return
    impl = _broadcast_room
    if impl is None:
        return
    try:
        await impl(canon)
    except Exception:
        logger.exception("[WS_BROADCAST][%s] broadcast failed", canon)
