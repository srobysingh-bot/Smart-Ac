"""Energy monitoring from a HA power/energy sensor entity."""

import logging
from typing import Optional

from . import config_manager
from .ha_client import HAClient

logger = logging.getLogger(__name__)


class EnergyMonitor:
    """
    Tracks real-time watt draw and cumulative kWh from a smart switch sensor.

    The energy sensor should report current power in Watts (W).
    kWh is approximated by integrating watt readings over time.
    """

    def __init__(self, ha: HAClient) -> None:
        self._ha = ha
        self._watt_draw: float = 0.0
        self._session_kwh: float = 0.0
        self._peak_watts: float = 0.0
        self._watt_samples: list[float] = []
        self._energy_start_kwh: float = 0.0
        ha.on_state_change(self._on_state_change)

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def watt_draw(self) -> float:
        return self._watt_draw

    @property
    def session_kwh(self) -> float:
        return self._session_kwh

    @property
    def peak_watts(self) -> float:
        return self._peak_watts

    @property
    def avg_watts(self) -> float:
        if not self._watt_samples:
            return 0.0
        return sum(self._watt_samples) / len(self._watt_samples)

    def reset_session(self) -> None:
        """Call at AC session start."""
        self._session_kwh = 0.0
        self._peak_watts = 0.0
        self._watt_samples = []
        self._energy_start_kwh = self._watt_draw  # capture baseline

    def record_tick(self, interval_seconds: float = 60) -> None:
        """
        Called every logic tick while AC is running.
        Accumulates energy from current watt draw.
        """
        if self._watt_draw <= 0:
            return
        kwh_this_tick = (self._watt_draw * interval_seconds) / 3_600_000
        self._session_kwh += kwh_this_tick
        self._watt_samples.append(self._watt_draw)
        if self._watt_draw > self._peak_watts:
            self._peak_watts = self._watt_draw

    async def refresh(self) -> float:
        """Poll the energy sensor entity. Returns current watt draw."""
        entity = config_manager.get("energy_sensor_entity")
        if not entity:
            return self._watt_draw

        state_str = await self._ha.get_state_value(entity)
        if state_str is None:
            return self._watt_draw

        try:
            self._watt_draw = float(state_str)
        except (ValueError, TypeError):
            logger.debug("Could not parse watt draw from '%s'", state_str)

        return self._watt_draw

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_state_change(self, entity_id: str, state: str, _attrs: dict) -> None:
        configured = config_manager.get("energy_sensor_entity")
        if entity_id != configured:
            return
        try:
            self._watt_draw = float(state)
        except (ValueError, TypeError):
            pass
