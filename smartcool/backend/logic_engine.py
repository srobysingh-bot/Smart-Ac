"""
HawaAI core decision engine — THE BRAIN.

Called every `logic_interval_seconds` by the scheduler.
Reads fresh config each tick so settings changes apply immediately.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from . import config_manager, ha_client, session_logger, weather_api

logger = logging.getLogger(__name__)

# Runtime state (in-memory, not persisted across restarts)
_ac_is_on: bool = False
_vacant_since: Optional[datetime] = None


async def tick() -> None:
    """
    Single decision-loop iteration.
    Reads fresh config every tick — settings changes apply immediately.
    """
    global _ac_is_on, _vacant_since

    cfg = config_manager.load_config()

    presence_entity = cfg.get("presence_entity", "")
    indoor_temp_entity = cfg.get("indoor_temp_entity", "")
    ac_switch_entity = cfg.get("ac_switch_entity", "")

    if not indoor_temp_entity:
        logger.warning("[HawaAI] Logic skipped — indoor_temp_entity not configured")
        return

    # --- Manual override: skip all automation ---
    if cfg.get("manual_override", False):
        logger.debug("[HawaAI] Manual override active — skipping logic")
        return

    # --- Read live sensor states from HA ---
    indoor_temp_raw = await ha_client.get_state(indoor_temp_entity)
    if indoor_temp_raw is None:
        logger.warning("[HawaAI] Cannot read indoor temp from %s", indoor_temp_entity)
        return

    try:
        indoor_temp = float(indoor_temp_raw)
    except ValueError:
        logger.error("[HawaAI] Invalid temp value: %s", indoor_temp_raw)
        return

    presence_raw = await ha_client.get_state(presence_entity) if presence_entity else None
    is_occupied = presence_raw in ("on", "occupied", "home", "detected")
    use_presence = cfg.get("use_presence", True)

    target_temp = float(cfg.get("target_temp", 24))
    hysteresis = float(cfg.get("hysteresis", 1.5))
    vacancy_timeout = int(cfg.get("vacancy_timeout_minutes", 5)) * 60

    logger.info(
        "[HawaAI] TICK | indoor=%.1f°C | presence=%s | ac=%s | target=%.1f°C",
        indoor_temp,
        "occupied" if is_occupied else "vacant",
        "ON" if _ac_is_on else "OFF",
        target_temp,
    )

    now = datetime.now(timezone.utc)

    # --- Write snapshot every tick ---
    weather = await weather_api.get_cached()
    await session_logger.add_snapshot(
        session_logger.current_session_id(),
        {
            "timestamp": now.isoformat(),
            "indoor_temp": indoor_temp,
            "outdoor_temp": weather.get("temp") if weather else None,
            "ac_state": _ac_is_on,
            "watt_draw": 0.0,
            "presence": is_occupied,
        },
    )

    # --- VACANCY LOGIC ---
    if use_presence and presence_entity and not is_occupied:
        if _vacant_since is None:
            _vacant_since = now
            logger.info("[HawaAI] Room just became vacant — starting vacancy timer")

        vacant_seconds = (now - _vacant_since).total_seconds()

        if _ac_is_on and vacant_seconds >= vacancy_timeout:
            logger.info("[HawaAI] Room vacant for %.0fs — turning AC OFF", vacant_seconds)
            await _turn_ac_off(ac_switch_entity, cfg, indoor_temp, reason="vacant")
        return

    # Room is occupied (or presence detection disabled)
    _vacant_since = None

    # --- TEMPERATURE LOGIC ---
    if indoor_temp > (target_temp + hysteresis) and not _ac_is_on:
        logger.info(
            "[HawaAI] Too warm (%.1f°C > %.1f°C) — turning AC ON",
            indoor_temp, target_temp + hysteresis,
        )
        await _turn_ac_on(ac_switch_entity, cfg, indoor_temp)

    elif indoor_temp <= (target_temp - hysteresis) and _ac_is_on:
        logger.info(
            "[HawaAI] Room cooled (%.1f°C ≤ %.1f°C) — turning AC OFF",
            indoor_temp, target_temp - hysteresis,
        )
        session_logger.mark_cooled()
        await _turn_ac_off(ac_switch_entity, cfg, indoor_temp, reason="cooled")

    elif indoor_temp <= target_temp and _ac_is_on:
        session_logger.mark_cooled()


async def _turn_ac_on(switch_entity: str, cfg: dict, indoor_temp: float) -> None:
    global _ac_is_on

    success = False
    if switch_entity:
        success = await ha_client.turn_on_ac(switch_entity)

    broadlink_entity = cfg.get("broadlink_entity", "")
    if broadlink_entity:
        target = int(cfg.get("target_temp", 24))
        await ha_client.send_broadlink_command(broadlink_entity, f"cool_{target}")

    if success or broadlink_entity:
        _ac_is_on = True
        weather = await weather_api.get_cached()
        await session_logger.start_session({
            "start_time": datetime.now(timezone.utc).isoformat(),
            "indoor_temp_start": indoor_temp,
            "outdoor_temp_start": weather.get("temp") if weather else None,
            "outdoor_humidity_start": weather.get("humidity") if weather else None,
            "target_temp": cfg.get("target_temp"),
            "ac_switch_entity": switch_entity,
            "ac_brand": cfg.get("ac_brand"),
            "ac_model": cfg.get("ac_model"),
            "room_name": cfg.get("room_name"),
        })
        logger.info("[HawaAI] AC ON — session started")
    else:
        logger.error("[HawaAI] Failed to turn AC on (no switch or broadlink configured)")


async def _turn_ac_off(
    switch_entity: str, cfg: dict, indoor_temp: float, reason: str
) -> None:
    global _ac_is_on

    success = False
    if switch_entity:
        success = await ha_client.turn_off_ac(switch_entity)

    broadlink_entity = cfg.get("broadlink_entity", "")
    if broadlink_entity:
        await ha_client.send_broadlink_command(broadlink_entity, "power_off")

    if success or broadlink_entity:
        _ac_is_on = False
        await session_logger.end_session({
            "end_time": datetime.now(timezone.utc).isoformat(),
            "indoor_temp_end": indoor_temp,
            "reason_stopped": reason,
        })
        logger.info("[HawaAI] AC OFF — reason: %s", reason)
    else:
        logger.error("[HawaAI] Failed to turn AC off (no switch or broadlink configured)")


def get_runtime_state() -> dict:
    """Returns current in-memory runtime state for the /api/status endpoint."""
    return {
        "ac_is_on": _ac_is_on,
        "session_id": session_logger.current_session_id(),
        "session_start_time": (
            session_logger.session_start_time().isoformat()
            if session_logger.session_start_time()
            else None
        ),
    }
