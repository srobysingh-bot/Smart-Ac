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

Hardware ON/OFF: only ``tick()`` → ``_turn_ac_on`` / ``_turn_ac_off`` → ``ac_adapter``.
AI adjusts targets only (`_get_ai_target_adjustment`); it never invokes ``ac_adapter`` or turn helpers.

Runtime isolation: ``_runtime_by_room`` maps one ``RoomRuntime`` per trimmed ``room_id`` (via ``_rt()``).
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from . import ac_adapter, config_manager, database, ha_client, session_logger, smart_cooling, weather_api
from . import room_registry
from .ai import (
    apply_ai_fan,
    ai_cache,
    fetch_ai_in_background,
    get_cached,
    should_run_ai,
    throttle_cache_use_log,
)
from .ai.ai_validator import AI_MAX_T, AI_MIN_T
from .utils import parse_presence
from .temperature_schedule import (
    apply_ai_bounded_adjustment,
    log_target_resolve,
    resolve_base_target_temp,
)

logger = logging.getLogger(__name__)

@dataclass
class RoomRuntime:
    """Isolated logic-engine state per room."""

    last_ai_enabled: Optional[bool] = None
    ac_is_on: bool = False
    vacant_since: Optional[datetime] = None
    session_start_time: Optional[datetime] = None
    session_start_temp: Optional[float] = None
    session_start_kwh: Optional[float] = None
    watts_samples: List[float] = field(default_factory=list)
    last_command_time: Optional[datetime] = None
    last_command: str = ""
    # Anti-spam: last temperature we commanded to the AC (setpoint) + wall time
    last_applied_setpoint: Optional[float] = None
    last_setpoint_command_at: Optional[datetime] = None
    last_schedule_slot: Optional[str] = None
    # HA setpoint sampled previous tick — detect intentional user knob changes vs drift
    prev_ha_setpoint_seen: Optional[float] = None
    manual_override_until: Optional[datetime] = None
    manual_override_temp: Optional[float] = None
    last_sent_command_key: Optional[str] = None
    compressor_on_since: Optional[datetime] = None
    compressor_off_since: Optional[datetime] = None
    # Epoch seconds (UTC wall); set when we observe ON/OFF transitions (command + power sync).
    last_ac_on_at: Optional[float] = None
    last_ac_off_at: Optional[float] = None
    # Epoch seconds — last tick where power thresholds confirmed compressor ON / standby OFF.
    last_power_confirmed_on: Optional[float] = None
    last_power_confirmed_off: Optional[float] = None
    # Wall time when compressor watts first crossed high threshold (debounce for session confirm)
    compressor_watts_high_since: Optional[datetime] = None

    # ── Single source of truth — set once per tick, read everywhere ──
    effective_ac_on: bool = False
    effective_ac_idle: bool = False
    effective_power_source: str = "init"       # "watts" | "cooldown" | "internal"
    # Manual remote likely ON — wall power not high yet (epoch timestamp, UTC)
    possible_on_since: Optional[float] = None
    # Confidence for UI overlay: watts path vs transient inference vs cooldown/internal
    ac_state_source: str = "system"
    effective_control_source: str = "none"     # safety_vacant | manual | schedule | thermostat | ai | cooldown | none
    effective_target_temp: float = 24.0

    # ── Command authority lock ──
    last_user_command_time: Optional[datetime] = None
    last_command_source: str = "system"                  # "user" | "system"

    # Last trusted occupancy reading when presence sensor is flaky (None/unavailable).
    last_known_presence: Optional[bool] = None

    # ── Startup recovery flag ──
    startup_state_loaded: bool = False

    # Session lifecycle — idle | provisional | confirmed (ended → idle after DB close)
    session_state: str = "idle"
    # First tick we believe the room is actively being cooled (effective ON); used for vacancy grace
    effective_on_since_ts: Optional[float] = None
    # Last IR / control ON or OFF command applied (wall time, UTC)
    last_decision_at: Optional[datetime] = None


_runtime_by_room: Dict[str, RoomRuntime] = {}
# Keys are canonical `normalize_room_id` strings — isolated per logical room.


def normalize_room_id(room_id: str) -> str:
    """Canonical room key: lower-case + strip — use for runtime, sessions, telemetry."""
    return (room_id or "").strip().lower()


def resolve_room_definition(base_cfg: dict, room_id: str):
    """Find room dict; match is case-insensitive on id after strip."""
    rid_plain = (room_id or "").strip()
    if not rid_plain:
        return None
    r0 = room_registry.get_room(base_cfg, rid_plain)
    if r0:
        return r0
    nid = normalize_room_id(rid_plain)
    for row in room_registry.list_room_dicts(base_cfg):
        if normalize_room_id(str(row.get("id") or "")) == nid:
            return row
    return None


def _rt(room_id: str) -> RoomRuntime:
    """Return runtime state strictly for canonical ``normalize_room_id``; no cross-room bleed."""
    rid = normalize_room_id(room_id)
    if rid not in _runtime_by_room:
        _runtime_by_room[rid] = RoomRuntime()
    return _runtime_by_room[rid]


# Command cooldown — after any climate command, skip control logic for this window.
_COOLDOWN_SECS: int = 60

# Power-based state thresholds
_WATTS_COMPRESSOR: float = 500.0   # watts above this → compressor running (AC ON)
_WATTS_FAN_ONLY:   float = 50.0    # watts between FAN_ONLY and COMPRESSOR → IDLE (fan only)

# Probable manual-ON inference: occupy + hot vs target while watts have not risen yet (seconds)
TRANSIENT_ON_WINDOW_SECS: float = 180.0
MIN_SESSION_SECONDS: float = 30.0
COMPRESSOR_STABLE_SECONDS: float = 10.0
VACANCY_SESSION_GRACE_SECONDS: float = 120.0
MIN_ON_TIME_SECONDS: float = 60.0
DECISION_LOCK_SECONDS: float = 30.0
MAX_PROVISIONAL_SECONDS: float = 180.0
# Recent IR/compressor-command window: session may open after explicit ON before ac_is_on latches.
_POST_ON_SESSION_INTENT_SECONDS: float = float(_COOLDOWN_SECS) + 120.0


def _seconds_since_last_command(st: RoomRuntime, now: datetime) -> float:
    if st.last_command_time is None:
        return float("inf")
    return (now - st.last_command_time).total_seconds()


def _bump_last_command_ir_cooldown(st: RoomRuntime, cmd_ts: datetime) -> None:
    """
    Always anchors cooldown to the most recent command.
    Previous bug: only updated if previous cooldown had expired.
    This meant rapid ON→OFF would anchor to ON, not OFF.
    """
    prev = st.last_command_time
    st.last_command_time = cmd_ts
    if prev is not None:
        elapsed = (cmd_ts - prev).total_seconds()
        if elapsed < _COOLDOWN_SECS:
            logger.debug(
                "[HawaAI] Cooldown reset to latest command: %.0fs since previous (window=%ds)",
                elapsed, _COOLDOWN_SECS,
            )


def _is_in_cooldown(st: RoomRuntime, now: datetime) -> bool:
    """Returns True if a command was issued within the cooldown window."""
    if st.last_command_time is None:
        return False
    elapsed = (now - st.last_command_time).total_seconds()
    return elapsed < _COOLDOWN_SECS


