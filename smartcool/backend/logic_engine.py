"""
SmartCool core decision engine — THE BRAIN.

Runs every `logic_interval_seconds` (default 60 s).
Reads presence, indoor temp, outdoor temp, energy, and AC state,
then decides whether to turn the AC on or off.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from . import config_manager
from .ac_controller import ACController
from .energy_monitor import EnergyMonitor
from .ha_client import HAClient
from .presence_handler import PresenceHandler
from .session_logger import SessionLogger
from .temperature_handler import TemperatureHandler
from .weather_api import WeatherData, get_weather

logger = logging.getLogger(__name__)


class LogicEngine:
    def __init__(
        self,
        ha: HAClient,
        presence: PresenceHandler,
        temp_handler: TemperatureHandler,
        energy: EnergyMonitor,
        ac: ACController,
        session_log: SessionLogger,
    ) -> None:
        self._ha = ha
        self._presence = presence
        self._temp = temp_handler
        self._energy = energy
        self._ac = ac
        self._log = session_log

        self._ac_on: bool = False
        self._last_action: str = "none"

        # Live status dict consumed by the /api/status endpoint
        self.status: dict = {
            "ac_on": False,
            "indoor_temp": None,
            "outdoor_temp": None,
            "outdoor_humidity": None,
            "presence": False,
            "watt_draw": 0.0,
            "session_kwh": 0.0,
            "session_id": None,
            "session_start": None,
            "last_action": "none",
            "manual_override": False,
        }

    # ── Main tick ─────────────────────────────────────────────────────────────

    async def tick(self) -> None:
        """Single decision-loop iteration. Called by the scheduler."""
        cfg = config_manager.get_all()

        if cfg.get("manual_override"):
            self.status["manual_override"] = True
            logger.debug("Manual override active — skipping logic")
            return

        self.status["manual_override"] = False
        interval = cfg.get("logic_interval_seconds", 60)

        # ── 1. Gather inputs ────────────────────────────────────────────────
        indoor_temp = await self._temp.refresh()
        presence = await self._presence.refresh() if cfg.get("use_presence", True) else True
        watt_draw = await self._energy.refresh()

        weather: Optional[WeatherData] = None
        outdoor_temp: Optional[float] = None
        outdoor_humidity: Optional[float] = None
        if cfg.get("use_outdoor_temp", True):
            weather = await get_weather()
            if weather:
                outdoor_temp = weather.temp_c
                outdoor_humidity = weather.humidity_pct

        # ── 2. AC state (best-effort from HA entity) ────────────────────────
        ac_entity = cfg.get("ac_switch_entity", "")
        if ac_entity:
            state_str = await self._ha.get_state_value(ac_entity)
            if state_str is not None:
                self._ac_on = state_str.lower() in ("on", "true", "1")

        # ── 3. Update live status ───────────────────────────────────────────
        self.status.update(
            {
                "ac_on": self._ac_on,
                "indoor_temp": indoor_temp,
                "outdoor_temp": outdoor_temp,
                "outdoor_humidity": outdoor_humidity,
                "presence": presence,
                "watt_draw": watt_draw,
                "session_kwh": self._energy.session_kwh,
                "session_id": self._log.current_session_id,
                "session_start": (
                    self._log.session_start_time.isoformat()
                    if self._log.session_start_time
                    else None
                ),
                "last_action": self._last_action,
            }
        )

        # Publish live sensor data to HA regardless of AC state
        await self._log.publish_live(indoor_temp, outdoor_temp, self._energy.session_kwh)

        # ── 4. Decision tree ────────────────────────────────────────────────
        target = float(cfg.get("target_temp", 24))
        hysteresis = float(cfg.get("hysteresis", 1.5))
        vacancy_timeout = float(cfg.get("vacancy_timeout_minutes", 5))

        # A. VACANT path
        if not presence:
            vacancy_min = self._presence.vacancy_minutes
            if self._ac_on and vacancy_min >= vacancy_timeout:
                logger.info(
                    "Vacant for %.1f min (timeout %.1f min) — turning AC off",
                    vacancy_min,
                    vacancy_timeout,
                )
                await self._turn_off("vacant")
            # Always record snapshot even while vacant
            await self._log.write_snapshot(indoor_temp, outdoor_temp, self._ac_on, watt_draw, False)
            return

        # B. OCCUPIED path
        if indoor_temp is None:
            logger.warning("No indoor temperature reading — cannot make decision")
            await self._log.write_snapshot(indoor_temp, outdoor_temp, self._ac_on, watt_draw, True)
            return

        # Turn ON if too hot
        if indoor_temp > (target + hysteresis):
            if not self._ac_on:
                logger.info(
                    "Indoor %.1f°C > target+hyst %.1f°C — turning AC ON",
                    indoor_temp,
                    target + hysteresis,
                )
                await self._turn_on(indoor_temp, outdoor_temp, outdoor_humidity)

        # Turn OFF if sufficiently cool
        elif indoor_temp <= (target - hysteresis):
            if self._ac_on:
                logger.info(
                    "Indoor %.1f°C <= target-hyst %.1f°C — turning AC OFF",
                    indoor_temp,
                    target - hysteresis,
                )
                self._log.mark_cooled()
                await self._turn_off("cooled")

        # Mark cooldown milestone even if AC stays on
        elif indoor_temp <= target and self._ac_on:
            self._log.mark_cooled()

        # Accumulate energy for running session
        if self._ac_on:
            self._energy.record_tick(interval)

        await self._log.write_snapshot(indoor_temp, outdoor_temp, self._ac_on, watt_draw, True)

    # ── Actions ───────────────────────────────────────────────────────────────

    async def _turn_on(
        self,
        indoor_temp: float,
        outdoor_temp: Optional[float],
        outdoor_humidity: Optional[float],
    ) -> None:
        cfg = config_manager.get_all()
        target = int(cfg.get("target_temp", 24))

        ok = await self._ac.turn_on(mode="cool", temp=target, fan="auto")
        if ok:
            self._ac_on = True
            self._last_action = "turn_on"
            self.status["ac_on"] = True
            self._energy.reset_session()
            await self._log.start_session(
                indoor_temp=indoor_temp,
                outdoor_temp=outdoor_temp,
                outdoor_humidity=outdoor_humidity,
                energy_start_kwh=self._energy.watt_draw,
            )
        else:
            logger.error("Failed to turn AC on")

    async def _turn_off(self, reason: str) -> None:
        ok = await self._ac.turn_off()
        if ok:
            self._ac_on = False
            self._last_action = f"turn_off:{reason}"
            self.status["ac_on"] = False
            await self._log.end_session(
                reason=reason,
                indoor_temp=self._temp.indoor_temp,
                session_kwh=self._energy.session_kwh,
                peak_watts=self._energy.peak_watts,
                avg_watts=self._energy.avg_watts,
            )
        else:
            logger.error("Failed to turn AC off")
