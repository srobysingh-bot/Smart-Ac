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

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from . import ac_adapter, config_manager, database, ha_client, live_broadcast, session_logger, smart_cooling, weather_api
from . import room_registry
from .room_log_store import room_log_store
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


def log_with_room(level: str, room_id: str, msg: str, *args) -> None:
    log_fn = getattr(logger, level, logger.info)
    log_fn(msg, *args)
    try:
        rendered = msg % args if args else msg
        room_log_store.append(room_id, rendered, level=level)
    except Exception:
        pass

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
    # Physical compressor / HA / inferred truth (never masked by pending ON).
    physical_ac_on: bool = False
    # UI / masked: False while pending_action == "on" even if physical is True.
    effective_ac_on: bool = False
    # Display phase: off | pending_on | on | pending_off | on_failed
    ac_state: str = "off"
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
    presence_only_present_since: Optional[datetime] = None
    presence_only_last_invalid_log_at: Optional[datetime] = None

    # ── Startup recovery flag ──
    startup_state_loaded: bool = False

    # Session lifecycle — idle | provisional | confirmed (ended → idle after DB close)
    session_state: str = "idle"
    # First tick we believe the room is actively being cooled (effective ON); used for vacancy grace
    effective_on_since_ts: Optional[float] = None
    # Last IR / control ON or OFF command applied (wall time, UTC)
    last_decision_at: Optional[datetime] = None

    # Delayed actuation (thermostat intent → pending → _turn_ac_*)
    pending_action: Optional[str] = None  # "on" | "off"
    pending_since: Optional[float] = None  # epoch seconds (wall)
    pending_delay_wakeup_task: Optional[asyncio.Task] = None
    # Single-shot delayed ON: after one IR emit in this pending cycle, wait for physical confirm
    # (power / HA) or tick-level timeout — no automatic retry wakeups.
    pending_on_ir_sent: bool = False
    pending_on_ir_sent_at: Optional[datetime] = None
    # Pending ON cleared early on soft power (below compressor threshold) — UI only, not sessions.
    soft_start_ui: bool = False
    # FP2 zone (optional): dwell + confirmation for ON-only gating; never used for OFF.
    zone_present: bool = False
    zone_entered_at: Optional[datetime] = None
    zone_confirmed: bool = False
    zone_dwell_passed: bool = False
    zone_confidence: str = "low"  # low | medium | high — forward-compatible
    # Last tick: HA zone entity returned a usable state (not missing/unavailable/unknown).
    zone_sensor_usable: bool = False
    # Last HA sample time while raw zone was "on" (usable reads only; exit-debounce anchor).
    zone_last_raw_on_at: Optional[datetime] = None
    zone_block_count: int = 0
    zone_allow_count: int = 0
    zone_log_sig: Optional[tuple] = None
    # Hybrid event triggers — last sampled values from HA WS (not authoritative for control)
    last_event_presence_bool: Optional[bool] = None
    last_event_probe_indoor_temp: Optional[float] = None
    # Last applied comfort-mode (effective_mode) — detects config changes to clear stale delays.
    last_effective_mode: Optional[str] = None


_runtime_by_room: Dict[str, RoomRuntime] = {}
# Keys are canonical `normalize_room_id` strings — isolated per logical room.

# Serialize tick vs stop_room per room (avoids double OFF / double session close with tick).
_room_ops_locks: Dict[str, asyncio.Lock] = {}
# Serialize scheduler tick vs event-triggered tick for same room (no overlapping decision loops).
_room_tick_serial_locks: Dict[str, asyncio.Lock] = {}

_TICK_TRIGGER_DEBOUNCE_SEC = 2.0
_tick_trigger_last_mono_by_room: Dict[str, float] = {}


def _room_tick_serial_lock(room_id_key: str) -> asyncio.Lock:
    lk = _room_tick_serial_locks.get(room_id_key)
    if lk is None:
        lk = asyncio.Lock()
        _room_tick_serial_locks[room_id_key] = lk
    return lk


def _cancel_pending_delay_wakeup_task(st: RoomRuntime) -> None:
    """Cancel scheduled delay_elapsed wakeup; safe when pending clears early."""
    t = st.pending_delay_wakeup_task
    if t is not None and not t.done():
        t.cancel()
    st.pending_delay_wakeup_task = None


def _cancel_all_pending_wakeup_tasks(st: RoomRuntime) -> None:
    _cancel_pending_delay_wakeup_task(st)


def _clear_pending_command_state(st: RoomRuntime) -> None:
    """Cancel delay wakeup and reset pending_* (used when pending intent is abandoned)."""
    _cancel_all_pending_wakeup_tasks(st)
    st.pending_action = None
    st.pending_since = None
    st.pending_on_ir_sent = False
    st.pending_on_ir_sent_at = None


def schedule_pending_completion_wakeup(
    *,
    rid_for_tick: str,
    room_canon: str,
    kind: str,
    delay_seconds: float,
) -> None:
    """
    Fire trigger_tick(delay_elapsed) after delay_seconds if pending_arm still matches.
    Non-blocking — complements the periodic scheduler tick.
    """
    if delay_seconds <= 0 or kind not in ("on", "off"):
        return
    canon = normalize_room_id(room_canon)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    st = _rt(canon)

    async def _alarm() -> None:
        try:
            await asyncio.sleep(float(delay_seconds))
        except asyncio.CancelledError:
            return
        st2 = _rt(canon)
        if kind == "on":
            if st2.pending_action != "on":
                return
        else:
            if st2.pending_action != "off":
                return
        trigger_tick(rid_for_tick, reason="delay_elapsed", skip_debounce=True)

    _cancel_pending_delay_wakeup_task(st)
    st.pending_delay_wakeup_task = loop.create_task(_alarm())


