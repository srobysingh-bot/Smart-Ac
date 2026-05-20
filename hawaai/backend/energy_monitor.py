"""Energy monitoring from a HA power/energy sensor entity."""

import logging

from . import config_manager
from .energy_config import normalize_power_value, resolve_runtime_energy_config

logger = logging.getLogger(__name__)


class EnergyMonitor:
    """
    Tracks real-time watt draw and cumulative kWh from a smart switch sensor.

    The energy sensor should report current power in Watts (W).
    kWh is approximated by integrating watt readings over time.
    """

    def __init__(self) -> None:
        self._watt_draw: float = 0.0
        self._session_kwh: float = 0.0
        self._peak_watts: float = 0.0
        self._watt_samples: list[float] = []
        self._energy_start_kwh: float = 0.0

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
        from . import ha_client
        resolved = await resolve_runtime_energy_config(config_manager.load_config())
        entity = resolved.power_entity
        if not entity:
            return self._watt_draw

        state_obj = await ha_client.get_entity_state_full(entity)
        if not state_obj:
            return self._watt_draw

        attrs = state_obj.get("attributes") or {}
        normalized = normalize_power_value(entity, state_obj.get("state"), attrs)
        if normalized.valid and normalized.watts is not None:
            self._watt_draw = float(normalized.watts)
        else:
            logger.debug(
                "Could not normalize watt draw from %s state=%r reason=%s",
                entity,
                state_obj.get("state"),
                normalized.reason,
            )

        return self._watt_draw
