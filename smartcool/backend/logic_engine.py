"""
HawaAI core decision engine — THE BRAIN.

Called every `logic_interval_seconds` by the scheduler.

AC is controlled via HA climate entity (preferred) or Broadlink IR remote (fallback).
Reads fresh config each tick so settings changes apply immediately.

AC state priority (source of truth):
  1. HA climate entity — `climate.study_ac` state ("cool"/"off"/etc.)
  2. Live power sensor (watts) — if no climate entity configured
  3. Internal _ac_is_on flag — fallback / restart memory
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
_session_start_kwh: Optional[float] = None

# Command-ignore window — after HawaAI sends an ON command, HA takes several seconds
# to update the climate entity state. Ignore "off" readings from HA during this window
# to prevent a false manual_off that would close the session immediately.
_last_command_time: Optional[datetime] = None   # when HawaAI last sent ON or OFF
_last_command: str = ""                          # "on" or "off"
_HA_STATE_DELAY_SECS: int = 30                   # seconds to ignore HA state after ON command


async def tick() -> None:
    """
    Single decision-loop iteration.

    STEP 1  Load fresh config
    STEP 2  Guard: required entities configured?
    STEP 3  Read live HA sensor values
    STEP 4  Parse presence
    STEP 5  Manual override check
    STEP 6A Determine authoritative AC state from HA (climate > power > internal)
    STEP 6B Sync internal flag + session management
    STEP 7  Write monitoring snapshot
    STEP 8  Vacancy logic
    STEP 9  Temperature logic (smart-adjustment aware)
    """
    global _ac_is_on, _vacant_since, _session_start_time, _session_start_temp, _session_start_kwh, _last_command_time, _last_command

    # STEP 1 — fresh config every tick
    cfg = config_manager.load_config()

    # STEP 2 — guard: can't run without at least indoor temp + presence
    presence_entity    = cfg.get("presence_entity", "")
    indoor_temp_entity = cfg.get("indoor_temp_entity", "")

    if not presence_entity or not indoor_temp_entity:
        logger.warning(
            "[HawaAI] Logic skipped — missing entity config (presence=%s, temp=%s)",
            bool(presence_entity), bool(indoor_temp_entity),
        )
        return

    # STEP 3 — read live sensor states from HA
    indoor_temp_raw = await ha_client.get_state(indoor_temp_entity)
    indoor_temp: Optional[float] = None

    if indoor_temp_raw not in (None, "unavailable", "unknown"):
        try:
            indoor_temp = float(indoor_temp_raw)
        except (ValueError, TypeError):
            logger.warning(
                "[HawaAI] Cannot parse temp %r from %s",
                indoor_temp_raw, indoor_temp_entity,
            )

    # Fallback: use climate entity's built-in thermistor when WiFi sensor is offline
    if indoor_temp is None:
        climate_entity_tmp = cfg.get("climate_entity", "").strip()
        if climate_entity_tmp:
            climate_tmp = await ha_client.get_climate_state(climate_entity_tmp)
            fallback = climate_tmp.get("current_temp")
            if fallback is not None:
                try:
                    indoor_temp = float(fallback)
                    logger.info(
                        "[HawaAI] Indoor sensor unavailable (%r) — using climate entity "
                        "current_temp fallback: %.1f°C",
                        indoor_temp_raw, indoor_temp,
                    )
                except (ValueError, TypeError):
                    pass

    if indoor_temp is None:
        logger.warning(
            "[HawaAI] Cannot read indoor temp from %s (returned %r) "
            "and no climate entity fallback available — skipping tick",
            indoor_temp_entity, indoor_temp_raw,
        )
        return

    presence_raw = await ha_client.get_state(presence_entity)

    # STEP 4 — robust presence parsing (handles FP2, mmWave, device_tracker, etc.)
    is_occupied = parse_presence(presence_raw)
    logger.info(
        "[HawaAI] Presence: %r → occupied=%s",
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
    smart_adj       = cfg.get("smart_temp_adjustment", False)

    # Weather — needed for smart adjustment and snapshot
    weather      = await weather_api.get_cached()
    outdoor_temp = weather.get("temp") if weather else None

    # ── Smart Temperature Adjustment ─────────────────────────────────────────
    # Raise/lower effective target based on outdoor conditions to save electricity
    effective_target = target_temp
    if smart_adj and outdoor_temp is not None:
        if outdoor_temp < 30:
            effective_target = target_temp + 1.0
        elif outdoor_temp < 35:
            effective_target = target_temp + 0.5
        elif outdoor_temp <= 40:
            effective_target = target_temp          # no change
        else:                                       # > 40°C
            effective_target = target_temp - 1.0

        if effective_target != target_temp:
            logger.info(
                "[HawaAI] Smart adj: outdoor=%.1f°C → effective target %.1f°C (config=%.1f°C)",
                outdoor_temp, effective_target, target_temp,
            )

    # ── Read live energy/power from HA ────────────────────────────────────────
    energy_power_entity = cfg.get("energy_power_entity", "")
    energy_watts = 0.0
    if energy_power_entity:
        energy_raw = await ha_client.get_state(energy_power_entity)
        try:
            energy_watts = float(energy_raw) if energy_raw else 0.0
        except (ValueError, TypeError):
            energy_watts = 0.0

    # STEP 6A — Determine authoritative AC state from HA
    #
    # Priority:
    #   1. Climate entity (real HVAC state) — most accurate, always preferred
    #   2. Live power sensor (watts) — AC-agnostic power reading
    #   3. Internal _ac_is_on flag — fallback when no external sensor
    climate_entity = cfg.get("climate_entity", "").strip()
    climate_data: dict = {}
    ac_actually_on: bool

    if climate_entity:
        climate_data   = await ha_client.get_climate_state(climate_entity)
        ac_actually_on = climate_data.get("is_on", False)

        if ac_actually_on and climate_data.get("current_temp") is None:
            logger.warning(
                "[HawaAI] Climate entity says ON but current_temp is None — "
                "entity may be stale. Trusting reported state: %s",
                climate_data.get("state"),
            )

        logger.debug(
            "[HawaAI] Climate %s state=%r current=%s target=%s is_on=%s",
            climate_entity,
            climate_data.get("state"),
            f"{climate_data['current_temp']:.1f}°" if climate_data.get("current_temp") is not None else "—",
            f"{climate_data['target_temp']:.1f}°" if climate_data.get("target_temp") is not None else "—",
            ac_actually_on,
        )
    elif energy_power_entity:
        if energy_watts > 50.0:
            ac_actually_on = True
        elif energy_watts < 10.0:
            ac_actually_on = False
        else:
            ac_actually_on = _ac_is_on    # grey zone: trust engine
    else:
        ac_actually_on = _ac_is_on        # no external sensor

    logger.info(
        "[HawaAI] TICK | indoor=%.1f°C | outdoor=%s | presence=%s | ac=%s | "
        "target=%.1f°C (eff=%.1f°C) | watts=%.0f",
        indoor_temp,
        f"{outdoor_temp:.1f}°C" if outdoor_temp is not None else "—",
        "occupied" if is_occupied else "vacant",
        "ON" if ac_actually_on else "OFF",
        target_temp, effective_target, energy_watts,
    )

    # STEP 6B — Sync internal flag & session bookkeeping
    #
    # ── Command-ignore window ─────────────────────────────────────────────────
    # HA climate entity state can lag 5–30s after a command is sent.
    # If HawaAI just sent an ON command and HA still shows "off", we must NOT
    # treat it as a manual-off — that would close the session immediately.
    # During the window, trust our own internal flag; after the window, trust HA.
    now = datetime.now(timezone.utc)
    secs_since_cmd = (
        (now - _last_command_time).total_seconds()
        if _last_command_time is not None
        else float("inf")
    )
    in_on_window = _last_command == "on" and secs_since_cmd < _HA_STATE_DELAY_SECS

    if ac_actually_on and not _ac_is_on:
        # HA shows ON, engine thought OFF → external turn-on (physical remote / app)
        logger.info("[HawaAI] AC turned ON externally — syncing engine state & opening session")
        _ac_is_on = True
        if _session_start_time is None:
            _session_start_time = now
            _session_start_temp = float(climate_data.get("current_temp") or indoor_temp)

    elif not ac_actually_on and _ac_is_on:
        if in_on_window:
            # HA state hasn't caught up yet — keep engine as ON until window expires
            logger.info(
                "[HawaAI] HA shows OFF but %.0fs within %ds ignore window after ON command "
                "— holding AC=ON until HA state catches up",
                secs_since_cmd, _HA_STATE_DELAY_SECS,
            )
            ac_actually_on = True   # override: keep ON for the rest of this tick
        else:
            # Outside ignore window → genuine external turn-off
            logger.info(
                "[HawaAI] AC turned OFF externally (HA state settled after %.0fs) "
                "— syncing engine state & closing session",
                secs_since_cmd,
            )
            await _turn_ac_off(cfg, indoor_temp, reason="manual_off")
            ac_actually_on = False

    # STEP 7 — Write monitoring snapshot
    await session_logger.add_snapshot(
        session_logger.current_session_id(),
        {
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "indoor_temp":  indoor_temp,
            "outdoor_temp": outdoor_temp,
            "ac_state":     ac_actually_on,
            "watt_draw":    energy_watts,
            "presence":     is_occupied,
        },
    )

    # STEP 8 — VACANCY LOGIC
    if use_presence and not is_occupied:
        # Set timer only on the first vacant tick (True → False transition)
        if _vacant_since is None:
            _vacant_since = datetime.now(timezone.utc)
            logger.info("[HawaAI] Room became vacant — vacancy timer started")

        vacancy_duration = (datetime.now(timezone.utc) - _vacant_since).total_seconds()
        logger.info(
            "[HawaAI] Vacant %.0fs / timeout %ds | AC=%s (HA-read)",
            vacancy_duration, vacancy_timeout, "ON" if ac_actually_on else "OFF",
        )

        if ac_actually_on and vacancy_duration >= vacancy_timeout:
            logger.info(
                "[HawaAI] Vacancy timeout reached (%.0fs) — turning AC OFF", vacancy_duration
            )
            await _turn_ac_off(cfg, indoor_temp, reason="vacant")

        return  # never run temp logic while vacant

    # Room occupied (or presence disabled) — reset vacancy timer
    _vacant_since = None

    # STEP 9 — TEMPERATURE LOGIC
    upper = effective_target + hysteresis   # turn ON  above this
    lower = effective_target - hysteresis   # turn OFF below this

    if indoor_temp > upper and not ac_actually_on:
        logger.info("[HawaAI] Too warm (%.1f°C > %.1f°C) — turning AC ON", indoor_temp, upper)
        await _turn_ac_on(cfg, indoor_temp)

    elif indoor_temp <= lower and ac_actually_on:
        logger.info("[HawaAI] Room cooled (%.1f°C ≤ %.1f°C) — turning AC OFF", indoor_temp, lower)
        session_logger.mark_cooled()
        await _turn_ac_off(cfg, indoor_temp, reason="cooled")

    elif indoor_temp <= effective_target and ac_actually_on:
        session_logger.mark_cooled()


# ── Turn AC ON ────────────────────────────────────────────────────────────────

async def _turn_ac_on(cfg: dict, indoor_temp: float) -> None:
    """
    Turn the AC on.

    Priority:
      1. HA climate entity (set_temperature to target, mode=cool) — precise, bidirectional
      2. Broadlink IR blast (ir_command_on) — fallback for dumb AC units

    Only marks ac=ON and starts a session after confirmed success.
    Sets _last_command = "on" so the ignore window protects the next tick.
    """
    global _ac_is_on, _session_start_time, _session_start_temp, _session_start_kwh, _last_command_time, _last_command

    climate_entity = (cfg.get("climate_entity") or "").strip()
    target_temp    = float(cfg.get("target_temp", 24))
    success        = False

    if climate_entity:
        success = await ha_client.set_climate_temperature(
            climate_entity, target_temp, mode="cool"
        )
        if success:
            logger.info(
                "[HawaAI] AC ON via climate entity %s → %.1f°C cool mode",
                climate_entity, target_temp,
            )
        else:
            logger.error("[HawaAI] AC ON via climate entity FAILED — not marking as ON")
    else:
        broadlink_entity = (cfg.get("broadlink_entity") or "").strip()
        cmd_on           = (cfg.get("ir_command_on") or "").strip()
        ir_device_name   = (cfg.get("ir_device_name") or "").strip()

        if not broadlink_entity:
            logger.error("[HawaAI] No climate entity or Broadlink configured — cannot turn AC ON")
            return
        if not cmd_on:
            logger.error(
                "[HawaAI] IR Power ON command is empty — "
                "set it in Settings → IR Command Mapping"
            )
            return

        success = await ha_client.send_broadlink_command(
            broadlink_entity, cmd_on, ir_device_name
        )
        if success:
            logger.info("[HawaAI] AC ON via Broadlink IR → '%s'", cmd_on)
        else:
            logger.error("[HawaAI] AC ON IR command failed — not marking as ON, will retry")

    if not success:
        return

    # Command confirmed — record kWh meter at session start
    kwh_entity = cfg.get("energy_kwh_entity", "")
    start_kwh  = None
    if kwh_entity:
        raw = await ha_client.get_state(kwh_entity)
        try:
            start_kwh = float(raw) if raw else None
        except (ValueError, TypeError):
            start_kwh = None
    _session_start_kwh = start_kwh

    _ac_is_on           = True
    _session_start_time = datetime.now(timezone.utc)
    _session_start_temp = indoor_temp
    # Start ignore window — HA entity state won't update for several seconds
    _last_command_time  = _session_start_time
    _last_command       = "on"

    weather = await weather_api.get_cached()
    await session_logger.start_session({
        "start_time":             _session_start_time.isoformat(),
        "indoor_temp_start":      indoor_temp,
        "outdoor_temp_start":     weather.get("temp") if weather else None,
        "outdoor_humidity_start": weather.get("humidity") if weather else None,
        "target_temp":            cfg.get("target_temp"),
        "ac_brand":               cfg.get("ac_brand"),
        "ac_model":               cfg.get("ac_model"),
        "room_name":              cfg.get("room_name"),
        "energy_kwh_start":       start_kwh,
    })
    logger.info(
        "[HawaAI] Session started — indoor=%.1f°C | kWh meter=%s",
        indoor_temp, start_kwh,
    )


# ── Turn AC OFF ───────────────────────────────────────────────────────────────

async def _turn_ac_off(cfg: dict, indoor_temp: float, reason: str) -> None:
    """
    Turn the AC off.

    Priority:
      1. HA climate entity turn_off
      2. Broadlink IR blast (ir_command_off)

    Always marks ac=OFF in the engine regardless of command outcome.
    If no open session exists, resets cleanly without logging session data.
    Records _last_command = "off" so a following ON command starts a clean window.
    """
    global _ac_is_on, _session_start_time, _session_start_temp, _session_start_kwh, _last_command_time, _last_command

    climate_entity = (cfg.get("climate_entity") or "").strip()

    if climate_entity:
        ok = await ha_client.set_climate_mode(climate_entity, "off")
        if ok:
            logger.info("[HawaAI] AC OFF via climate entity %s", climate_entity)
        else:
            logger.error(
                "[HawaAI] AC OFF via climate entity FAILED — marking OFF anyway"
            )
    else:
        broadlink_entity = (cfg.get("broadlink_entity") or "").strip()
        cmd_off          = (cfg.get("ir_command_off") or "").strip()
        ir_device_name   = (cfg.get("ir_device_name") or "").strip()

        if broadlink_entity and cmd_off:
            ir_ok = await ha_client.send_broadlink_command(
                broadlink_entity, cmd_off, ir_device_name
            )
            if not ir_ok:
                logger.error(
                    "[HawaAI] AC OFF IR command '%s' FAILED — marking OFF anyway to stop retry loop",
                    cmd_off,
                )
        elif not broadlink_entity:
            logger.warning(
                "[HawaAI] No Broadlink configured — marking ac=OFF without IR command"
            )
        else:
            logger.error(
                "[HawaAI] IR Power OFF command is empty — set it in Settings → IR Command Mapping"
            )

    # Always flip internal flag regardless of command outcome
    _ac_is_on          = False
    _last_command_time = datetime.now(timezone.utc)
    _last_command      = "off"

    # No open session — just reset and return cleanly
    if _session_start_time is None:
        logger.info("[HawaAI] AC OFF (%s) — no open session to close", reason)
        _session_start_kwh = None
        return

    now          = datetime.now(timezone.utc)
    cool_minutes = (now - _session_start_time).total_seconds() / 60.0

    # Calculate session kWh consumed
    kwh_consumed = None
    cost         = None
    kwh_entity   = cfg.get("energy_kwh_entity", "")
    if kwh_entity and _session_start_kwh is not None:
        raw = await ha_client.get_state(kwh_entity)
        try:
            end_kwh = float(raw) if raw else None
            if end_kwh is not None:
                kwh_consumed = round(end_kwh - _session_start_kwh, 4)
                tariff       = float(cfg.get("energy_tariff_per_kwh", 8.0))
                cost         = round(kwh_consumed * tariff, 2)
                logger.info(
                    "[HawaAI] Session energy: %.4f kWh · cost %.2f (tariff %.2f/kWh)",
                    kwh_consumed, cost, tariff,
                )
        except (ValueError, TypeError):
            pass

    await session_logger.end_session({
        "end_time":              now.isoformat(),
        "indoor_temp_end":       indoor_temp,
        "time_to_cool_minutes":  round(cool_minutes, 1),
        "reason_stopped":        reason,
        "energy_kwh":            kwh_consumed,
        "cost":                  cost,
    })

    logger.info(
        "[HawaAI] AC OFF — reason=%s | %.1fmin | kWh=%s",
        reason, cool_minutes, kwh_consumed,
    )

    _session_start_time = None
    _session_start_temp = None
    _session_start_kwh  = None


# ── Runtime state for /api/status ────────────────────────────────────────────

def get_runtime_state() -> dict:
    """Returns current in-memory runtime state for the /api/status endpoint."""
    from datetime import datetime, timezone as _tz
    now = datetime.now(_tz.utc)
    secs = (now - _last_command_time).total_seconds() if _last_command_time else None
    in_window = (
        _last_command == "on"
        and secs is not None
        and secs < _HA_STATE_DELAY_SECS
    )
    return {
        "ac_is_on":           _ac_is_on,
        "session_id":         session_logger.current_session_id(),
        "session_start_time": (
            _session_start_time.isoformat() if _session_start_time else None
        ),
        "session_start_kwh":  _session_start_kwh,
        # Diagnostic: tells Dashboard whether we're in the ignore window
        "ha_state_settling":  in_window,
        "last_command":       _last_command or None,
    }