def trigger_tick(
    room_id_raw: str,
    *,
    reason: str,
    skip_debounce: bool = False,
) -> None:
    """
    Event-driven entry: schedules logic_engine.tick (non-blocking).
    Scheduler remains the authoritative fallback every logic_interval_seconds.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("[TICK_TRIGGER] skipped — no event loop (%s)", reason)
        return

    rq = (room_id_raw or "").strip()
    if not rq:
        return

    canon = normalize_room_id(rq)
    base_cfg = config_manager.load_config()
    room_def = resolve_room_definition(base_cfg, rq)
    if not room_def or room_def.get("disabled"):
        return

    mono = time.monotonic()
    if not skip_debounce:
        last_m = _tick_trigger_last_mono_by_room.get(canon, 0.0)
        if mono - last_m < _TICK_TRIGGER_DEBOUNCE_SEC:
            logger.debug(
                "[TICK_TRIGGER][%s] debounced %.2fs<%s (%s)",
                canon,
                mono - last_m,
                _TICK_TRIGGER_DEBOUNCE_SEC,
                reason,
            )
            return
        _tick_trigger_last_mono_by_room[canon] = mono

    lk = _room_tick_serial_lock(canon)
    if reason != "delay_elapsed" and lk.locked():
        logger.debug("[TICK_TRIGGER][%s] skipped tick in flight (%s)", canon, reason)
        return

    logger.info("[TICK_TRIGGER][%s] reason=%s", canon, reason)
    loop.create_task(_triggered_tick_runner(rq, canon))


async def _triggered_tick_runner(rid_stored: str, canon_key: str) -> None:
    try:
        await tick(rid_stored)
    except Exception:
        logger.exception("[TICK_TRIGGER][%s] tick runner failed", canon_key)


def _room_ops_lock(room_id_key: str) -> asyncio.Lock:
    lock = _room_ops_locks.get(room_id_key)
    if lock is None:
        lock = asyncio.Lock()
        _room_ops_locks[room_id_key] = lock
    return lock


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
# After first delayed-path ON IR in a pending cycle, wait this long for physical confirmation
# (compressor watts / HA command) before surfacing on_failed and clearing pending.
PENDING_ON_CONFIRM_TIMEOUT_SECS: float = 20.0

# Power-based state thresholds
_WATTS_COMPRESSOR: float = 500.0   # watts above this → compressor running (AC ON)
_WATTS_FAN_ONLY:   float = 50.0    # watts between FAN_ONLY and COMPRESSOR → IDLE (fan only)
# Soft-start draw after IR ON but before compressor crosses _WATTS_COMPRESSOR (tune per install).
MIN_SOFT_ON_WATTS: float = 120.0

# Probable manual-ON inference: occupy + hot vs target while watts have not risen yet (seconds)
TRANSIENT_ON_WINDOW_SECS: float = 180.0
MIN_SESSION_SECONDS: float = 30.0
COMPRESSOR_STABLE_SECONDS: float = 10.0
VACANCY_SESSION_GRACE_SECONDS: float = 120.0
MIN_ON_TIME_SECONDS: float = 60.0
RUNNING_OFF_BLOCK_SECS: float = 180.0
VACANCY_CONFIRM_SECS: float = 60.0
DECISION_LOCK_SECONDS: float = 30.0
MAX_PROVISIONAL_SECONDS: float = 180.0
# Recent IR/compressor-command window: session may open after explicit ON before ac_is_on latches.
_POST_ON_SESSION_INTENT_SECONDS: float = float(_COOLDOWN_SECS) + 120.0


def _seconds_since_last_command(st: RoomRuntime, now: datetime) -> float:
    if st.last_command_time is None:
        return float("inf")
    return (now - st.last_command_time).total_seconds()


def _power_band_indicates_on(
    energy_watts_valid: bool,
    in_cooldown: bool,
    energy_watts: float,
) -> bool:
    return bool(
        energy_watts_valid
        and not in_cooldown
        and energy_watts >= _WATTS_FAN_ONLY
    )


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
    vacancy_timeout = max(
        int(cfg.get("vacancy_timeout_minutes", 5)) * 60,
        float(VACANCY_CONFIRM_SECS),
    )
    use_presence = cfg.get("use_presence", True)

    # ── PRIORITY 1: Safety — vacancy (may issue OFF even during global cooldown) ─
    if use_presence and not is_occupied:
        if st.vacant_since is None:
            st.vacant_since = now
        elapsed = (now - st.vacant_since).total_seconds()
        if elapsed < float(VACANCY_CONFIRM_SECS):
            return ("hold", "vacancy_debounce", effective_target)
        if st.vacant_since is not None:
            if elapsed >= vacancy_timeout and (ac_on or st.ac_is_on):
                on_age = _seconds_since_effective_on_or_command(st, now)
                if on_age < float(RUNNING_OFF_BLOCK_SECS):
                    log_with_room(
                        "info",
                        room_id,
                        "[VACANCY] Ignored for room=%s — running protection (%.0fs < %.0fs)",
                        room_id,
                        on_age,
                        RUNNING_OFF_BLOCK_SECS,
                    )
                    return ("hold_vacant", "running_protection", effective_target)
                if st.physical_ac_on:
                    if on_age < float(VACANCY_SESSION_GRACE_SECONDS):
                        log_with_room(
                            "info",
                            room_id,
                            "[VACANCY] Ignored for room=%s — cooling grace (%.0fs < %.0fs) "
                            "(effective_on / last_on)",
                            room_id,
                            on_age,
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


def normalize_control_mode(cfg: dict) -> str:
    mode = str(cfg.get("control_mode") or "thermostat").strip().lower()
    return mode if mode in ("thermostat", "presence_only") else "thermostat"


def normalize_ir_backend(cfg: dict) -> str:
    backend = str(cfg.get("ir_backend") or "broadlink").strip().lower()
    return backend if backend in ("broadlink", "tuya") else "broadlink"


async def _auto_detect_ir_backend(climate_entity: str) -> Optional[str]:
    if not climate_entity:
        return None
    try:
        full = await ha_client.get_entity_state_full(climate_entity)
    except Exception:
        return None
    attrs = (full or {}).get("attributes") or {}
    candidates = (
        attrs.get("integration"),
        attrs.get("platform"),
        attrs.get("device_class"),
    )
    for candidate in candidates:
        text = str(candidate or "").strip().lower()
        if text in ("tuya", "broadlink"):
            return text
    return None


async def resolve_ir_backend(room_id: str, cfg: dict, climate_entity: str) -> str:
    raw = str(cfg.get("ir_backend") or "").strip().lower()
    if raw in ("broadlink", "tuya"):
        log_with_room("info", room_id, "[IR] backend=%s (manual)", raw)
        return raw

    detected = await _auto_detect_ir_backend(climate_entity)
    if detected:
        log_with_room("info", room_id, "[IR] backend=%s (auto-detected)", detected)
        return detected

    log_with_room("info", room_id, "[IR] backend=broadlink (fallback)")
    return "broadlink"


def _presence_only_on_dwell_seconds(cfg: dict) -> float:
    try:
        return max(0.0, min(float(cfg.get("presence_only_on_dwell_seconds", 20)), 3600.0))
    except (TypeError, ValueError):
        return 20.0


def _presence_only_max_runtime_seconds(cfg: dict) -> float:
    try:
        minutes = float(cfg.get("presence_only_max_runtime_minutes", 240))
    except (TypeError, ValueError):
        minutes = 240.0
    return max(1.0, min(minutes, 24 * 60.0)) * 60.0


def _presence_raw_invalid(raw: object) -> bool:
    return raw is None or str(raw).strip().lower() in ("unavailable", "unknown", "")


def _presence_only_runtime_seconds(st: RoomRuntime, now: datetime) -> Optional[float]:
    if st.effective_on_since_ts is not None:
        return max(0.0, now.timestamp() - float(st.effective_on_since_ts))
    if st.last_ac_on_at is not None:
        return max(0.0, now.timestamp() - float(st.last_ac_on_at))
    if st.last_command == "on" and st.last_command_time is not None:
        return max(0.0, (now - st.last_command_time).total_seconds())
    return None


def _resolve_presence_only_decision(
    room_id: str,
    cfg: dict,
    st: RoomRuntime,
    presence_raw: object,
    ac_on: bool,
    now: datetime,
) -> Tuple[str, str, bool]:
    """
    Presence-only control: occupancy drives ON/OFF; temperature and AI are ignored.
    Returns (action, source, occupied).
    """
    if _presence_raw_invalid(presence_raw):
        st.presence_only_present_since = None
        if (
            st.presence_only_last_invalid_log_at is None
            or (now - st.presence_only_last_invalid_log_at).total_seconds() >= 60
        ):
            log_with_room(
                "warning",
                room_id,
                "[PRESENCE_ONLY][%s] Presence unavailable (%r) — holding current state",
                room_id,
                presence_raw,
            )
            st.presence_only_last_invalid_log_at = now
        return "hold", "presence_unavailable", False

    occupied = parse_presence(presence_raw)
    st.last_known_presence = occupied
    st.presence_only_last_invalid_log_at = None

    runtime = _presence_only_runtime_seconds(st, now)
    max_runtime = _presence_only_max_runtime_seconds(cfg)
    if ac_on and runtime is not None and runtime >= max_runtime:
        log_with_room(
            "warning",
            room_id,
            "[PRESENCE_ONLY][%s] Max runtime exceeded %.0fs >= %.0fs — forcing OFF",
            room_id,
            runtime,
            max_runtime,
        )
        return "off", "presence_max_runtime", occupied

    if occupied:
        st.vacant_since = None
        if ac_on:
            st.presence_only_present_since = now
            return "hold", "presence_only", occupied
        if st.presence_only_present_since is None:
            st.presence_only_present_since = now
            return "hold", "presence_dwell", occupied
        elapsed = (now - st.presence_only_present_since).total_seconds()
        dwell = _presence_only_on_dwell_seconds(cfg)
        if elapsed < dwell:
            return "hold", "presence_dwell", occupied
        return "on", "presence_only", occupied

    st.presence_only_present_since = None
    if ac_on:
        if st.vacant_since is None:
            st.vacant_since = now
        elapsed_vacant = (now - st.vacant_since).total_seconds()
        if elapsed_vacant < float(VACANCY_CONFIRM_SECS):
            return "hold", "vacancy_debounce", occupied
        vacancy_timeout = max(
            int(cfg.get("vacancy_timeout_minutes", 5)) * 60,
            float(VACANCY_CONFIRM_SECS),
        )
        if elapsed_vacant < vacancy_timeout:
            return "hold", "presence_vacancy_grace", occupied
        return "off", "presence_vacant", occupied

    st.vacant_since = None
    return "hold", "presence_only", occupied


async def _fp2_zone_sensor_tick(room_id: str, cfg: dict, now: datetime) -> None:
    """
    FP2 zone: exit-grace debounce, entry dwell, HA glitch preservation.
    ``zone_confirmed`` := dwell_passed AND sensor_usable (extend confidence later).
    """
    st = _rt(room_id)
    zone_e = (str(cfg.get("zone_entity_id") or "")).strip()
    try:
        dwell = int(cfg.get("zone_dwell_seconds", 20))
    except (TypeError, ValueError):
        dwell = 20
    dwell = max(0, min(int(dwell), 3600))
    try:
        grace = int(cfg.get("zone_exit_grace_seconds", 4))
    except (TypeError, ValueError):
        grace = 4
    grace = max(0, min(int(grace), 120))

    if not zone_e:
        st.zone_present = False
        st.zone_entered_at = None
        st.zone_confirmed = False
        st.zone_dwell_passed = False
        st.zone_sensor_usable = False
        st.zone_last_raw_on_at = None
        st.zone_confidence = "low"
        st.zone_log_sig = None
        return

    raw = await ha_client.get_state(zone_e)
    low = (raw or "").strip().lower()
    usable = bool(raw is not None and low not in ("unknown", "unavailable", ""))
    st.zone_sensor_usable = usable

    if not usable:
        # Keep debounced presence / dwell anchor across transient HA failures.
        st.zone_confirmed = False
        if st.zone_entered_at is not None:
            st.zone_dwell_passed = (
                (now - st.zone_entered_at).total_seconds() >= float(dwell)
            ) and st.zone_present
        else:
            st.zone_dwell_passed = False
        st.zone_confidence = "low"
        return

    raw_on = low == "on"
    if raw_on:
        st.zone_last_raw_on_at = now
        debounced_present = True
    else:
        if st.zone_last_raw_on_at is None:
            debounced_present = False
        else:
            debounced_present = (
                (now - st.zone_last_raw_on_at).total_seconds() <= float(grace)
            )

    if not debounced_present:
        st.zone_last_raw_on_at = None

    st.zone_present = debounced_present

    if not debounced_present:
        st.zone_entered_at = None
        st.zone_dwell_passed = False
        st.zone_confirmed = False
        st.zone_confidence = "low"
        return

    if st.zone_entered_at is None:
        if raw_on:
            # First stable ON sample: assume dwell may already be satisfied (user was in zone).
            st.zone_entered_at = now - timedelta(seconds=float(dwell))
            epoch_min = datetime(1970, 1, 1, tzinfo=timezone.utc)
            if st.zone_entered_at < epoch_min:
                st.zone_entered_at = epoch_min
        else:
            st.zone_entered_at = now

    elapsed = (now - st.zone_entered_at).total_seconds()
    dwell_passed = elapsed >= float(dwell)
    st.zone_dwell_passed = dwell_passed
    st.zone_confirmed = bool(dwell_passed and usable)
    st.zone_confidence = "high" if dwell_passed else "medium"


def _fp2_zone_apply_on_gate(
    room_id: str,
    cfg: dict,
    action: str,
    source: str,
) -> Tuple[str, str, bool]:
    """
    ON-only: block thermostat ON until FP2 zone dwell confirms, when enabled.
    Never blocks OFF, safety, cooldown, or user paths. Missing/unusable zone → allow (fallback).
    Returns (action, source, zone_gate_blocked).
    """
    st = _rt(room_id)
    if action != "on" or str(source) != "thermostat":
        return action, source, False
    zone_e = (str(cfg.get("zone_entity_id") or "")).strip()
    required = bool(cfg.get("zone_required_for_on", False))
    if not required or not zone_e:
        return action, source, False
    if not st.zone_sensor_usable:
        st.zone_allow_count += 1
        return action, source, False
    if st.zone_confirmed:
        st.zone_allow_count += 1
        return action, source, False
    st.zone_block_count += 1
    return "hold", "zone_gate", True


def _fp2_zone_log_snapshot(room_id: str, st: RoomRuntime, *, gating: str) -> None:
    """[ZONE] lines when ``zone_entity_id`` is configured (caller logs only on state change)."""
    log_with_room("info", room_id, "[ZONE] present=%s", st.zone_present)
    log_with_room(
        "info",
        room_id,
        "[ZONE] entered_at=%s",
        st.zone_entered_at.isoformat() if st.zone_entered_at else "none",
    )
    log_with_room("info", room_id, "[ZONE] confirmed=%s", st.zone_confirmed)
    log_with_room("info", room_id, "[ZONE] confidence=%s", st.zone_confidence)
    log_with_room("info", room_id, "[ZONE] gating=%s", gating)


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
            st.physical_ac_on = True
            st.effective_ac_on = True
            st.ac_is_on = True
        elif ha_state in ("off", "unavailable", "unknown", ""):
            st.physical_ac_on = False
            st.effective_ac_on = False
            st.ac_is_on = False
        else:
            # Non-cooling modes (fan_only/heat/dry/auto/...) should not be treated as cooling ON.
            st.physical_ac_on = False
            st.effective_ac_on = False
            st.ac_is_on = False
            logger.info(
                "[HawaAI] Startup: hvac_mode='%s' treated as OFF (not cooling)",
                ha_state,
            )
        st.ac_state = "on" if st.physical_ac_on else "off"
        logger.info(
            "[HawaAI] Startup state loaded for room=%s ac_on=%s ha_state=%s",
            room_id, st.physical_ac_on, ha_state,
        )
    except Exception as e:
        logger.warning("[HawaAI] Could not load startup state for room=%s: %s", room_id, e)
    finally:
        st.startup_state_loaded = True
        st.possible_on_since = None
        _clear_pending_command_state(st)


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


async def effective_target_for_temp_cross(
    room_canon: str,
    merged_cfg: dict,
    *,
    indoor_temp: float,
) -> Optional[float]:
    """
    Band center aligned with thermostat math in ``_tick_impl`` after effective-mode
    band clamp and before HA setpoint-lock:

    schedule slot → outdoor curve → ``eff_aw`` + AI delta → ``apply_effective_mode_engine_target``.

    Omitting `_manual_override_resolve` here avoids mutating `prev_ha_setpoint_seen` from
    the WebSocket path; if a user knob lock is active, the periodic tick still reconciles bands.

    Returns None when ``manual_override`` (global skip flag) matches tick early-return.
    """
    if merged_cfg.get("manual_override", False):
        return None
    base_temp, _slot = resolve_base_target_temp(merged_cfg)
    weather = await weather_api.get_cached()
    outdoor_temp = weather.get("temp") if weather else None
    smart_curve = smart_temp_adjustment_enabled(merged_cfg) and bool(
        merged_cfg.get("use_outdoor_temp", True)
    )
    eff_aw = compute_effective_target(base_temp, outdoor_temp, smart_curve)
    ai_delta = await _get_ai_target_adjustment(room_canon, indoor_temp, eff_aw, merged_cfg)
    planned_raw = float(eff_aw + ai_delta)
    return apply_effective_mode_engine_target(
        room_id=room_canon,
        base_temp=float(base_temp),
        planned_with_ai=planned_raw,
        cfg=merged_cfg,
        control_log=False,
    )


_EFF_DELTA_MIN = 1.0
_EFF_DELTA_MAX = 5.0


def effective_max_delta_deg(cfg: dict) -> float:
    """Max °C above schedule base for auto combined adjustment and manual ceiling (default 3, clamp 1–5)."""
    try:
        return max(_EFF_DELTA_MIN, min(float(cfg.get("effective_max_delta_deg", 3.0)), _EFF_DELTA_MAX))
    except (TypeError, ValueError):
        return 3.0


def apply_effective_mode_engine_target(
    *,
    room_id: str,
    base_temp: float,
    planned_with_ai: float,
    cfg: dict,
    control_log: bool = True,
) -> float:
    """
    Band-limited engine target before HA setpoint lock.

    * auto: ``planned_with_ai`` may sit below ``base_temp`` when weather pulls the curve down
      or AI delta is negative. We intentionally do not model “cool below schedule base” in this band:
      ``delta_lift = planned_with_ai - base`` then clamp ``delta_lift = max(0, min(delta_lift, max_up))``.
    * manual: ``manual_effective_temp`` when set, else same as auto; clamped to [base, base+max_delta].

    Thermostat ON/OFF still uses ``effective_target`` vs indoor temp + hysteresis (unchanged).
    """
    base_b = float(base_temp)
    max_up = effective_max_delta_deg(cfg)
    mode = str(cfg.get("effective_mode") or "auto").strip().lower()
    if mode not in ("auto", "manual"):
        mode = "auto"

    raw_manual = cfg.get("manual_effective_temp")
    used_manual_value: Optional[float] = None

    def _auto_lift_from_pipeline(raw_eff: float) -> float:
        """Explicit band: discard sub-base pipeline output; cap uplift at max_delta."""
        raw_effective = float(raw_eff)
        delta_lift = raw_effective - base_b
        delta_lift = max(0.0, min(delta_lift, max_up))
        return base_b + delta_lift

    if mode == "manual" and raw_manual is not None:
        try:
            mv = float(raw_manual)
        except (TypeError, ValueError):
            et = _auto_lift_from_pipeline(planned_with_ai)
            log_mode = "manual_invalid_fallback_auto"
        else:
            used_manual_value = mv
            et = max(base_b, min(mv, base_b + max_up))
            log_mode = "manual"
    else:
        et = _auto_lift_from_pipeline(planned_with_ai)
        log_mode = "auto" if mode == "auto" else "manual_unset_auto"

    if control_log:
        if log_mode == "manual" and used_manual_value is not None:
            log_with_room(
                "info",
                room_id,
                "[CONTROL][%s] mode=manual base=%.1f manual=%.1f effective=%.1f max_delta=%.1f",
                room_id,
                base_b,
                used_manual_value,
                et,
                max_up,
            )
        else:
            log_with_room(
                "info",
                room_id,
                "[CONTROL][%s] mode=%s base=%.1f planned_raw=%.1f effective=%.1f max_delta=%.1f",
                room_id,
                log_mode,
                base_b,
                planned_with_ai,
                et,
                max_up,
            )
    return float(et)


def sync_effective_mode_transition(st: RoomRuntime, room_id: str, cfg: dict) -> None:
    """When ``effective_mode`` changes in config, abandon in-flight ON/OFF delays (stale intent)."""
    cur = str(cfg.get("effective_mode") or "auto").strip().lower()
    if cur not in ("auto", "manual"):
        cur = "auto"
    if st.last_effective_mode is not None and st.last_effective_mode != cur:
        log_with_room(
            "info",
            room_id,
            "[CONTROL][%s] effective_mode %s → %s — clearing pending_action / pending_since",
            room_id,
            st.last_effective_mode,
            cur,
        )
        _clear_pending_command_state(st)
    st.last_effective_mode = cur


def _nonnegative_delay_seconds(cfg: dict, key: str) -> float:
    try:
        v = float(cfg.get(key, 0))
    except (TypeError, ValueError):
        v = 0.0
    return max(0.0, min(v, 86_400.0))


def _sync_pending_for_action(st: RoomRuntime, decision_action: str) -> None:
    """
    Hard-reset pending when decision no longer matches scheduled actuation.
    - decision not in (on, off) → clear
    - decision != pending_action → clear

    Always cancels delay wakeup before clearing pending so stale ``delay_elapsed``
    triggers cannot fire after intent changed.
    """
    if decision_action not in ("on", "off"):
        _clear_pending_command_state(st)
        return
    if st.pending_action is not None and st.pending_action != decision_action:
        _clear_pending_command_state(st)


def _seconds_since_effective_on_or_command(st: RoomRuntime, now: datetime) -> float:
    now_ts = now.timestamp()

    if st.effective_on_since_ts is not None:
        return now_ts - float(st.effective_on_since_ts)
    if st.last_command_time is not None and st.last_command == "on":
        return (now - st.last_command_time).total_seconds()
    return float("inf")


def _apply_pending_on_decision_lock(
    room_id: str,
    st: RoomRuntime,
    action: str,
    source: str,
) -> Tuple[str, str]:
    if action == "on" and st.pending_action == "on" and st.pending_on_ir_sent:
        log_with_room(
            "info",
            room_id,
            "[CONTROL] Skip ON — already pending",
        )
        return "hold", "pending_on_lock"
    return action, source


def _apply_pending_on_off_block(
    room_id: str,
    st: RoomRuntime,
    action: str,
    source: str,
    now: datetime,
) -> Tuple[str, str]:
    if (
        action == "off"
        and st.pending_action == "on"
        and st.pending_on_ir_sent
    ):
        elapsed = (
            (now - st.pending_on_ir_sent_at).total_seconds()
            if st.pending_on_ir_sent_at is not None
            else 0.0
        )
        if elapsed < float(PENDING_ON_CONFIRM_TIMEOUT_SECS):
            log_with_room(
                "info",
                room_id,
                "[CONTROL] Block OFF (%s) — pending ON not yet confirmed (%.1fs)",
                source,
                elapsed,
            )
            return "hold", "pending_on_protection"
    return action, source


def _apply_running_state_off_block(
    room_id: str,
    st: RoomRuntime,
    action: str,
    source: str,
    now: datetime,
    ha_mode: object,
) -> Tuple[str, str]:
    """
    Block OFF if AC is already running and vacancy is unstable.
    """
    if action != "off":
        return action, source

    if str(ha_mode or "").strip().lower() != "cool":
        return action, source

    time_since_on = _seconds_since_effective_on_or_command(st, now)

    if time_since_on < float(RUNNING_OFF_BLOCK_SECS):
        log_with_room(
            "info",
            room_id,
            "[CONTROL] Block OFF (%s) — running protection (%.1fs)",
            source,
            time_since_on,
        )
        return "hold", "running_protection"

    return action, source


def _pending_on_emit_hold_in_progress(st: RoomRuntime, action: str) -> bool:
    return action == "hold" and st.pending_action == "on" and st.pending_on_ir_sent


async def _clear_timed_out_pending_on(
    room_id: str,
    st: RoomRuntime,
    now: datetime,
) -> bool:
    if (
        st.pending_action != "on"
        or not st.pending_on_ir_sent
        or st.pending_on_ir_sent_at is None
        or st.physical_ac_on
    ):
        return False

    elapsed_ir = (now - st.pending_on_ir_sent_at).total_seconds()
    if elapsed_ir < float(PENDING_ON_CONFIRM_TIMEOUT_SECS):
        return False

    st.soft_start_ui = False
    log_with_room(
        "error",
        room_id,
        "[HawaAI][%s] AC failed to turn ON — no physical confirmation within %.0fs after single IR emit",
        room_id,
        PENDING_ON_CONFIRM_TIMEOUT_SECS,
    )
    st.ac_state = "on_failed"
    st.last_command = "on_failed"
    try:
        await live_broadcast.broadcast_room_update(room_id)
    except Exception:
        pass
    _clear_pending_command_state(st)
    return True


def _clear_pending_when_physically_satisfied(
    st: RoomRuntime,
    *,
    manual_override_active: bool,
    confirmed_ac_on: bool,
    physical_ac_on: bool,
) -> None:
    """
    Drop stale pending timers once physical intent matches observation (before thermostat).
    Pending ON clears only on confirmed compressor/HA/command — NOT inferred-only transient ON.
    Pending OFF clears when full physical observation says compressor is OFF.
    """
    if st.pending_action == "on":
        if confirmed_ac_on or manual_override_active:
            _clear_pending_command_state(st)
            return
    elif st.pending_action == "off":
        if not physical_ac_on:
            _clear_pending_command_state(st)


def _sync_ac_display_fields(st: RoomRuntime) -> None:
    """
    Mask effective_ac_on during pending ON so UI matches intent; set ac_state phase.
    Must run after actuation updated pending_action / physical_ac_on is current.
    """
    if st.pending_action == "on":
        st.effective_ac_on = False
        st.ac_state = "pending_on"
        return

    if st.soft_start_ui and not st.physical_ac_on:
        st.effective_ac_on = False
        st.ac_state = "on"
        return

    if st.ac_state == "on_failed" and st.physical_ac_on:
        st.ac_state = "on"

    st.effective_ac_on = st.physical_ac_on

    if st.pending_action == "off" and st.physical_ac_on:
        st.ac_state = "pending_off"
    elif st.ac_state == "on_failed":
        st.effective_ac_on = False
    elif st.physical_ac_on:
        st.ac_state = "on"
    else:
        st.ac_state = "off"


def _decision_lock_blocks_delayed_emit(st: RoomRuntime, now: datetime) -> bool:
    """True if a real ON/OFF command was issued recently — delayed path must not bypass this."""
    lda = st.last_decision_at
    if lda is None:
        return False
    return (now - lda).total_seconds() < float(DECISION_LOCK_SECONDS)


def _delay_control_bypass(st: RoomRuntime, cfg: dict, now: datetime, source: str) -> bool:
    """Safety and user-authority paths skip ON/OFF delay."""
    if str(source).startswith("safety"):
        return True
    if _is_user_authority_active(st, cfg, now):
        return True
    if st.last_command_source == "user":
        return True
    return False


def record_user_api_command(room_id: str) -> None:
    """Mark that the user sent a command via API (rate limit must pass first)."""
    st = _rt(room_id)
    st.last_user_command_time = datetime.now(timezone.utc)
    st.last_command_source = "user"
    st.soft_start_ui = False
    _clear_pending_command_state(st)


def _session_creation_eligible(st: RoomRuntime, now: datetime) -> bool:
    """
    Opening a cooling session requires real AC intent/on state — NOT inferred-only effective_ac_on.

    ``effective_target`` / comfort mode does not gate eligibility; compressor power, ``ac_is_on``,
    and recent commanded ON do.
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
    fp = _fingerprint_turn_on(target)
    duplicate_intent = st.last_sent_command_key == fp
    resend_after_missed_ack = duplicate_intent and not st.physical_ac_on

    # Dedup only when compressor is observed ON; same fingerprint + OFF → allow resend path.
    if duplicate_intent and st.physical_ac_on:
        logger.info(
            "[HawaAI][%s] Skip ON: duplicate fingerprint (%s) — physical ON observed",
            room_id,
            fp,
        )
        return False

    # IR cooldown: bypass when resending same ON while still physically OFF (missed IR / HA drop).
    if _is_in_cooldown(st, now) and not _is_user_authority_active(st, cfg, now):
        if not resend_after_missed_ack:
            secs = _seconds_since_last_command(st, now)
            logger.info(
                "[HawaAI][%s] Skip ON: global IR cooldown window (elapsed=%.0fs < %ds)",
                room_id,
                min(secs, float(_COOLDOWN_SECS)),
                _COOLDOWN_SECS,
            )
            return False
        logger.warning(
            "[HawaAI][%s] Resend ON: duplicate fingerprint (%s) but physical OFF — bypass IR cooldown",
            room_id,
            fp,
        )

    min_iv = float(cfg.get("min_command_interval_seconds", 150))

    secs = _seconds_since_last_command(st, now)
    if secs < min_iv and not resend_after_missed_ack:
        logger.info(
            "[HawaAI][%s] Skip ON: cooldown (%.0fs < %.0fs)",
            room_id,
            secs,
            min_iv,
        )
        return False
    if secs < min_iv and resend_after_missed_ack:
        logger.warning(
            "[HawaAI][%s] Resend ON: duplicate fingerprint (%s) but physical OFF — "
            "bypass min interval (elapsed=%.0fs < %.0fs)",
            room_id,
            fp,
            secs,
            min_iv,
        )

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

    canon = normalize_room_id(rid_raw)
    async with _room_tick_serial_lock(canon):
        async with _room_ops_lock(canon):
            await _tick_impl(rid_raw, canon)
    await live_broadcast.broadcast_room_update(canon)


