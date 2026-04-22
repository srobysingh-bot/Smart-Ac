"""Presence detection handler using any HA binary_sensor."""

import logging
from datetime import datetime, timezone
from typing import Optional

from . import config_manager
from .ha_client import HAClient

logger = logging.getLogger(__name__)


class PresenceHandler:
    """
    Tracks occupancy state and vacancy duration.

    Presence is derived from whatever entity the user configured
    (typically an Aqara FP2 or any binary_sensor).
    """

    def __init__(self, ha: HAClient) -> None:
        self._ha = ha
        self._occupied: bool = False
        self._vacancy_since: Optional[datetime] = None
        ha.on_state_change(self._on_state_change)

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_occupied(self) -> bool:
        return self._occupied

    @property
    def vacancy_minutes(self) -> float:
        """Minutes since the room became vacant, or 0 if occupied."""
        if self._occupied or self._vacancy_since is None:
            return 0.0
        delta = datetime.now(timezone.utc) - self._vacancy_since
        return delta.total_seconds() / 60.0

    async def refresh(self) -> bool:
        """Poll the entity state and update internal state. Returns occupied flag."""
        entity = config_manager.get("presence_entity")
        if not entity:
            return True  # No entity configured — assume occupied

        state_str = await self._ha.get_state_value(entity)
        if state_str is None:
            logger.warning("Presence entity %s not found in HA", entity)
            return self._occupied  # Return cached value

        occupied = state_str.lower() in ("on", "home", "detected", "occupied")
        self._set_occupied(occupied)
        return self._occupied

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_state_change(self, entity_id: str, state: str, _attrs: dict) -> None:
        configured = config_manager.get("presence_entity")
        if entity_id != configured:
            return
        occupied = state.lower() in ("on", "home", "detected", "occupied")
        self._set_occupied(occupied)
        logger.debug("Presence state updated: %s → %s", entity_id, state)

    def _set_occupied(self, occupied: bool) -> None:
        if occupied:
            self._occupied = True
            self._vacancy_since = None
        elif self._occupied:  # transition occupied → vacant
            self._occupied = False
            self._vacancy_since = datetime.now(timezone.utc)
            logger.info("Room became vacant at %s", self._vacancy_since.isoformat())