def _is_user_authority_active(st: RoomRuntime, cfg: dict, now: datetime) -> bool:
    """
    Returns True if the user issued a command recently enough that
    the system should not override it.
    """
    if st.last_user_command_time is None:
        return False
    lock_secs = int(cfg.get("user_authority_lock_secs", 120))
    elapsed = (now - st.last_user_command_time).total_seconds()
    return elapsed < lock_secs


def _resolve_control_decision(
    room_id: str,
    cfg: dict,
    indoor_temp: float,
    effective_target: float,
    is_occupied: bool,
    ac_on: bool,
    now: datetime,
) -> Tuple[str, str, float]:
    """
    Deterministic priority engine. Returns (action, source, target).

    Priority order (highest wins):
      1. SAFETY        — vacancy hard-off when timer expires (runs even during IR cooldown)
      2. USER LOCK     — API user authority overrides thermostat + cooldown hold
      3. COOLDOWN      — block thermostat ON/OFF until window elapses (safety exempt above)
      4. THERMOSTAT    — hysteresis ON/OFF
      5. HOLD          — nothing to do

    AI never appears in this function.
    AI only adjusts effective_target BEFORE this function is called.
    """
    st = _rt(room_id)
    on_delta = float(cfg.get("thermostat_on_delta_deg", 0.7))
    off_delta = float(cfg.get("thermostat_off_delta_deg", 0.3))
    vacancy_timeout = int(cfg.get("vacancy_timeout_minutes", 5)) * 60
    use_presence = cfg.get("use_presence", True)

    # ── PRIORITY 1: Safety — vacancy (may issue OFF even during global cooldown) ─
    if use_presence and not is_occupied:
        if st.vacant_since is not None:
            elapsed = (now - st.vacant_since).total_seconds()
            if elapsed >= vacancy_timeout and (ac_on or st.ac_is_on):
                if st.effective_ac_on:
                    now_ts = now.timestamp()
                    on_age_secs = None
                    if st.effective_on_since_ts is not None:
                        on_age_secs = now_ts - float(st.effective_on_since_ts)
                    elif st.last_ac_on_at is not None:
                        on_age_secs = now_ts - float(st.last_ac_on_at)
                    if on_age_secs is not None and on_age_secs < float(VACANCY_SESSION_GRACE_SECONDS):
                        logger.info(
                            "[VACANCY] Ignored for room=%s — cooling grace (%.0fs < %.0fs) "
                            "(effective_on / last_on)",
                            room_id,
                            on_age_secs,
                            VACANCY_SESSION_GRACE_SECONDS,
                        )
                        return ("hold_vacant", "safety_vacant", effective_target)
                return ("off", "safety_vacant", effective_target)
        return ("hold_vacant", "safety_vacant", effective_target)

    # ── PRIORITY 2: User authority — overrides thermostat and cooldown ───────────
    if _is_user_authority_active(st, cfg, now):
        return ("hold", "manual", effective_target)

    # ── PRIORITY 3: Global IR cooldown — block thermostat commands only ─────────
    if _is_in_cooldown(st, now):
        return ("hold_cooldown", "cooldown", effective_target)

    # ── PRIORITY 4: Thermostat hysteresis ─────────────────────────────────────────
    delta = indoor_temp - effective_target

    if delta > on_delta and not ac_on:
        return ("on", "thermostat", effective_target)

    if delta < -off_delta and ac_on:
        return ("off", "thermostat_reached", effective_target)

    # ── PRIORITY 5: Hold ─────────────────────────────────────────────────────────
    return ("hold", "thermostat", effective_target)


_REQUIRED_SNAPSHOT_FIELDS = {
    "session_id", "room_id", "indoor_temp",
    "ac_state", "presence", "control_source", "effective_final_temp",
}


def _validate_snapshot(data: dict, room_id: str) -> bool:
    """Returns True if snapshot is safe to write. Logs and returns False otherwise."""
    sid = data.get("session_id")
    if sid is None or (isinstance(sid, str) and not str(sid).strip()):
        logger.error("[SNAPSHOT] Skipping snapshot for room=%s — missing or blank session_id", room_id)
        return False
    for field in _REQUIRED_SNAPSHOT_FIELDS:
        if field == "session_id":
            continue
        if data.get(field) is None:
            logger.error(
                "[SNAPSHOT] Skipping snapshot for room=%s — required field '%s' is None",
                room_id, field,
            )
            return False
    return True


async def _load_startup_state(room_id: str, cfg: dict) -> None:
    """
    Called once before the first tick for a room.
    Reads the real climate entity state from HA and populates RoomRuntime.
    This prevents a redundant command on addon restart.
    """
    st = _rt(room_id)
    if st.startup_state_loaded:
        return

    try:
        climate_entity = cfg.get("climate_entity") or cfg.get("ac_entity")
        if not (climate_entity or "").strip():
            return

        climate_data = await ha_client.get_climate_state(str(climate_entity).strip())
        if not climate_data:
            return

        ha_state = (climate_data.get("state") or "off").lower()
        if ha_state == "cool":
            st.effective_ac_on = True
            st.ac_is_on = True
        elif ha_state in ("off", "unavailable", "unknown", ""):
            st.effective_ac_on = False
            st.ac_is_on = False
        else:
            st.effective_ac_on = True
            st.ac_is_on = True
        logger.info(
            "[HawaAI] Startup state loaded for room=%s ac_on=%s ha_state=%s",
            room_id, st.effective_ac_on, ha_state,
        )
    except Exception as e:
        logger.warning("[HawaAI] Could not load startup state for room=%s: %s", room_id, e)
    finally:
        st.startup_state_loaded = True
        st.possible_on_since = None


async def _get_ai_target_adjustment(
    room_id: str,
    indoor_temp: float,
    base_target: float,
    cfg: dict,
) -> float:
    """
    Returns a bounded temperature adjustment delta ONLY.
    This function:
      - NEVER calls _turn_ac_on(), _turn_ac_off(), or any HA command
      - NEVER modifies RoomRuntime directly
      - NEVER raises — returns 0.0 on any failure
      - Returns a value clamped to ±1.0 °C
    """
    if not cfg.get("ai_enabled", False):
        return 0.0
    if (cfg.get("temperature_mode") or "manual") != "schedule_ai":
        return 0.0

    try:
        rec = get_cached(room_id)
        if not rec:
            return 0.0

        raw_target = float(rec.get("target_temp", base_target))
        delta = raw_target - base_target
        clamped = max(-1.0, min(1.0, delta))

        logger.debug(
            "[AI] room=%s raw_adj=%.2f clamped=%.2f base=%.1f",
            room_id, delta, clamped, base_target,
        )
        return clamped

    except Exception as e:
        logger.warning("[AI] Advisory failed for room=%s, using 0.0: %s", room_id, e)
        return 0.0


def record_user_api_command(room_id: str) -> None:
    """Mark that the user sent a command via API (rate limit must pass first)."""
    st = _rt(room_id)
    st.last_user_command_time = datetime.now(timezone.utc)
    st.last_command_source = "user"


