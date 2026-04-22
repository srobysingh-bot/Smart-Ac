"""
Session logger — writes start/end records and snapshots to SQLite.

Also publishes SmartCool sensor states back to Home Assistant so users
can reference them in their own dashboards and automations.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from . import config_manager, database
from .ha_client import HAClient

logger = logging.getLogger(__name__)

# HA entity IDs published by SmartCool
_SENSORS = {
    "indoor_temp":   "sensor.smartcool_indoor_temp",
    "outdoor_temp":  "sensor.smartcool_outdoor_temp",
    "session_kwh":   "sensor.smartcool_session_kwh",
    "ac_active":     "binary_sensor.smartcool_ac_active",
    "daily_cost":    "sensor.smartcool_daily_cost",
    "time_to_cool":  "sensor.smartcool_time_to_cool",
}


class SessionLogger:
    def __init__(self, ha: HAClient) -> None:
        self._ha = ha
        self._current_session_id: Optional[str] = None
        self._session_start_time: Optional[datetime] = None
        self._start_indoor_temp: Optional[float] = None
        self._cooled_at: Optional[datetime] = None  # first time temp <= target

    # ── Session lifecycle ──────────────────────────────────────────────────────

    async def start_session(
        self,
        indoor_temp: Optional[float],
        outdoor_temp: Optional[float],
        outdoor_humidity: Optional[float],
        energy_start_kwh: float,
    ) -> str:
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        cfg = config_manager.get_all()

        record = {
            "session_id": session_id,
            "start_time": now.isoformat(),
            "indoor_temp_start": indoor_temp,
            "outdoor_temp_start": outdoor_temp,
            "outdoor_humidity_start": outdoor_humidity,
            "target_temp": cfg.get("target_temp"),
            "ac_entity_id": cfg.get("ac_switch_entity"),
            "ac_brand": cfg.get("ac_brand"),
            "ac_model": cfg.get("ac_model"),
            "room_name": cfg.get("room_name"),
            "presence_trigger": "occupied",
            "energy_start_kwh": energy_start_kwh,
            "day_of_week": now.weekday(),   # 0=Mon … 6=Sun
            "hour_of_day": now.hour,
        }

        await database.insert_session_start(record)

        self._current_session_id = session_id
        self._session_start_time = now
        self._start_indoor_temp = indoor_temp
        self._cooled_at = None

        logger.info("Session started: %s (%.1f°C indoor)", session_id, indoor_temp or 0)
        await self._publish_ac_active(True)
        return session_id

    async def end_session(
        self,
        reason: str,
        indoor_temp: Optional[float],
        session_kwh: float,
        peak_watts: float,
        avg_watts: float,
    ) -> None:
        if not self._current_session_id:
            return

        now = datetime.now(timezone.utc)
        tariff = config_manager.get("energy_tariff_per_kwh", 8.0)
        cost = round(session_kwh * tariff, 2)

        cool_minutes: Optional[float] = None
        if self._cooled_at and self._session_start_time:
            cool_minutes = (
                (self._cooled_at - self._session_start_time).total_seconds() / 60.0
            )

        await database.update_session_end(
            self._current_session_id,
            {
                "end_time": now.isoformat(),
                "indoor_temp_end": indoor_temp,
                "time_to_cool_minutes": round(cool_minutes, 1) if cool_minutes else None,
                "energy_consumed_kwh": round(session_kwh, 4),
                "cost_estimate": cost,
                "reason_stopped": reason,
                "peak_watt_draw": round(peak_watts, 1),
                "avg_watt_draw": round(avg_watts, 1),
            },
        )

        logger.info(
            "Session ended: %s | reason=%s | %.3f kWh | ₹%.2f",
            self._current_session_id,
            reason,
            session_kwh,
            cost,
        )

        # Publish last time-to-cool to HA
        if cool_minutes is not None:
            await self._ha.publish_sensor_state(
                _SENSORS["time_to_cool"],
                round(cool_minutes, 1),
                {"unit_of_measurement": "min", "friendly_name": "SmartCool Time to Cool"},
            )

        # Today's running cost
        stats = await database.get_today_stats()
        await self._ha.publish_sensor_state(
            _SENSORS["daily_cost"],
            round(stats["total_cost"], 2),
            {
                "unit_of_measurement": config_manager.get("currency", "INR"),
                "friendly_name": "SmartCool Daily Cost",
            },
        )

        self._current_session_id = None
        self._session_start_time = None
        self._start_indoor_temp = None
        self._cooled_at = None

        await self._publish_ac_active(False)

    async def write_snapshot(
        self,
        indoor_temp: Optional[float],
        outdoor_temp: Optional[float],
        ac_state: bool,
        watt_draw: float,
        presence: bool,
    ) -> None:
        """Called every tick to record a monitoring snapshot."""
        snap = {
            "session_id": self._current_session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "indoor_temp": indoor_temp,
            "outdoor_temp": outdoor_temp,
            "ac_state": ac_state,
            "watt_draw": watt_draw,
            "presence": presence,
        }
        await database.insert_snapshot(snap)

    def mark_cooled(self) -> None:
        """Mark the time target temp was first reached (for time-to-cool metric)."""
        if self._cooled_at is None:
            self._cooled_at = datetime.now(timezone.utc)

    # ── HA sensor publishing ──────────────────────────────────────────────────

    async def publish_live(
        self,
        indoor_temp: Optional[float],
        outdoor_temp: Optional[float],
        session_kwh: float,
    ) -> None:
        if indoor_temp is not None:
            await self._ha.publish_sensor_state(
                _SENSORS["indoor_temp"],
                round(indoor_temp, 1),
                {
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "friendly_name": "SmartCool Indoor Temp",
                },
            )
        if outdoor_temp is not None:
            await self._ha.publish_sensor_state(
                _SENSORS["outdoor_temp"],
                round(outdoor_temp, 1),
                {
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "friendly_name": "SmartCool Outdoor Temp",
                },
            )
        await self._ha.publish_sensor_state(
            _SENSORS["session_kwh"],
            round(session_kwh, 3),
            {
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "friendly_name": "SmartCool Session Energy",
            },
        )

    async def _publish_ac_active(self, active: bool) -> None:
        await self._ha.publish_sensor_state(
            _SENSORS["ac_active"],
            "on" if active else "off",
            {
                "device_class": "running",
                "friendly_name": "SmartCool AC Active",
            },
        )

    @property
    def current_session_id(self) -> Optional[str]:
        return self._current_session_id

    @property
    def session_start_time(self) -> Optional[datetime]:
        return self._session_start_time
