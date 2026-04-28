"""
HawaAI core decision engine — THE BRAIN.

Called every `logic_interval_seconds` by the scheduler.

AC control architecture (v1.2.0):
  ┌───────────────────────────────────────────────────────────────────────┐
  │  CONTROL  →  Aerostate (climate entity) via ac_adapter               │
  │               ac_adapter → HA climate services → Broadlink → AC      │
  │  STATE    →  Power sensor (watts) — primary ground truth              │
  │               Internal _ac_is_on flag — used during 60 s cooldown     │
  │  DISPLAY  →  Climate entity read-only (temp, mode, fan, swing)        │
  └───────────────────────────────────────────────────────────────────────┘

Power-based state bands (watts):
  > 500 W   →  ON   (compressor running)
  50–500 W  →  IDLE (fan-only; compressor resting between cycles)
  < 50 W    →  OFF

Why power sensor for STATE detection (not climate entity):
  - Climate entity is a cloud-integration state that can lag or be stale
  - Real physical behavior is always reflected by wall-socket power draw
  - 500 W threshold cleanly separates compressor-on from fan-only

Cooldown (60 s after any climate command):
  Immediately after the command, the AC needs time to respond and the
  power draw starts from 0. During this window we trust the internal flag
  to avoid false "OFF" detection and a premature re-send of the ON command.
  After 60 s the power sensor takes over as the authoritative source.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import ac_adapter, config_manager, ha_client, session_logger, smart_cooling, weather_api
from . import room_registry
from .ai import (
    apply_ai_fan,
    fetch_ai_in_background,
    get_cached,
    should_run_ai,
    throttle_cache_use_log,
)
from .ai.ai_validator import AI_MAX_T, AI_MIN_T
from .utils import parse_presence

logger = logging.getLogger(__name__)

@dataclass
class RoomRuntime:
    """Isolated logic-engine state per room."""

    last_ai_enabled: Optional[bool] = None
    ac_is_on: bool = False
    startup_sync_done: bool = False
    vacant_since: Optional[datetime] = None
    session_start_time: Optional[datetime] = None
    session_start_temp: Optional[float] = None
    session_start_kwh: Optional[float] = None
    watts_samples: List[float] = field(default_factory=list)
    last_command_time: Optional[datetime] = None
    last_command: str = ""


_runtime_by_room: Dict[str, RoomRuntime] = {}


def _rt(room_id: str) -> RoomRuntime:
    if room_id not in _runtime_by_room:
        _runtime_by_room[room_id] = RoomRuntime()
    return _runtime_by_room[room_id]


# Command cooldown — after any climate command, skip control logic for this window.
_COOLDOWN_SECS: int = 60

# Power-based state thresholds
_WATTS_COMPRESSOR: float = 500.0   # watts above this → compressor running (AC ON)
_WATTS_FAN_ONLY:   float = 50.0    # watts between FAN_ONLY and COMPRESSOR → IDLE (fan only)


def smart_temp_adjustment_enabled(cfg: dict) -> bool:
    """
    Smart outdoor-based target adjustment.

    Prefer explicit ``smart_enabled`` (bool) when present in merged config;
    otherwise ``smart_temp_adjustment``. If the latter is missing, default True
    for backward compatibility with older installs that only had the key in UI.
    """
    se = cfg.get("smart_enabled")
    if isinstance(se, bool):
        return se
    v = cfg.get("smart_temp_adjustment")
    if v is None:
        return True
    return bool(v)


def compute_effective_target(
    target_temp: float,
    outdoor_temp: Optional[float],
    smart_enabled: bool,
) -> float:
    """Outdoor-aware setpoint (same formula as historical HawaAI smart adj)."""
    if not smart_enabled or outdoor_temp is None:
        return target_temp
    if outdoor_temp < 30:
        return target_temp + 1.0
    if outdoor_temp < 35:
        return target_temp + 0.5
    if outdoor_temp <= 40:
        return target_temp
    return target_temp - 1.0


async def tick(room_id: str) -> None:
    """
    Single decision-loop iteration for one room.
    """
    st = _rt(room_id)
    # STEP 1 — fresh config every tick (global + room merge)
    base_cfg = config_manager.load_config()
    room_def = room_registry.get_room(base_cfg, room_id)
    if not room_def:
        logger.debug("[HawaAI] tick skipped — unknown room_id=%s", room_id)
        return
    if not (str(room_def.get("climate_entity") or "")).strip():
        logger.debug("[HawaAI] tick skipped [%s] — no climate_entity", room_id)
        return
    cfg = room_registry.merge_room_config(base_cfg, room_def)

    _ae = bool(cfg.get("ai_enabled", False))
    if st.last_ai_enabled is not None and _ae != st.last_ai_enabled:
        logger.info("[AI][%s] %s", room_id, "Enabled" if _ae else "Disabled")
    st.last_ai_enabled = _ae

    # STEP 2 — guard: can't run without at least indoor temp + presence
    presence_entity    = cfg.get("presence_entity", "")
    indoor_temp_entity = cfg.get("indoor_temp_entity", "")

    if not presence_entity or not indoor_temp_entity:
        logger.warning(
            "[HawaAI][%s] Logic skipped — missing entity config (presence=%s, temp=%s)",
            room_id,
            bool(presence_entity), bool(indoor_temp_entity),
        )
        return

    # STEP 2.5 — startup sync
    if not st.startup_sync_done:
        ce = (cfg.get("climate_entity") or "").strip()
        if ce:
            cd = await ha_client.get_climate_state(ce)
            st_raw = (cd.get("state") or "off").lower()
            if st_raw == "cool":
                st.ac_is_on = True
                logger.info(
                    "[HawaAI][%s] Startup sync → AC already ON (cool), skipping first command cycle",
                    room_id,
                )
            elif st_raw in ("off", "unavailable", "unknown", ""):
                st.ac_is_on = False
                logger.info(
                    "[HawaAI][%s] Startup sync → AC OFF (%s), skipping first command cycle",
                    room_id,
                    st_raw or "off",
                )
            else:
                st.ac_is_on = True
                logger.info(
                    "[HawaAI][%s] Startup sync → AC already ON (mode=%s), skipping first command cycle",
                    room_id,
                    st_raw,
                )
            st.startup_sync_done = True
            return
        st.startup_sync_done = True

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
    smart_adj       = smart_temp_adjustment_enabled(cfg)

    # Weather — needed for smart adjustment and snapshot
    weather      = await weather_api.get_cached()
    outdoor_temp = weather.get("temp") if weather else None

    # ── Smart Temperature Adjustment (base; optional AI may soft-override below) ─
    base_effective = compute_effective_target(target_temp, outdoor_temp, smart_adj)
    if smart_adj:
        if outdoor_temp is None:
            logger.info(
                "[HawaAI] Smart adj: enabled — no outdoor temp yet → effective=%.1f°C (config)",
                base_effective,
            )
        elif base_effective != target_temp:
            logger.info(
                "[HawaAI] Smart adj: outdoor=%.1f°C → effective %.1f°C (config=%.1f°C)",
                outdoor_temp, base_effective, target_temp,
            )
        else:
            logger.info(
                "[HawaAI] Smart adj: outdoor=%.1f°C → effective unchanged at %.1f°C",
                outdoor_temp, base_effective,
            )
    effective_target = base_effective

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

    energy_kwh_reading: Optional[float] = None
    kwh_entity_snap = (cfg.get("energy_kwh_entity") or "").strip()
    if kwh_entity_snap:
        raw_ke = await ha_client.get_state(kwh_entity_snap)
        if raw_ke not in (None, "unavailable", "unknown", ""):
            try:
                energy_kwh_reading = float(raw_ke)
            except (ValueError, TypeError):
                energy_kwh_reading = None

    # STEP 6A — Cooldown gate timer
    #
    # Compute this FIRST so it can guard the power-based state decision below.
    # The cooldown begins when an IR command is sent and lasts 60 s. During
    # this window the power draw is still rising from 0, so we must not let
    # the power sensor report "OFF" and trigger another ON command.
    now = datetime.now(timezone.utc)
    secs_since_cmd = (
        (now - st.last_command_time).total_seconds()
        if st.last_command_time is not None
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
    #   2. Internal ac_is_on flag (during cooldown or when no power sensor)
    #
    # Climate entity is NEVER used for ON/OFF decisions.
    ac_idle: bool = False    # True when fan is running but compressor is off

    if energy_watts_valid and not in_cooldown:
        # Power sensor is the authoritative source outside the cooldown window.
        if energy_watts > _WATTS_COMPRESSOR:
            ac_on   = True
            ac_idle = False
            # Sync internal flag if AC was externally turned on (e.g. via physical remote)
            if not st.ac_is_on:
                logger.info(
                    "[HawaAI][%s] AC confirmed ON by power sensor (%.0f W > %.0f W threshold) "
                    "— syncing internal flag", room_id, energy_watts, _WATTS_COMPRESSOR,
                )
                st.ac_is_on = True
        elif energy_watts >= _WATTS_FAN_ONLY:
            # IDLE zone: compressor is resting between cycles. Keep current state
            # so we don't oscillate. The engine already knows its intent.
            ac_on   = st.ac_is_on
            ac_idle = True
        else:
            # < 50 W → AC is genuinely off (compressor and fan both stopped)
            ac_on   = False
            ac_idle = False
            if st.ac_is_on:
                logger.info(
                    "[HawaAI][%s] AC confirmed OFF by power sensor (%.0f W < %.0f W threshold) "
                    "— syncing internal flag", room_id, energy_watts, _WATTS_FAN_ONLY,
                )
                st.ac_is_on = False
                # ── CRITICAL: close any open session ─────────────────────────
                if st.session_start_time is not None:
                    logger.info(
                        "[HawaAI][%s] External power-off detected — finalizing open session",
                        room_id,
                    )
                    await _close_session(room_id, cfg, indoor_temp, reason="power_off")
        power_source = "watts"
    else:
        # No valid power reading or inside cooldown — trust internal flag.
        ac_on        = st.ac_is_on
        ac_idle      = False
        power_source = "cooldown" if in_cooldown else "internal"

    ac_state_label = (
        f"IDLE({energy_watts:.0f}W)" if ac_idle
        else f"ON({energy_watts:.0f}W)"  if ac_on
        else "OFF"
    )
    logger.info(
        "[HawaAI][%s] TICK | indoor=%.1f°C | outdoor=%s | presence=%s | ac=%s "
        "[src=%s] | target=%.1f°C (eff=%.1f°C)",
        room_id,
        indoor_temp,
        f"{outdoor_temp:.1f}°C" if outdoor_temp is not None else "—",
        "occupied" if is_occupied else "vacant",
        ac_state_label, power_source,
        target_temp, base_effective,
    )

    # STEP 7 — Cooldown gate
    if in_cooldown:
        logger.info(
            "[HawaAI][%s] Cooldown active — %.0fs / %ds since '%s' command — "
            "skipping control logic this tick",
            room_id,
            secs_since_cmd, _COOLDOWN_SECS, st.last_command,
        )

    # STEP 8 — Write monitoring snapshot
    outdoor_humidity = weather.get("humidity") if weather else None
    ai_rec = get_cached(room_id) if cfg.get("ai_enabled", False) else None
    sp = None
    fm = None
    if climate_data:
        sp = climate_data.get("target_temp")
        fm = climate_data.get("fan_mode")

    ai_tgt = ai_fan = ai_conf = None
    if ai_rec:
        try:
            if ai_rec.get("target_temp") is not None:
                ai_tgt = float(ai_rec["target_temp"])
        except (TypeError, ValueError):
            pass
        try:
            if ai_rec.get("fan_mode") is not None:
                ai_fan = str(ai_rec["fan_mode"])
        except (TypeError, ValueError):
            pass
        try:
            if ai_rec.get("confidence") is not None:
                ai_conf = float(ai_rec["confidence"])
        except (TypeError, ValueError):
            pass

    await session_logger.add_snapshot(
        room_id,
        session_logger.current_session_id(room_id),
        {
            "timestamp":       now.isoformat(),
            "indoor_temp":   indoor_temp,
            "outdoor_temp":  outdoor_temp,
            "outdoor_humidity": outdoor_humidity,
            "ac_state":      ac_on,
            "watt_draw":     energy_watts,
            "presence":      is_occupied,
            "setpoint":      sp,
            "fan_mode":      fm,
            "energy_kwh":    energy_kwh_reading,
            "ai_target_temp": ai_tgt,
            "ai_fan_mode": ai_fan,
            "ai_confidence": ai_conf,
        },
    )

    # Collect a watts reading for this tick (used for energy calculation at session end).
    if session_logger.current_session_id(room_id) and energy_watts_valid:
        st.watts_samples.append(energy_watts)

    if in_cooldown:
        return  # skip STEP 9 and STEP 10 during cooldown

    # STEP 9 — VACANCY LOGIC
    if use_presence and not is_occupied:
        if st.vacant_since is None:
            st.vacant_since = now
            logger.info("[HawaAI][%s] Room became vacant — vacancy timer started", room_id)

        vacancy_duration = (now - st.vacant_since).total_seconds()
        logger.info(
            "[HawaAI][%s] Vacant %.0fs / timeout %ds | AC=%s",
            room_id,
            vacancy_duration, vacancy_timeout, "ON" if ac_on else "OFF",
        )

        if ac_on and vacancy_duration >= vacancy_timeout:
            logger.info(
                "[HawaAI][%s] Vacancy timeout reached (%.0fs) — turning AC OFF",
                room_id,
                vacancy_duration,
            )
            await _turn_ac_off(room_id, cfg, indoor_temp, reason="vacant")

        return  # never run temp logic while vacant

    # Room occupied (or presence disabled) — reset vacancy timer
    st.vacant_since = None

    # AI soft override (non-blocking fetch; cached setpoint + fan, occupied only)
    if cfg.get("ai_enabled", False) and is_occupied:
        if should_run_ai(room_id, cfg, is_occupied, indoor_temp):
            fetch_ai_in_background(
                room_id,
                cfg, indoor_temp, target_temp, base_effective, outdoor_temp, is_occupied,
            )
        rec = get_cached(room_id)
        if rec and throttle_cache_use_log(room_id):
            logger.debug("[AI][%s] Cached used", room_id)
        if rec and is_occupied and rec.get("action") and rec.get("action") != "none":
            try:
                ai_t = float(rec.get("target_temp", effective_target))
                if AI_MIN_T <= ai_t <= AI_MAX_T:
                    new_eff = min(effective_target, ai_t)
                    if new_eff < effective_target - 0.01:
                        logger.debug(
                            "[AI] Target adjusted (safe clamp) %.1f°C → %.1f°C (AI suggested %.1f°C)",
                            effective_target, new_eff, ai_t,
                        )
                    effective_target = new_eff
            except (TypeError, ValueError):
                pass

    # STEP 10 — TEMPERATURE LOGIC
    upper = effective_target + hysteresis   # turn ON  above this
    lower = effective_target - hysteresis   # turn OFF below this

    if indoor_temp > upper and not ac_on:
        logger.info("[HawaAI][%s] Too warm (%.1f°C > %.1f°C) — turning AC ON", room_id, indoor_temp, upper)
        await _turn_ac_on(room_id, cfg, indoor_temp, effective_target)

    elif indoor_temp <= lower and ac_on:
        logger.info("[HawaAI][%s] Room cooled (%.1f°C ≤ %.1f°C) — turning AC OFF", room_id, indoor_temp, lower)
        session_logger.mark_cooled(room_id)
        await _turn_ac_off(room_id, cfg, indoor_temp, reason="cooled")

    elif indoor_temp <= effective_target and ac_on:
        session_logger.mark_cooled(room_id)

    if smart_adj and climate_entity and ac_on:
        await smart_cooling.apply_effective_target(
            room_id,
            climate_entity   = climate_entity,
            effective_target = effective_target,
            current_target   = climate_data.get("target_temp"),
            ac_on            = ac_on,
            manual_override  = False,
        )

    _rec = get_cached(room_id) if cfg.get("ai_enabled", False) else None
    _use_ai_fan = (
        _rec
        and is_occupied
        and _rec.get("action") not in (None, "none")
        and _rec.get("fan_mode")
    )
    if _use_ai_fan:
        await apply_ai_fan(
            room_id,
            climate_entity,
            str(_rec.get("fan_mode", "auto")),
            str(_rec.get("action", "none")),
        )
    elif cfg.get("smart_cooling_enabled", False):
        await smart_cooling.apply_smart_cooling(
            room_id,
            indoor_temp     = indoor_temp,
            target_temp     = effective_target,
            ac_on           = ac_on,
            ac_idle         = ac_idle,
            is_occupied     = is_occupied,
            manual_override = cfg.get("manual_override", False),
            climate_entity  = climate_entity,
            enabled         = True,
        )


# ── Turn AC ON ────────────────────────────────────────────────────────────────

async def _turn_ac_on(
    room_id: str,
    cfg: dict,
    indoor_temp: float,
    effective_target: Optional[float] = None,
) -> None:
    """Turn AC ON for one room; updates RoomRuntime + per-room session."""
    st = _rt(room_id)

    climate_entity = (cfg.get("climate_entity") or "").strip()
    if not climate_entity:
        logger.error(
            "[HawaAI][%s] AC ON FAILED — no climate entity configured.",
            room_id,
        )
        return

    target = effective_target if effective_target is not None else float(cfg.get("target_temp", 24))

    success = await ac_adapter.turn_on(
        entity_id   = climate_entity,
        temperature = target,
        fan_mode    = "auto",
        hvac_mode   = "cool",
    )
    if not success:
        logger.error(
            "[HawaAI][%s] AC ON via Aerostate FAILED — not marking as ON, will retry next tick",
            room_id,
        )
        return

    kwh_entity = cfg.get("energy_kwh_entity", "")
    start_kwh = None
    if kwh_entity:
        raw = await ha_client.get_state(kwh_entity)
        try:
            start_kwh = float(raw) if raw else None
        except (ValueError, TypeError):
            start_kwh = None
    st.session_start_kwh = start_kwh

    st.ac_is_on = True
    st.session_start_time = now = datetime.now(timezone.utc)
    st.session_start_temp = indoor_temp
    st.last_command_time = now
    st.last_command = "on"
    st.watts_samples = []

    weather = await weather_api.get_cached()
    await session_logger.start_session(room_id, {
        "start_time":             st.session_start_time.isoformat(),
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
        "[HawaAI][%s] Session started — indoor=%.1f°C | kWh meter=%s",
        room_id,
        indoor_temp, start_kwh,
    )


async def _close_session(room_id: str, cfg: dict, indoor_temp: float, reason: str) -> None:
    """Finalize session for one room."""
    st = _rt(room_id)

    if st.session_start_time is None:
        logger.debug("[HawaAI][%s] _close_session(%s) — no open session, skipping", room_id, reason)
        return

    now = datetime.now(timezone.utc)
    duration_secs = (now - st.session_start_time).total_seconds()
    cool_minutes = duration_secs / 60.0

    logger.info(
        "[HawaAI][%s] [SESSION END] reason=%s | duration=%.0fs (%.1f min)",
        room_id,
        reason, duration_secs, cool_minutes,
    )

    if st.watts_samples:
        avg_watts = sum(st.watts_samples) / len(st.watts_samples)
        peak_watts = max(st.watts_samples)
    else:
        avg_watts = 0.0
        peak_watts = None

    logger.info("[HawaAI][%s] Avg watts: %.0f W (%d samples)", room_id, avg_watts, len(st.watts_samples))

    kwh_consumed: Optional[float] = None
    energy_from_meter = False
    kwh_entity = (cfg.get("energy_kwh_entity") or "").strip()
    if kwh_entity and st.session_start_kwh is not None:
        raw_end = await ha_client.get_state(kwh_entity)
        if raw_end not in (None, "unavailable", "unknown", ""):
            try:
                end_k = float(raw_end)
                kwh_consumed = max(0.0, round(end_k - float(st.session_start_kwh), 4))
                energy_from_meter = True
            except (ValueError, TypeError):
                kwh_consumed = None

    if kwh_consumed is None:
        if st.watts_samples and avg_watts >= 100.0 and duration_secs > 0:
            kwh_consumed = max(0.0, (avg_watts * duration_secs) / 3_600_000.0)
            kwh_consumed = round(kwh_consumed, 4)

    tariff = float(cfg.get("energy_tariff_per_kwh", 8.0))
    cost: Optional[float] = (
        round(kwh_consumed * tariff, 2) if kwh_consumed is not None else None
    )
    if kwh_consumed is not None:
        logger.info(
            "[HawaAI][%s] Session energy: %.4f kWh (%s) | Cost: ₹%.2f",
            room_id,
            kwh_consumed,
            "meter" if energy_from_meter else "estimated from power",
            cost or 0.0,
        )
    else:
        logger.info("[HawaAI][%s] Energy: N/A (no meter / power data) | Cost: N/A", room_id)

    await session_logger.end_session(room_id, {
        "end_time":              now.isoformat(),
        "indoor_temp_end":       indoor_temp,
        "time_to_cool_minutes":  round(cool_minutes, 1),
        "reason_stopped":        reason,
        "energy_kwh":            kwh_consumed,
        "cost":                  cost,
        "avg_watts":             round(avg_watts, 1) if avg_watts else None,
        "peak_watts":            round(peak_watts, 1) if peak_watts is not None else None,
        "user_override":         1 if reason in ("power_off", "manual", "manual_off") else 0,
    })

    st.session_start_time = None
    st.session_start_temp = None
    st.session_start_kwh = None
    st.watts_samples = []
    smart_cooling.reset(room_id)


async def _turn_ac_off(room_id: str, cfg: dict, indoor_temp: float, reason: str) -> None:
    st = _rt(room_id)
    climate_entity = (cfg.get("climate_entity") or "").strip()

    await ac_adapter.turn_off(climate_entity)

    st.ac_is_on = False
    st.last_command_time = datetime.now(timezone.utc)
    st.last_command = "off"

    await _close_session(room_id, cfg, indoor_temp, reason)


def get_runtime_state(room_id: str) -> dict:
    """In-memory runtime state for /api/rooms/{id}/status."""
    from datetime import datetime, timezone as _tz

    st = _rt(room_id)
    now = datetime.now(_tz.utc)
    secs_since_cmd = (now - st.last_command_time).total_seconds() if st.last_command_time else None
    in_cooldown = (
        secs_since_cmd is not None
        and secs_since_cmd < _COOLDOWN_SECS
    )
    sc = smart_cooling.get_state(room_id)
    base_cfg = config_manager.load_config()
    room_def = room_registry.get_room(base_cfg, room_id)
    merged = room_registry.merge_room_config(base_cfg, room_def) if room_def else base_cfg
    _ce = get_cached(room_id) if merged.get("ai_enabled") else None
    return {
        "ac_is_on":              st.ac_is_on,
        "ai_enabled":            bool(merged.get("ai_enabled", False)),
        "ai_cached":             _ce is not None,
        "session_id":            session_logger.current_session_id(room_id),
        "session_start_time":    (
            st.session_start_time.isoformat() if st.session_start_time else None
        ),
        "session_start_kwh":     st.session_start_kwh,
        "cooldown_active":       in_cooldown,
        "last_command":          st.last_command or None,
        "secs_since_cmd":        round(secs_since_cmd, 1) if secs_since_cmd is not None else None,
        "watts_on_threshold":    _WATTS_COMPRESSOR,
        "watts_idle_threshold":  _WATTS_FAN_ONLY,
        "smart_mode":            sc["smart_mode"],
        "smart_fan_mode":        sc["smart_fan_mode"],
        "last_applied_target":   sc.get("last_applied_target"),
    }