def _session_creation_eligible(st: RoomRuntime, now: datetime) -> bool:
    """
    Opening a cooling session requires real AC intent/on state — NOT inferred-only effective_ac_on.
    True when runtime believes the unit is ON, or we recently sent an IR/cool-down ON command.
    """
    if st.ac_is_on:
        return True
    if (
        st.last_command == "on"
        and st.last_command_source in ("user", "system")
        and st.last_command_time is not None
    ):
        age = (now - st.last_command_time).total_seconds()
        return age >= 0 and age <= _POST_ON_SESSION_INTENT_SECONDS
    return False


def _vacancy_signals_ac_should_stop(
    st: RoomRuntime,
    *,
    energy_watts_valid: bool,
    energy_watts: float,
) -> bool:
    """Vacant-room rule uses runtime intent and/or live power draw (not climate entity)."""
    if st.ac_is_on:
        return True
    if energy_watts_valid:
        # Core rule: compressor over threshold…
        if energy_watts > _WATTS_COMPRESSOR:
            return True
        # … or fan-only / idle band — still wasting energy while empty.
        if energy_watts >= _WATTS_FAN_ONLY:
            return True
    return False


def should_send_setpoint_command(
    st: RoomRuntime,
    new_temp: float,
    now: datetime,
    cfg: dict,
) -> Tuple[bool, str]:
    """
    Avoid repeated IR / climate service spam: require both a meaningful delta from the
    last applied setpoint and a minimum interval since last setpoint command.
    """
    dmin = float(cfg.get("setpoint_min_delta_deg", 0.7))
    tmin = float(cfg.get("setpoint_command_min_interval_seconds", 180))
    try:
        nt = round(float(new_temp), 1)
    except (TypeError, ValueError):
        return False, "invalid_temp"

    if st.last_applied_setpoint is None:
        return True, "initial_setpoint"

    if st.last_setpoint_command_at is not None:
        secs = (now - st.last_setpoint_command_at).total_seconds()
        if secs < tmin:
            return False, (
                f"blocked_setpoint_interval {secs:.0f}s<{tmin}s "
                "(use last_applied_temp / last_command_ts gate)"
            )
    try:
        last = float(st.last_applied_setpoint)
    except (TypeError, ValueError):
        return True, "recover_invalid_last_applied"

    if abs(nt - round(last, 1)) < dmin:
        return False, f"blocked_setpoint_delta |Δ|<{dmin}°C (last={last:.1f} new={nt:.1f})"
    return True, "allowed"


def record_setpoint_command(room_id: str, temp: float, ts: datetime) -> None:
    st = _rt(room_id)
    try:
        st.last_applied_setpoint = round(float(temp), 1)
    except (TypeError, ValueError):
        st.last_applied_setpoint = None
    st.last_setpoint_command_at = ts


def clear_setpoint_command_tracking(room_id: str) -> None:
    st = _rt(room_id)
    st.last_applied_setpoint = None
    st.last_setpoint_command_at = None


def _manual_override_resolve(
    room_id: str,
    cfg: dict,
    climate_data: dict,
    indoor_temp: Optional[float],
    now: datetime,
    engine_planned_target: float,
) -> Tuple[bool, float]:
    """
    Lock HA setpoint to user intent for a bounded time while it diverges from the
    engine-planned target. Skips schedule/AI adjustments while active.

    Activation only when HA setpoint *changes from the previous tick* (avoids startup
    spurious locks and expiry → immediate re-lock while HA still stale).
    """
    st = _rt(room_id)
    dur_min = float(cfg.get("manual_override_duration_minutes", 30))
    detect = float(cfg.get("manual_override_detect_delta_deg", 0.5))
    exit_near = float(cfg.get("manual_override_exit_within_deg", 0.5))

    raw_ct = climate_data.get("target_temp") if climate_data else None
    ct: Optional[float] = None
    if raw_ct is not None:
        try:
            ct = float(raw_ct)
        except (TypeError, ValueError):
            ct = None

    if st.manual_override_until is not None and now >= st.manual_override_until:
        logger.info(
            "[HawaAI][%s] Timed manual override expired",
            room_id,
        )
        st.manual_override_until = None
        st.manual_override_temp = None

    if (
        st.manual_override_until is not None
        and now < st.manual_override_until
        and st.manual_override_temp is not None
        and indoor_temp is not None
    ):
        if abs(float(indoor_temp) - float(st.manual_override_temp)) <= exit_near:
            logger.info(
                "[HawaAI][%s] Skip: manual override active — exited (near target)",
                room_id,
            )
            st.manual_override_until = None
            st.manual_override_temp = None

    if (
        st.manual_override_until is not None
        and now < st.manual_override_until
        and st.manual_override_temp is not None
    ):
        if ct is not None:
            st.prev_ha_setpoint_seen = ct
        return True, float(st.manual_override_temp)

    if ct is None:
        return False, engine_planned_target

    prev = st.prev_ha_setpoint_seen
    if prev is None:
        st.prev_ha_setpoint_seen = ct
        return False, engine_planned_target

    ha_changed = abs(ct - prev) >= 0.09
    st.prev_ha_setpoint_seen = ct

    if ha_changed and abs(ct - engine_planned_target) >= detect:
        st.manual_override_temp = ct
        st.manual_override_until = now + timedelta(minutes=dur_min)
        logger.info(
            "[HawaAI][%s] manual override lock — user %.1f°C vs engine %.1f°C for %dm",
            room_id,
            ct,
            engine_planned_target,
            int(dur_min),
        )
        return True, float(ct)

    return False, engine_planned_target


def _fingerprint_turn_on(temp: float) -> str:
    return f"on:{round(float(temp), 1)}"


def _fingerprint_turn_off() -> str:
    return "off"


def _gate_turn_ac_on(
    room_id: str,
    cfg: dict,
    target: float,
    now: datetime,
) -> bool:
    st = _rt(room_id)
    # IR cooldown bypass: explicit user commands should be able to resume cooling.
    if _is_in_cooldown(st, now) and not _is_user_authority_active(st, cfg, now):
        secs = _seconds_since_last_command(st, now)
        logger.info(
            "[HawaAI][%s] Skip ON: global IR cooldown window (elapsed=%.0fs < %ds)",
            room_id,
            min(secs, float(_COOLDOWN_SECS)),
            _COOLDOWN_SECS,
        )
        return False

    min_iv = float(cfg.get("min_command_interval_seconds", 150))

    secs = _seconds_since_last_command(st, now)
    if secs < min_iv:
        logger.info(
            "[HawaAI][%s] Skip ON: cooldown (%.0fs < %.0fs)",
            room_id, secs, min_iv,
        )
        return False

    fp = _fingerprint_turn_on(target)
    if st.last_sent_command_key == fp:
        logger.info(
            "[HawaAI][%s] Skip ON: duplicate command (%s)",
            room_id,
            fp,
        )
        return False

    min_off = float(cfg.get("compressor_min_off_seconds", 180))
    if st.compressor_off_since is not None:
        off_elapsed = (now - st.compressor_off_since).total_seconds()
        if off_elapsed < min_off:
            logger.info(
                "[HawaAI][%s] Skip ON: compressor min OFF (%.0fs < %.0fs)",
                room_id,
                off_elapsed,
                min_off,
            )
            return False

    return True


