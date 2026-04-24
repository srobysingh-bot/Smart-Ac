"""
HawaAI core decision engine — THE BRAIN.

Called every `logic_interval_seconds` by the scheduler.

AC control architecture (v1.1.17):
  ┌──────────────────────────────────────────────────────────────────────┐
  │  CONTROL  →  Broadlink IR ONLY  (ir_command_on / ir_command_off)    │
  │  STATE    →  Power sensor (watts) — primary ground truth             │
  │             Internal _ac_is_on flag — used during 60 s cooldown      │
  │  DISPLAY  →  Climate entity read-only (temp, mode, fan, swing)       │
  └──────────────────────────────────────────────────────────────────────┘

Power-based state bands (watts):
  > 500 W   →  ON   (compressor running)
  50–500 W  →  IDLE (fan-only; compressor resting between cycles)
  < 50 W    →  OFF

Why power, not climate entity:
  - Climate entity is a cloud-integration state that can lag or be stale
  - Real physical behavior is always reflected by wall-socket power draw
  - 500 W threshold cleanly separates compressor-on from fan-only

Cooldown (60 s after any IR command):
  Immediately after the IR signal, the AC needs time to respond and the
  power draw starts from 0. During this window we trust the internal flag
  to avoid false "OFF" detection and a premature re-send of the ON command.
  After 60 s the power sensor takes over as the authoritative source.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from . import config_manager, ha_client, session_logger, weather_api
from .utils import parse_presence

logger = logging.getLogger(__name__)

# ── In-memory runtime state (not persisted across restarts) ───────────────────
_ac_is_on: bool = False                         # SOLE source of truth for AC state
_vacant_since: Optional[datetime] = None
_session_start_time: Optional[datetime] = None
_session_start_temp: Optional[float] = None
_session_start_kwh: Optional[float] = None

# Command cooldown — after any IR command, skip control logic for this many
# seconds to let the AC respond to the IR signal before re-evaluating.
_COOLDOWN_SECS: int = 60
_last_command_time: Optional[datetime] = None   # when the last IR command was sent
_last_command: str = ""                          # "on" or "off"

# Power-based state thresholds
_WATTS_COMPRESSOR: float = 500.0   # watts above this → compressor running (AC ON)
_WATTS_FAN_ONLY:   float = 50.0    # watts between FAN_ONLY and COMPRESSOR → IDLE (fan only)


async def tick() -> None:
    """
    Single decision-loop iteration.

    STEP 1  Load fresh config
    STEP 2  Guard: required entities configured?
    STEP 3  Read live indoor temp (climate entity used as fallback only)
    STEP 4  Parse presence
    STEP 5  Manual override check
    STEP 6  AC state = internal _ac_is_on flag (no HA round-trip)
    STEP 7  Cooldown gate — skip control if command was sent recently
    STEP 8  Write monitoring snapshot
    STEP 9  Vacancy logic
    STEP 10 Temperature logic (smart-adjustment aware)
    """
    global _ac_is_on, _vacant_since, _session_start_time, _session_start_temp, \
           _session_start_kwh, _last_command_time, _last_command

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

    # STEP 3 — read live indoor temperature
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
    climate_entity = cfg.get("climate_entity", "").strip()
    climate_data: dict = {}

    if climate_entity:
        climate_data = await ha_client.get_climate_state(climate_entity)

    if indoor_temp is None and climate_data:
        fallback = climate_data.get("current_temp")
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
    energy_watts: float = 0.0
    energy_watts_valid: bool = False     # True only when sensor returned a real number

    if energy_power_entity:
        energy_raw = await ha_client.get_state(energy_power_entity)
        if energy_raw not in (None, "unavailable", "unknown", ""):
            try:
                energy_watts       = float(energy_raw)
                energy_watts_valid = True
            except (ValueError, TypeError):
                energy_watts = 0.0

    # STEP 6A — Cooldown gate timer
    #
    # Compute this FIRST so it can guard the power-based state decision below.
    # The cooldown begins when an IR command is sent and lasts 60 s. During
    # this window the power draw is still rising from 0, so we must not let
    # the power sensor report "OFF" and trigger another ON command.
    now = datetime.now(timezone.utc)
    secs_since_cmd = (
        (now - _last_command_time).total_seconds()
        if _last_command_time is not None
        else float("inf")
    )
    in_cooldown = secs_since_cmd < _COOLDOWN_SECS

    # STEP 6B — Determine authoritative AC state
    #
    # Priority order:
    #   1. Power sensor (after cooldown expires) — physical ground truth
    #      > 500 W → compressor running    → ON
    #      50–500 W → fan-only / resting   → IDLE  (keep current engine state)
    #      < 50 W  → completely off        → OFF
    #   2. Internal _ac_is_on flag (during cooldown or when no power sensor)
    #
    # Climate entity is NEVER used for ON/OFF decisions.
    ac_idle: bool = False    # True when fan is running but compressor is off

    if energy_watts_valid and not in_cooldown:
        # Power sensor is the authoritative source outside the cooldown window.
        if energy_watts > _WATTS_COMPRESSOR:
            ac_on   = True
            ac_idle = False
            # Sync internal flag if AC was externally turned on (e.g. via physical remote)
            if not _ac_is_on:
                logger.info(
                    "[HawaAI] AC confirmed ON by power sensor (%.0f W > %.0f W threshold) "
                    "— syncing internal flag", energy_watts, _WATTS_COMPRESSOR,
                )
                _ac_is_on = True
        elif energy_watts >= _WATTS_FAN_ONLY:
            # IDLE zone: compressor is resting between cycles. Keep current state
            # so we don't oscillate. The engine already knows its intent.
            ac_on   = _ac_is_on
            ac_idle = True
        else:
            # < 50 W → AC is genuinely off (compressor and fan both stopped)
            ac_on   = False
            ac_idle = False
            if _ac_is_on:
                logger.info(
                    "[HawaAI] AC confirmed OFF by power sensor (%.0f W < %.0f W threshold) "
                    "— syncing internal flag", energy_watts, _WATTS_FAN_ONLY,
                )
                _ac_is_on = False
        power_source = "watts"
    else:
        # No valid power reading or inside cooldown — trust internal flag.
        ac_on        = _ac_is_on
        ac_idle      = False
        power_source = "cooldown" if in_cooldown else "internal"

    ac_state_label = (
        f"IDLE({energy_watts:.0f}W)" if ac_idle
        else f"ON({energy_watts:.0f}W)"  if ac_on
        else "OFF"
    )
    logger.info(
        "[HawaAI] TICK | indoor=%.1f°C | outdoor=%s | presence=%s | ac=%s "
        "[src=%s] | target=%.1f°C (eff=%.1f°C)",
        indoor_temp,
        f"{outdoor_temp:.1f}°C" if outdoor_temp is not None else "—",
        "occupied" if is_occupied else "vacant",
        ac_state_label, power_source,
        target_temp, effective_target,
    )

    # STEP 7 — Cooldown gate
    if in_cooldown:
        logger.info(
            "[HawaAI] Cooldown active — %.0fs / %ds since '%s' command — "
            "skipping control logic this tick",
            secs_since_cmd, _COOLDOWN_SECS, _last_command,
        )

    # STEP 8 — Write monitoring snapshot
    await session_logger.add_snapshot(
        session_logger.current_session_id(),
        {
            "timestamp":    now.isoformat(),
            "indoor_temp":  indoor_temp,
            "outdoor_temp": outdoor_temp,
            "ac_state":     ac_on,
            "watt_draw":    energy_watts,
            "presence":     is_occupied,
        },
    )

    if in_cooldown:
        return  # skip STEP 9 and STEP 10 during cooldown

    # STEP 9 — VACANCY LOGIC
    if use_presence and not is_occupied:
        if _vacant_since is None:
            _vacant_since = now
            logger.info("[HawaAI] Room became vacant — vacancy timer started")

        vacancy_duration = (now - _vacant_since).total_seconds()
        logger.info(
            "[HawaAI] Vacant %.0fs / timeout %ds | AC=%s",
            vacancy_duration, vacancy_timeout, "ON" if ac_on else "OFF",
        )

        if ac_on and vacancy_duration >= vacancy_timeout:
            logger.info(
                "[HawaAI] Vacancy timeout reached (%.0fs) — turning AC OFF", vacancy_duration
            )
            await _turn_ac_off(cfg, indoor_temp, reason="vacant")

        return  # never run temp logic while vacant

    # Room occupied (or presence disabled) — reset vacancy timer
    _vacant_since = None

    # STEP 10 — TEMPERATURE LOGIC
    upper = effective_target + hysteresis   # turn ON  above this
    lower = effective_target - hysteresis   # turn OFF below this

    if indoor_temp > upper and not ac_on:
        logger.info("[HawaAI] Too warm (%.1f°C > %.1f°C) — turning AC ON", indoor_temp, upper)
        await _turn_ac_on(cfg, indoor_temp)

    elif indoor_temp <= lower and ac_on:
        logger.info("[HawaAI] Room cooled (%.1f°C ≤ %.1f°C) — turning AC OFF", indoor_temp, lower)
        session_logger.mark_cooled()
        await _turn_ac_off(cfg, indoor_temp, reason="cooled")

    elif indoor_temp <= effective_target and ac_on:
        session_logger.mark_cooled()


# ── Turn AC ON ────────────────────────────────────────────────────────────────

async def _turn_ac_on(cfg: dict, indoor_temp: float) -> None:
    """
    Send AC ON command via Broadlink IR (the ONLY control method).

    The climate entity is NOT used for ON/OFF control — it is display-only.
    Internal flag _ac_is_on is set True only on confirmed IR success.
    Starts a session and records the kWh meter baseline.
    Sets _last_command="on" to activate the 60 s cooldown on the next tick.
    """
    global _ac_is_on, _session_start_time, _session_start_temp, \
           _session_start_kwh, _last_command_time, _last_command

    broadlink_entity = (cfg.get("broadlink_entity") or "").strip()
    cmd_on           = (cfg.get("ir_command_on")    or "").strip()
    ir_device_name   = (cfg.get("ir_device_name")   or "").strip()

    if not broadlink_entity:
        logger.error(
            "[HawaAI] AC ON FAILED — no Broadlink entity configured. "
            "Set 'broadlink_entity' in Settings → IR Command Mapping."
        )
        return
    if not cmd_on:
        logger.error(
            "[HawaAI] AC ON FAILED — IR ON command is empty. "
            "Set 'ir_command_on' in Settings → IR Command Mapping."
        )
        return

    success = await ha_client.send_broadlink_command(
        broadlink_entity, cmd_on, ir_device_name
    )
    if not success:
        logger.error(
            "[HawaAI] AC ON IR command '%s' FAILED — not marking as ON, will retry next tick",
            cmd_on,
        )
        return

    logger.info("[HawaAI] AC ON via Broadlink IR → command='%s'", cmd_on)

    # Record kWh meter baseline for this session
    kwh_entity = cfg.get("energy_kwh_entity", "")
    start_kwh  = None
    if kwh_entity:
        raw = await ha_client.get_state(kwh_entity)
        try:
            start_kwh = float(raw) if raw else None
        except (ValueError, TypeError):
            start_kwh = None
    _session_start_kwh = start_kwh

    # Update internal state
    _ac_is_on           = True
    _session_start_time = now = datetime.now(timezone.utc)
    _session_start_temp = indoor_temp
    _last_command_time  = now
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
    Send AC OFF command via Broadlink IR (the ONLY control method).

    The climate entity is NOT used for ON/OFF control — it is display-only.
    Internal flag _ac_is_on is set False regardless of IR outcome so we don't
    get stuck in an ON state if the IR fails.
    Closes the open session and calculates energy consumed.
    Sets _last_command="off" to activate the 60 s cooldown on the next tick.
    """
    global _ac_is_on, _session_start_time, _session_start_temp, \
           _session_start_kwh, _last_command_time, _last_command

    broadlink_entity = (cfg.get("broadlink_entity") or "").strip()
    cmd_off          = (cfg.get("ir_command_off")   or "").strip()
    ir_device_name   = (cfg.get("ir_device_name")   or "").strip()

    if broadlink_entity and cmd_off:
        ir_ok = await ha_client.send_broadlink_command(
            broadlink_entity, cmd_off, ir_device_name
        )
        if ir_ok:
            logger.info("[HawaAI] AC OFF via Broadlink IR → command='%s'", cmd_off)
        else:
            logger.error(
                "[HawaAI] AC OFF IR command '%s' FAILED — "
                "marking OFF anyway to prevent stuck-ON state",
                cmd_off,
            )
    elif not broadlink_entity:
        logger.warning(
            "[HawaAI] No Broadlink configured — marking ac=OFF without sending IR command"
        )
    else:
        logger.error(
            "[HawaAI] IR OFF command is empty — set it in Settings → IR Command Mapping"
        )

    # Always flip internal flag regardless of IR outcome
    _ac_is_on          = False
    _last_command_time = datetime.now(timezone.utc)
    _last_command      = "off"

    # No open session — reset cleanly
    if _session_start_time is None:
        logger.info("[HawaAI] AC OFF (%s) — no open session to close", reason)
        _session_start_kwh = None
        return

    now          = datetime.now(timezone.utc)
    cool_minutes = (now - _session_start_time).total_seconds() / 60.0

    # Calculate session energy consumed
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
    now            = datetime.now(_tz.utc)
    secs_since_cmd = (now - _last_command_time).total_seconds() if _last_command_time else None
    in_cooldown    = (
        secs_since_cmd is not None
        and secs_since_cmd < _COOLDOWN_SECS
    )
    return {
        "ac_is_on":              _ac_is_on,
        "session_id":            session_logger.current_session_id(),
        "session_start_time":    (
            _session_start_time.isoformat() if _session_start_time else None
        ),
        "session_start_kwh":     _session_start_kwh,
        # Diagnostics
        "cooldown_active":       in_cooldown,
        "last_command":          _last_command or None,
        "secs_since_cmd":        round(secs_since_cmd, 1) if secs_since_cmd is not None else None,
        "watts_on_threshold":    _WATTS_COMPRESSOR,
        "watts_idle_threshold":  _WATTS_FAN_ONLY,
    }