async def _tick_presence_only_mode(
    *,
    rid_raw: str,
    room_id: str,
    cfg: dict,
    climate_data: dict,
    presence_raw: object,
    indoor_temp: float,
    now: datetime,
    st: RoomRuntime,
) -> None:
    target = climate_data.get("target_temp") if climate_data else None
    try:
        et_eff = float(target if target is not None else cfg.get("target_temp", 24))
    except (TypeError, ValueError):
        et_eff = 24.0
    st.effective_target_temp = et_eff

    energy_power_entity = cfg.get("energy_power_entity", "")
    energy_watts: float = 0.0
    energy_watts_valid = False
    if energy_power_entity:
        energy_raw = await ha_client.get_state(energy_power_entity)
        if energy_raw not in (None, "unavailable", "unknown", ""):
            try:
                energy_watts = float(energy_raw)
                energy_watts_valid = True
            except (ValueError, TypeError):
                energy_watts = 0.0

    in_cooldown = _is_in_cooldown(st, now)
    if energy_watts_valid and not in_cooldown:
        if energy_watts > _WATTS_COMPRESSOR:
            ac_on = True
            st.last_power_confirmed_on = now.timestamp()
            if not st.ac_is_on:
                st.ac_is_on = True
                st.last_ac_on_at = now.timestamp()
            st.ac_state_source = "power"
        elif energy_watts < _WATTS_FAN_ONLY:
            ac_on = False
            st.last_power_confirmed_off = now.timestamp()
            if st.ac_is_on:
                st.ac_is_on = False
                st.last_ac_off_at = now.timestamp()
                if st.session_start_time is not None:
                    await _close_session(room_id, cfg, indoor_temp, reason="power_off")
            st.ac_state_source = "power"
        else:
            ac_on = True
            if not st.ac_is_on:
                st.ac_is_on = True
                st.last_ac_on_at = now.timestamp()
            st.ac_state_source = "power"
    else:
        ac_on = st.ac_is_on
        st.ac_state_source = "cooldown" if in_cooldown else "system"

    st.physical_ac_on = bool(ac_on)
    confirmed_ac_on = bool(ac_on)
    if st.physical_ac_on:
        if st.effective_on_since_ts is None:
            st.effective_on_since_ts = now.timestamp()
    else:
        st.effective_on_since_ts = None

    _clear_pending_when_physically_satisfied(
        st,
        manual_override_active=False,
        confirmed_ac_on=confirmed_ac_on,
        physical_ac_on=st.physical_ac_on,
    )

    action, source, occupied = _resolve_presence_only_decision(
        room_id,
        cfg,
        st,
        presence_raw,
        st.physical_ac_on,
        now,
    )
    action, source = _apply_pending_on_decision_lock(room_id, st, action, source)
    action, source = _apply_pending_on_off_block(room_id, st, action, source, now)
    action, source = _apply_running_state_off_block(
        room_id,
        st,
        action,
        source,
        now,
        (climate_data or {}).get("mode"),
    )
    control_action, control_source = action, source

    user_bypass_decision_lock = (
        _is_user_authority_active(st, cfg, now) or st.last_command_source == "user"
    )
    if (
        control_action in ("on", "off")
        and not str(control_source).startswith("safety")
        and not user_bypass_decision_lock
    ):
        lda = st.last_decision_at
        if lda is not None:
            elapsed_ld = (now - lda).total_seconds()
            if elapsed_ld < float(DECISION_LOCK_SECONDS):
                action, source = "hold", "decision_lock"
                control_action, control_source = action, source

    st.effective_control_source = source
    pending_on_hold_sources = ("pending_on_lock", "pending_on_protection")
    preserve_pending_on_hold = _pending_on_emit_hold_in_progress(st, control_action)
    if control_source not in pending_on_hold_sources and not preserve_pending_on_hold:
        _sync_pending_for_action(st, control_action)

    if (
        control_action != "on"
        and control_source not in pending_on_hold_sources
        and not preserve_pending_on_hold
    ):
        st.soft_start_ui = False
    if (
        st.ac_state == "on_failed"
        and control_action != "on"
        and control_source not in pending_on_hold_sources
    ):
        st.ac_state = "on" if st.physical_ac_on else "off"

    power_watts = energy_watts if energy_watts_valid else None
    if (
        control_action in ("on", "hold")
        and control_source in ("presence_only", *pending_on_hold_sources)
        and st.pending_action == "on"
        and st.pending_on_ir_sent
        and power_watts is not None
        and not st.physical_ac_on
        and power_watts > MIN_SOFT_ON_WATTS
    ):
        _clear_pending_command_state(st)
        st.soft_start_ui = True

    if (
        control_action in ("on", "hold")
        and control_source in ("presence_only", *pending_on_hold_sources)
        and st.pending_action == "on"
        and st.pending_on_ir_sent
        and st.pending_on_ir_sent_at is not None
        and not st.physical_ac_on
    ):
        await _clear_timed_out_pending_on(room_id, st, now)

    log_with_room(
        "info",
        room_id,
        "[TICK] room=%s action=%s source=%s control_mode=presence_only occupied=%s power=%sW",
        room_id,
        action,
        source,
        occupied,
        f"{energy_watts:.0f}" if energy_watts_valid else "n/a",
    )

    bypass_actuation_delay = _delay_control_bypass(st, cfg, now, control_source)
    if control_action == "on":
        if bypass_actuation_delay:
            await _turn_ac_on(room_id, cfg, indoor_temp, et_eff, now=now)
            _clear_pending_command_state(st)
        elif st.ac_state == "on_failed":
            log_with_room(
                "info",
                room_id,
                "[DELAY_ON][%s] presence-only ON suppressed — ac_state=on_failed",
                room_id,
            )
        else:
            await _handle_delayed_on(
                rid_raw,
                room_id,
                cfg,
                indoor_temp,
                et_eff,
                now,
                st,
                confirmed_ac_on=confirmed_ac_on,
            )
        st.last_command_source = "system"
    elif control_action == "off":
        force_off = control_source == "presence_max_runtime"
        reason_off = "max_runtime" if force_off else "vacant"
        if bypass_actuation_delay:
            await _turn_ac_off(room_id, cfg, indoor_temp, reason_off, now=now, force=force_off)
            _clear_pending_command_state(st)
        else:
            await _handle_delayed_off(
                rid_raw,
                room_id,
                cfg,
                indoor_temp,
                now,
                st,
                reason=reason_off,
                force=force_off,
            )
        st.last_command_source = "system"

    await _maintain_session_lifecycle(
        room_id,
        cfg,
        indoor_temp,
        et_eff,
        now,
        energy_watts_valid=energy_watts_valid,
        energy_watts=energy_watts,
        in_cooldown=in_cooldown,
        confirmed_ac_on=confirmed_ac_on,
        inferred_only_physical=False,
    )
    _sync_ac_display_fields(st)