def _gate_turn_ac_off(room_id: str, cfg: dict, now: datetime, *, force: bool = False) -> bool:
    """
    Decide whether to send an OFF command.

    Never skip because "duplicate off" fingerprint — HA/device can miss commands;
    rely on internal state (intent) only for "already off".
    Caller must ensure st.ac_is_on before calling guard + _turn_ac_off.
    Vacancy/security path uses ``force=True`` to bypass throttle + compressor protections.
    """
    st = _rt(room_id)

    if force:
        logger.info("[HawaAI][%s] Enforcing OFF (force=safety/thermostat bypass)", room_id)
        return True

    if _is_in_cooldown(st, now):
        secs = _seconds_since_last_command(st, now)
        logger.info(
            "[HawaAI][%s] Skip OFF: global IR cooldown window (elapsed=%.0fs < %ds)",
            room_id,
            min(secs, float(_COOLDOWN_SECS)),
            _COOLDOWN_SECS,
        )
        return False

    min_iv = float(cfg.get("min_command_interval_seconds", 150))

    secs = _seconds_since_last_command(st, now)
    if secs < min_iv:
        logger.info(
            "[HawaAI][%s] Skip OFF: cooldown (%.0fs < %.0fs)",
            room_id, secs, min_iv,
        )
        return False

    min_on = float(cfg.get("compressor_min_on_seconds", 300))
    if st.compressor_on_since is not None:
        on_elapsed = (now - st.compressor_on_since).total_seconds()
        if on_elapsed < min_on:
            logger.info(
                "[HawaAI][%s] Skip OFF: compressor min ON (%.0fs < %.0fs)",
                room_id,
                on_elapsed,
                min_on,
            )
            return False

    return True


async def _maybe_record_ai_user_adjustment(
    room_id: str,
    cfg: dict,
    climate_data: dict,
    now: datetime,
) -> None:
    """
    If climate setpoint diverges from the last AI recommendation (and config baseline),
    label the pending ai_decisions row for ML (user override heuristic).
    """
    if not bool(cfg.get("ai_enabled", False)):
        return
    if str(cfg.get("temperature_mode") or "manual") != "schedule_ai":
        return
    pend = ai_cache.get_pending_ml_label(room_id)
    if not pend or not climate_data:
        return
    decision_id, ts_iso, ai_target = pend
    raw_ct = climate_data.get("target_temp")
    if raw_ct is None:
        return
    try:
        ct = float(raw_ct)
    except (TypeError, ValueError):
        return
    try:
        t_dec = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        if t_dec.tzinfo is None:
            t_dec = t_dec.replace(tzinfo=timezone.utc)
        delay = (now - t_dec).total_seconds()
    except (TypeError, ValueError):
        return
    if delay < 90:
        return
    if abs(ct - ai_target) <= 0.55:
        ai_cache.clear_pending_ml_label(room_id)
        return
    base_t = float(cfg.get("target_temp", 24))
    if abs(ct - ai_target) > 0.55 and abs(ct - base_t) > 0.12:
        await database.update_ai_decision_ml_labels(
            decision_id,
            user_adjusted=1,
            user_target_temp=ct,
            adjustment_delay_seconds=delay,
        )
        ai_cache.clear_pending_ml_label(room_id)
    elif delay > 14_400:
        ai_cache.clear_pending_ml_label(room_id)


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


def bounded_effective_from_ai_cache(
    room_id: str,
    cfg: dict,
    effective_after_weather: float,
    is_occupied: bool,
) -> Tuple[float, bool]:
    """
    Effective control target after optional ±1 °C bounded AI read from cache.
    Does not invoke the model (cache may be stale until async fetch completes).
    Returns (final_target, adjustment_applied).
    """
    if (cfg.get("temperature_mode") or "manual") != "schedule_ai":
        return effective_after_weather, False
    if not cfg.get("ai_enabled"):
        return effective_after_weather, False
    if not is_occupied:
        return effective_after_weather, False

    rec = get_cached(room_id)
    if not rec or rec.get("action") in (None, "none"):
        return effective_after_weather, False
    try:
        ai_t = float(rec.get("target_temp", effective_after_weather))
    except (TypeError, ValueError):
        return effective_after_weather, False
    if not (AI_MIN_T <= ai_t <= AI_MAX_T):
        return effective_after_weather, False

    bounded = apply_ai_bounded_adjustment(effective_after_weather, ai_t)
    changed = abs(bounded - effective_after_weather) >= 0.01
    if changed:
        logger.debug(
            "[AI][%s] Bounded effective %.2f°C → %.2f°C (model %.2f°C)",
            room_id, effective_after_weather, bounded, ai_t,
        )
    return bounded, changed


