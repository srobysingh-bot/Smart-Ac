"""
HawaAI core decision engine — THE BRAIN.

Called every `logic_interval_seconds` by the scheduler.
AC is controlled ONLY via Broadlink IR remote — no smart switch.
Reads fresh config each tick so settings changes apply immediately.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from . import config_manager, ha_client, session_logger, weather_api
from .utils import parse_presence

logger = logging.getLogger(__name__)

# Runtime state (in-memory, not persisted across restarts)
_ac_is_on: bool = False
_vacant_since: Optional[datetime] = None
_session_start_time: Optional[datetime] = None
_session_start_temp: Optional[float] = None
_session_start_kwh: Optional[float] = None   # kWh meter reading when session started


async def tick() -> None:
    """
    Single decision-loop iteration.

    STEP 1  Load fresh config
    STEP 2  Guard: required entities configured?
    STEP 3  Read live HA sensor values
    STEP 4  Parse presence
    STEP 5  Manual override check
    STEP 6  Vacancy logic
    STEP 7  Temperature logic
    """
    global _ac_is_on, _vacant_since, _session_start_kwh

    # STEP 1 — fresh config every tick
    cfg = config_manager.load_config()

    # STEP 2 — guard: can't run without these two entities
    presence_entity   = cfg.get("presence_entity", "")
    indoor_temp_entity = cfg.get("indoor_temp_entity", "")

    if not presence_entity or not indoor_temp_entity:
        logger.warning(
            "[HawaAI] Logic skipped — missing entity config (presence=%s, temp=%s)",
            bool(presence_entity), bool(indoor_temp_entity),
        )
        return

    # STEP 3 — read live sensor states from HA
    indoor_temp_raw = await ha_client.get_state(indoor_temp_entity)
    if indoor_temp_raw is None:
        logger.warning("[HawaAI] Cannot read indoor temp from %s", indoor_temp_entity)
        return

    try:
        indoor_temp = float(indoor_temp_raw)
    except (ValueError, TypeError):
        logger.error("[HawaAI] Invalid temp value: %r from %s", indoor_temp_raw, indoor_temp_entity)
        return

    presence_raw = await ha_client.get_state(presence_entity)

    # STEP 4 — parse presence (BUG 1 FIX — robust check for all HA presence sensor formats)
    is_occupied = parse_presence(presence_raw)
    # Always log raw value so addon logs show exactly what HA is returning
    logger.info(
        "[HawaAI] Presence raw value from HA: %r → occupied=%s",
        presence_raw, is_occupied,
    )

    # STEP 5 — manual override
    if cfg.get("manual_override", False):
        logger.info("[HawaAI] Manual override active — skipping logic")
        return

    target_temp     = float(cfg.get("target_temp", 24))
    hysteresis      = float(cfg.get("hysteresis", 1.5))
    vacancy_timeout = int(cfg.get("vacancy_timeout_minutes", 5)) * 60
    use_presence    = cfg.get("use_presence", True)

    logger.info(
        "[HawaAI] TICK | indoor=%.1f°C | presence=%s | ac=%s | target=%.1f°C",
        indoor_temp,
        "occupied" if is_occupied else "vacant",
        "ON" if _ac_is_on else "OFF",
        target_temp,
    )

    now = datetime.now(timezone.utc)

    # BUG 2 FIX — power-based AC state sync using LIVE WATTS entity
    # Detects AC turned on/off by physical remote (outside HawaAI's control)
    energy_power_entity = cfg.get("energy_power_entity", "")
    energy_watts = 0.0
    if energy_power_entity:
        energy_raw = await ha_client.get_state(energy_power_entity)
        try:
            energy_watts = float(energy_raw) if energy_raw else 0.0
        except (ValueError, TypeError):
            energy_watts = 0.0

        if energy_watts > 50.0 and not _ac_is_on:
            logger.info(
                "[HawaAI] AC detected ON via power (%.0fW) but engine thought OFF — syncing state",
                energy_watts,
            )
            _ac_is_on = True
            if _session_start_time is None:
                _session_start_time = datetime.now(timezone.utc)
                _session_start_temp = indoor_temp

        elif energy_watts < 10.0 and _ac_is_on:
            logger.info(
                "[HawaAI] AC power dropped to %.0fW but engine thought ON — syncing OFF",
                energy_watts,
            )
            await _turn_ac_off(cfg, indoor_temp, reason="manual_off")

    # Write snapshot every tick for chart
    weather = await weather_api.get_cached()
    await session_logger.add_snapshot(
        session_logger.current_session_id(),
        {
            "timestamp": now.isoformat(),
            "indoor_temp": indoor_temp,
            "outdoor_temp": weather.get("temp") if weather else None,
            "ac_state": _ac_is_on,
            "watt_draw": energy_watts,
            "presence": is_occupied,
        },
    )

    # STEP 6 — VACANCY LOGIC
    if use_presence and not is_occupied:
        if _vacant_since is None:
            _vacant_since = now
            logger.info("[HawaAI] Room just became vacant — starting vacancy timer")

        elapsed = (now - _vacant_since).total_seconds()

        if _ac_is_on and elapsed >= vacancy_timeout:
            logger.info("[HawaAI] Room vacant for %.0fs — turning AC OFF", elapsed)
            await _turn_ac_off(cfg, indoor_temp, reason="vacant")
        return  # always return early when vacant, regardless of AC state

    # Room is occupied (or presence disabled) — reset vacancy timer
    _vacant_since = None

    # STEP 7 — TEMPERATURE LOGIC
    upper_threshold = target_temp + hysteresis   # e.g. 24 + 1.5 = 25.5°C
    lower_threshold = target_temp - hysteresis   # e.g. 24 - 1.5 = 22.5°C

    if indoor_temp > upper_threshold and not _ac_is_on:
        logger.info(
            "[HawaAI] Too warm (%.1f°C > %.1f°C) — turning AC ON",
            indoor_temp, upper_threshold,
        )
        await _turn_ac_on(cfg, indoor_temp)

    elif indoor_temp <= lower_threshold and _ac_is_on:
        logger.info(
            "[HawaAI] Room cooled (%.1f°C ≤ %.1f°C) — turning AC OFF",
            indoor_temp, lower_threshold,
        )
        session_logger.mark_cooled()
        await _turn_ac_off(cfg, indoor_temp, reason="cooled")

    elif indoor_temp <= target_temp and _ac_is_on:
        # Between thresholds and cooling: just record milestone
        session_logger.mark_cooled()


async def _turn_ac_on(cfg: dict, indoor_temp: float) -> None:
    """Send Broadlink IR 'on' command and start a session."""
    global _ac_is_on, _session_start_time, _session_start_temp, _session_start_kwh

    broadlink_entity = cfg.get("broadlink_entity", "")
    # BUG 3C FIX — use user-configured IR command name (must match learned Broadlink command exactly)
    cmd_on = (cfg.get("ir_command_on") or "").strip()

    if not broadlink_entity:
        logger.warning("[HawaAI] No broadlink_entity configured — marking ac=ON without IR command")
    elif not cmd_on:
        logger.error(
            "[HawaAI] IR Power ON command is empty! "
            "Go to Settings → IR Command Mapping and enter the exact Broadlink command name."
        )
    else:
        ok = await ha_client.send_broadlink_command(broadlink_entity, cmd_on)
        if ok:
            logger.info("[HawaAI] AC ON — IR sent: '%s' to %s", cmd_on, broadlink_entity)
        else:
            logger.error("[HawaAI] Broadlink command '%s' failed for %s", cmd_on, broadlink_entity)

    # Record kWh meter reading at session start for consumed-kWh calculation
    kwh_entity = cfg.get("energy_kwh_entity", "")
    start_kwh = None
    if kwh_entity:
        raw = await ha_client.get_state(kwh_entity)
        try:
            start_kwh = float(raw) if raw else None
        except (ValueError, TypeError):
            start_kwh = None
    _session_start_kwh = start_kwh

    # Always flip the state and start a session so the logic doesn't cycle
    _ac_is_on = True
    _session_start_time = datetime.now(timezone.utc)
    _session_start_temp = indoor_temp

    weather = await weather_api.get_cached()
    await session_logger.start_session({
        "start_time": _session_start_time.isoformat(),
        "indoor_temp_start": indoor_temp,
        "outdoor_temp_start": weather.get("temp") if weather else None,
        "outdoor_humidity_start": weather.get("humidity") if weather else None,
        "target_temp": cfg.get("target_temp"),
        "ac_brand": cfg.get("ac_brand"),
        "ac_model": cfg.get("ac_model"),
        "room_name": cfg.get("room_name"),
        "energy_kwh_start": start_kwh,
    })
    logger.info("[HawaAI] AC ON via Broadlink — session started (kWh meter: %s)", start_kwh)


async def _turn_ac_off(cfg: dict, indoor_temp: float, reason: str) -> None:
    """Send Broadlink IR 'off' command and end the session."""
    global _ac_is_on, _session_start_time, _session_start_temp, _session_start_kwh

    broadlink_entity = cfg.get("broadlink_entity", "")
    cmd_off = (cfg.get("ir_command_off") or "").strip()

    if not broadlink_entity:
        logger.warning("[HawaAI] No broadlink_entity configured — marking ac=OFF without IR command")
    elif not cmd_off:
        logger.error(
            "[HawaAI] IR Power OFF command is empty! "
            "Go to Settings → IR Command Mapping and enter the exact Broadlink command name."
        )
    else:
        ok = await ha_client.send_broadlink_command(broadlink_entity, cmd_off)
        if ok:
            logger.info("[HawaAI] AC OFF — IR sent: '%s' to %s | reason: %s", cmd_off, broadlink_entity, reason)
        else:
            logger.error("[HawaAI] Broadlink command '%s' failed for %s", cmd_off, broadlink_entity)

    _ac_is_on = False

    # Calculate kWh consumed during this session
    kwh_consumed = None
    cost = None
    kwh_entity = cfg.get("energy_kwh_entity", "")
    if kwh_entity and _session_start_kwh is not None:
        raw = await ha_client.get_state(kwh_entity)
        try:
            end_kwh = float(raw) if raw else None
            if end_kwh is not None:
                kwh_consumed = round(end_kwh - _session_start_kwh, 4)
                tariff = float(cfg.get("energy_tariff_per_kwh", 8.0))
                cost = round(kwh_consumed * tariff, 2)
                logger.info(
                    "[HawaAI] Session energy: %.4f kWh consumed, cost %.2f (tariff %.2f/kWh)",
                    kwh_consumed, cost, tariff,
                )
        except (ValueError, TypeError):
            pass
    _session_start_kwh = None

    if _session_start_time is not None:
        now = datetime.now(timezone.utc)
        cool_minutes = (now - _session_start_time).total_seconds() / 60.0
        await session_logger.end_session({
            "end_time": now.isoformat(),
            "indoor_temp_end": indoor_temp,
            "time_to_cool_minutes": round(cool_minutes, 1),
            "reason_stopped": reason,
            "energy_kwh": kwh_consumed,
            "cost": cost,
        })
        _session_start_time = None
        _session_start_temp = None

    logger.info("[HawaAI] AC OFF via Broadlink — reason: %s", reason)


def get_runtime_state() -> dict:
    """Returns current in-memory runtime state for the /api/status endpoint."""
    return {
        "ac_is_on": _ac_is_on,
        "session_id": session_logger.current_session_id(),
        "session_start_time": (
            _session_start_time.isoformat() if _session_start_time else None
        ),
        "session_start_kwh": _session_start_kwh,
    }