async def _tick_impl(rid_raw: str, room_id: str) -> None:
    """
    Core tick body. Caller must hold ``_room_ops_lock(room_id)`` so this never races ``stop_room``.
    """
    base_cfg = config_manager.load_config()
    room_def = resolve_room_definition(base_cfg, rid_raw)
    if not room_def:
        logger.debug("[HawaAI] tick skipped — unknown room_id=%s", rid_raw)
        return
    if room_def.get("disabled"):
        logger.debug("[HawaAI] tick skipped [%s] — room disabled (no logic, snapshots, or commands)", room_id)
        return

    st = _rt(room_id)
    logger.info("[ROOM] tick room_id=%s (canonical=%s)", rid_raw, room_id)
    if not (str(room_def.get("climate_entity") or "")).strip():
        logger.debug("[HawaAI] tick skipped [%s] — no climate_entity", room_id)
        return
    cfg = room_registry.merge_room_config(base_cfg, room_def)
    control_mode = normalize_control_mode(cfg)
    presence_only = control_mode == "presence_only"

    sync_effective_mode_transition(st, room_id, cfg)

    _ae = bool(cfg.get("ai_enabled", False))
    if st.last_ai_enabled is not None and _ae != st.last_ai_enabled:
        logger.info("[AI][%s] %s", room_id, "Enabled" if _ae else "Disabled")
    st.last_ai_enabled = _ae

    presence_entity = cfg.get("presence_entity", "")
    indoor_temp_entity = cfg.get("indoor_temp_entity", "")

    if not presence_entity or (not presence_only and not indoor_temp_entity):
        logger.warning(
            "[HawaAI][%s] Logic skipped — missing entity config (presence=%s, temp=%s)",
            room_id,
            bool(presence_entity), bool(indoor_temp_entity),
        )
        return

    await _load_startup_state(room_id, cfg)

    indoor_temp_raw = await ha_client.get_state(indoor_temp_entity) if indoor_temp_entity else None
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

    if indoor_temp is None and not presence_only:
        logger.warning(
            "[HawaAI] tick skipped for room=%s — indoor_temp is None (HA unavailable?)",
            room_id,
        )
        return
    if indoor_temp is None:
        try:
            indoor_temp = float(cfg.get("target_temp", 24))
        except (TypeError, ValueError):
            indoor_temp = 24.0

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

    if presence_only:
        await _tick_presence_only_mode(
            rid_raw=rid_raw,
            room_id=room_id,
            cfg=cfg,
            climate_data=climate_data,
            presence_raw=presence_raw,
            indoor_temp=float(indoor_temp),
            now=now,
            st=st,
        )
        return

    base_temp, slot_label = resolve_base_target_temp(cfg)
    log_target_resolve(room_id, cfg, base_temp, slot_label)
    temperature_mode_str = (cfg.get("temperature_mode") or "manual")

    vacancy_timeout = max(
        int(cfg.get("vacancy_timeout_minutes", 5)) * 60,
        float(VACANCY_CONFIRM_SECS),
    )
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
            ac_on = True
            ac_idle = True
            st.compressor_watts_high_since = None
            if not st.ac_is_on:
                logger.info(
                    "[HawaAI][%s] AC treated ON by fan-only power (%.0f W >= %.0f W idle threshold) "
                    "— syncing internal flag",
                    room_id, energy_watts, _WATTS_FAN_ONLY,
                )
                st.ac_is_on = True
                st.last_ac_on_at = now.timestamp()
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
    engine_target = apply_effective_mode_engine_target(
        room_id=room_id,
        base_temp=float(base_temp),
        planned_with_ai=float(planned_with_ai),
        cfg=cfg,
        control_log=True,
    )

    manual_override_active, effective_target = _manual_override_resolve(
        room_id, cfg, climate_data or {}, indoor_temp, now, engine_target,
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
    power_idle = _power_band_indicates_on(
        energy_watts_valid, in_cooldown, energy_watts,
    ) and not power_high
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

    st.physical_ac_on = bool(power_high or power_idle or st.ac_is_on or is_probably_on)
    confirmed_ac_on = bool(power_high or power_idle or st.ac_is_on)

    if st.physical_ac_on:
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

    if st.physical_ac_on:
        if st.effective_on_since_ts is None:
            st.effective_on_since_ts = now_ts
    else:
        st.effective_on_since_ts = None

    if st.ac_state == "on_failed":
        st.soft_start_ui = False
    if st.soft_start_ui:
        if (
            st.physical_ac_on
            or confirmed_ac_on
            or not energy_watts_valid
            or energy_watts <= MIN_SOFT_ON_WATTS
        ):
            st.soft_start_ui = False

    _clear_pending_when_physically_satisfied(
        st,
        manual_override_active=manual_override_active,
        confirmed_ac_on=confirmed_ac_on,
        physical_ac_on=st.physical_ac_on,
    )

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

    await _fp2_zone_sensor_tick(room_id, cfg, now)

    action, source, tgt = _resolve_control_decision(
        room_id, cfg, indoor_temp, et_eff,
        occ_res, ac_on, now,
    )
    action, source, zone_gate_blocked = _fp2_zone_apply_on_gate(room_id, cfg, action, source)
    action, source = _apply_pending_on_decision_lock(room_id, st, action, source)
    action, source = _apply_pending_on_off_block(room_id, st, action, source, now)
    action, source = _apply_running_state_off_block(
        room_id,
        st,
        action,
        source,
        now,
        (climate_data or {}).get("mode"),
    )
    control_action, control_source = action, source

    zone_e_log = (str(cfg.get("zone_entity_id") or "")).strip()
    if zone_e_log:
        zone_sig = (
            st.zone_present,
            st.zone_entered_at.isoformat() if st.zone_entered_at else None,
            st.zone_confirmed,
            st.zone_sensor_usable,
            st.zone_confidence,
            st.zone_dwell_passed,
            "blocked" if zone_gate_blocked else "allowed",
        )
        if zone_sig != st.zone_log_sig:
            _fp2_zone_log_snapshot(
                room_id,
                st,
                gating="blocked" if zone_gate_blocked else "allowed",
            )
            st.zone_log_sig = zone_sig
    user_bypass_decision_lock = (
        _is_user_authority_active(st, cfg, now) or st.last_command_source == "user"
    )
    if (
        control_action in ("on", "off")
        and not str(control_source).startswith("safety")
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
                    control_action,
                    control_source,
                )
                action, source = "hold", "decision_lock"
    st.effective_control_source = source

    pending_on_hold_sources = ("pending_on_lock", "pending_on_protection")

    preserve_pending_on_hold = _pending_on_emit_hold_in_progress(st, control_action)
    if control_source not in pending_on_hold_sources and not preserve_pending_on_hold:
        _sync_pending_for_action(st, control_action)
    bypass_actuation_delay = _delay_control_bypass(st, cfg, now, control_source)

    if (
        control_action != "on"
        and control_source not in pending_on_hold_sources
        and not preserve_pending_on_hold
    ):
        st.soft_start_ui = False

    if (
        st.ac_state == "on_failed"
        and control_action != "on"
        and control_source not in pending_on_hold_sources
        and not preserve_pending_on_hold
    ):
        st.ac_state = "on" if st.physical_ac_on else "off"

    power_watts = energy_watts if energy_watts_valid else None
    soft_on_detected = (
        not st.physical_ac_on
        and power_watts is not None
        and power_watts > MIN_SOFT_ON_WATTS
    )
    if (
        control_action in ("on", "hold")
        and control_source in ("thermostat", *pending_on_hold_sources)
        and st.pending_action == "on"
        and st.pending_on_ir_sent
        and soft_on_detected
    ):
        log_with_room(
            "info",
            room_id,
            "[HawaAI][%s] Soft ON detected (power=%.0fW) — clearing pending early",
            room_id,
            float(power_watts),
        )
        _clear_pending_command_state(st)
        st.soft_start_ui = True

    if (
        control_action in ("on", "hold")
        and control_source in ("thermostat", *pending_on_hold_sources)
        and st.pending_action == "on"
        and st.pending_on_ir_sent
        and st.pending_on_ir_sent_at is not None
        and not st.physical_ac_on
    ):
        await _clear_timed_out_pending_on(room_id, st, now)

    delta_audit = indoor_temp - et_eff
    in_cd_audit = _is_in_cooldown(st, now)
    ha_mode_tick = climate_data.get("mode") if climate_data else None
    log_with_room(
        "info",
        room_id,
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

    if control_action == "on":
        if bypass_actuation_delay:
            await _turn_ac_on(room_id, cfg, indoor_temp, et_eff, now=now)
            _clear_pending_command_state(st)
            st.last_command_source = "system"
        elif st.ac_state == "on_failed" and not str(control_source).startswith("safety"):
            log_with_room(
                "info",
                room_id,
                "[DELAY_ON][%s] automated ON suppressed — ac_state=on_failed (await demand change / user)",
                room_id,
            )
        else:
            await _handle_delayed_on(
                rid_raw, room_id, cfg, indoor_temp, et_eff, now, st,
                confirmed_ac_on=confirmed_ac_on,
            )
            st.last_command_source = "system"

    elif control_action == "off":
        # Vacancy / safety OFF must abandon any thermostat delayed-ON countdown immediately.
        if str(control_source).startswith("safety"):
            _clear_pending_command_state(st)

        reason_off = "vacant" if "vacant" in control_source else "target_reached"
        force_off = control_source.startswith("safety") or control_source == "thermostat_reached"
        if bypass_actuation_delay:
            if control_source == "thermostat_reached":
                session_logger.mark_cooled(room_id)
            await _turn_ac_off(
                room_id, cfg, indoor_temp, reason_off, now=now, force=force_off,
            )
            _clear_pending_command_state(st)
        else:
            await _handle_delayed_off(
                rid_raw, room_id, cfg, indoor_temp, now, st,
                reason=reason_off, force=force_off,
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

    # After actuation: ac_is_on may have flipped this tick — session gate uses post-command truth.
    ph_after = (
        energy_watts_valid and not in_cooldown and energy_watts > _WATTS_COMPRESSOR
    )
    session_confirmed_ac_on = bool(ph_after or st.ac_is_on)
    session_inferred_only = bool(st.physical_ac_on and not session_confirmed_ac_on)

    await _maintain_session_lifecycle(
        room_id,
        cfg,
        indoor_temp,
        now,
        et_eff,
        energy_watts_valid=energy_watts_valid,
        energy_watts=energy_watts,
        in_cooldown=in_cooldown,
        confirmed_ac_on=session_confirmed_ac_on,
        inferred_only_physical=session_inferred_only,
    )

    _sync_ac_display_fields(st)

    if session_logger.current_session_id(room_id) and energy_watts_valid:
        st.watts_samples.append(energy_watts)

    if manual_override_active:
        logger.info(
            "[HawaAI][%s] Skip: manual override active — control at %.1f°C (schedule/AI bypassed)",
            room_id,
            effective_target,
        )

    session_active = bool(st.physical_ac_on or st.ac_is_on)
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
            "ac_state": st.physical_ac_on,
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
    confirmed_ac_on: bool,
    inferred_only_physical: bool,
) -> None:
    """
    Open provisional session on confirmed compressor / command ON; never inferred-only pending path.

    Gating uses ``confirmed_ac_on`` / ``_session_creation_eligible`` (power + ``ac_is_on`` / IR),
    not thermostat ``effective_target`` alone — switching comfort effective_mode does not start sessions by itself.
    """
    st = _rt(room_id)
    sid_open = session_logger.current_session_id(room_id)
    if sid_open and session_logger.current_session_is_provisional(room_id):
        start_ref = st.session_start_time or session_logger.session_start_time(room_id)
        if start_ref is not None:
            prov_age = (now - start_ref).total_seconds()
            if prov_age > float(MAX_PROVISIONAL_SECONDS):
                log_with_room(
                    "info",
                    room_id,
                    "[SESSION_PROVISIONAL_TIMEOUT] room=%s session=%s age=%.0fs (max %.0fs) — closing",
                    room_id,
                    sid_open,
                    prov_age,
                    MAX_PROVISIONAL_SECONDS,
                )
                await _close_session(room_id, cfg, indoor_temp, reason="provisional_timeout")
                return

    eligibility = _session_creation_eligible(st, now)
    eligible_confirmed_session = eligibility and confirmed_ac_on and not inferred_only_physical

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

    if eligible_confirmed_session and session_logger.current_session_id(room_id) is None:
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
    # No DB session until delayed ON executes or watts confirm intent — avoids phantom sessions.
    if st.pending_action == "on":
        return

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
    log_with_room(
        "info",
        room_id,
        "[SESSION_START] room=%s session=%s provisional=1 indoor=%.1f°C target=%.1f°C",
        room_id,
        sid,
        indoor_temp,
        target,
    )


# ── Delayed actuation (intent → pending timer → turn) ─────────────────────────

async def _tuya_double_emit(room_id: str, cfg: dict, target: float, now: datetime) -> None:
    await asyncio.sleep(2)

    st = _rt(room_id)
    if st.physical_ac_on:
        return
    if st.pending_action != "on":
        return

    log_with_room(
        "warning",
        room_id,
        "[IR][tuya] retry ON (double emit)",
    )

    await _turn_ac_on(
        room_id,
        cfg,
        None,
        target,
        now=datetime.now(timezone.utc),
        allow_pending_on_emit=True,
    )


async def _handle_delayed_on(
    rid_stored: str,
    room_canon: str,
    cfg: dict,
    indoor_temp: float,
    et_eff: float,
    now: datetime,
    st: RoomRuntime,
    *,
    confirmed_ac_on: bool,
) -> None:
    delay = _nonnegative_delay_seconds(cfg, "on_delay_seconds")
    ts = time.time()

    if confirmed_ac_on:
        log_with_room(
            "debug",
            room_canon,
            "[DELAY_ON][%s] compressor ON confirmed (power or HA/command) — clearing pending_on",
            room_canon,
        )
        _clear_pending_command_state(st)
        return

    if st.pending_action != "on":
        st.pending_action = "on"
        st.pending_since = ts
        st.pending_on_ir_sent = False
        st.pending_on_ir_sent_at = None
        log_with_room(
            "info",
            room_canon,
            "[DELAY_ON][%s] ARM pending_on pending_since=%.3f delay_s=%.0f",
            room_canon,
            st.pending_since,
            delay,
        )
        if delay > 0:
            schedule_pending_completion_wakeup(
                rid_for_tick=rid_stored,
                room_canon=room_canon,
                kind="on",
                delay_seconds=delay,
            )
            return

    if st.pending_since is None:
        st.pending_since = ts
        log_with_room(
            "warning",
            room_canon,
            "[DELAY_ON][%s] repaired missing pending_since=%.3f",
            room_canon,
            st.pending_since,
        )
        return

    if delay > 0:
        elapsed = ts - float(st.pending_since)
        logger.debug(
            "[DELAY_ON][%s] wait pending_since=%.3f elapsed=%.2fs delay=%.0fs",
            room_canon,
            st.pending_since,
            elapsed,
            delay,
        )
        if elapsed < delay:
            return

    if st.pending_on_ir_sent:
        log_with_room(
            "info",
            room_canon,
            "[DELAY_ON][%s] Skip duplicate ON — already sent at %s",
            room_canon,
            st.pending_on_ir_sent_at,
        )
        return

    if _decision_lock_blocks_delayed_emit(st, now):
        log_with_room(
            "info",
            room_canon,
            "[DECISION_LOCK][%s] delayed ON held — lock active pending_since=%.3f — wait for next tick",
            room_canon,
            st.pending_since,
        )
        return

    _cancel_pending_delay_wakeup_task(st)
    log_with_room(
        "info",
        room_canon,
        "[DELAY_ON][%s] TRIGGER _turn_ac_on (single emit) pending_since=%.3f delay_s=%.0f",
        room_canon,
        st.pending_since,
        delay,
    )
    st.pending_on_ir_sent = True
    st.pending_on_ir_sent_at = now

    climate_entity = (cfg.get("climate_entity") or "").strip()
    ir_backend = await resolve_ir_backend(room_canon, cfg, climate_entity)
    sent = await _turn_ac_on(
        room_canon,
        cfg,
        indoor_temp,
        et_eff,
        now=now,
        allow_pending_on_emit=True,
    )
    if sent and ir_backend == "tuya" and st.pending_action == "on":
        asyncio.create_task(_tuya_double_emit(room_canon, cfg, et_eff, now))
    if st.pending_action != "on":
        return


async def _handle_delayed_off(
    rid_stored: str,
    room_canon: str,
    cfg: dict,
    indoor_temp: float,
    now: datetime,
    st: RoomRuntime,
    *,
    reason: str,
    force: bool,
) -> None:
    delay = _nonnegative_delay_seconds(cfg, "off_delay_seconds")
    ts = time.time()

    if not st.physical_ac_on:
        _clear_pending_command_state(st)
        return

    if delay <= 0:
        if _decision_lock_blocks_delayed_emit(st, now):
            logger.info(
                "[DECISION_LOCK][%s] immediate OFF deferred — %.0fs lock since last IR",
                room_canon,
                DECISION_LOCK_SECONDS,
            )
            return
        if reason == "target_reached":
            session_logger.mark_cooled(room_canon)
        logger.info("[DELAY_OFF][%s] TRIGGER _turn_ac_off (delay=0)", room_canon)
        await _turn_ac_off(room_canon, cfg, indoor_temp, reason, now=now, force=force)
        _clear_pending_command_state(st)
        return

    if st.pending_action != "off":
        st.pending_action = "off"
        st.pending_since = ts
        logger.info(
            "[DELAY_OFF][%s] ARM pending_off pending_since=%.3f delay_s=%.0f (reason=%s force=%s)",
            room_canon,
            st.pending_since,
            delay,
            reason,
            force,
        )
        schedule_pending_completion_wakeup(
            rid_for_tick=rid_stored,
            room_canon=room_canon,
            kind="off",
            delay_seconds=delay,
        )
        return

    if st.pending_since is None:
        st.pending_since = ts
        logger.warning("[DELAY_OFF][%s] repaired missing pending_since=%.3f", room_canon, st.pending_since)
        return

    elapsed = ts - float(st.pending_since)
    logger.debug(
        "[DELAY_OFF][%s] wait pending_since=%.3f elapsed=%.2fs delay=%.0fs",
        room_canon,
        st.pending_since,
        elapsed,
        delay,
    )
    if elapsed >= delay:
        if _decision_lock_blocks_delayed_emit(st, now):
            logger.info(
                "[DECISION_LOCK][%s] delayed OFF held — elapsed=%.1fs pending_since=%.3f",
                room_canon,
                elapsed,
                st.pending_since,
            )
            return
        if reason == "target_reached":
            session_logger.mark_cooled(room_canon)
        logger.info(
            "[DELAY_OFF][%s] TRIGGER _turn_ac_off elapsed=%.2fs delay=%.0fs pending_since=%.3f "
            "(single emit)",
            room_canon,
            elapsed,
            delay,
            st.pending_since,
        )
        await _turn_ac_off(room_canon, cfg, indoor_temp, reason, now=now, force=force)
        _clear_pending_command_state(st)


# ── Turn AC ON ────────────────────────────────────────────────────────────────

def _resolve_supported_fan_mode(requested: str, supported: object) -> Optional[str]:
    modes = [str(x).strip() for x in (supported or []) if str(x).strip()]
    if not modes:
        return None
    req = str(requested or "").strip()
    if not req:
        return None
    exact = next((m for m in modes if m == req), None)
    if exact:
        return exact
    low_map = {m.lower(): m for m in modes}
    return low_map.get(req.lower())


async def _turn_ac_on_tuya(
    room_id: str,
    climate_entity: str,
    temperature: float,
    *,
    fan_mode: str = "auto",
    hvac_mode: str = "cool",
) -> bool:
    if not climate_entity:
        log_with_room("error", room_id, "[IR][tuya] AC ON FAILED — no climate entity configured")
        return False

    state = await ha_client.get_climate_state(climate_entity)
    supported_fan = _resolve_supported_fan_mode(fan_mode, state.get("fan_modes"))
    current_fan = str(state.get("fan_mode") or "").strip().lower()

    payload_temp = {
        "entity_id": climate_entity,
        "temperature": float(temperature),
        "hvac_mode": hvac_mode,
    }

    log_with_room(
        "info",
        room_id,
        "[IR][tuya] step=set_temperature entity=%s temp=%.1f hvac=%s",
        climate_entity,
        float(temperature),
        hvac_mode,
    )
    ok_temp = await ha_client.call_service("climate", "set_temperature", payload_temp)

    if not ok_temp:
        log_with_room(
            "warning",
            room_id,
            "[IR][tuya] set_temperature full payload failed before fan step",
        )
        return False

    if supported_fan:
        if current_fan == supported_fan.lower():
            log_with_room("info", room_id, "[IR][tuya] step=set_fan_mode skipped_already=%s", supported_fan)
        else:
            log_with_room("info", room_id, "[IR][tuya] step=set_fan_mode entity=%s fan=%s", climate_entity, supported_fan)
            ok_fan = await ha_client.call_service("climate", "set_fan_mode", {
                "entity_id": climate_entity,
                "fan_mode": supported_fan,
            })
            if not ok_fan:
                log_with_room("warning", room_id, "[IR][tuya] step=set_fan_mode failed fan=%s", supported_fan)
    else:
        log_with_room(
            "info",
            room_id,
            "[IR][tuya] step=skip_fan_mode_unsupported requested=%s supported=%s",
            fan_mode,
            state.get("fan_modes") or [],
        )

    return True


async def _turn_ac_on(
    room_id: str,
    cfg: dict,
    indoor_temp: float,
    effective_target: Optional[float] = None,
    now: Optional[datetime] = None,
    *,
    allow_pending_on_emit: bool = False,
) -> bool:
    """Turn AC ON for one room; updates RoomRuntime + per-room session. Returns False if IR not sent."""
    st = _rt(room_id)
    tnow = now if now is not None else datetime.now(timezone.utc)

    # Delayed thermostat ON path: exactly one IR emit per pending cycle (see _handle_delayed_on).
    # Further automated _turn_ac_on calls are suppressed until pending clears or user authority bypasses.
    if (
        st.pending_action == "on"
        and st.pending_on_ir_sent
        and not allow_pending_on_emit
        and not st.physical_ac_on
        and not _is_user_authority_active(st, cfg, tnow)
    ):
        logger.info(
            "[HawaAI][%s] Skip AC ON — delayed pending cycle already emitted IR; awaiting physical confirm",
            room_id,
        )
        return False

    climate_entity = (cfg.get("climate_entity") or "").strip()
    if not climate_entity:
        logger.error(
            "[HawaAI][%s] AC ON FAILED — no climate entity configured.",
            room_id,
        )
        return False

    target = effective_target if effective_target is not None else float(cfg.get("target_temp", 24))

    # Avoid redundant IR when compressor is already observed ON from power or HA/command —
    # never skip based on inferred-only transient ON.
    if st.physical_ac_on and st.ac_state_source != "inferred":
        logger.info(
            "[HawaAI][%s] Skip AC ON command — ON already confirmed (%s)",
            room_id,
            st.ac_state_source,
        )
        return True

    if not _gate_turn_ac_on(room_id, cfg, target, tnow):
        return False

    # Setpoint anti-spam is ONLY for redundant setpoint updates while already cooling.
    # It must never block an initial ON command (especially same-temp retries after a missed IR/HA send).
    if st.physical_ac_on and st.ac_state_source != "inferred":
        ok_sp, skip_sp = should_send_setpoint_command(st, target, tnow, cfg)
        if not ok_sp:
            logger.info(
                "[HawaAI][%s] Skip redundant setpoint update (%s)",
                room_id,
                skip_sp,
            )
            return True

    ir_backend = await resolve_ir_backend(room_id, cfg, climate_entity)
    if ir_backend == "tuya":
        success = await _turn_ac_on_tuya(
            room_id,
            climate_entity,
            target,
            fan_mode="auto",
            hvac_mode="cool",
        )
    else:
        log_with_room("info", room_id, "[IR][broadlink] dispatch=ac_adapter.turn_on entity=%s", climate_entity)
        success = await ac_adapter.turn_on(
            entity_id   = climate_entity,
            temperature = target,
            fan_mode    = "auto",
            hvac_mode   = "cool",
        )
    if not success:
        logger.error(
            "[HawaAI][%s] AC ON via Aerostate FAILED — not marking as ON; "
            "await physical confirmation or tick timeout (no automatic IR retry)",
            room_id,
        )
        return False

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
    return True


async def _indoor_temp_for_shutdown(cfg: dict) -> float:
    """Best-effort indoor temp for session end / AC off when tick is not running."""
    indoor_temp_entity = (cfg.get("indoor_temp_entity") or "").strip()
    indoor_temp: Optional[float] = None
    if indoor_temp_entity:
        raw = await ha_client.get_state(indoor_temp_entity)
        if raw not in (None, "unavailable", "unknown", ""):
            try:
                indoor_temp = float(raw)
            except (ValueError, TypeError):
                pass
    climate_entity = (cfg.get("climate_entity") or "").strip()
    if indoor_temp is None and climate_entity:
        climate_data = await ha_client.get_climate_state(climate_entity)
        fallback = climate_data.get("current_temp")
        if fallback is not None:
            try:
                indoor_temp = float(fallback)
            except (ValueError, TypeError):
                pass
    if indoor_temp is not None:
        return indoor_temp
    try:
        return float(cfg.get("target_temp", 24))
    except (TypeError, ValueError):
        return 24.0


async def stop_room(room_id_raw: str, *, shutdown_reason: str) -> None:
    """
    Stop automation runtime for a room: optional forced AC OFF, close open session, drop runtime.
    Call after persisting disabled=True when disabling or deleting a room.
    """
    canon = normalize_room_id(room_id_raw)
    if not canon:
        return
    async with _room_ops_lock(canon):
        await _stop_room_locked(room_id_raw, canon, shutdown_reason)


async def _stop_room_locked(room_id_raw: str, canon: str, shutdown_reason: str) -> None:
    st = _runtime_by_room.get(canon)
    had_runtime = st is not None
    if st is not None:
        _clear_pending_command_state(st)

    base_cfg = config_manager.load_config()
    room_def = resolve_room_definition(base_cfg, room_id_raw)
    cfg = (
        room_registry.merge_room_config(base_cfg, room_def)
        if room_def
        else {**base_cfg, "climate_entity": "", "ac_entity": ""}
    )

    indoor = await _indoor_temp_for_shutdown(cfg)

    want_hw_off = bool(st and (st.ac_is_on or st.physical_ac_on))
    if want_hw_off:
        await _turn_ac_off(canon, cfg, indoor, shutdown_reason, force=True)

    if session_logger.has_open_session(canon):
        await _close_session(canon, cfg, indoor, shutdown_reason)
    elif had_runtime:
        clear_setpoint_command_tracking(canon)
        smart_cooling.reset(canon)

    _runtime_by_room.pop(canon, None)


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
        log_with_room(
            "warning",
            room_id,
            "[SESSION_END] room=%s session=%s — missing start anchor; using now",
            room_id,
            open_sid,
        )
        start_ref = now

    duration_secs = max(0.0, (now - start_ref).total_seconds())
    short_invalid = duration_secs < float(MIN_SESSION_SECONDS)
    if short_invalid:
        log_with_room(
            "info",
            room_id,
            "[SESSION_INVALID] room=%s session=%s duration=%.2fs (< %.0fs)",
            room_id,
            open_sid,
            duration_secs,
            MIN_SESSION_SECONDS,
        )

    cool_minutes = duration_secs / 60.0

    log_with_room(
        "info",
        room_id,
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

    if reason not in (
        "manual",
        "manual_off",
        "power_off",
        "room_disabled",
        "room_deleted",
    ):
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
        log_with_room("info", room_id, "[VACANCY] AC OFF forced")

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

    on_d = _nonnegative_delay_seconds(merged, "on_delay_seconds")
    off_d = _nonnegative_delay_seconds(merged, "off_delay_seconds")
    pending_remaining: Optional[float] = None
    if st.pending_since is not None and st.pending_action in ("on", "off"):
        delay_key = (
            "on_delay_seconds" if st.pending_action == "on" else "off_delay_seconds"
        )
        pend_delay = _nonnegative_delay_seconds(merged, delay_key)
        if pend_delay > 0:
            rem = pend_delay - (time.time() - float(st.pending_since))
            pending_remaining = max(0.0, round(rem, 1))
    try:
        zdwell = int(merged.get("zone_dwell_seconds", 20))
    except (TypeError, ValueError):
        zdwell = 20
    zdwell = max(0, min(zdwell, 3600))
    try:
        zgrace = int(merged.get("zone_exit_grace_seconds", 4))
    except (TypeError, ValueError):
        zgrace = 4
    zgrace = max(0, min(zgrace, 120))

    zone_e = str(merged.get("zone_entity_id") or "").strip()
    zone_ui_phase = "inactive"
    zone_dwell_elapsed_seconds: Optional[float] = None
    zone_dwell_remaining_seconds: Optional[float] = None
    if not zone_e:
        zone_ui_phase = "inactive"
    elif not st.zone_sensor_usable:
        zone_ui_phase = "unusable"
    elif st.zone_confirmed:
        zone_ui_phase = "present"
    elif st.zone_present and not st.zone_confirmed:
        zone_ui_phase = "waiting"
        if st.zone_entered_at is not None:
            _elapsed = (now - st.zone_entered_at).total_seconds()
        else:
            _elapsed = 0.0
        zone_dwell_elapsed_seconds = round(
            min(max(0.0, _elapsed), float(zdwell)), 1
        )
        zone_dwell_remaining_seconds = round(
            max(0.0, float(zdwell) - _elapsed), 1
        )
    else:
        zone_ui_phase = "absent"

    return {
        "ac_is_on":              st.physical_ac_on,
        "physical_ac_on":        st.physical_ac_on,
        "effective_ac_on":       st.effective_ac_on,
        "ac_state":              st.ac_state,
        "ac_idle":               st.effective_ac_idle,
        "power_source":          st.effective_power_source,
        "ac_state_source":       st.ac_state_source,
        "control_source":        st.effective_control_source,
        "control_mode":          normalize_control_mode(merged),
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
        "ir_backend":            normalize_ir_backend(merged),
        "manual_override_active": mo_active,
        "manual_override_expires_at": mo_until if mo_active else None,
        "manual_override_target_temp": st.manual_override_temp if mo_active else None,
        "min_command_interval_seconds": int(min_iv),
        "on_delay_seconds":           on_d,
        "off_delay_seconds":          off_d,
        "pending_action":             st.pending_action,
        "pending_since_ts":           st.pending_since,
        "pending_remaining_seconds":  pending_remaining,
        "pending_on_ir_sent": bool(st.pending_on_ir_sent),
        "pending_on_ir_sent_at": (
            st.pending_on_ir_sent_at.isoformat() if st.pending_on_ir_sent_at else None
        ),
        "pending_on_confirm_timeout_seconds": float(PENDING_ON_CONFIRM_TIMEOUT_SECS),
        "running_off_block_seconds": float(RUNNING_OFF_BLOCK_SECS),
        "zone_entity_id": (str(merged.get("zone_entity_id") or "").strip() or None),
        "zone_dwell_seconds": zdwell,
        "zone_exit_grace_seconds": zgrace,
        "zone_required_for_on": bool(merged.get("zone_required_for_on", False)),
        "zone_present": st.zone_present,
        "zone_entered_at": (
            st.zone_entered_at.isoformat() if st.zone_entered_at else None
        ),
        "zone_confirmed": st.zone_confirmed,
        "zone_dwell_passed": st.zone_dwell_passed,
        "zone_confidence": st.zone_confidence,
        "zone_sensor_usable": st.zone_sensor_usable,
        "zone_block_count": int(st.zone_block_count),
        "zone_allow_count": int(st.zone_allow_count),
        "zone_ui_phase": zone_ui_phase,
        "zone_dwell_elapsed_seconds": zone_dwell_elapsed_seconds,
        "zone_dwell_remaining_seconds": zone_dwell_remaining_seconds,
        "effective_mode":             str(merged.get("effective_mode") or "auto"),
        "manual_effective_temp":      merged.get("manual_effective_temp"),
        "effective_max_delta_deg":    effective_max_delta_deg(merged),
    }