async def tick(room_id: str) -> None:
    """
    Single decision-loop iteration for one room.
    """
    rid_raw = (room_id or "").strip()
    if not rid_raw:
        logger.error("[ROOM] tick rejected — missing room_id")
        return

    room_id = normalize_room_id(rid_raw)

    st = _rt(room_id)
    base_cfg = config_manager.load_config()
    room_def = resolve_room_definition(base_cfg, rid_raw)
    if not room_def:
        logger.debug("[HawaAI] tick skipped — unknown room_id=%s", rid_raw)
        return
    logger.info("[ROOM] tick room_id=%s (canonical=%s)", rid_raw, room_id)
    if not (str(room_def.get("climate_entity") or "")).strip():
        logger.debug("[HawaAI] tick skipped [%s] — no climate_entity", room_id)
        return
    cfg = room_registry.merge_room_config(base_cfg, room_def)

    _ae = bool(cfg.get("ai_enabled", False))
    if st.last_ai_enabled is not None and _ae != st.last_ai_enabled:
        logger.info("[AI][%s] %s", room_id, "Enabled" if _ae else "Disabled")
    st.last_ai_enabled = _ae

    presence_entity = cfg.get("presence_entity", "")
    indoor_temp_entity = cfg.get("indoor_temp_entity", "")

    if not presence_entity or not indoor_temp_entity:
        logger.warning(
            "[HawaAI][%s] Logic skipped — missing entity config (presence=%s, temp=%s)",
            room_id,
            bool(presence_entity), bool(indoor_temp_entity),
        )
        return

    await _load_startup_state(room_id, cfg)

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

    climate_entity = (cfg.get("climate_entity") or "").strip()
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
            "[HawaAI] tick skipped for room=%s — indoor_temp is None (HA unavailable?)",
            room_id,
        )
        return

    presence_raw = await ha_client.get_state(presence_entity)
    use_presence = cfg.get("use_presence", True)
    is_occupied_bool: Optional[bool]

    pres_invalid = presence_raw is None or str(presence_raw).lower() in (
        "unavailable", "unknown", "",
    )
    if use_presence:
        if pres_invalid:
            if st.last_known_presence is not None:
                is_occupied_bool = st.last_known_presence
                logger.warning(
                    "[HawaAI] Presence sensor unavailable (%r) — using last known occupied=%s",
                    presence_raw, is_occupied_bool,
                )
            else:
                is_occupied_bool = True
                logger.warning(
                    "[HawaAI] Presence unknown (no stale) — assuming occupied=TRUE (safe)",
                )
        else:
            is_occupied_bool = parse_presence(presence_raw)
            st.last_known_presence = is_occupied_bool
    else:
        is_occupied_bool = True

    logger.info(
        "[HawaAI] Presence: %r → occupied=%s",
        presence_raw, is_occupied_bool,
    )

    if cfg.get("manual_override", False):
        logger.info("[HawaAI] Manual override active — skipping logic")
        return

    now = datetime.now(timezone.utc)

    base_temp, slot_label = resolve_base_target_temp(cfg)
    log_target_resolve(room_id, cfg, base_temp, slot_label)
    temperature_mode_str = (cfg.get("temperature_mode") or "manual")

    vacancy_timeout = int(cfg.get("vacancy_timeout_minutes", 5)) * 60
    smart_curve = smart_temp_adjustment_enabled(cfg) and bool(cfg.get("use_outdoor_temp", True))

    weather = await weather_api.get_cached()
    outdoor_temp = weather.get("temp") if weather else None
    outdoor_humidity = weather.get("humidity") if weather else None

    effective_after_weather = compute_effective_target(base_temp, outdoor_temp, smart_curve)
    eff_aw = effective_after_weather
    if smart_curve:
        if outdoor_temp is None:
            logger.info(
                "[HawaAI] Smart adj: enabled — no outdoor temp yet → effective=%.1f°C (base)",
                effective_after_weather,
            )
        elif effective_after_weather != base_temp:
            logger.info(
                "[HawaAI] Smart adj: outdoor=%.1f°C → effective %.1f°C (base=%.1f°C)",
                outdoor_temp, effective_after_weather, base_temp,
            )
        else:
            logger.info(
                "[HawaAI] Smart adj: outdoor=%.1f°C → effective unchanged at %.1f°C",
                outdoor_temp, effective_after_weather,
            )

    indoor_humidity: Optional[float] = None
    ih_entity = (cfg.get("indoor_humidity_entity") or "").strip()
    if ih_entity:
        raw_ih = await ha_client.get_state(ih_entity)
        if raw_ih not in (None, "unavailable", "unknown", ""):
            try:
                indoor_humidity = float(raw_ih)
            except (ValueError, TypeError):
                pass

    energy_power_entity = cfg.get("energy_power_entity", "")
    energy_watts: float = 0.0
    energy_watts_valid: bool = False

    if energy_power_entity:
        energy_raw = await ha_client.get_state(energy_power_entity)
        if energy_raw not in (None, "unavailable", "unknown", ""):
            try:
                energy_watts = float(energy_raw)
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

    in_cooldown = _is_in_cooldown(st, now)

    ac_idle: bool = False

    if energy_watts_valid and not in_cooldown:
        if energy_watts > _WATTS_COMPRESSOR:
            ac_on = True
            ac_idle = False
            st.last_power_confirmed_on = now.timestamp()
            if st.compressor_watts_high_since is None:
                st.compressor_watts_high_since = now
            if not st.ac_is_on:
                logger.info(
                    "[HawaAI][%s] AC confirmed ON by power sensor (%.0f W > %.0f W threshold) "
                    "— syncing internal flag",
                    room_id, energy_watts, _WATTS_COMPRESSOR,
                )
                st.ac_is_on = True
                st.last_ac_on_at = now.timestamp()
        elif energy_watts >= _WATTS_FAN_ONLY:
            ac_on = st.ac_is_on
            ac_idle = True
            st.compressor_watts_high_since = None
        else:
            ac_on = False
            ac_idle = False
            st.compressor_watts_high_since = None
            st.last_power_confirmed_off = now.timestamp()
            if st.ac_is_on:
                logger.info(
                    "[HawaAI][%s] AC confirmed OFF by power sensor (%.0f W < %.0f W threshold) "
                    "— syncing internal flag",
                    room_id, energy_watts, _WATTS_FAN_ONLY,
                )
                st.ac_is_on = False
                st.last_ac_off_at = now.timestamp()
                if st.session_start_time is not None:
                    logger.info(
                        "[HawaAI][%s] External power-off detected — finalizing open session",
                        room_id,
                    )
                    await _close_session(room_id, cfg, indoor_temp, reason="power_off")
        power_source = "watts"
    else:
        ac_on = st.ac_is_on
        ac_idle = False
        power_source = "cooldown" if in_cooldown else "internal"
        st.compressor_watts_high_since = None

    st.effective_ac_idle = ac_idle
    st.effective_power_source = power_source

    secs_since_cmd = (
        (now - st.last_command_time).total_seconds()
        if st.last_command_time is not None
        else float("inf")
    )
    pres_label = "occupied" if is_occupied_bool else "vacant"
    ac_state_label = (
        f"IDLE({energy_watts:.0f}W)" if ac_idle
        else (f"ON({energy_watts:.0f}W)" if ac_on else "OFF")
    )
    logger.info(
        "[HawaAI][%s] TICK | indoor=%.1f°C | outdoor=%s | presence=%s | ac=%s "
        "[src=%s] | temp_mode=%s ha_mode=%s slot=%s | base=%.1f°C (weather_eff=%.1f°C)",
        room_id,
        indoor_temp,
        f"{outdoor_temp:.1f}°C" if outdoor_temp is not None else "—",
        pres_label,
        ac_state_label,
        power_source,
        temperature_mode_str,
        (climate_data.get("mode") if climate_data else None) or "—",
        slot_label,
        base_temp,
        eff_aw,
    )

    if in_cooldown:
        logger.info(
            "[HawaAI][%s] Cooldown active — %.0fs / %ds since '%s' command — "
            "skipping IR control later this tick",
            room_id,
            secs_since_cmd, _COOLDOWN_SECS, st.last_command,
        )

    rst = _rt(room_id)
    if rst.last_schedule_slot != slot_label:
        if rst.last_schedule_slot is not None:
            logger.info(
                "[HawaAI][%s] Schedule slot boundary: %s → %s",
                room_id, rst.last_schedule_slot, slot_label,
            )
        rst.last_schedule_slot = slot_label

    use_ai_layer = (
        bool(cfg.get("ai_enabled", False))
        and bool(is_occupied_bool)
        and temperature_mode_str == "schedule_ai"
    )

    if use_ai_layer:
        if not in_cooldown and should_run_ai(
            room_id, cfg, bool(is_occupied_bool), indoor_temp,
            control_base_temp=base_temp,
        ):
            fetch_ai_in_background(
                room_id,
                cfg,
                indoor_temp,
                base_temp,
                eff_aw,
                outdoor_temp,
                bool(is_occupied_bool),
            )
        rec = get_cached(room_id)
        if rec and throttle_cache_use_log(room_id):
            logger.debug("[AI][%s] Cached used", room_id)

    ai_delta = await _get_ai_target_adjustment(room_id, indoor_temp, eff_aw, cfg)
    planned_with_ai = eff_aw + ai_delta

    manual_override_active, effective_target = _manual_override_resolve(
        room_id, cfg, climate_data or {}, indoor_temp, now, planned_with_ai,
    )
    if manual_override_active:
        try:
            et_u = float(effective_target)
            logger.info(
                "[HawaAI][%s] User override window — HA setpoint %.1f°C drives control; "
                "smart outdoor adjustment and AI clamp bypassed",
                room_id,
                et_u,
            )
        except (TypeError, ValueError):
            pass

    et_eff = float(effective_target)
    st.effective_target_temp = et_eff

    now_ts = now.timestamp()
    power_high = energy_watts_valid and not in_cooldown and energy_watts > _WATTS_COMPRESSOR
    power_low = energy_watts_valid and not in_cooldown and energy_watts < _WATTS_FAN_ONLY

    probable_on = (
        bool(is_occupied_bool)
        and indoor_temp > et_eff + 1.5
        and not ac_on
    )
    if probable_on:
        if st.possible_on_since is None:
            st.possible_on_since = now_ts
    else:
        st.possible_on_since = None

    is_probably_on = (
        st.possible_on_since is not None
        and (now_ts - st.possible_on_since) < TRANSIENT_ON_WINDOW_SECS
    )

    if power_high:
        st.possible_on_since = None
    if power_low and not is_probably_on:
        st.possible_on_since = None

    st.effective_ac_on = bool(power_high or st.ac_is_on or is_probably_on)

    if st.effective_ac_on:
        inferred_only = is_probably_on and not power_high and not st.ac_is_on
        if inferred_only:
            st.ac_state_source = "inferred"
        elif energy_watts_valid and not in_cooldown:
            st.ac_state_source = "power"
        else:
            st.ac_state_source = "system"
    elif energy_watts_valid and not in_cooldown:
        st.ac_state_source = "power"
    else:
        st.ac_state_source = "system"

    if st.effective_ac_on:
        if st.effective_on_since_ts is None:
            st.effective_on_since_ts = now_ts
    else:
        st.effective_on_since_ts = None

    schedule_slot_snap: Optional[str] = (
        slot_label if temperature_mode_str != "manual" else None
    )
    ai_rec = (
        get_cached(room_id)
        if (
            cfg.get("ai_enabled", False)
            and (cfg.get("temperature_mode") or "manual") == "schedule_ai"
        )
        else None
    )
    sp = climate_data.get("target_temp") if climate_data else None
    fm = climate_data.get("fan_mode") if climate_data else None

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

    if use_presence:
        if not bool(is_occupied_bool):
            if st.vacant_since is None:
                st.vacant_since = now
                logger.info("[HawaAI][%s] Room became vacant — vacancy timer started", room_id)
            vacancy_duration = (now - st.vacant_since).total_seconds()
            logger.info(
                "[HawaAI][%s] Vacant %.0fs / timeout %ds | AC=%s",
                room_id,
                vacancy_duration, vacancy_timeout, "ON" if ac_on else "OFF",
            )
        else:
            st.vacant_since = None
    else:
        st.vacant_since = None

    occ_res = True if not use_presence else bool(is_occupied_bool)

    # Drop stale API "user" marker so thermostat lock still works after authority expires.
    if st.last_command_source == "user" and not _is_user_authority_active(st, cfg, now):
        st.last_command_source = "system"

    action, source, tgt = _resolve_control_decision(
        room_id, cfg, indoor_temp, et_eff,
        occ_res, ac_on, now,
    )
    user_bypass_decision_lock = (
        _is_user_authority_active(st, cfg, now) or st.last_command_source == "user"
    )
    if (
        action in ("on", "off")
        and not str(source).startswith("safety")
        and not user_bypass_decision_lock
    ):
        lda = st.last_decision_at
        if lda is not None:
            elapsed_ld = (now - lda).total_seconds()
            if elapsed_ld < float(DECISION_LOCK_SECONDS):
                logger.info(
                    "[DECISION_LOCK][%s] HOLD — %.1fs since last decision (< %.0fs); "
                    "blocking action=%s source=%s",
                    room_id,
                    elapsed_ld,
                    DECISION_LOCK_SECONDS,
                    action,
                    source,
                )
                action, source = "hold", "decision_lock"
    st.effective_control_source = source

    delta_audit = indoor_temp - et_eff
    in_cd_audit = _is_in_cooldown(st, now)
    ha_mode_tick = climate_data.get("mode") if climate_data else None
    logger.info(
        "[TICK] room=%s action=%s source=%s indoor=%.2f°C target=%.2f°C delta=%+.2f°C "
        "power=%sW ir_cooldown_active=%s occupied=%s temp_mode=%s ha_mode=%s",
        room_id,
        action,
        source,
        indoor_temp,
        et_eff,
        delta_audit,
        f"{energy_watts:.0f}" if energy_watts_valid else "n/a",
        in_cd_audit,
        occ_res,
        temperature_mode_str,
        ha_mode_tick or "—",
    )

    if action == "on":
        await _turn_ac_on(room_id, cfg, indoor_temp, et_eff, now=now)
        st.last_command_source = "system"

    elif action == "off":
        # Safety + thermostat target-reached: always force=True (_gate_turn_ac_off bypass).
        reason = "vacant" if "vacant" in source else "target_reached"
        if source.startswith("safety") or source == "thermostat_reached":
            if source == "thermostat_reached":
                session_logger.mark_cooled(room_id)
            await _turn_ac_off(
                room_id, cfg, indoor_temp, reason, now=now, force=True,
            )
        else:
            await _turn_ac_off(
                room_id, cfg, indoor_temp, reason, now=now, force=False,
            )
        st.last_command_source = "system"

    elif action in ("hold_vacant", "hold_cooldown", "hold"):
        pass

    use_ai_layer_hold = (
        bool(cfg.get("ai_enabled", False))
        and bool(is_occupied_bool)
        and temperature_mode_str == "schedule_ai"
        and not manual_override_active
    )
    _rec = get_cached(room_id) if use_ai_layer_hold else None

    if action == "hold" and ac_on and not _is_user_authority_active(st, cfg, now) and not in_cooldown:
        ai_fan_applied = False
        if (
            _rec
            and _rec.get("fan_mode")
            and _rec.get("action") not in (None, "none")
        ):
            await apply_ai_fan(
                room_id,
                climate_entity,
                str(_rec.get("fan_mode", "auto")),
                str(_rec.get("action", "none")),
            )
            ai_fan_applied = True

        if not ai_fan_applied and cfg.get("smart_cooling_enabled", False):
            await smart_cooling.apply_smart_cooling(
                room_id,
                indoor_temp=indoor_temp,
                target_temp=et_eff,
                ac_on=ac_on,
                ac_idle=ac_idle,
                is_occupied=bool(is_occupied_bool),
                manual_override=False,
                climate_entity=climate_entity,
                enabled=True,
            )

    if smart_curve and climate_entity and ac_on and occ_res and not in_cooldown:
        interval = int(cfg.get("setpoint_command_min_interval_seconds", 180))
        meaningful = float(cfg.get("setpoint_min_delta_deg", 0.7))
        await smart_cooling.apply_effective_target(
            room_id,
            climate_entity=climate_entity,
            effective_target=et_eff,
            current_target=climate_data.get("target_temp"),
            ac_on=ac_on,
            manual_override=cfg.get("manual_override", False) or manual_override_active,
            min_interval_seconds=interval,
            meaningful_delta_deg=meaningful,
        )

    if not manual_override_active:
        await _maybe_record_ai_user_adjustment(room_id, cfg, climate_data or {}, now)

    await _maintain_session_lifecycle(
        room_id,
        cfg,
        indoor_temp,
        now,
        et_eff,
        energy_watts_valid=energy_watts_valid,
        energy_watts=energy_watts,
        in_cooldown=in_cooldown,
    )

    if session_logger.current_session_id(room_id) and energy_watts_valid:
        st.watts_samples.append(energy_watts)

    if manual_override_active:
        logger.info(
            "[HawaAI][%s] Skip: manual override active — control at %.1f°C (schedule/AI bypassed)",
            room_id,
            effective_target,
        )

    session_active = bool(st.effective_ac_on or st.ac_is_on)
    current_session = session_logger.current_session_id(room_id)

    if current_session and session_active:
        ai_adj_snap = (
            manual_override_active
            or (
                temperature_mode_str == "schedule_ai"
                and bool(cfg.get("ai_enabled", False))
                and abs(ai_delta) > 0.01
            )
        )
        hv_m = climate_data.get("mode") if climate_data else None
        snapshot_inner = {
            "timestamp": now.isoformat(),
            "indoor_temp": indoor_temp,
            "outdoor_temp": outdoor_temp,
            "outdoor_humidity": outdoor_humidity,
            "indoor_humidity": indoor_humidity,
            "ac_state": st.effective_ac_on,
            "watt_draw": energy_watts,
            "presence": bool(is_occupied_bool) if is_occupied_bool is not None else False,
            "setpoint": sp,
            "fan_mode": fm,
            "energy_kwh": energy_kwh_reading,
            "ai_target_temp": ai_tgt,
            "ai_fan_mode": ai_fan,
            "ai_confidence": ai_conf,
            "schedule_slot": schedule_slot_snap,
            "schedule_base_temp": base_temp,
            "effective_after_weather": eff_aw,
            "effective_final_temp": et_eff,
            "ai_adjust_applied": 1 if ai_adj_snap else 0,
            "target_temp": base_temp,
            "control_source": st.effective_control_source,
            "hvac_mode": hv_m,
        }
        snap_full = {
            "session_id": current_session,
            "room_id": room_id,
            **snapshot_inner,
        }
        if _validate_snapshot(snap_full, room_id):
            await session_logger.add_snapshot(room_id, current_session, snapshot_inner)
    elif session_active and not current_session:
        logger.warning(
            "[SNAPSHOT] session_active but no session row for room=%s after ensure — skipping",
            room_id,
        )


