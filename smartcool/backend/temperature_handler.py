"""Indoor temperature polling from a HA sensor entity."""

import logging
from typing import Optional

from . import config_manager
from .ha_client import HAClient

logger = logging.getLogger(__name__)


class TemperatureHandler:
    """
    Reads the indoor temperature from the configured HA sensor.
    Updates automatically via WebSocket state_changed events and
    can also be polled manually.
    """

    def __init__(self, ha: HAClient) -> None:
        self._ha = ha
        self._indoor_temp: Optional[float] = None
        ha.on_state_change(self._on_state_change)

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def indoor_temp(self) -> Optional[float]:
        return self._indoor_temp

    async def refresh(self) -> Optional[float]:
        """Fetch latest indoor temperature from HA."""
        entity = config_manager.get("indoor_temp_entity")
        if not entity:
            return None

        state_str = await self._ha.get_state_value(entity)
        if state_str is None:
            logger.warning("Indoor temp entity %s not available", entity)
            return self._indoor_temp

        try:
            self._indoor_temp = float(state_str)
        except ValueError:
            logger.warning("Could not parse indoor temp '%s' from %s", state_str, entity)

        return self._indoor_temp

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_state_change(self, entity_id: str, state: str, _attrs: dict) -> None:
        configured = config_manager.get("indoor_temp_entity")
        if entity_id != configured:
            return
        try:
            self._indoor_temp = float(state)
            logger.debug("Indoor temp updated: %.1f°C", self._indoor_temp)
        except (ValueError, TypeError):
            pass