async def _maintain_session_lifecycle(
    room_id: str,
    cfg: dict,
    indoor_temp: float,
    now: datetime,
    et_eff: float,
    *,
    energy_watts_valid: bool,
    energy_watts: float,
    in_cooldown: bool,
) -> None:
    """Open provisional session on real AC ON / explicit ON intent; upgrade when compressor is stable."""
    st = _rt(room_id)
    sid_open = session_logger.current_session_id(room_id)
    if sid_open and session_logger.current_session_is_provisional(room_id):
        start_ref = st.session_start_time or session_logger.session_start_time(room_id)
        if start_ref is not None:
            prov_age = (now - start_ref).total_seconds()
            if prov_age > float(MAX_PROVISIONAL_SECONDS):
                logger.info(
                    "[SESSION_PROVISIONAL_TIMEOUT] room=%s session=%s age=%.0fs (max %.0fs) — closing",
                    room_id,
                    sid_open,
                    prov_age,
                    MAX_PROVISIONAL_SECONDS,
                )
                await _close_session(room_id, cfg, indoor_temp, reason="provisional_timeout")
                return

    eligibility = _session_creation_eligible(st, now)

    stable_power = (
        energy_watts_valid
        and not in_cooldown
        and energy_watts > _WATTS_COMPRESSOR
        and st.compressor_watts_high_since is not None
        and (now - st.compressor_watts_high_since).total_seconds() >= float(COMPRESSOR_STABLE_SECONDS)
    )
    no_meter_confirm = False
    if (not energy_watts_valid) or in_cooldown:
        if st.ac_is_on and st.last_ac_on_at is not None:
            since_ir = now.timestamp() - float(st.last_ac_on_at)
            if since_ir >= float(COMPRESSOR_STABLE_SECONDS):
                no_meter_confirm = True

    if eligibility and session_logger.current_session_id(room_id) is None:
        await _start_provisional_session(room_id, cfg, indoor_temp, now, et_eff)

    if (
        session_logger.current_session_id(room_id) is not None
        and session_logger.current_session_is_provisional(room_id)
        and (stable_power or no_meter_confirm)
    ):
        await session_logger.upgrade_current_session_to_confirmed(room_id)
        st.session_state = "confirmed"


async def _start_provisional_session(
    room_id: str,
    cfg: dict,
    indoor_temp: float,
    now: datetime,
    et_eff: float,
) -> None:
    """
    Start a DB session while expecting cooling before compressor watts confirm.
    Same session_id is upgraded later when power is stable (provisional=0).
    """
    if session_logger.current_session_id(room_id) is not None:
        return

    st = _rt(room_id)
    target = float(et_eff)
    kwh_entity = (cfg.get("energy_kwh_entity") or "").strip()
    start_kwh = None
    if kwh_entity:
        raw = await ha_client.get_state(kwh_entity)
        try:
            start_kwh = float(raw) if raw else None
        except (ValueError, TypeError):
            start_kwh = None

    st.session_start_kwh = start_kwh
    st.session_start_time = now
    st.session_start_temp = indoor_temp
    st.compressor_on_since = now
    st.compressor_off_since = None
    st.watts_samples = []
    st.session_state = "provisional"
    weather = await weather_api.get_cached()

    sid = await session_logger.start_session(
        room_id,
        {
            "start_time":             st.session_start_time.isoformat(),
            "indoor_temp_start":      indoor_temp,
            "outdoor_temp_start":     weather.get("temp") if weather else None,
            "outdoor_humidity_start": weather.get("humidity") if weather else None,
            "target_temp":            target,
            "ac_brand":               cfg.get("ac_brand"),
            "ac_model":               cfg.get("ac_model"),
            "room_name":              cfg.get("room_name"),
            "energy_kwh_start":       start_kwh,
            "provisional":            True,
            "is_record_valid":       1,
        },
    )
    logger.info(
        "[SESSION_START] room=%s session=%s provisional=1 indoor=%.1f°C target=%.1f°C",
        room_id,
        sid,
        indoor_temp,
        target,
    )


# ── Turn AC ON ────────────────────────────────────────────────────────────────

async def _turn_ac_on(
    room_id: str,
    cfg: dict,
    indoor_temp: float,
    effective_target: Optional[float] = None,
    now: Optional[datetime] = None,
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

    tnow = now if now is not None else datetime.now(timezone.utc)
    if not _gate_turn_ac_on(room_id, cfg, target, tnow):
        return

    ok_sp, skip_sp = should_send_setpoint_command(st, target, tnow, cfg)
    if not ok_sp:
        logger.info("[HawaAI][%s] Skip AC ON command — %s", room_id, skip_sp)
        return

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

    st.ac_is_on = True
    st.last_ac_on_at = time.time()
    cmd_ts = datetime.now(timezone.utc)
    _bump_last_command_ir_cooldown(st, cmd_ts)
    st.last_command = "on"
    st.last_sent_command_key = _fingerprint_turn_on(target)
    st.compressor_on_since = None
    st.compressor_off_since = None
    record_setpoint_command(room_id, target, cmd_ts)
    st.last_decision_at = cmd_ts
    logger.info(
        "[HawaAI][%s] AC ON accepted; provisional session opens on next lifecycle sync",
        room_id,
    )


async def _close_session(room_id: str, cfg: dict, indoor_temp: float, reason: str) -> None:
    """Finalize session for one room; always closes DB row (short runs flagged is_record_valid=0)."""
    st = _rt(room_id)
    open_sid = session_logger.current_session_id(room_id)
    if open_sid is None:
        logger.debug("[HawaAI][%s] _close_session(%s) — no open session, skipping", room_id, reason)
        return

    sl_start = session_logger.session_start_time(room_id)
    start_ref = st.session_start_time or sl_start
    now = datetime.now(timezone.utc)
    if start_ref is None:
        logger.warning(
            "[SESSION_END] room=%s session=%s — missing start anchor; using now",
            room_id,
            open_sid,
        )
        start_ref = now

    duration_secs = max(0.0, (now - start_ref).total_seconds())
    short_invalid = duration_secs < float(MIN_SESSION_SECONDS)
    if short_invalid:
        logger.info(
            "[SESSION_INVALID] room=%s session=%s duration=%.2fs (< %.0fs)",
            room_id,
            open_sid,
            duration_secs,
            MIN_SESSION_SECONDS,
        )

    cool_minutes = duration_secs / 60.0

    logger.info(
        "[SESSION_END] room=%s session=%s reason=%s | duration=%.0fs (%.2f min) short_invalid=%s",
        room_id,
        open_sid,
        reason,
        duration_secs,
        cool_minutes,
        short_invalid,
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
        "is_record_valid":       0 if short_invalid else 1,
    })

    st.session_start_time = None
    st.session_start_temp = None
    st.session_start_kwh = None
    st.watts_samples = []
    st.session_state = "idle"
    clear_setpoint_command_tracking(room_id)
    smart_cooling.reset(room_id)


async def _turn_ac_off(
    room_id: str,
    cfg: dict,
    indoor_temp: float,
    reason: str,
    now: Optional[datetime] = None,
    *,
    force: bool = False,
) -> None:
    st = _rt(room_id)
    climate_entity = (cfg.get("climate_entity") or "").strip()

    tnow = now if now is not None else datetime.now(timezone.utc)

    if reason not in ("manual", "manual_off", "power_off"):
        if st.last_ac_on_at is not None:
            on_secs = tnow.timestamp() - float(st.last_ac_on_at)
            if on_secs < MIN_ON_TIME_SECONDS:
                logger.info(
                    "[OFF BLOCKED][%s] Min runtime not reached (%.0fs < %.0fs)",
                    room_id,
                    on_secs,
                    MIN_ON_TIME_SECONDS,
                )
                return

    if not force:
        if not st.ac_is_on:
            return
        if not _gate_turn_ac_off(room_id, cfg, tnow, force=False):
            return
    elif reason == "vacant":
        # Hard policy: vacancy must not be skipped for duplicate/cooldown — still log once.
        logger.info("[VACANCY] AC OFF forced")

    await ac_adapter.turn_off(climate_entity)

    clear_setpoint_command_tracking(room_id)

    ts_off = time.time()
    st.ac_is_on = False
    st.last_ac_off_at = ts_off
    cmd_ts = datetime.now(timezone.utc)
    if force:
        # Forced vacancy/safety OFF always anchors IR cooldown window (explicit command intent).
        st.last_command_time = cmd_ts
    else:
        _bump_last_command_ir_cooldown(st, cmd_ts)
    st.last_command = "off"
    st.last_sent_command_key = _fingerprint_turn_off()
    st.compressor_off_since = cmd_ts
    st.compressor_on_since = None
    st.last_decision_at = cmd_ts

    await _close_session(room_id, cfg, indoor_temp, reason)


def get_runtime_state(room_id: str) -> dict:
    """In-memory runtime state for /api/rooms/{id}/status."""
    from datetime import datetime, timezone as _tz

    canonical = normalize_room_id(room_id)
    st = _rt(canonical)
    now = datetime.now(_tz.utc)
    secs_since_cmd = (now - st.last_command_time).total_seconds() if st.last_command_time else None

    sc = smart_cooling.get_state(canonical)
    base_cfg = config_manager.load_config()
    room_def = resolve_room_definition(base_cfg, room_id)
    merged = room_registry.merge_room_config(base_cfg, room_def) if room_def else base_cfg
    min_iv = float(merged.get("min_command_interval_seconds", 150))

    mo_until = st.manual_override_until.isoformat() if st.manual_override_until else None
    mo_active = bool(
        st.manual_override_until is not None
        and now < st.manual_override_until
        and st.manual_override_temp is not None
    )
    in_cooldown = (
        secs_since_cmd is not None
        and secs_since_cmd < _COOLDOWN_SECS
    )
    _ce = get_cached(canonical) if merged.get("ai_enabled") else None
    sp_secs = None
    if st.last_setpoint_command_at is not None:
        sp_secs = round((now - st.last_setpoint_command_at).total_seconds(), 1)
    return {
        "ac_is_on":              st.effective_ac_on,
        "effective_ac_on":       st.effective_ac_on,
        "ac_idle":               st.effective_ac_idle,
        "power_source":          st.effective_power_source,
        "ac_state_source":       st.ac_state_source,
        "control_source":        st.effective_control_source,
        "target_temp":           st.effective_target_temp,
        "last_command_source":   st.last_command_source,
        "last_ac_on_at":         st.last_ac_on_at,
        "last_ac_off_at":        st.last_ac_off_at,
        "last_power_confirmed_on":  st.last_power_confirmed_on,
        "last_power_confirmed_off": st.last_power_confirmed_off,
        "ai_enabled":            bool(merged.get("ai_enabled", False)),
        "ai_cached":             _ce is not None,
        "session_id":            session_logger.current_session_id(canonical),
        "session_start_time":    (
            st.session_start_time.isoformat() if st.session_start_time else None
        ),
        "session_start_kwh":     st.session_start_kwh,
        "cooldown_active":       in_cooldown,
        "last_command":          st.last_command or None,
        "secs_since_cmd":        round(secs_since_cmd, 1) if secs_since_cmd is not None else None,
        "last_applied_temp":     st.last_applied_setpoint,
        "secs_since_setpoint_command": sp_secs,
        "watts_on_threshold":    _WATTS_COMPRESSOR,
        "watts_idle_threshold":  _WATTS_FAN_ONLY,
        "smart_mode":            sc["smart_mode"],
        "smart_fan_mode":        sc["smart_fan_mode"],
        "last_applied_target":   sc.get("last_applied_target"),
        "manual_override_active": mo_active,
        "manual_override_expires_at": mo_until if mo_active else None,
        "manual_override_target_temp": st.manual_override_temp if mo_active else None,
        "min_command_interval_seconds": int(min_iv),
    }
