"""
HawaAI core decision engine.

Called every `logic_interval_seconds` by the scheduler.

Authority separation:
  Presence/zone signals decide occupancy and vacancy.
  Thermostat logic decides target temperature and ON/OFF intent.
  IR/runtime state (`RoomRuntime.ac_is_on`) is the HVAC state authority.
  Breaker power/kWh telemetry is observational only.

Breaker telemetry feeds UI, diagnostics, confidence, and session analytics. It
must never drive occupancy, vacancy, thermostat gating, pending-action
reconciliation, physical AC truth, or runtime ON/OFF transitions.

Hardware ON/OFF: only `tick()` -> `_turn_ac_on` / `_turn_ac_off` -> backend adapter.
AI adjusts targets only; it never invokes backend adapters or turn helpers.

Runtime isolation: `_runtime_by_room` maps one `RoomRuntime` per trimmed
`room_id` (via `_rt()`).
"""
import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from . import (
    ac_aerostate_adapter,
    ac_tuya_adapter,
    config_manager,
    database,
    ha_client,
    humidity_comfort,
    live_broadcast,
    runtime_self_heal,
    session_logger,
    sleep_optimizer,
    smart_cooling,
    weather_api,
)
from .energy_config import (
    EnergyConfigMode,
    discover_energy_entities_for_device,
    read_validated_energy_state,
    resolve_energy_config,
    resolve_runtime_energy_config,
)
from . import room_registry
from .room_log_store import LOG_SCOPE_RUNTIME, room_log_store
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


def log_with_room(
    level: str,
    room_id: str,
    msg: str,
    *args,
    scope: str = LOG_SCOPE_RUNTIME,
) -> None:
    log_fn = getattr(logger, level, logger.info)
    log_fn(msg, *args)
    try:
        rendered = msg % args if args else msg
        room_log_store.append(room_id, rendered, level=level, scope=scope)
    except Exception:
        pass


def _parse_energy_sensor_value(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in ("unavailable", "unknown", "none", "nan"):
            return None
        value = text
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _energy_runtime_status(
    entity_id: str,
    raw_state: object,
    parsed_value: Optional[float],
) -> str:
    if not entity_id:
        return "missing"
    if parsed_value is not None:
        return "ok"
    if raw_state is None:
        return "unavailable"
    return "invalid"


TELEMETRY_STALE_SECONDS: float = 60.0
TELEMETRY_OFFLINE_SECONDS: float = 180.0
TELEMETRY_LONG_OUTAGE_LOG_SECONDS: float = 300.0


def _telemetry_status_from_invalid_age(age_seconds: float) -> str:
    if age_seconds >= TELEMETRY_OFFLINE_SECONDS:
        return "offline"
    if age_seconds >= TELEMETRY_STALE_SECONDS:
        return "stale"
    return "recovering"


def _telemetry_confidence_for_status(status: str) -> str:
    return {
        "healthy": "high",
        "recovering": "medium",
        "stale": "low",
        "offline": "none",
        "unconfigured": "none",
    }.get(status, "none")


def _telemetry_age(st: "RoomRuntime", now: datetime) -> Optional[float]:
    if st.telemetry_invalid_since is None:
        return None
    return max(0.0, (now - st.telemetry_invalid_since).total_seconds())


def _apply_telemetry_cache(
    st: "RoomRuntime",
    *,
    now: datetime,
    configured: bool,
    power_entity: str,
    kwh_entity: str,
    parsed_power: Optional[float],
    parsed_kwh: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    """Update observational telemetry state without changing HVAC authority."""
    power_configured = bool(power_entity)
    kwh_configured = bool(kwh_entity)
    power_live = parsed_power is not None
    kwh_live = parsed_kwh is not None

    st.telemetry_power_live_valid = power_live
    st.telemetry_kwh_live_valid = kwh_live

    if parsed_power is not None:
        st.last_valid_power_watts = parsed_power
        st.last_valid_timestamp = now
    if parsed_kwh is not None:
        st.last_valid_energy_kwh = parsed_kwh
        st.last_valid_timestamp = now

    effective_power: Optional[float] = None
    effective_kwh: Optional[float] = None

    if not configured:
        st.telemetry_gap = False
        st.telemetry_invalid_since = None
        st.telemetry_status = "unconfigured"
        st.telemetry_confidence = "none"
    else:
        invalid_power = power_configured and not power_live
        invalid_kwh = kwh_configured and not kwh_live
        has_invalid = invalid_power or invalid_kwh

        if not has_invalid:
            st.telemetry_gap = False
            st.telemetry_invalid_since = None
            st.telemetry_status = "healthy"
            st.telemetry_confidence = "high"
            effective_power = parsed_power
            effective_kwh = parsed_kwh
        else:
            if st.telemetry_invalid_since is None:
                st.telemetry_invalid_since = now
            invalid_age = _telemetry_age(st, now) or 0.0
            status = _telemetry_status_from_invalid_age(invalid_age)

            st.telemetry_gap = True
            st.telemetry_status = status
            st.telemetry_confidence = _telemetry_confidence_for_status(status)

            use_cache = status != "offline"
            effective_power = parsed_power
            effective_kwh = parsed_kwh
            if effective_power is None and use_cache:
                effective_power = st.last_valid_power_watts
            if effective_kwh is None and use_cache:
                effective_kwh = st.last_valid_energy_kwh

    st.energy_watts = effective_power
    st.energy_kwh = effective_kwh
    return effective_power, effective_kwh


def _should_log_telemetry_invalid(
    st: "RoomRuntime",
    *,
    now: datetime,
    kind: str,
    status: str,
    raw_state: object,
    reason: str,
) -> bool:
    sig = (kind, status, reason, str(raw_state))
    transition = st.telemetry_invalid_log_sig != sig
    long_outage = False
    if not transition and status in ("stale", "offline"):
        if st.telemetry_last_invalid_log_at is None:
            long_outage = True
        else:
            age = (now - st.telemetry_last_invalid_log_at).total_seconds()
            long_outage = age >= TELEMETRY_LONG_OUTAGE_LOG_SECONDS
    if transition or long_outage:
        st.telemetry_invalid_log_sig = sig
        st.telemetry_last_invalid_log_at = now
        return True
    return False


def _log_energy_runtime_diagnostic(
    room_id: str,
    st: "RoomRuntime",
    *,
    mode: str,
    configured: bool,
    device_lookup_skipped: bool,
    power_entity: str,
    kwh_entity: str,
    raw_power_state: object,
    raw_kwh_state: object,
    parsed_power: Optional[float],
    parsed_kwh: Optional[float],
    power_validation_reason: str = "",
    power_confidence: str = "none",
    power_suspicious: bool = False,
) -> None:
    now = datetime.now(timezone.utc)
    power_status = _energy_runtime_status(power_entity, raw_power_state, parsed_power)
    kwh_status = _energy_runtime_status(kwh_entity, raw_kwh_state, parsed_kwh)
    sig = (
        mode,
        configured,
        device_lookup_skipped,
        power_entity,
        power_status,
        st.telemetry_status,
        power_validation_reason,
        power_confidence,
        power_suspicious,
        kwh_entity,
        kwh_status,
    )
    if sig == st.energy_runtime_log_sig:
        return
    st.energy_runtime_log_sig = sig

    log_with_room(
        "info",
        room_id,
        "[POWER] room=%s mode=%s power_entity=%s kwh_entity=%s device_lookup_skipped=%s",
        room_id,
        mode,
        power_entity or "none",
        kwh_entity or "none",
        device_lookup_skipped,
    )

    if not configured:
        return

    if power_status == "missing":
        log_with_room("info", room_id, "[POWER] room=%s entity_missing kind=power", room_id)
    elif power_status != "ok":
        if _should_log_telemetry_invalid(
            st,
            now=now,
            kind="power",
            status=st.telemetry_status,
            raw_state=raw_power_state,
            reason=power_validation_reason or power_status,
        ):
            log_with_room(
                "warning",
                room_id,
                "[POWER] room=%s invalid_power_state entity=%s state=%r reason=%s telemetry=%s confidence=%s",
                room_id,
                power_entity,
                raw_power_state,
                power_validation_reason or power_status,
                st.telemetry_status,
                st.telemetry_confidence,
            )
    elif power_confidence:
        logger.debug(
            "[ENERGY_RUNTIME] room=%s power_normalized entity=%s confidence=%s suspicious=%s reason=%s watts=%s",
            room_id,
            power_entity,
            power_confidence,
            power_suspicious,
            power_validation_reason or "ok",
            parsed_power,
        )

    if kwh_status == "missing":
        log_with_room(
            "info",
            room_id,
            "[POWER] room=%s entity_missing kind=kwh optional=true",
            room_id,
        )
    elif kwh_status != "ok":
        if _should_log_telemetry_invalid(
            st,
            now=now,
            kind="kwh",
            status=st.telemetry_status,
            raw_state=raw_kwh_state,
            reason=kwh_status,
        ):
            log_with_room(
                "warning",
                room_id,
                "[POWER] room=%s invalid_kwh_state entity=%s state=%r telemetry=%s confidence=%s",
                room_id,
                kwh_entity,
                raw_kwh_state,
                st.telemetry_status,
                st.telemetry_confidence,
            )


async def _read_runtime_energy(
    room_id: str,
    cfg: dict,
    st: "RoomRuntime",
    *,
    now: Optional[datetime] = None,
) -> Tuple[Optional[float], Optional[float]]:
    tnow = now or datetime.now(timezone.utc)
    resolved = await resolve_runtime_energy_config(cfg, room_id=room_id)
    mode = resolved.mode.value
    power_entity = resolved.power_entity
    kwh_entity = resolved.kwh_entity
    device_lookup_skipped = resolved.device_lookup_skipped

    raw_power_state, parsed_power, _power_validation = await read_validated_energy_state(
        room_id,
        power_entity,
        kind="power",
    )
    raw_kwh_state, parsed_kwh, _kwh_validation = await read_validated_energy_state(
        room_id,
        kwh_entity,
        kind="energy",
    )

    device_id = str(cfg.get("energy_device_id") or "").strip()
    missing_power = not power_entity or not _power_validation.valid
    missing_kwh = bool(kwh_entity and not _kwh_validation.valid)
    if device_id and (missing_power or missing_kwh):
        device_lookup_skipped = False
        discovered = await discover_energy_entities_for_device(device_id, room_id=room_id)
        recovered_power = str(discovered.get("power_entity") or "").strip()
        recovered_kwh = str(discovered.get("kwh_entity") or "").strip()
        if missing_power and recovered_power and recovered_power != power_entity:
            power_entity = recovered_power
            raw_power_state, parsed_power, _power_validation = await read_validated_energy_state(
                room_id,
                power_entity,
                kind="power",
            )
        if (not kwh_entity or missing_kwh) and recovered_kwh and recovered_kwh != kwh_entity:
            kwh_entity = recovered_kwh
            raw_kwh_state, parsed_kwh, _kwh_validation = await read_validated_energy_state(
                room_id,
                kwh_entity,
                kind="energy",
            )

    st.energy_config_mode = mode
    st.energy_configured = resolved.configured
    st.energy_device_id = resolved.device_id or device_id
    st.energy_device_name = resolved.device_name
    st.energy_device_lookup_skipped = device_lookup_skipped
    st.energy_power_entity = power_entity
    st.energy_kwh_entity = kwh_entity
    st.energy_power_raw_state = raw_power_state
    st.energy_kwh_raw_state = raw_kwh_state
    st.energy_power_unit = _power_validation.unit
    st.energy_power_confidence = _power_validation.confidence
    st.energy_power_validation_reason = _power_validation.reason
    st.energy_power_suspicious = bool(_power_validation.suspicious)

    effective_power, effective_kwh = _apply_telemetry_cache(
        st,
        now=tnow,
        configured=resolved.configured,
        power_entity=power_entity,
        kwh_entity=kwh_entity,
        parsed_power=parsed_power,
        parsed_kwh=parsed_kwh,
    )

    _log_energy_runtime_diagnostic(
        room_id,
        st,
        mode=mode,
        configured=resolved.configured,
        device_lookup_skipped=device_lookup_skipped,
        power_entity=power_entity,
        kwh_entity=kwh_entity,
        raw_power_state=raw_power_state,
        raw_kwh_state=raw_kwh_state,
        parsed_power=parsed_power,
        parsed_kwh=parsed_kwh,
        power_validation_reason=_power_validation.reason,
        power_confidence=_power_validation.confidence,
        power_suspicious=_power_validation.suspicious,
    )
    return effective_power, effective_kwh


async def refresh_runtime_energy(room_id: str, cfg: Optional[dict] = None) -> Tuple[Optional[float], Optional[float]]:
    """Refresh only room-scoped energy runtime from the latest effective config."""
    canon = normalize_room_id(room_id)
    if cfg is None:
        base_cfg = config_manager.load_config()
        room_def = resolve_room_definition(base_cfg, room_id)
        cfg = room_registry.merge_room_config(base_cfg, room_def) if room_def else base_cfg
    return await _read_runtime_energy(canon, cfg, _rt(canon))

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
    # HA setpoint sampled previous tick â€” detect intentional user knob changes vs drift
    prev_ha_setpoint_seen: Optional[float] = None
    manual_override_until: Optional[datetime] = None
    manual_override_temp: Optional[float] = None
    last_sent_command_key: Optional[str] = None
    compressor_on_since: Optional[datetime] = None
    compressor_off_since: Optional[datetime] = None
    # Epoch seconds (UTC wall); set when runtime/IR observes ON/OFF transitions.
    last_ac_on_at: Optional[float] = None
    last_ac_off_at: Optional[float] = None
    # â”€â”€ Single source of truth â€” set once per tick, read everywhere â”€â”€
    # Physical compressor / HA / inferred truth (never masked by pending ON).
    physical_ac_on: bool = False
    # UI / masked: False while pending_action == "on" even if physical is True.
    effective_ac_on: bool = False
    # Display phase: off | pending_on | on | pending_off | on_failed
    ac_state: str = "off"
    effective_ac_idle: bool = False
    effective_power_source: str = "init"       # "cooldown" | "internal"
    # Manual remote likely ON while runtime settles (epoch timestamp, UTC).
    possible_on_since: Optional[float] = None
    # Runtime state source for UI overlay; telemetry has separate confidence.
    ac_state_source: str = "system"
    effective_control_source: str = "none"     # safety_vacant | manual | schedule | thermostat | ai | cooldown | none
    effective_target_temp: float = 24.0

    # â”€â”€ Command authority lock â”€â”€
    last_user_command_time: Optional[datetime] = None
    last_command_source: str = "system"                  # "user" | "system"
    manual_override_config_active: bool = False

    # Last trusted occupancy reading when presence sensor is flaky (None/unavailable).
    last_known_presence: Optional[bool] = None
    # Canonical runtime occupancy after all authoritative inputs are reconciled.
    occupied: bool = True
    vacancy_active: bool = False
    vacancy_hold: bool = False
    safety_vacant: bool = False
    pending_vacancy: bool = False
    pending_vacancy_task: Optional[asyncio.Task] = None
    pending_vacancy_deadline: Optional[float] = None
    vacancy_generation: int = 0
    vacancy_reason: str = ""
    thermostat_blocked: bool = False
    off_reason: Optional[str] = None
    stale_idle: bool = False
    presence_last_true_at: Optional[datetime] = None
    presence_last_false_at: Optional[datetime] = None
    stable_occupied: bool = True
    vacancy_confirmed_at: Optional[datetime] = None
    last_confirmed_on_at: Optional[datetime] = None
    presence_only_present_since: Optional[datetime] = None
    presence_only_last_invalid_log_at: Optional[datetime] = None
    presence_only_idle: bool = False
    presence_control_disabled_logged: bool = False

    # â”€â”€ Startup recovery flag â”€â”€
    startup_state_loaded: bool = False

    # Session lifecycle â€” idle | provisional | confirmed (ended â†’ idle after DB close)
    session_state: str = "idle"
    session_runtime_confirmed: bool = False
    off_dispatch_pending: bool = False
    off_dispatched_at: Optional[datetime] = None
    off_finalized: bool = False
    off_settled_at: Optional[datetime] = None
    pending_off_confirmation: bool = False
    pending_off_sent_at: Optional[datetime] = None
    pending_off_retry_count: int = 0
    off_confirmation_failed: bool = False
    last_confirmed_off_at: Optional[datetime] = None
    # First tick we believe the room is actively being cooled (effective ON); used for vacancy grace
    effective_on_since_ts: Optional[float] = None
    # Last IR / control ON or OFF command applied (wall time, UTC)
    last_decision_at: Optional[datetime] = None
    # Last physical IR dispatch and short post-ON lock for stateless IR stability.
    ir_last_sent_ts: Optional[datetime] = None
    just_turned_on_until: Optional[datetime] = None

    # Delayed actuation (thermostat intent â†’ pending â†’ _turn_ac_*)
    pending_action: Optional[str] = None  # "on" | "off"
    pending_since: Optional[float] = None  # epoch seconds (wall)
    pending_delay_wakeup_task: Optional[asyncio.Task] = None
    # Single-shot delayed ON: after one IR emit in this pending cycle, wait for
    # runtime confirmation or tick-level timeout; telemetry is observational.
    pending_on_ir_sent: bool = False
    pending_on_ir_sent_at: Optional[datetime] = None
    # Pending ON visual state while runtime waits for IR confirmation.
    soft_start_ui: bool = False
    on_failed_retry_used: bool = False
    # FP2 zone (optional): live occupancy reconciliation plus dwell/confirmation for ON gating.
    zone_present: bool = False
    zone_entered_at: Optional[datetime] = None
    zone_confirmed: bool = False
    zone_dwell_passed: bool = False
    zone_confidence: str = "low"  # low | medium | high â€” forward-compatible
    # Last tick: HA zone entity returned a usable state (not missing/unavailable/unknown).
    zone_sensor_usable: bool = False
    # Last HA sample time while raw zone was "on" (usable reads only; exit-debounce anchor).
    zone_last_raw_on_at: Optional[datetime] = None
    zone_block_count: int = 0
    zone_allow_count: int = 0
    zone_log_sig: Optional[tuple] = None
    energy_config_mode: str = EnergyConfigMode.UNCONFIGURED.value
    energy_configured: bool = False
    energy_device_id: str = ""
    energy_device_name: str = ""
    energy_device_lookup_skipped: bool = True
    energy_power_entity: str = ""
    energy_kwh_entity: str = ""
    energy_power_raw_state: Optional[object] = None
    energy_kwh_raw_state: Optional[object] = None
    energy_watts: Optional[float] = None
    energy_kwh: Optional[float] = None
    energy_power_unit: str = ""
    energy_power_confidence: str = "none"
    energy_power_validation_reason: str = ""
    energy_power_suspicious: bool = False
    energy_runtime_log_sig: Optional[tuple] = None
    telemetry_power_live_valid: bool = False
    telemetry_kwh_live_valid: bool = False
    telemetry_status: str = "unconfigured"
    telemetry_confidence: str = "none"
    telemetry_gap: bool = False
    telemetry_invalid_since: Optional[datetime] = None
    telemetry_last_invalid_log_at: Optional[datetime] = None
    telemetry_invalid_log_sig: Optional[tuple] = None
    hvac_control_confidence: str = "medium"
    last_valid_power_watts: Optional[float] = None
    last_valid_energy_kwh: Optional[float] = None
    last_valid_timestamp: Optional[datetime] = None
    # Hybrid event triggers â€” last sampled values from HA WS (not authoritative for control)
    last_event_presence_bool: Optional[bool] = None
    last_event_probe_indoor_temp: Optional[float] = None
    # Last applied comfort-mode (effective_mode) â€” detects config changes to clear stale delays.
    last_effective_mode: Optional[str] = None
    # Last temperature-plan context used for thermostat target sync.
    last_target_context_key: Optional[tuple] = None
    last_temperature_mode: Optional[str] = None
    last_control_effective_target_temp: Optional[float] = None
    effective_target_source: str = "init"
    sleep_offset: float = 0.0
    sleep_phase: str = "inactive"
    sleep_optimization_active: bool = False
    sleep_suspended_reason: Optional[str] = None
    last_sleep_log_sig: Optional[tuple] = None
    humidity_percent: Optional[float] = None
    feels_like_temp: Optional[float] = None
    dew_point: Optional[float] = None
    humidity_offset: float = 0.0
    comfort_score: float = 0.0
    comfort_level: str = "unknown"
    humidity_band: str = "unavailable"
    dry_mode_recommended: bool = False
    last_humidity_log_sig: Optional[tuple] = None
    thermal_load_level: str = "low"
    thermal_load_confidence: str = "low"
    thermal_load_score: float = 0.0
    thermal_load_temp_ema: Optional[float] = None
    thermal_load_rise_rate_ema: float = 0.0
    thermal_load_last_sample_at: Optional[datetime] = None
    thermal_load_candidate_since: Optional[datetime] = None
    thermal_load_last_high_at: Optional[datetime] = None
    thermal_load_compensation_offset: float = 0.0
    thermal_load_compensation_active: bool = False
    cooling_saturated: bool = False
    thermal_load_summary: str = "Monitoring room load"
    last_thermal_load_log_sig: Optional[tuple] = None


_runtime_by_room: Dict[str, RoomRuntime] = {}
# Keys are canonical `normalize_room_id` strings â€” isolated per logical room.

# Serialize tick vs stop_room per room (avoids double OFF / double session close with tick).
_room_ops_locks: Dict[str, asyncio.Lock] = {}
# Serialize scheduler tick vs event-triggered tick for same room (no overlapping decision loops).
_room_tick_serial_locks: Dict[str, asyncio.Lock] = {}

_TICK_TRIGGER_DEBOUNCE_SEC = 2.0
_tick_trigger_last_mono_by_room: Dict[str, float] = {}
_startup_stabilization_until_mono: float = 0.0
_startup_stabilization_logged_rooms: set[str] = set()


def start_startup_stabilization(seconds: float) -> None:
    """Temporarily route ticks through hydration-only startup work."""
    global _startup_stabilization_until_mono
    window = max(0.0, float(seconds or 0.0))
    _startup_stabilization_until_mono = time.monotonic() + window
    _startup_stabilization_logged_rooms.clear()


def end_startup_stabilization() -> None:
    """End startup stabilization and allow normal control ticks again."""
    global _startup_stabilization_until_mono
    _startup_stabilization_until_mono = 0.0
    _startup_stabilization_logged_rooms.clear()


def startup_stabilization_active() -> bool:
    return time.monotonic() < _startup_stabilization_until_mono


def startup_stabilization_remaining_seconds() -> float:
    return max(0.0, _startup_stabilization_until_mono - time.monotonic())


def manual_override_enabled(cfg: dict) -> bool:
    """Durable room-level Manual Override flag; legacy key remains an alias."""
    if not isinstance(cfg, dict):
        return False
    if "manual_override_enabled" in cfg and "manual_override" in cfg:
        return bool(cfg.get("manual_override_enabled")) or bool(cfg.get("manual_override"))
    if "manual_override_enabled" in cfg:
        return bool(cfg.get("manual_override_enabled"))
    return bool(cfg.get("manual_override", False))


def _manual_override_user_settings(cfg: dict) -> dict:
    raw = cfg.get("override_user_settings") if isinstance(cfg, dict) else None
    return dict(raw) if isinstance(raw, dict) else {}


def _restore_persisted_manual_override(room_id: str, cfg: dict, st: RoomRuntime) -> bool:
    """Restore persistent user-authority before any automation decision can run."""
    if not manual_override_enabled(cfg):
        return False
    if not st.manual_override_config_active:
        log_with_room("info", room_id, "[OVERRIDE] restored persistent manual override")
    st.manual_override_config_active = True
    st.effective_control_source = "manual"
    settings = _manual_override_user_settings(cfg)
    raw_target = settings.get("target_temp", cfg.get("target_temp"))
    try:
        if raw_target is not None:
            st.manual_override_temp = float(raw_target)
    except (TypeError, ValueError):
        pass
    return True


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
    if st.pending_vacancy_task is t:
        st.pending_vacancy_task = None


def _cancel_all_pending_wakeup_tasks(st: RoomRuntime) -> None:
    _cancel_pending_delay_wakeup_task(st)
    t = st.pending_vacancy_task
    if t is not None and not t.done():
        t.cancel()
    st.pending_vacancy_task = None


def _clear_pending_command_state(st: RoomRuntime) -> None:
    """Cancel delay wakeup and reset pending_* (used when pending intent is abandoned)."""
    _cancel_all_pending_wakeup_tasks(st)
    st.pending_action = None
    st.pending_since = None
    st.pending_on_ir_sent = False
    st.pending_on_ir_sent_at = None


def _clear_pending_off_confirmation(st: RoomRuntime, *, failed: bool = False) -> None:
    st.pending_off_confirmation = False
    st.pending_off_sent_at = None
    st.pending_off_retry_count = 0
    st.off_confirmation_failed = bool(failed)


def _is_vacancy_off_reason(reason: Optional[str]) -> bool:
    return str(reason or "") in {
        "vacant",
        "presence_vacant",
        "safety_vacant",
        "presence_vacancy_grace",
        "vacancy_debounce",
    }


def _start_vacancy_cycle(
    room_id: str,
    st: RoomRuntime,
    now: datetime,
    *,
    reason: str,
    timeout_seconds: float,
) -> None:
    if st.vacant_since is not None:
        if not st.vacancy_reason:
            st.vacancy_reason = reason
        if st.pending_vacancy_deadline is None:
            elapsed = max(0.0, (now - st.vacant_since).total_seconds())
            remaining = max(0.0, float(timeout_seconds) - elapsed)
            st.pending_vacancy_deadline = time.time() + remaining
        return
    st.vacant_since = now
    st.vacancy_generation += 1
    st.vacancy_reason = reason
    st.pending_vacancy_deadline = time.time() + max(0.0, float(timeout_seconds))
    log_with_room(
        "info",
        room_id,
        "[VACANCY] started timeout=%.0fs generation=%s reason=%s",
        timeout_seconds,
        st.vacancy_generation,
        reason,
    )


def _cancel_pending_vacancy_shutdown(
    room_id: str,
    st: RoomRuntime,
    *,
    due_to: str,
) -> bool:
    had_pending = bool(
        st.pending_vacancy
        or st.pending_vacancy_task is not None
        or st.pending_vacancy_deadline is not None
        or _is_vacancy_off_reason(st.off_reason)
        or st.vacant_since is not None
        or st.vacancy_reason
    )
    t = st.pending_vacancy_task
    if t is not None and not t.done():
        t.cancel()
    if st.pending_delay_wakeup_task is t:
        st.pending_delay_wakeup_task = None
    st.pending_vacancy_task = None
    st.pending_vacancy_deadline = None
    st.vacancy_reason = ""
    if st.pending_action == "off" and _is_vacancy_off_reason(st.off_reason):
        _clear_pending_command_state(st)
        st.off_reason = None
    st.pending_vacancy = False
    if had_pending:
        st.vacancy_generation += 1
        log_with_room(
            "info",
            room_id,
            "[VACANCY] cancelled due_to=%s generation=%s",
            due_to,
            st.vacancy_generation,
        )
    return had_pending


def schedule_pending_completion_wakeup(
    *,
    rid_for_tick: str,
    room_canon: str,
    kind: str,
    delay_seconds: float,
    vacancy_generation: Optional[int] = None,
) -> None:
    """
    Fire trigger_tick(delay_elapsed) after delay_seconds if pending_arm still matches.
    Non-blocking â€” complements the periodic scheduler tick.
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
            if vacancy_generation is not None:
                if vacancy_generation != st2.vacancy_generation:
                    log_with_room(
                        "info",
                        canon,
                        "[VACANCY] stale_timer_ignored reason=generation_mismatch generation=%s current=%s",
                        vacancy_generation,
                        st2.vacancy_generation,
                    )
                    return
                if st2.occupied or st2.stable_occupied:
                    log_with_room(
                        "info",
                        canon,
                        "[VACANCY] stale_timer_ignored reason=reoccupancy generation=%s",
                        vacancy_generation,
                    )
                    return
        trigger_tick(rid_for_tick, reason="delay_elapsed", skip_debounce=True)

    _cancel_pending_delay_wakeup_task(st)
    task = loop.create_task(_alarm())
    st.pending_delay_wakeup_task = task
    if vacancy_generation is not None:
        st.pending_vacancy_task = task


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
        logger.debug("[TICK_TRIGGER] skipped â€” no event loop (%s)", reason)
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
    if lk.locked():
        # Drop all event-triggered ticks while a tick is already running.
        # The scheduler tick is the fallback for any work skipped here.
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
    """Canonical room key: lower-case + strip â€” use for runtime, sessions, telemetry."""
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


# Command cooldown â€” after any climate command, skip control logic for this window.
_COOLDOWN_SECS: int = 60
# After first delayed-path ON IR in a pending cycle, wait this long for runtime
# confirmation before surfacing on_failed and clearing pending.
PENDING_ON_CONFIRM_TIMEOUT_SECS: float = 20.0
IR_SEND_LOCK_SECONDS: float = 10.0
POST_ON_STABILIZATION_SECONDS: float = 20.0

# Telemetry analytics/reference thresholds; never HVAC authority.
_WATTS_COMPRESSOR: float = 500.0   # analytics threshold for compressor-like draw
_WATTS_FAN_ONLY:   float = 50.0    # UI/reference threshold for fan-only-like draw

# Probable manual-ON inference window from occupancy + target demand (seconds).
TRANSIENT_ON_WINDOW_SECS: float = 180.0
MIN_SESSION_SECONDS: float = 30.0
COMPRESSOR_STABLE_SECONDS: float = 10.0
VACANCY_SESSION_GRACE_SECONDS: float = 120.0
MIN_ON_TIME_SECONDS: float = 90.0
RUNNING_OFF_BLOCK_SECS: float = 180.0
SESSION_FINALIZATION_GRACE_SECONDS: float = 20.0
MEANINGFUL_SESSION_SECONDS: float = 180.0
MIN_SESSION_ENERGY_KWH: float = 0.001
OFF_TERMINAL_RECONCILE_SECONDS: float = 60.0
OFF_CONFIRM_WATTS: float = 120.0
OFF_CONFIRM_RETRY_SECONDS: float = 25.0
MAX_OFF_CONFIRM_RETRIES: int = 2
VACANCY_CONFIRM_SECS: float = 60.0
PRESENCE_STABILIZATION_SECS: float = 60.0
DECISION_LOCK_SECONDS: float = 65.0
MAX_PROVISIONAL_SECONDS: float = 180.0
# Recent IR/compressor-command window: session may open after explicit ON before ac_is_on latches.
_POST_ON_SESSION_INTENT_SECONDS: float = float(_COOLDOWN_SECS) + 120.0
THERMAL_LOAD_PERSIST_SECONDS: float = 300.0
THERMAL_LOAD_RELEASE_SECONDS: float = 480.0
THERMAL_LOAD_MAX_COMPENSATION_DEG: float = 1.0
THERMAL_LOAD_MEDIUM_COMPENSATION_DEG: float = 0.5
THERMAL_LOAD_MIN_TARGET_C: float = 16.0
THERMAL_LOAD_SATURATION_TARGET_C: float = 17.0


def _cfg_float(
    cfg: dict,
    key: str,
    default: float,
    *,
    lo: Optional[float] = None,
    hi: Optional[float] = None,
) -> float:
    try:
        val = float(cfg.get(key, default))
        if not math.isfinite(val):
            raise ValueError("non-finite")
    except (TypeError, ValueError):
        val = float(default)
    if lo is not None:
        val = max(float(lo), val)
    if hi is not None:
        val = min(float(hi), val)
    return val


def _cfg_int(
    cfg: dict,
    key: str,
    default: int,
    *,
    lo: Optional[int] = None,
    hi: Optional[int] = None,
) -> int:
    try:
        raw = cfg.get(key, default)
        val = int(raw)
    except (TypeError, ValueError):
        try:
            val = int(float(cfg.get(key, default)))
        except (TypeError, ValueError):
            val = int(default)
    if lo is not None:
        val = max(int(lo), val)
    if hi is not None:
        val = min(int(hi), val)
    return val


def _seconds_since_last_command(st: RoomRuntime, now: datetime) -> float:
    if st.last_command_time is None:
        return float("inf")
    return (now - st.last_command_time).total_seconds()


def _seconds_since_last_ir(st: RoomRuntime, now: datetime) -> float:
    if st.ir_last_sent_ts is None:
        return float("inf")
    return (now - st.ir_last_sent_ts).total_seconds()


def _ir_send_lock_active(st: RoomRuntime, now: datetime) -> bool:
    return _seconds_since_last_ir(st, now) < float(IR_SEND_LOCK_SECONDS)


def _post_on_stabilization_active(st: RoomRuntime, now: datetime) -> bool:
    return st.just_turned_on_until is not None and now < st.just_turned_on_until


def _bump_last_command_ir_cooldown(st: RoomRuntime, cmd_ts: datetime) -> None:
    """
    Always anchors cooldown to the most recent command.
    Previous bug: only updated if previous cooldown had expired.
    This meant rapid ONâ†’OFF would anchor to ON, not OFF.
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
    lock_secs = _cfg_int(cfg, "user_authority_lock_secs", 120, lo=0)
    elapsed = (now - st.last_user_command_time).total_seconds()
    return elapsed < lock_secs


def clear_manual_override(
    room_id: str,
    *,
    reason: str = "manual_override_cleared",
) -> bool:
    """
    Clear runtime-only manual override/user-authority latches immediately.

    This does not send IR or alter thermostat math; it only releases state that
    can keep automation paused after the persisted manual_override flag is false.
    """
    canon = normalize_room_id(room_id)
    st = _rt(canon)
    had_override_state = bool(
        st.manual_override_config_active
        or st.manual_override_until is not None
        or st.manual_override_temp is not None
        or st.prev_ha_setpoint_seen is not None
        or st.last_user_command_time is not None
        or st.last_command_source == "user"
        or st.effective_control_source == "manual"
    )

    st.manual_override_config_active = False
    st.manual_override_until = None
    st.manual_override_temp = None
    st.prev_ha_setpoint_seen = None
    st.last_user_command_time = None
    st.last_command_source = "system"
    if st.effective_control_source == "manual":
        st.effective_control_source = "none"

    if had_override_state:
        log_with_room("info", canon, "[OVERRIDE] cleared reason=%s", reason)
        log_with_room("info", canon, "[OVERRIDE] runtime_resumed")
    return had_override_state


async def clear_manual_override_and_resume(
    room_id: str,
    *,
    reason: str = "manual_override_cleared",
) -> bool:
    """Clear override latches, publish runtime, and request an immediate tick."""
    canon = normalize_room_id(room_id)
    cleared = clear_manual_override(canon, reason=reason)
    try:
        await live_broadcast.broadcast_room_update(canon)
    except Exception:
        logger.debug("[OVERRIDE][%s] runtime broadcast failed", canon, exc_info=True)
    trigger_tick(canon, reason=reason, skip_debounce=True)
    return cleared


def _has_vacancy_runtime_state(st: RoomRuntime) -> bool:
    return bool(
        not st.occupied
        or st.vacancy_active
        or st.vacancy_hold
        or st.safety_vacant
        or st.pending_vacancy
        or st.thermostat_blocked
        or st.vacant_since is not None
        or st.vacancy_confirmed_at is not None
        or st.stable_occupied is False
        or st.last_known_presence is False
        or st.presence_only_idle
        or st.effective_control_source in (
            "safety_vacant",
            "vacancy_debounce",
            "presence_vacant",
            "presence_vacancy_grace",
            "presence_idle",
        )
    )


def _clear_vacancy_state(
    room_id: str,
    st: RoomRuntime,
    now: datetime,
    *,
    reason: str,
) -> bool:
    """
    Make confirmed occupancy canonical and release stale vacancy/off latches.

    Returns True when a recovery transition was actually performed. Repeated
    occupied ticks remain quiet so the recovery log is a transition signal.
    """
    had_vacancy_state = _has_vacancy_runtime_state(st)
    previous_occupied = bool(st.occupied)
    previous_vacancy_active = bool(st.vacancy_active)

    st.occupied = True
    st.stable_occupied = True
    st.last_known_presence = True
    st.presence_last_true_at = now
    st.presence_last_false_at = None
    _cancel_pending_vacancy_shutdown(room_id, st, due_to=reason)
    st.vacant_since = None
    st.vacancy_confirmed_at = None
    st.vacancy_active = False
    st.vacancy_hold = False
    st.safety_vacant = False
    st.pending_vacancy = False
    st.thermostat_blocked = False
    st.stale_idle = False
    st.presence_only_idle = False

    if st.pending_action == "off" and _is_vacancy_off_reason(st.off_reason):
        _clear_pending_command_state(st)

    if st.last_command == "off" and _is_vacancy_off_reason(st.off_reason):
        st.last_command_time = None
        st.last_command = ""
        st.last_sent_command_key = None
        st.off_dispatch_pending = False
        st.off_dispatched_at = None
        st.off_finalized = False
        st.off_settled_at = None
        _clear_pending_off_confirmation(st)
        st.off_reason = None

    if st.effective_control_source in (
        "safety_vacant",
        "vacancy_debounce",
        "presence_vacant",
        "presence_vacancy_grace",
        "presence_idle",
    ):
        st.effective_control_source = "none"

    if had_vacancy_state:
        log_with_room(
            "info",
            room_id,
            "[OCCUPANCY] zone_present=%s zone_confirmed=%s runtime_occupied=%s "
            "vacancy_active=%s recovery_triggered=%s",
            st.zone_present,
            st.zone_confirmed,
            previous_occupied,
            previous_vacancy_active,
            True,
        )
        log_with_room("info", room_id, "[RUNTIME] vacancy_cleared reason=%s", reason)
        return True
    return False


def _log_occupancy_sync_transition(
    room_id: str,
    st: RoomRuntime,
    *,
    ha_presence: Optional[bool],
    source: str,
) -> None:
    vacant_since = st.vacant_since.isoformat() if st.vacant_since else None
    logger.debug(
        "[OCCUPANCY_SYNC] room=%s ha_presence=%s runtime_occupied=%s "
        "stable_occupied=%s vacant_since=%s source=%s",
        room_id,
        ha_presence,
        st.occupied,
        st.stable_occupied,
        vacant_since,
        source,
    )


def _mark_runtime_vacant(
    room_id: str,
    st: RoomRuntime,
    now: datetime,
    *,
    reason: str,
) -> None:
    was_occupied = bool(st.occupied or st.stable_occupied or st.last_known_presence)
    st.occupied = False
    st.stable_occupied = False
    st.last_known_presence = False
    if (
        st.presence_last_false_at is None
        or (
            st.presence_last_true_at is not None
            and st.presence_last_false_at < st.presence_last_true_at
        )
    ):
        st.presence_last_false_at = now
    if reason == "zone_exit" and was_occupied:
        log_with_room(
            "info",
            room_id,
            "[OCCUPANCY] zone_present=%s runtime_occupied=False vacancy_pending=True",
            st.zone_present,
        )


def _zone_required_for_on_active(cfg: dict) -> bool:
    return bool(cfg.get("zone_required_for_on", False)) and bool(
        str(cfg.get("zone_entity_id") or "").strip()
    )


def _zone_waiting_for_confirmation(cfg: dict, st: RoomRuntime, is_occupied: bool) -> bool:
    return bool(is_occupied and _zone_required_for_on_active(cfg) and not st.zone_confirmed)


def _zone_presence_holds_vacancy(cfg: dict, st: RoomRuntime) -> bool:
    return bool(
        _zone_required_for_on_active(cfg)
        and not st.zone_confirmed
        and st.last_known_presence
    )


def _sync_runtime_occupancy(
    room_id: str,
    st: RoomRuntime,
    is_occupied: bool,
    now: datetime,
    *,
    cfg: Optional[dict] = None,
    source: str = "ha_presence",
) -> bool:
    before = (
        st.occupied,
        st.stable_occupied,
        st.last_known_presence,
        st.vacant_since,
    )

    if _zone_waiting_for_confirmation(cfg or {}, st, is_occupied):
        st.occupied = False
        st.stable_occupied = False
        st.last_known_presence = True
        st.presence_last_true_at = now
        st.presence_last_false_at = None
        st.presence_only_present_since = None
        _cancel_pending_vacancy_shutdown(room_id, st, due_to="zone_wait_presence")
        resolved = False
    elif is_occupied:
        _clear_vacancy_state(room_id, st, now, reason="presence_reentry")
        resolved = True
    else:
        _mark_runtime_vacant(room_id, st, now, reason="presence_exit")
        resolved = False

    after = (
        st.occupied,
        st.stable_occupied,
        st.last_known_presence,
        st.vacant_since,
    )
    if after != before:
        _log_occupancy_sync_transition(
            room_id,
            st,
            ha_presence=bool(is_occupied),
            source=source,
        )
    return resolved


def _resolve_authoritative_room_presence(
    presence_raw: object,
    *,
    use_presence: bool,
) -> Tuple[bool, str]:
    if not use_presence:
        return True, "presence_disabled"
    if _presence_raw_invalid(presence_raw):
        return False, "presence_unavailable"
    return bool(parse_presence(presence_raw)), "ha_presence"


def normalize_use_presence(cfg: dict) -> bool:
    raw = cfg.get("use_presence", True) if isinstance(cfg, dict) else True
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return True
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in {"false", "0", "off", "no"}:
            return False
        if value in {"true", "1", "on", "yes"}:
            return True
    return True


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
      1. SAFETY        â€” vacancy hard-off when timer expires (runs even during IR cooldown)
      2. USER LOCK     â€” API user authority overrides thermostat + cooldown hold
      3. COOLDOWN      â€” block thermostat ON/OFF until window elapses (safety exempt above)
      4. THERMOSTAT    â€” hysteresis ON/OFF
      5. HOLD          â€” nothing to do

    AI never appears in this function.
    AI only adjusts effective_target BEFORE this function is called.
    """
    st = _rt(room_id)
    on_delta = _cfg_float(cfg, "thermostat_on_delta_deg", 0.7, lo=0.0)
    off_delta = _cfg_float(cfg, "thermostat_off_delta_deg", 0.3, lo=0.0)
    vacancy_timeout = max(
        _cfg_int(cfg, "vacancy_timeout_minutes", 5, lo=0) * 60,
        float(VACANCY_CONFIRM_SECS),
    )
    use_presence = normalize_use_presence(cfg)

    # â”€â”€ PRIORITY 1: Safety â€” vacancy (may issue OFF even during global cooldown) â”€
    if use_presence and not is_occupied and _zone_presence_holds_vacancy(cfg, st):
        return ("hold", "zone_wait", effective_target)

    if use_presence and not is_occupied:
        st.vacancy_active = True
        st.thermostat_blocked = True
        if st.vacant_since is None:
            _start_vacancy_cycle(
                room_id,
                st,
                now,
                reason="safety_vacant",
                timeout_seconds=vacancy_timeout,
            )
        elapsed = (now - st.vacant_since).total_seconds()
        if elapsed < float(VACANCY_CONFIRM_SECS):
            st.pending_vacancy = True
            st.vacancy_hold = False
            st.safety_vacant = False
            log_with_room(
                "info",
                room_id,
                "[CONTROL] Block OFF â€” vacancy not stable (%.1fs < %.0fs)",
                elapsed,
                VACANCY_CONFIRM_SECS,
            )
            return ("hold", "vacancy_debounce", effective_target)
        st.pending_vacancy = False
        if st.vacant_since is not None:
            if elapsed >= vacancy_timeout and (ac_on or st.ac_is_on):
                st.vacancy_hold = True
                st.safety_vacant = True
                on_age = _seconds_since_effective_on_or_command(st, now)
                if on_age < float(RUNNING_OFF_BLOCK_SECS):
                    log_with_room(
                        "info",
                        room_id,
                        "[CONTROL] Block OFF â€” post-ON protection (%s, %.1fs < %.0fs)",
                        "safety_vacant",
                        on_age,
                        RUNNING_OFF_BLOCK_SECS,
                    )
                    return ("hold_vacant", "running_protection", effective_target)
                if st.physical_ac_on:
                    if on_age < float(VACANCY_SESSION_GRACE_SECONDS):
                        log_with_room(
                            "info",
                            room_id,
                            "[VACANCY] Ignored for room=%s â€” cooling grace (%.0fs < %.0fs) "
                            "(effective_on / last_on)",
                            room_id,
                            on_age,
                            VACANCY_SESSION_GRACE_SECONDS,
                        )
                        return ("hold_vacant", "safety_vacant", effective_target)
                st.vacancy_hold = False
                return ("off", "safety_vacant", effective_target)
        st.vacancy_hold = True
        st.safety_vacant = True
        return ("hold_vacant", "safety_vacant", effective_target)
    if use_presence:
        _cancel_pending_vacancy_shutdown(room_id, st, due_to="reoccupancy")
        st.vacant_since = None

    # â”€â”€ PRIORITY 2: User authority â€” overrides thermostat and cooldown â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if _is_user_authority_active(st, cfg, now):
        return ("hold", "manual", effective_target)

    # â”€â”€ PRIORITY 3: Global IR cooldown â€” block thermostat commands only â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if (
        _is_in_cooldown(st, now)
        and st.pending_action != "on"
        and st.ac_state != "on_failed"
    ):
        return ("hold_cooldown", "cooldown", effective_target)

    # â”€â”€ PRIORITY 4: Thermostat hysteresis â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    delta = indoor_temp - effective_target

    if delta > on_delta and not ac_on:
        return ("on", "thermostat", effective_target)

    if delta < -off_delta and ac_on:
        return ("off", "thermostat_reached", effective_target)

    # â”€â”€ PRIORITY 5: Hold â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    return ("hold", "thermostat", effective_target)


def normalize_control_mode(cfg: dict) -> str:
    mode = str(cfg.get("control_mode") or "thermostat").strip().lower()
    return mode if mode in ("thermostat", "presence_only") else "thermostat"


def normalize_ir_backend(cfg: dict) -> str:
    backend = str(cfg.get("ir_backend") or "aerostate").strip().lower()
    return backend if backend in ("aerostate", "tuya") else "aerostate"


async def resolve_ir_backend(room_id: str, cfg: dict, climate_entity: str) -> str:
    backend = normalize_ir_backend(cfg)
    log_with_room("info", room_id, "[IR] backend=%s", backend)
    return backend


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


def _stabilize_presence(
    st: RoomRuntime,
    presence_raw: object,
    now: datetime,
    room_id: Optional[str] = None,
) -> bool:
    raw_presence = parse_presence(presence_raw)

    if raw_presence:
        if (
            st.presence_last_true_at is None
            or (
                st.presence_last_false_at is not None
                and st.presence_last_true_at < st.presence_last_false_at
            )
        ):
            st.presence_last_true_at = now

        if not st.stable_occupied:
            elapsed_true = (now - st.presence_last_true_at).total_seconds()
            if elapsed_true < float(PRESENCE_STABILIZATION_SECS):
                st.last_known_presence = False
                return False

        st.stable_occupied = True
        st.last_known_presence = True
        st.vacancy_confirmed_at = None
        return st.stable_occupied

    if (
        st.presence_last_false_at is None
        or (
            st.presence_last_true_at is not None
            and st.presence_last_false_at < st.presence_last_true_at
        )
    ):
        st.presence_last_false_at = now

    elapsed = (now - st.presence_last_false_at).total_seconds()

    if elapsed < float(VACANCY_CONFIRM_SECS):
        if st.stable_occupied and room_id:
            log_with_room(
                "info",
                room_id,
                "[CONTROL] Block OFF â€” vacancy not stable (%.1fs < %.0fs)",
                elapsed,
                VACANCY_CONFIRM_SECS,
            )
        st.last_known_presence = st.stable_occupied
        return st.stable_occupied

    st.stable_occupied = False
    st.last_known_presence = False
    if st.vacancy_confirmed_at is None:
        st.vacancy_confirmed_at = now
    return st.stable_occupied


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
    resolved_occupied: Optional[bool] = None,
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
                "[PRESENCE_ONLY][%s] Presence unavailable (%r) â€” holding current state",
                room_id,
                presence_raw,
            )
            st.presence_only_last_invalid_log_at = now
        return "hold", "presence_unavailable", False

    occupied = (
        bool(resolved_occupied)
        if resolved_occupied is not None
        else _resolve_authoritative_room_presence(presence_raw, use_presence=True)[0]
    )
    st.presence_only_last_invalid_log_at = None

    runtime = _presence_only_runtime_seconds(st, now)
    max_runtime = _presence_only_max_runtime_seconds(cfg)
    if ac_on and runtime is not None and runtime >= max_runtime:
        log_with_room(
            "warning",
            room_id,
            "[PRESENCE_ONLY][%s] Max runtime exceeded %.0fs >= %.0fs â€” forcing OFF",
            room_id,
            runtime,
            max_runtime,
        )
        return "off", "presence_max_runtime", occupied

    if _zone_presence_holds_vacancy(cfg, st):
        st.presence_only_present_since = None
        return "hold", "zone_wait", False

    if occupied:
        st.presence_only_idle = False
        _cancel_pending_vacancy_shutdown(room_id, st, due_to="reoccupancy")
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
    vacancy_timeout = max(
        _cfg_int(cfg, "vacancy_timeout_minutes", 5, lo=0) * 60,
        float(VACANCY_CONFIRM_SECS),
    )
    if ac_on:
        if st.vacant_since is None:
            _start_vacancy_cycle(
                room_id,
                st,
                now,
                reason="presence_vacant",
                timeout_seconds=vacancy_timeout,
            )
        log_with_room(
            "info",
            room_id,
            "[PRESENCE_ONLY] vacancy_ts=%s",
            st.vacant_since.isoformat(),
        )
        elapsed_vacant = (now - st.vacant_since).total_seconds()
        if elapsed_vacant < float(VACANCY_CONFIRM_SECS):
            return "hold", "vacancy_debounce", occupied
        if elapsed_vacant < vacancy_timeout:
            return "hold", "presence_vacancy_grace", occupied
        return "off", "presence_vacant", occupied

    if st.presence_only_idle:
        return "idle", "presence_idle", occupied

    if st.vacant_since is None:
        _start_vacancy_cycle(
            room_id,
            st,
            now,
            reason="presence_idle",
            timeout_seconds=vacancy_timeout,
        )
    log_with_room(
        "info",
        room_id,
        "[PRESENCE_ONLY] vacancy_ts=%s",
        st.vacant_since.isoformat(),
    )
    return "idle", "presence_idle", occupied


async def _finalize_presence_only_idle(
    room_id: str,
    cfg: dict,
    indoor_temp: float,
    now: datetime,
    st: RoomRuntime,
    *,
    reason: str,
    duplicate_off_block_detected: bool = False,
) -> None:
    """Collapse vacant + already-off presence-only runtime into an idempotent idle state."""
    open_session = session_logger.current_session_id(room_id)
    had_runtime_state = any(
        (
            st.pending_action is not None,
            st.pending_on_ir_sent,
            st.ac_is_on,
            st.physical_ac_on,
            st.effective_ac_on,
            st.effective_ac_idle,
            st.effective_on_since_ts is not None,
            st.possible_on_since is not None,
            st.soft_start_ui,
            st.session_start_time is not None,
            st.session_state != "idle",
            bool(st.watts_samples),
            st.last_command_time is not None,
            st.last_command != "",
            st.last_sent_command_key is not None,
            st.last_decision_at is not None,
            st.ir_last_sent_ts is not None,
            open_session is not None,
        )
    )
    entering_idle = not st.presence_only_idle or had_runtime_state
    vacancy_ts = st.vacant_since.isoformat() if st.vacant_since else "none"

    if duplicate_off_block_detected:
        log_with_room(
            "warning",
            room_id,
            "[PRESENCE_ONLY] duplicate_off_block_detected pending_action=%s pending_on_ir_sent=%s",
            st.pending_action,
            st.pending_on_ir_sent,
        )

    if open_session is not None:
        await _close_session(room_id, cfg, indoor_temp, reason=reason)

    if not entering_idle and not duplicate_off_block_detected:
        return

    if entering_idle:
        log_with_room(
            "info",
            room_id,
            "[PRESENCE_ONLY] off_finalize reason=%s",
            reason,
        )
        log_with_room(
            "info",
            room_id,
            "[PRESENCE_ONLY] vacancy_ts=%s",
            vacancy_ts,
        )

    _clear_pending_command_state(st)
    clear_setpoint_command_tracking(room_id)
    smart_cooling.reset(room_id)

    st.ac_is_on = False
    st.physical_ac_on = False
    st.effective_ac_on = False
    st.effective_ac_idle = False
    st.ac_state = "off"
    if st.ac_state_source == "cooldown":
        st.effective_power_source = "cooldown"
    else:
        st.effective_power_source = "internal"
    st.effective_control_source = "presence_idle"
    st.effective_on_since_ts = None
    st.possible_on_since = None
    st.presence_only_present_since = None
    st.vacant_since = None
    st.soft_start_ui = False
    st.on_failed_retry_used = False
    st.session_start_time = None
    st.session_start_temp = None
    st.session_start_kwh = None
    st.watts_samples = []
    st.session_state = "idle"
    st.compressor_on_since = None
    st.compressor_off_since = now
    st.last_command_time = None
    st.last_command = ""
    st.last_sent_command_key = None
    st.last_decision_at = None
    st.ir_last_sent_ts = None
    st.just_turned_on_until = None
    st.last_user_command_time = None
    st.last_command_source = "system"
    st.off_dispatch_pending = False
    st.off_dispatched_at = None
    st.off_finalized = True
    st.off_settled_at = now
    st.last_confirmed_off_at = now
    _clear_pending_off_confirmation(st)
    st.presence_only_idle = True

    if entering_idle:
        log_with_room(
            "info",
            room_id,
            "[PRESENCE_ONLY] runtime_reset",
        )
        log_with_room(
            "info",
            room_id,
            "[PRESENCE_ONLY] idle_entered",
        )


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
    Never blocks OFF, safety, cooldown, or user paths. Required zone-gated ON needs confirmed zone presence.
    Returns (action, source, zone_gate_blocked).
    """
    st = _rt(room_id)
    if action != "on" or str(source) != "thermostat":
        return action, source, False
    zone_e = (str(cfg.get("zone_entity_id") or "")).strip()
    required = bool(cfg.get("zone_required_for_on", False))
    if not required or not zone_e:
        return action, source, False

    already_running_or_starting = bool(
        st.ac_is_on
        or st.physical_ac_on
        or st.effective_ac_on
        or st.ac_state in ("on", "pending_on")
        or st.pending_action == "on"
        or st.pending_on_ir_sent
    )
    if already_running_or_starting:
        st.zone_allow_count += 1
        return action, source, False

    if st.zone_confirmed:
        st.zone_allow_count += 1
        return action, source, False
    st.zone_block_count += 1
    log_with_room("info", room_id, "[CONTROL] zone_gate_blocked")
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
        logger.error("[SNAPSHOT] Skipping snapshot for room=%s â€” missing or blank session_id", room_id)
        return False
    for field in _REQUIRED_SNAPSHOT_FIELDS:
        if field == "session_id":
            continue
        if data.get(field) is None:
            logger.error(
                "[SNAPSHOT] Skipping snapshot for room=%s â€” required field '%s' is None",
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
    _restore_persisted_manual_override(room_id, cfg, st)

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


async def startup_hydrate_room(room_id: str) -> None:
    """
    Restore startup runtime/telemetry only; never evaluates HVAC decisions or sends commands.
    """
    rid_raw = (room_id or "").strip()
    if not rid_raw:
        return
    base_cfg = config_manager.load_config()
    room_def = resolve_room_definition(base_cfg, rid_raw)
    if not room_def or room_def.get("disabled"):
        return
    canon = normalize_room_id(rid_raw)
    cfg = room_registry.merge_room_config(base_cfg, room_def)
    st = _rt(canon)
    await _load_startup_state(canon, cfg)
    try:
        await _read_runtime_energy(canon, cfg, st, now=datetime.now(timezone.utc))
    except Exception:
        logger.debug("[CONTROL][%s] startup hydrate telemetry refresh failed", canon, exc_info=True)


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
      - NEVER raises â€” returns 0.0 on any failure
      - Returns a value clamped to Â±1.0 Â°C
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

    schedule slot â†’ outdoor curve â†’ ``eff_aw`` + AI delta â†’ ``apply_effective_mode_engine_target``.

    Omitting `_manual_override_resolve` here avoids mutating `prev_ha_setpoint_seen` from
    the WebSocket path; if a user knob lock is active, the periodic tick still reconciles bands.

    Returns None when ``manual_override`` (global skip flag) matches tick early-return.
    """
    if manual_override_enabled(merged_cfg):
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
    target_before_sleep = apply_effective_mode_engine_target(
        room_id=room_canon,
        base_temp=float(base_temp),
        planned_with_ai=planned_raw,
        cfg=merged_cfg,
        control_log=False,
    )
    sleep_result = _apply_sleep_optimizer_layer(
        room_canon,
        merged_cfg,
        now=datetime.now(timezone.utc),
        indoor_temp=indoor_temp,
        target_before_sleep=target_before_sleep,
        log_change=False,
    )
    humidity_percent = await _read_indoor_humidity(merged_cfg)
    humidity_result = _apply_humidity_comfort_layer(
        room_canon,
        merged_cfg,
        indoor_temp=indoor_temp,
        humidity_percent=humidity_percent,
        target_before_humidity=sleep_result.adjusted_target,
        ac_on=False,
        log_change=False,
    )
    return _apply_thermal_load_comfort_layer(
        room_canon,
        merged_cfg,
        now=datetime.now(timezone.utc),
        indoor_temp=indoor_temp,
        outdoor_temp=outdoor_temp,
        humidity_percent=humidity_percent,
        target_before_thermal=float(humidity_result.adjusted_target),
        ac_on=False,
        occupied=True,
        climate_data={},
        log_change=False,
    )


_EFF_DELTA_MIN = 1.0
_EFF_DELTA_MAX = 5.0


def effective_max_delta_deg(cfg: dict) -> float:
    """Max Â°C above schedule base for auto combined adjustment and manual ceiling (default 3, clamp 1â€“5)."""
    return _cfg_float(
        cfg,
        "effective_max_delta_deg",
        3.0,
        lo=_EFF_DELTA_MIN,
        hi=_EFF_DELTA_MAX,
    )


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
      or AI delta is negative. We intentionally do not model â€œcool below schedule baseâ€ in this band:
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
            "[CONTROL][%s] effective_mode %s â†’ %s â€” clearing pending_action / pending_since",
            room_id,
            st.last_effective_mode,
            cur,
        )
        _clear_pending_command_state(st)
    st.last_effective_mode = cur


def _target_context_key(cfg: dict, slot_label: str, base_temp: float) -> tuple:
    mode = str(cfg.get("temperature_mode") or "manual").strip().lower()
    if mode not in ("manual", "schedule", "schedule_ai"):
        mode = "manual"
    eff_mode = str(cfg.get("effective_mode") or "auto").strip().lower()
    if eff_mode not in ("auto", "manual"):
        eff_mode = "auto"
    raw_manual_eff = cfg.get("manual_effective_temp")
    try:
        manual_eff = (
            round(float(raw_manual_eff), 1)
            if raw_manual_eff is not None and str(raw_manual_eff).strip() != ""
            else None
        )
    except (TypeError, ValueError):
        manual_eff = None
    return (
        mode,
        slot_label if mode != "manual" else "manual",
        round(float(base_temp), 1),
        bool(cfg.get("ai_enabled", False)),
        eff_mode,
        manual_eff,
        round(effective_max_delta_deg(cfg), 1),
    )


def _clear_stale_target_runtime_state(
    st: RoomRuntime,
    room_id: str,
    *,
    reason: str,
    reset_target: Optional[float] = None,
) -> None:
    _clear_pending_command_state(st)
    st.manual_override_until = None
    st.manual_override_temp = None
    st.prev_ha_setpoint_seen = None
    if reset_target is not None:
        try:
            st.effective_target_temp = float(reset_target)
        except (TypeError, ValueError):
            pass
    log_with_room(
        "info",
        room_id,
        "[TARGET_SYNC] cleared stale runtime target state reason=%s",
        reason,
    )


def sync_target_context_transition(
    st: RoomRuntime,
    room_id: str,
    cfg: dict,
    slot_label: str,
    base_temp: float,
) -> None:
    """
    Schedule/manual/AI target-plan changes invalidate HA setpoint-derived state.

    This prevents an old manual/climate target from replacing the freshly
    computed control effective target during thermostat evaluation.
    """
    cur = _target_context_key(cfg, slot_label, base_temp)
    prev = st.last_target_context_key
    if prev is not None and prev != cur:
        log_with_room(
            "info",
            room_id,
            "[TARGET_SYNC] target context changed %s -> %s",
            prev,
            cur,
        )
        _clear_stale_target_runtime_state(
            st,
            room_id,
            reason="target_context_changed",
            reset_target=base_temp,
        )
    elif (
        prev is None
        and cur[0] != "manual"
        and (st.manual_override_until is not None or st.manual_override_temp is not None)
    ):
        _clear_stale_target_runtime_state(
            st,
            room_id,
            reason="non_manual_initial_context",
            reset_target=base_temp,
        )
    st.last_target_context_key = cur
    st.last_temperature_mode = cur[0]


def _nonnegative_delay_seconds(cfg: dict, key: str) -> float:
    try:
        v = float(cfg.get(key, 0))
    except (TypeError, ValueError):
        v = 0.0
    return max(0.0, min(v, 86_400.0))


def _sync_pending_for_action(st: RoomRuntime, decision_action: str) -> None:
    """
    Hard-reset pending when decision no longer matches scheduled actuation.
    - decision not in (on, off) â†’ clear
    - decision != pending_action â†’ clear

    Always cancels delay wakeup before clearing pending so stale ``delay_elapsed``
    triggers cannot fire after intent changed.
    """
    if (
        st.pending_off_confirmation
        and st.pending_action == "off"
        and decision_action in ("off", "hold", "hold_vacant")
    ):
        return
    if decision_action not in ("on", "off"):
        _clear_pending_command_state(st)
        return
    if st.pending_action is not None and st.pending_action != decision_action:
        _clear_pending_command_state(st)


def _seconds_since_effective_on_or_command(st: RoomRuntime, now: datetime) -> float:
    now_ts = now.timestamp()

    if st.effective_on_since_ts is not None:
        return now_ts - float(st.effective_on_since_ts)
    if st.last_confirmed_on_at is not None:
        return (now - st.last_confirmed_on_at).total_seconds()
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
            "[CONTROL] Skip ON â€” already pending",
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
        and (
            st.pending_action == "on"
            or st.pending_on_ir_sent
        )
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
                "[CONTROL] Block OFF â€” pending ON protected (%s, %.1fs)",
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
            "[CONTROL] Block OFF â€” post-ON protection (%s, %.1fs < %.0fs)",
            source,
            time_since_on,
            RUNNING_OFF_BLOCK_SECS,
        )
        return "hold", "running_protection"

    return action, source


def _pending_on_emit_hold_in_progress(st: RoomRuntime, action: str) -> bool:
    return action == "hold" and st.pending_action == "on" and st.pending_on_ir_sent


def _presence_only_awaiting_off_confirmation(st: RoomRuntime) -> bool:
    return (
        (st.pending_action == "off" or st.pending_off_confirmation)
        and st.last_command == "off"
        and not st.presence_only_idle
    )


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
        "[HawaAI][%s] AC failed to turn ON â€” no physical confirmation within %.0fs after single IR emit",
        room_id,
        PENDING_ON_CONFIRM_TIMEOUT_SECS,
    )
    st.ac_state = "on_failed"
    st.last_command = "on_failed"
    st.on_failed_retry_used = False
    try:
        await live_broadcast.broadcast_room_update(room_id)
    except Exception:
        pass
    _clear_pending_command_state(st)
    return True


def _on_failed_retry_allowed(room_id: str, st: RoomRuntime, now: datetime) -> bool:
    if st.ac_state != "on_failed" or st.on_failed_retry_used:
        return False
    if st.last_command_time is None:
        return False
    elapsed = (now - st.last_command_time).total_seconds()
    if elapsed < 30.0:
        return False
    st.on_failed_retry_used = True
    log_with_room("info", room_id, "[CONTROL] on_retry_allowed")
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
    Pending ON clears only on runtime/IR confirmation, not inferred-only transient ON.
    Pending OFF clears when full physical observation says compressor is OFF.
    """
    if st.pending_action == "on":
        if confirmed_ac_on or manual_override_active:
            _clear_pending_command_state(st)
            return
    elif st.pending_action == "off":
        if st.pending_off_confirmation:
            return
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

    if (st.pending_action == "off" or st.pending_off_confirmation) and st.physical_ac_on:
        st.ac_state = "pending_off"
    elif st.ac_state == "on_failed":
        st.effective_ac_on = False
    elif st.physical_ac_on:
        st.ac_state = "on"
    else:
        st.ac_state = "off"


def _climate_reports_off(climate_data: dict) -> bool:
    mode = str(
        (climate_data or {}).get("mode")
        or (climate_data or {}).get("hvac_mode")
        or (climate_data or {}).get("state")
        or ""
    ).strip().lower()
    return mode in ("off", "idle")


def _off_confirm_watts_threshold(cfg: dict) -> float:
    return _cfg_float(cfg, "off_confirm_watts", OFF_CONFIRM_WATTS, lo=0.0, hi=500.0)


def _off_confirmation_status(
    cfg: dict,
    *,
    power_watts: Optional[float],
    climate_data: dict,
) -> Tuple[bool, str]:
    threshold = _off_confirm_watts_threshold(cfg)
    if power_watts is not None:
        if float(power_watts) < threshold:
            return True, "power_below_threshold"
        return False, "power_high"
    if _climate_reports_off(climate_data):
        return True, "climate_off"
    return False, "no_confirmation"


def _pending_off_still_vacant(st: RoomRuntime) -> bool:
    return _is_vacancy_off_reason(st.off_reason) and not st.occupied and not st.stable_occupied


def _cancel_pending_off_due_to_reentry(room_id: str, st: RoomRuntime) -> None:
    log_with_room("info", room_id, "[OFF_CONFIRM] canceled reason=reoccupancy")
    _clear_pending_command_state(st)
    st.off_dispatch_pending = False
    st.off_dispatched_at = None
    st.off_finalized = False
    st.off_settled_at = None
    st.off_reason = None
    st.last_command = ""
    st.last_command_time = None
    st.last_sent_command_key = None
    st.last_decision_at = None
    _clear_pending_off_confirmation(st)


async def _dispatch_off_ir(room_id: str, cfg: dict, climate_entity: str) -> bool:
    ir_backend = await resolve_ir_backend(room_id, cfg, climate_entity)
    if ir_backend == "tuya":
        return bool(await ac_tuya_adapter.turn_off(climate_entity))
    if ir_backend == "aerostate":
        return bool(await ac_aerostate_adapter.turn_off(climate_entity))
    logger.error("[HawaAI][%s] AC OFF FAILED: unsupported ir_backend=%s", room_id, ir_backend)
    return False


def _runtime_has_open_session(room_id: str, st: RoomRuntime) -> bool:
    return bool(
        session_logger.current_session_id(room_id)
        or st.session_start_time is not None
        or st.session_state != "idle"
    )


def _terminal_off_elapsed(st: RoomRuntime, now: datetime) -> bool:
    if st.last_command_time is None:
        return True
    return (now - st.last_command_time).total_seconds() >= float(OFF_TERMINAL_RECONCILE_SECONDS)


def _finalize_runtime_off_state(
    room_id: str,
    st: RoomRuntime,
    now: datetime,
    *,
    reason: str,
) -> None:
    first_finalized = not st.off_finalized
    had_stale_idle = bool(
        st.ac_is_on
        or st.physical_ac_on
        or st.effective_ac_on
        or st.effective_ac_idle
        or st.ac_state != "off"
        or st.effective_on_since_ts is not None
        or st.possible_on_since is not None
        or st.soft_start_ui
        or st.compressor_on_since is not None
        or st.last_confirmed_on_at is not None
    )

    st.ac_is_on = False
    st.physical_ac_on = False
    st.effective_ac_on = False
    st.effective_ac_idle = False
    st.ac_state = "off"
    st.effective_power_source = "internal"
    st.ac_state_source = "system"
    st.effective_on_since_ts = None
    st.possible_on_since = None
    st.soft_start_ui = False
    st.compressor_on_since = None
    st.compressor_off_since = st.compressor_off_since or now
    st.last_confirmed_on_at = None
    st.session_runtime_confirmed = False
    st.stale_idle = bool(had_stale_idle)
    st.off_dispatch_pending = False
    st.off_dispatched_at = None
    st.off_finalized = True
    st.off_settled_at = now
    st.last_confirmed_off_at = now
    _clear_pending_off_confirmation(st)

    if had_stale_idle or first_finalized:
        log_with_room("info", room_id, "[RUNTIME] reconciliation_complete reason=%s", reason)
        if had_stale_idle:
            log_with_room("info", room_id, "[RUNTIME] idle_expired")
            log_with_room("info", room_id, "[RUNTIME] stale_idle_cleared")
        log_with_room("info", room_id, "[RUNTIME] finalized_off")
        log_with_room("info", room_id, "[RUNTIME] off_settled")


def _maybe_finalize_terminal_off(
    room_id: str,
    st: RoomRuntime,
    now: datetime,
    *,
    climate_data: dict,
    in_cooldown: bool,
) -> bool:
    if in_cooldown:
        return False
    if st.pending_off_confirmation:
        return False
    if st.pending_action == "on" or st.pending_on_ir_sent:
        return False
    if _runtime_has_open_session(room_id, st):
        return False
    if st.last_command not in ("off", "manual_off"):
        return False
    if not st.off_finalized and not _terminal_off_elapsed(st, now):
        return False
    if not _climate_reports_off(climate_data):
        return False
    if st.pending_action == "off":
        _clear_pending_command_state(st)
    _finalize_runtime_off_state(
        room_id,
        st,
        now,
        reason="terminal_off",
    )
    return True


async def _finalize_confirmed_pending_off(
    room_id: str,
    cfg: dict,
    indoor_temp: float,
    st: RoomRuntime,
    now: datetime,
    *,
    confirmation_source: str,
) -> None:
    reason = st.off_reason or "vacant"
    if session_logger.current_session_id(room_id) is not None:
        await _close_session(room_id, cfg, indoor_temp, reason)
    _clear_pending_command_state(st)
    _finalize_runtime_off_state(
        room_id,
        st,
        now,
        reason=f"off_confirmed_{confirmation_source}",
    )


async def _handle_pending_off_confirmation(
    room_id: str,
    cfg: dict,
    indoor_temp: float,
    st: RoomRuntime,
    now: datetime,
    *,
    telemetry_power_reading: Optional[float],
    climate_data: dict,
    room_is_occupied: Optional[bool] = None,
) -> bool:
    if not st.pending_off_confirmation:
        return False

    still_vacant = (
        not bool(room_is_occupied)
        if room_is_occupied is not None
        else _pending_off_still_vacant(st)
    )

    if _is_vacancy_off_reason(st.off_reason) and not still_vacant:
        _cancel_pending_off_due_to_reentry(room_id, st)
        return False

    confirmed, source = _off_confirmation_status(
        cfg,
        power_watts=telemetry_power_reading,
        climate_data=climate_data or {},
    )
    if confirmed:
        log_with_room("info", room_id, "[OFF_CONFIRM] confirmed source=%s", source)
        await _finalize_confirmed_pending_off(
            room_id,
            cfg,
            indoor_temp,
            st,
            now,
            confirmation_source=source,
        )
        return True

    if source != "power_high" or not still_vacant:
        return False

    sent_at = st.pending_off_sent_at or st.off_dispatched_at
    if sent_at is None:
        st.pending_off_sent_at = now
        return False
    elapsed = (now - sent_at).total_seconds()
    if elapsed < float(OFF_CONFIRM_RETRY_SECONDS):
        return False

    if st.pending_off_retry_count >= int(MAX_OFF_CONFIRM_RETRIES):
        log_with_room(
            "error",
            room_id,
            "[OFF_CONFIRM] failed retries=%s power=%.1fW threshold=%.1fW",
            st.pending_off_retry_count,
            float(telemetry_power_reading),
            _off_confirm_watts_threshold(cfg),
        )
        _clear_pending_command_state(st)
        st.off_dispatch_pending = False
        st.off_dispatched_at = None
        st.off_finalized = False
        st.off_settled_at = None
        _clear_pending_off_confirmation(st, failed=True)
        st.ac_state = "on" if st.physical_ac_on or st.ac_is_on else "off"
        return False

    climate_entity = (cfg.get("climate_entity") or "").strip()
    st.pending_off_retry_count += 1
    log_with_room(
        "warning",
        room_id,
        "[OFF_CONFIRM] retry_off attempt=%s power=%.1fW threshold=%.1fW",
        st.pending_off_retry_count,
        float(telemetry_power_reading),
        _off_confirm_watts_threshold(cfg),
    )
    if await _dispatch_off_ir(room_id, cfg, climate_entity):
        st.ir_last_sent_ts = now
        st.last_command_time = now
        st.last_decision_at = now
        st.off_dispatched_at = now
        st.pending_off_sent_at = now
    return False


def _off_dispatch_elapsed(st: RoomRuntime, now: datetime) -> float:
    if st.off_dispatched_at is None:
        return float("inf")
    return max(0.0, (now - st.off_dispatched_at).total_seconds())


def _should_suppress_duplicate_off(
    room_id: str,
    st: RoomRuntime,
    now: datetime,
    *,
    climate_data: dict,
) -> bool:
    if st.last_command != "off":
        return False

    if st.pending_off_confirmation:
        log_with_room("info", room_id, "[RUNTIME] duplicate_off_suppressed reason=off_confirmation_pending")
        return True
    if st.off_confirmation_failed and _is_vacancy_off_reason(st.off_reason):
        log_with_room("info", room_id, "[RUNTIME] duplicate_off_suppressed reason=off_confirmation_failed")
        return True

    ha_off = _climate_reports_off(climate_data)

    suppress = False
    reason = ""
    if st.off_finalized and ha_off:
        suppress = True
        reason = "settled_off"
    elif (
        st.off_dispatch_pending
        and ha_off
        and _off_dispatch_elapsed(st, now) >= float(OFF_TERMINAL_RECONCILE_SECONDS)
    ):
        _finalize_runtime_off_state(
            room_id,
            st,
            now,
            reason="duplicate_off_reconciled",
        )
        suppress = True
        reason = "settled_off"
    elif st.off_dispatch_pending and _off_dispatch_elapsed(st, now) < float(OFF_TERMINAL_RECONCILE_SECONDS):
        suppress = True
        reason = "reconciliation_active"
    elif _is_in_cooldown(st, now) and ha_off:
        suppress = True
        reason = "cooldown_off"

    if not suppress:
        return False

    log_with_room("info", room_id, "[RUNTIME] duplicate_off_suppressed reason=%s", reason)
    return True


def _decision_lock_blocks_delayed_emit(st: RoomRuntime, now: datetime) -> bool:
    """True if a real ON/OFF command was issued recently â€” delayed path must not bypass this."""
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
    Opening a cooling session requires real AC intent/on state â€” NOT inferred-only effective_ac_on.

    ``effective_target`` / comfort mode does not gate eligibility; ``ac_is_on``
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


def _epoch_after_start(epoch_ts: Optional[float], start_ref: Optional[datetime]) -> bool:
    if epoch_ts is None or start_ref is None:
        return False
    try:
        return datetime.fromtimestamp(float(epoch_ts), timezone.utc) >= start_ref
    except (TypeError, ValueError, OSError):
        return False


def _mark_session_runtime_confirmed(
    room_id: str,
    st: RoomRuntime,
    now: datetime,
    *,
    source: str,
) -> None:
    first_runtime = not st.session_runtime_confirmed
    st.session_runtime_confirmed = True
    st.session_state = "confirmed"
    if first_runtime:
        log_with_room("info", room_id, "[SESSION] runtime_confirmed source=%s", source)


def _session_has_confirmation_evidence(
    st: RoomRuntime,
    start_ref: Optional[datetime],
    duration_secs: float,
    *,
    avg_watts: Optional[float] = None,
    peak_watts: Optional[float] = None,
    kwh_consumed: Optional[float] = None,
) -> bool:
    if st.session_runtime_confirmed:
        return True
    if st.session_state == "confirmed":
        return True
    if peak_watts is not None and peak_watts > _WATTS_COMPRESSOR:
        return True
    if avg_watts is not None and avg_watts >= 100.0 and duration_secs >= MIN_SESSION_SECONDS:
        return True
    if kwh_consumed is not None and kwh_consumed >= MIN_SESSION_ENERGY_KWH:
        return True
    if duration_secs >= MEANINGFUL_SESSION_SECONDS and (
        st.ac_is_on
        or st.physical_ac_on
        or _epoch_after_start(
            st.last_confirmed_on_at.timestamp() if st.last_confirmed_on_at else None,
            start_ref,
        )
    ):
        return True
    return False


def _session_finalization_grace_seconds(cfg: dict, reason: str) -> float:
    if reason in ("room_disabled", "room_deleted", "self_heal_orphan_session"):
        return 0.0
    if not resolve_energy_config(cfg).configured:
        return 0.0
    try:
        raw = cfg.get("session_finalization_grace_seconds", SESSION_FINALIZATION_GRACE_SECONDS)
        return max(0.0, min(float(raw), 30.0))
    except (TypeError, ValueError):
        return SESSION_FINALIZATION_GRACE_SECONDS


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
    dmin = _cfg_float(cfg, "setpoint_min_delta_deg", 0.7, lo=0.0)
    tmin = _cfg_float(cfg, "setpoint_command_min_interval_seconds", 180.0, lo=0.0)
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
        return False, f"blocked_setpoint_delta |Î”|<{dmin}Â°C (last={last:.1f} new={nt:.1f})"
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
    spurious locks and expiry â†’ immediate re-lock while HA still stale).
    """
    st = _rt(room_id)
    dur_min = _cfg_float(cfg, "manual_override_duration_minutes", 30.0, lo=0.0)
    detect = _cfg_float(cfg, "manual_override_detect_delta_deg", 0.5, lo=0.0)
    exit_near = _cfg_float(cfg, "manual_override_exit_within_deg", 0.5, lo=0.0)

    raw_ct = climate_data.get("target_temp") if climate_data else None
    ct: Optional[float] = None
    if raw_ct is not None:
        try:
            ct = float(raw_ct)
        except (TypeError, ValueError):
            ct = None

    temperature_mode = str(cfg.get("temperature_mode") or "manual").strip().lower()
    if temperature_mode not in ("manual", "schedule", "schedule_ai"):
        temperature_mode = "manual"
    if temperature_mode != "manual":
        if st.manual_override_until is not None or st.manual_override_temp is not None:
            logger.info(
                "[HawaAI][%s] Clearing manual setpoint lock because temperature_mode=%s",
                room_id,
                temperature_mode,
            )
        clear_manual_override(room_id, reason="temperature_mode_changed")
        if ct is not None:
            st.prev_ha_setpoint_seen = ct
        return False, engine_planned_target

    if st.manual_override_until is not None and now >= st.manual_override_until:
        logger.info(
            "[HawaAI][%s] Timed manual override expired",
            room_id,
        )
        clear_manual_override(room_id, reason="manual_override_expired")

    if (
        st.manual_override_until is not None
        and now < st.manual_override_until
        and st.manual_override_temp is not None
        and indoor_temp is not None
    ):
        if abs(float(indoor_temp) - float(st.manual_override_temp)) <= exit_near:
            logger.info(
                "[HawaAI][%s] Skip: manual override active â€” exited (near target)",
                room_id,
            )
            clear_manual_override(room_id, reason="manual_override_target_reached")

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
            "[HawaAI][%s] manual override lock â€” user %.1fÂ°C vs engine %.1fÂ°C for %dm",
            room_id,
            ct,
            engine_planned_target,
            int(dur_min),
        )
        log_with_room("info", room_id, "[OVERRIDE] enabled")
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

    if _ir_send_lock_active(st, now):
        secs = _seconds_since_last_ir(st, now)
        logger.info(
            "[HawaAI][%s] Skip ON: IR send lock active (elapsed=%.1fs < %.0fs)",
            room_id,
            secs,
            IR_SEND_LOCK_SECONDS,
        )
        return False

    if _post_on_stabilization_active(st, now):
        until = st.just_turned_on_until.isoformat() if st.just_turned_on_until else None
        logger.info(
            "[HawaAI][%s] Skip ON: post-ON stabilization active until %s",
            room_id,
            until,
        )
        return False

    # Dedup only when compressor is observed ON; same fingerprint + OFF â†’ allow resend path.
    if duplicate_intent and st.physical_ac_on:
        logger.info(
            "[HawaAI][%s] Skip ON: duplicate fingerprint (%s) â€” physical ON observed",
            room_id,
            fp,
        )
        return False

    # IR cooldown: bypass when resending same ON while still physically OFF (missed IR / HA drop).
    if (
        _is_in_cooldown(st, now)
        and st.pending_action != "on"
        and not _is_user_authority_active(st, cfg, now)
    ):
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
            "[HawaAI][%s] Resend ON: duplicate fingerprint (%s) but physical OFF â€” bypass IR cooldown",
            room_id,
            fp,
        )

    min_iv = _cfg_float(cfg, "min_command_interval_seconds", 150.0, lo=0.0)

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
            "[HawaAI][%s] Resend ON: duplicate fingerprint (%s) but physical OFF â€” "
            "bypass min interval (elapsed=%.0fs < %.0fs)",
            room_id,
            fp,
            secs,
            min_iv,
        )

    min_off = _cfg_float(cfg, "compressor_min_off_seconds", 180.0, lo=0.0)
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

    Never skip because "duplicate off" fingerprint â€” HA/device can miss commands;
    rely on internal state (intent) only for "already off".
    Caller must ensure st.ac_is_on before calling guard + _turn_ac_off.
    Vacancy/security path uses ``force=True`` to bypass throttle + compressor protections.
    """
    st = _rt(room_id)

    if _ir_send_lock_active(st, now):
        secs = _seconds_since_last_ir(st, now)
        logger.info(
            "[HawaAI][%s] Skip OFF: IR send lock active (elapsed=%.1fs < %.0fs)",
            room_id,
            secs,
            IR_SEND_LOCK_SECONDS,
        )
        return False

    if _post_on_stabilization_active(st, now):
        until = st.just_turned_on_until.isoformat() if st.just_turned_on_until else None
        logger.info(
            "[HawaAI][%s] Skip OFF: post-ON stabilization active until %s",
            room_id,
            until,
        )
        return False

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

    min_iv = _cfg_float(cfg, "min_command_interval_seconds", 150.0, lo=0.0)

    secs = _seconds_since_last_command(st, now)
    if secs < min_iv:
        logger.info(
            "[HawaAI][%s] Skip OFF: cooldown (%.0fs < %.0fs)",
            room_id, secs, min_iv,
        )
        return False

    min_on = _cfg_float(cfg, "compressor_min_on_seconds", 300.0, lo=0.0)
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
    base_t = _cfg_float(cfg, "target_temp", 24.0)
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
    Effective control target after optional Â±1 Â°C bounded AI read from cache.
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
            "[AI][%s] Bounded effective %.2fÂ°C â†’ %.2fÂ°C (model %.2fÂ°C)",
            room_id, effective_after_weather, bounded, ai_t,
        )
    return bounded, changed


def _sleep_manual_target_cap(cfg: dict) -> Optional[float]:
    """User-facing manual target is a cap on additional sleep relaxation only."""
    try:
        return float(cfg.get("target_temp"))
    except (TypeError, ValueError):
        return None


def _apply_sleep_optimizer_layer(
    room_id: str,
    cfg: dict,
    *,
    now: datetime,
    indoor_temp: Optional[float],
    target_before_sleep: float,
    log_change: bool,
) -> sleep_optimizer.SleepAdjustment:
    """
    Passive target modifier: schedule/weather/AI/effective-mode target in, adjusted target out.
    No AC control, sessions, occupancy, cooldown, or command state is touched here.
    """
    result = sleep_optimizer.calculate_sleep_adjustment(
        cfg,
        current_time=now,
        target_temp=float(target_before_sleep),
        indoor_temp=indoor_temp,
        user_manual_target=_sleep_manual_target_cap(cfg),
    )

    st = _rt(room_id)
    st.sleep_offset = float(result.offset)
    st.sleep_phase = result.phase
    st.sleep_optimization_active = bool(result.active)
    st.sleep_suspended_reason = result.suspended

    if log_change:
        sig = (
            bool(result.active),
            round(float(result.offset), 2),
            result.phase,
            result.suspended or "none",
        )
        if sig != st.last_sleep_log_sig:
            log_with_room(
                "info",
                room_id,
                "[SLEEP] active=%s offset=%+.1f phase=%s suspended=%s",
                bool(result.active),
                float(result.offset),
                result.phase,
                result.suspended or "none",
            )
            st.last_sleep_log_sig = sig

    return result


def _humidity_sensor_entity(cfg: dict) -> str:
    """Preferred new key with legacy indoor_humidity_entity fallback."""
    return (
        str(cfg.get("humidity_entity_id") or "").strip()
        or str(cfg.get("indoor_humidity_entity") or "").strip()
    )


async def _read_indoor_humidity(cfg: dict) -> Optional[float]:
    entity_id = _humidity_sensor_entity(cfg)
    if not entity_id:
        return None
    raw = await ha_client.get_state(entity_id)
    return humidity_comfort.valid_humidity_percent(raw)


async def _apply_runtime_self_heal(
    room_id: str,
    cfg: dict,
    st: RoomRuntime,
    *,
    now: datetime,
    indoor_temp: Optional[float],
    indoor_temp_raw: object,
    indoor_humidity: Optional[float],
    climate_data: dict,
    in_cooldown: bool,
) -> runtime_self_heal.HealthReport:
    """
    Passive runtime resilience hook.

    The self-heal engine only emits recommendations. This adapter applies the
    safe, idempotent ones by reusing existing runtime/session helpers. It never
    sends IR and never changes thermostat decisions.
    """
    climate_entity = (cfg.get("climate_entity") or cfg.get("ac_entity") or "").strip()
    humidity_entity = _humidity_sensor_entity(cfg)
    session_id = session_logger.current_session_id(room_id)
    report = runtime_self_heal.evaluate(
        runtime_self_heal.runtime_snapshot_from_object(
            room_id,
            st,
            session_id=session_id,
        ),
        runtime_self_heal.ObservationSnapshot(
            climate_entity=climate_entity,
            climate_state=(climate_data or {}).get("mode") or (climate_data or {}).get("state"),
            climate_available=bool(climate_data),
            climate_last_updated=(climate_data or {}).get("last_updated"),
            # Breaker telemetry is observational only; never feed it into
            # runtime self-heal recommendations that can rebuild HVAC state.
            power_entity="",
            power_watts=None,
            power_available=True,
            sensors=(
                runtime_self_heal.SensorSnapshot(
                    str(cfg.get("indoor_temp_entity") or "").strip(),
                    indoor_temp_raw if indoor_temp_raw is not None else indoor_temp,
                    available=indoor_temp is not None,
                    kind="temperature",
                ),
                runtime_self_heal.SensorSnapshot(
                    humidity_entity,
                    indoor_humidity,
                    available=indoor_humidity is not None,
                    kind="humidity",
                ),
            ),
        ),
        now=now,
    )
    runtime_self_heal.log_report_changes(report)

    for rec in report.recommendations:
        action = rec.action
        if (
            action == runtime_self_heal.RecoveryAction.CLEAR_STALE_PENDING_ON
            and st.pending_action == "on"
        ):
            _clear_pending_command_state(st)
            if st.ac_state == "pending_on":
                st.ac_state = "on" if st.physical_ac_on else "off"
        elif (
            action == runtime_self_heal.RecoveryAction.CLEAR_STALE_PENDING_OFF
            and st.pending_action == "off"
            and not st.pending_off_confirmation
        ):
            _clear_pending_command_state(st)
            if st.ac_state == "pending_off":
                st.ac_state = "on" if st.physical_ac_on else "off"
        elif action == runtime_self_heal.RecoveryAction.RELEASE_FAILED_ON_RETRY:
            if st.ac_state == "on_failed":
                st.on_failed_retry_used = False
        elif action == runtime_self_heal.RecoveryAction.REBUILD_RUNTIME:
            if in_cooldown:
                continue
            observed_on = bool(rec.metadata.get("observed_on"))
            if observed_on != bool(st.ac_is_on or st.physical_ac_on):
                st.ac_is_on = observed_on
                st.physical_ac_on = observed_on
                st.effective_ac_on = observed_on
                st.ac_state = "on" if observed_on else "off"
                if observed_on:
                    st.last_ac_on_at = now.timestamp()
                    st.last_confirmed_on_at = now
                    st.effective_on_since_ts = st.effective_on_since_ts or now.timestamp()
                else:
                    st.last_ac_off_at = now.timestamp()
                    st.effective_on_since_ts = None
                    st.possible_on_since = None
        elif action == runtime_self_heal.RecoveryAction.CLOSE_ORPHAN_SESSION:
            if session_logger.current_session_id(room_id) is not None and indoor_temp is not None:
                await _close_session(room_id, cfg, float(indoor_temp), "self_heal_orphan_session")

    return report


def _clear_humidity_runtime(st: RoomRuntime) -> None:
    st.humidity_percent = None
    st.feels_like_temp = None
    st.dew_point = None
    st.humidity_offset = 0.0
    st.comfort_score = 0.0
    st.comfort_level = "unknown"
    st.humidity_band = "unavailable"
    st.dry_mode_recommended = False


def _apply_humidity_comfort_layer(
    room_id: str,
    cfg: dict,
    *,
    indoor_temp: float,
    humidity_percent: Optional[float],
    target_before_humidity: float,
    ac_on: bool,
    log_change: bool,
) -> humidity_comfort.HumidityComfort:
    """
    Passive humidity modifier: target in, adjusted target out.
    The calculation is pure; this wrapper only copies diagnostics into runtime state.
    """
    result = humidity_comfort.calculate_humidity_comfort(
        cfg,
        indoor_temp=float(indoor_temp),
        target_temp=float(target_before_humidity),
        humidity_percent=humidity_percent,
        ac_on=bool(ac_on),
    )

    st = _rt(room_id)
    st.humidity_percent = result.humidity_percent
    st.feels_like_temp = result.feels_like_temp
    st.dew_point = result.dew_point
    st.humidity_offset = float(result.humidity_offset)
    st.comfort_score = float(result.comfort_score)
    st.comfort_level = result.comfort_level
    st.humidity_band = result.humidity_band
    st.dry_mode_recommended = bool(result.dry_mode_recommended)

    if log_change:
        sig = (
            result.humidity_percent,
            result.feels_like_temp,
            round(float(result.humidity_offset), 2),
            result.comfort_level,
            bool(result.dry_mode_recommended),
            result.reason,
        )
        if sig != st.last_humidity_log_sig:
            humidity_label = (
                f"{result.humidity_percent:.0f}%"
                if result.humidity_percent is not None else "n/a"
            )
            feels_label = (
                f"{result.feels_like_temp:.1f}"
                if result.feels_like_temp is not None else "n/a"
            )
            log_with_room(
                "info",
                room_id,
                "[HUMIDITY] humidity=%s feels_like=%s offset=%+.1f comfort=%s "
                "dry_mode_recommended=%s",
                humidity_label,
                feels_label,
                float(result.humidity_offset),
                result.comfort_level,
                bool(result.dry_mode_recommended),
            )
            st.last_humidity_log_sig = sig

    return result


def _fan_mode_is_high(fan_mode: object) -> bool:
    mode = str(fan_mode or "").strip().lower()
    return mode in {
        "high",
        "max",
        "turbo",
        "powerful",
        "boost",
        "f4",
        "f5",
        "5",
    }


def _thermal_load_reset(st: RoomRuntime) -> None:
    st.thermal_load_level = "low"
    st.thermal_load_confidence = "low"
    st.thermal_load_score = 0.0
    st.thermal_load_candidate_since = None
    st.thermal_load_compensation_offset = 0.0
    st.thermal_load_compensation_active = False
    st.cooling_saturated = False
    st.thermal_load_summary = "Monitoring room load"


def _apply_thermal_load_comfort_layer(
    room_id: str,
    cfg: dict,
    *,
    now: datetime,
    indoor_temp: float,
    outdoor_temp: Optional[float],
    humidity_percent: Optional[float],
    target_before_thermal: float,
    ac_on: bool,
    occupied: bool,
    climate_data: dict,
    log_change: bool,
) -> float:
    """
    Passive thermal-load modifier.

    It only lowers the effective comfort target slightly when room-specific
    thermal stress persists. It never turns HVAC on/off and never changes fan
    mode. Cooling saturation disables further compensation so the automation
    stays calm when the AC is already near its practical limit.
    """
    st = _rt(room_id)
    if not bool(cfg.get("adaptive_thermal_load_enabled", True)):
        _thermal_load_reset(st)
        st.thermal_load_summary = "Adaptive room load disabled"
        return float(target_before_thermal)

    target = float(target_before_thermal)
    if not occupied:
        _thermal_load_reset(st)
        return target

    previous_temp = st.thermal_load_temp_ema
    previous_sample = st.thermal_load_last_sample_at
    if previous_temp is None:
        st.thermal_load_temp_ema = float(indoor_temp)
    else:
        st.thermal_load_temp_ema = (previous_temp * 0.70) + (float(indoor_temp) * 0.30)

    if previous_sample is not None and previous_temp is not None:
        elapsed_min = max(0.0, (now - previous_sample).total_seconds() / 60.0)
        if 0.25 <= elapsed_min <= 15.0:
            instant_rate = (float(indoor_temp) - previous_temp) / elapsed_min
            st.thermal_load_rise_rate_ema = (
                st.thermal_load_rise_rate_ema * 0.75
                + float(instant_rate) * 0.25
            )
    st.thermal_load_last_sample_at = now

    gap = float(indoor_temp) - target
    rise_rate = float(st.thermal_load_rise_rate_ema)
    score = 0.0

    if gap >= 1.0:
        score += 1.0
    if gap >= 2.0:
        score += 1.0
    if gap >= 3.0:
        score += 1.0
    if rise_rate >= 0.03:
        score += 1.0
    if rise_rate >= 0.08:
        score += 1.0
    if ac_on and gap >= 1.5 and rise_rate > -0.02:
        score += 1.0
    if outdoor_temp is not None and float(outdoor_temp) >= 38.0:
        score += 1.0
    if outdoor_temp is not None and float(outdoor_temp) >= 42.0:
        score += 1.0
    if humidity_percent is not None and float(humidity_percent) >= 65.0:
        score += 0.5
    if humidity_percent is not None and float(humidity_percent) >= 75.0:
        score += 0.5
    if st.zone_confirmed:
        score += 0.5

    st.thermal_load_score = round(score, 2)
    if score >= 5.0:
        level = "high"
    elif score >= 3.0:
        level = "medium"
    else:
        level = "low"

    if score >= 3.0:
        if st.thermal_load_candidate_since is None:
            st.thermal_load_candidate_since = now
        st.thermal_load_last_high_at = now
    elif (
        st.thermal_load_last_high_at is not None
        and (now - st.thermal_load_last_high_at).total_seconds() >= THERMAL_LOAD_RELEASE_SECONDS
    ):
        st.thermal_load_candidate_since = None

    persisted = (
        (now - st.thermal_load_candidate_since).total_seconds()
        if st.thermal_load_candidate_since is not None else 0.0
    )
    if score >= 5.0 and persisted >= THERMAL_LOAD_PERSIST_SECONDS:
        confidence = "high"
    elif score >= 3.0 and persisted >= (THERMAL_LOAD_PERSIST_SECONDS * 0.6):
        confidence = "medium"
    else:
        confidence = "low"

    current_target = climate_data.get("target_temp") if climate_data else None
    try:
        current_target_f = float(current_target)
    except (TypeError, ValueError):
        current_target_f = None
    fan_high = _fan_mode_is_high(climate_data.get("fan_mode") if climate_data else None)
    min_target = _cfg_float(
        cfg,
        "thermal_load_min_target",
        THERMAL_LOAD_MIN_TARGET_C,
        lo=float(AI_MIN_T),
        hi=24.0,
    )
    saturated = bool(
        target <= THERMAL_LOAD_SATURATION_TARGET_C
        or (current_target_f is not None and current_target_f <= THERMAL_LOAD_SATURATION_TARGET_C)
        or (ac_on and fan_high and target <= (THERMAL_LOAD_SATURATION_TARGET_C + 1.0))
    )

    offset = 0.0
    if not saturated:
        if level == "high" and confidence == "high":
            offset = -THERMAL_LOAD_MAX_COMPENSATION_DEG
        elif level in ("medium", "high") and confidence in ("medium", "high"):
            offset = -THERMAL_LOAD_MEDIUM_COMPENSATION_DEG

    requested_offset = offset
    adjusted = max(min_target, target + requested_offset)
    if adjusted >= target:
        offset = 0.0
        adjusted = target
        if requested_offset < -0.01:
            saturated = True

    st.thermal_load_level = level
    st.thermal_load_confidence = confidence
    st.thermal_load_compensation_offset = round(float(offset), 2)
    st.thermal_load_compensation_active = bool(offset < -0.01)
    st.cooling_saturated = bool(saturated)
    if saturated:
        st.thermal_load_summary = "Max comfort cooling active"
    elif st.thermal_load_compensation_active:
        st.thermal_load_summary = "Comfort compensation active"
    elif level in ("medium", "high"):
        st.thermal_load_summary = "Monitoring elevated room load"
    else:
        st.thermal_load_summary = "Room load stable"

    if log_change:
        sig = (
            st.thermal_load_level,
            st.thermal_load_confidence,
            round(st.thermal_load_compensation_offset, 1),
            bool(st.cooling_saturated),
        )
        if sig != st.last_thermal_load_log_sig:
            log_with_room(
                "info",
                room_id,
                "[THERMAL_LOAD] level=%s confidence=%s score=%.1f rise=%.3fC/min offset=%+.1f saturated=%s",
                st.thermal_load_level,
                st.thermal_load_confidence,
                st.thermal_load_score,
                rise_rate,
                st.thermal_load_compensation_offset,
                bool(st.cooling_saturated),
            )
            st.last_thermal_load_log_sig = sig

    return float(adjusted)


async def tick(room_id: str) -> None:
    """
    Single decision-loop iteration for one room.
    """
    rid_raw = (room_id or "").strip()
    if not rid_raw:
        logger.error("[ROOM] tick rejected â€” missing room_id")
        return

    canon = normalize_room_id(rid_raw)
    async with _room_tick_serial_lock(canon):
        async with _room_ops_lock(canon):
            if startup_stabilization_active():
                if canon not in _startup_stabilization_logged_rooms:
                    _startup_stabilization_logged_rooms.add(canon)
                    logger.info(
                        "[CONTROL][%s] startup_stabilization_active remaining=%.1fs active_decisions=suppressed",
                        canon,
                        startup_stabilization_remaining_seconds(),
                    )
                await startup_hydrate_room(rid_raw)
            else:
                await _tick_impl(rid_raw, canon)
    await live_broadcast.broadcast_room_update(canon)


async def _tick_presence_only_mode(
    *,
    rid_raw: str,
    room_id: str,
    cfg: dict,
    climate_data: dict,
    presence_raw: object,
    resolved_occupied: bool,
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
    st.sleep_offset = 0.0
    st.sleep_phase = "inactive"
    st.sleep_optimization_active = False
    st.sleep_suspended_reason = None
    _clear_humidity_runtime(st)

    telemetry_power_reading, _telemetry_kwh = await _read_runtime_energy(room_id, cfg, st, now=now)
    telemetry_power_valid = telemetry_power_reading is not None
    telemetry_power_watts: float = (
        float(telemetry_power_reading) if telemetry_power_reading is not None else 0.0
    )

    in_cooldown = _is_in_cooldown(st, now)
    awaiting_presence_off_confirmation = _presence_only_awaiting_off_confirmation(st)
    ac_on = bool(st.ac_is_on)
    st.ac_state_source = "system"
    st.hvac_control_confidence = "medium" if st.pending_action else "high"

    st.physical_ac_on = bool(ac_on)
    confirmed_ac_on = bool(ac_on)
    if await _handle_pending_off_confirmation(
        room_id,
        cfg,
        indoor_temp,
        st,
        now,
        telemetry_power_reading=telemetry_power_reading,
        climate_data=climate_data or {},
        room_is_occupied=bool(resolved_occupied),
    ):
        ac_on = False
        confirmed_ac_on = False
    if _maybe_finalize_terminal_off(
        room_id,
        st,
        now,
        climate_data=climate_data or {},
        in_cooldown=_is_in_cooldown(st, now),
    ):
        ac_on = False
        confirmed_ac_on = False
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
    await _apply_runtime_self_heal(
        room_id,
        cfg,
        st,
        now=now,
        indoor_temp=indoor_temp,
        indoor_temp_raw=indoor_temp,
        indoor_humidity=None,
        climate_data=climate_data or {},
        in_cooldown=in_cooldown,
    )
    presence_off_confirmed = awaiting_presence_off_confirmation and not st.physical_ac_on

    action, source, occupied = _resolve_presence_only_decision(
        room_id,
        cfg,
        st,
        presence_raw,
        st.physical_ac_on,
        now,
        resolved_occupied=resolved_occupied,
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

    duplicate_idle_off_block = (
        control_action == "idle"
        and (st.pending_action is not None or st.pending_on_ir_sent or st.ac_state == "pending_off")
    )
    st.effective_control_source = source
    if control_action == "idle":
        await _finalize_presence_only_idle(
            room_id,
            cfg,
            indoor_temp,
            now,
            st,
            reason="presence_vacant" if presence_off_confirmed else "presence_idle",
            duplicate_off_block_detected=duplicate_idle_off_block,
        )
        action = control_action = "idle"
        source = control_source = "presence_idle"
        st.effective_control_source = source

    pending_on_hold_sources = ("pending_on_lock", "pending_on_protection")
    preserve_pending_on_hold = _pending_on_emit_hold_in_progress(st, control_action)
    preserve_pending_off_hold = (
        control_action == "hold"
        and not occupied
        and st.physical_ac_on
        and _presence_only_awaiting_off_confirmation(st)
    )
    if (
        control_source not in pending_on_hold_sources
        and not preserve_pending_on_hold
        and not preserve_pending_off_hold
    ):
        _sync_pending_for_action(st, control_action)

    if (
        control_action != "on"
        and control_source not in pending_on_hold_sources
        and not preserve_pending_on_hold
        and not preserve_pending_off_hold
    ):
        st.soft_start_ui = False
    if (
        st.ac_state == "on_failed"
        and control_action != "on"
        and control_source not in pending_on_hold_sources
    ):
        st.ac_state = "on" if st.physical_ac_on else "off"

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
        f"{telemetry_power_watts:.0f}" if telemetry_power_valid else "n/a",
    )

    bypass_actuation_delay = _delay_control_bypass(st, cfg, now, control_source)
    if control_action == "on":
        if bypass_actuation_delay:
            await _turn_ac_on(room_id, cfg, indoor_temp, et_eff, now=now)
            _clear_pending_command_state(st)
        elif st.ac_state == "on_failed" and not _on_failed_retry_allowed(room_id, st, now):
            log_with_room(
                "info",
                room_id,
                "[DELAY_ON][%s] presence-only ON suppressed â€” ac_state=on_failed",
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
        force_off = control_source in ("presence_vacant", "presence_max_runtime")
        reason_off = "max_runtime" if control_source == "presence_max_runtime" else "vacant"
        if _should_suppress_duplicate_off(
            room_id,
            st,
            now,
            climate_data=climate_data or {},
        ):
            if st.off_finalized:
                _clear_pending_command_state(st)
            action = control_action = "hold"
            source = control_source = "off_settled"
        elif control_source == "presence_vacant":
            if st.pending_action != "off":
                st.pending_action = "off"
                st.pending_since = time.time()
            await _turn_ac_off(
                room_id,
                cfg,
                indoor_temp,
                reason_off,
                now=now,
                force=True,
                close_session_on_send=False,
            )
        elif bypass_actuation_delay:
            await _turn_ac_off(room_id, cfg, indoor_temp, reason_off, now=now, force=force_off)
            if not st.pending_off_confirmation:
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
        now,
        et_eff,
        in_cooldown=in_cooldown,
        confirmed_ac_on=confirmed_ac_on,
        inferred_only_physical=False,
    )
    _sync_ac_display_fields(st)
    _maybe_finalize_terminal_off(
        room_id,
        st,
        now,
        climate_data=climate_data or {},
        in_cooldown=_is_in_cooldown(st, now),
    )
    if (
        session_logger.current_session_id(room_id)
        and st.telemetry_power_live_valid
        and not st.telemetry_gap
        and telemetry_power_reading is not None
    ):
        st.watts_samples.append(telemetry_power_watts)


async def _tick_manual_override_observe_only(
    *,
    room_id: str,
    cfg: dict,
    climate_data: dict,
    indoor_temp: float,
    now: datetime,
    st: RoomRuntime,
) -> None:
    """
    Persistent Manual Override is user authority. Keep telemetry/session/runtime
    observational paths alive, but do not run automation modifiers or dispatch
    climate commands.
    """
    _restore_persisted_manual_override(room_id, cfg, st)
    target_raw = (
        climate_data.get("target_temp")
        if climate_data and climate_data.get("target_temp") is not None
        else _manual_override_user_settings(cfg).get("target_temp", cfg.get("target_temp", 24.0))
    )
    try:
        target = float(target_raw)
    except (TypeError, ValueError):
        target = _cfg_float(cfg, "target_temp", 24.0)

    st.effective_target_temp = target
    st.last_control_effective_target_temp = target
    st.effective_target_source = "manual_override"
    st.effective_control_source = "manual"
    st.sleep_offset = 0.0
    st.sleep_phase = "manual_override"
    st.sleep_optimization_active = False
    st.sleep_suspended_reason = "manual_override"
    st.thermal_load_compensation_offset = 0.0
    st.thermal_load_compensation_active = False
    st.thermal_load_summary = "Automation paused by user"

    telemetry_power_reading, _telemetry_kwh = await _read_runtime_energy(
        room_id, cfg, st, now=now
    )
    telemetry_power_watts = (
        float(telemetry_power_reading) if telemetry_power_reading is not None else 0.0
    )
    in_cooldown = _is_in_cooldown(st, now)
    _maybe_finalize_terminal_off(
        room_id,
        st,
        now,
        climate_data=climate_data or {},
        in_cooldown=in_cooldown,
    )
    st.physical_ac_on = bool(st.ac_is_on)
    st.effective_ac_on = bool(st.physical_ac_on)
    if st.physical_ac_on:
        if st.effective_on_since_ts is None:
            st.effective_on_since_ts = now.timestamp()
    else:
        st.effective_on_since_ts = None
    _clear_pending_when_physically_satisfied(
        st,
        manual_override_active=True,
        confirmed_ac_on=bool(st.ac_is_on),
        physical_ac_on=bool(st.physical_ac_on),
    )
    await _maintain_session_lifecycle(
        room_id,
        cfg,
        indoor_temp,
        now,
        target,
        in_cooldown=in_cooldown,
        confirmed_ac_on=bool(st.ac_is_on),
        inferred_only_physical=False,
    )
    _sync_ac_display_fields(st)
    if (
        session_logger.current_session_id(room_id)
        and st.telemetry_power_live_valid
        and not st.telemetry_gap
        and telemetry_power_reading is not None
    ):
        st.watts_samples.append(telemetry_power_watts)
    log_with_room("info", room_id, "[OVERRIDE] active persistent=true automation_paused=true")


async def _tick_impl(rid_raw: str, room_id: str) -> None:
    """
    Core tick body. Caller must hold ``_room_ops_lock(room_id)`` so this never races ``stop_room``.
    """
    now = datetime.now(timezone.utc)

    base_cfg = config_manager.load_config()
    room_def = resolve_room_definition(base_cfg, rid_raw)
    if not room_def:
        logger.debug("[HawaAI] tick skipped â€” unknown room_id=%s", rid_raw)
        return
    if room_def.get("disabled"):
        logger.debug("[HawaAI] tick skipped [%s] â€” room disabled (no logic, snapshots, or commands)", room_id)
        return

    st = _rt(room_id)
    logger.info("[ROOM] tick room_id=%s (canonical=%s)", rid_raw, room_id)
    if not (str(room_def.get("climate_entity") or "")).strip():
        logger.debug("[HawaAI] tick skipped [%s] â€” no climate_entity", room_id)
        return
    cfg = room_registry.merge_room_config(base_cfg, room_def)
    control_mode = normalize_control_mode(cfg)
    presence_only = control_mode == "presence_only"
    use_presence = normalize_use_presence(cfg)

    sync_effective_mode_transition(st, room_id, cfg)

    _ae = bool(cfg.get("ai_enabled", False))
    if st.last_ai_enabled is not None and _ae != st.last_ai_enabled:
        logger.info("[AI][%s] %s", room_id, "Enabled" if _ae else "Disabled")
    st.last_ai_enabled = _ae

    presence_entity = cfg.get("presence_entity", "")
    indoor_temp_entity = cfg.get("indoor_temp_entity", "")

    if (use_presence and not presence_entity) or (not presence_only and not indoor_temp_entity):
        logger.warning(
            "[HawaAI][%s] Logic skipped â€” missing entity config (presence=%s, temp=%s)",
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
                    "[HawaAI] Indoor sensor unavailable (%r) â€” using climate entity "
                    "current_temp fallback: %.1fÂ°C",
                    indoor_temp_raw, indoor_temp,
                )
            except (ValueError, TypeError):
                pass

    if indoor_temp is None and not presence_only:
        logger.warning(
            "[HawaAI] tick skipped for room=%s â€” indoor_temp is None (HA unavailable?)",
            room_id,
        )
        return
    if indoor_temp is None:
        try:
            indoor_temp = _cfg_float(cfg, "target_temp", 24.0)
        except (TypeError, ValueError):
            indoor_temp = 24.0

    presence_raw = await ha_client.get_state(presence_entity) if use_presence else None
    is_occupied_bool, occupancy_source = _resolve_authoritative_room_presence(
        presence_raw,
        use_presence=bool(use_presence),
    )

    if use_presence and occupancy_source == "presence_unavailable":
        logger.warning(
            "[HawaAI] Presence sensor unavailable (%r) - treating occupied=False "
            "until a valid presence reading returns",
            presence_raw,
        )
    if not use_presence:
        if not st.presence_control_disabled_logged:
            log_with_room("info", room_id, "[OCCUPANCY] presence_control_disabled")
            st.presence_control_disabled_logged = True
    else:
        st.presence_control_disabled_logged = False

    await _fp2_zone_sensor_tick(room_id, cfg, now)
    is_occupied_bool = _sync_runtime_occupancy(
        room_id,
        st,
        bool(is_occupied_bool),
        now,
        cfg=cfg,
        source=occupancy_source,
    )

    logger.info(
        "[HawaAI] Presence: %r â†’ occupied=%s",
        presence_raw, is_occupied_bool,
    )

    if manual_override_enabled(cfg):
        if not st.manual_override_config_active:
            log_with_room("info", room_id, "[OVERRIDE] enabled persistent=true")
        logger.info("[HawaAI] Manual override active - automation paused by user")
        await _tick_manual_override_observe_only(
            room_id=room_id,
            cfg=cfg,
            climate_data=climate_data or {},
            indoor_temp=float(indoor_temp),
            now=now,
            st=st,
        )
        return

    if st.manual_override_config_active:
        clear_manual_override(room_id, reason="manual_override_config_false")

    if presence_only:
        await _tick_presence_only_mode(
            rid_raw=rid_raw,
            room_id=room_id,
            cfg=cfg,
            climate_data=climate_data,
            presence_raw=presence_raw,
            resolved_occupied=bool(is_occupied_bool),
            indoor_temp=float(indoor_temp),
            now=now,
            st=st,
        )
        return

    base_temp, slot_label = resolve_base_target_temp(cfg)
    log_target_resolve(room_id, cfg, base_temp, slot_label)
    temperature_mode_str = (cfg.get("temperature_mode") or "manual")
    sync_target_context_transition(st, room_id, cfg, slot_label, base_temp)

    vacancy_timeout = max(
        _cfg_int(cfg, "vacancy_timeout_minutes", 5, lo=0) * 60,
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
                "[HawaAI] Smart adj: enabled â€” no outdoor temp yet â†’ effective=%.1fÂ°C (base)",
                effective_after_weather,
            )
        elif effective_after_weather != base_temp:
            logger.info(
                "[HawaAI] Smart adj: outdoor=%.1fÂ°C â†’ effective %.1fÂ°C (base=%.1fÂ°C)",
                outdoor_temp, effective_after_weather, base_temp,
            )
        else:
            logger.info(
                "[HawaAI] Smart adj: outdoor=%.1fÂ°C â†’ effective unchanged at %.1fÂ°C",
                outdoor_temp, effective_after_weather,
            )

    indoor_humidity = await _read_indoor_humidity(cfg)

    telemetry_power_reading, energy_kwh_reading = await _read_runtime_energy(
        room_id, cfg, st, now=now
    )
    telemetry_power_valid = telemetry_power_reading is not None
    telemetry_power_watts: float = (
        float(telemetry_power_reading) if telemetry_power_reading is not None else 0.0
    )

    in_cooldown = _is_in_cooldown(st, now)

    ac_on = bool(st.ac_is_on)
    ac_idle = False
    power_source = "cooldown" if in_cooldown else "internal"
    st.hvac_control_confidence = "medium" if st.pending_action else "high"

    st.effective_ac_idle = ac_idle
    st.effective_power_source = power_source
    if _maybe_finalize_terminal_off(
        room_id,
        st,
        now,
        climate_data=climate_data or {},
        in_cooldown=_is_in_cooldown(st, now),
    ):
        ac_on = False
        ac_idle = False
        power_source = st.effective_power_source
        st.effective_ac_idle = False

    secs_since_cmd = (
        (now - st.last_command_time).total_seconds()
        if st.last_command_time is not None
        else float("inf")
    )
    pres_label = "occupied" if is_occupied_bool else "vacant"
    ac_state_label = "OFF"
    if ac_idle:
        ac_state_label = (
            f"IDLE({telemetry_power_watts:.0f}W)" if telemetry_power_valid else "IDLE"
        )
    elif ac_on:
        ac_state_label = (
            f"ON({telemetry_power_watts:.0f}W)" if telemetry_power_valid else "ON"
        )
    logger.info(
        "[HawaAI][%s] TICK | indoor=%.1fÂ°C | outdoor=%s | presence=%s | ac=%s "
        "[src=%s] | temp_mode=%s ha_mode=%s slot=%s | base=%.1fÂ°C (weather_eff=%.1fÂ°C)",
        room_id,
        indoor_temp,
        f"{outdoor_temp:.1f}Â°C" if outdoor_temp is not None else "â€”",
        pres_label,
        ac_state_label,
        power_source,
        temperature_mode_str,
        (climate_data.get("mode") if climate_data else None) or "â€”",
        slot_label,
        base_temp,
        eff_aw,
    )

    if in_cooldown:
        logger.info(
            "[HawaAI][%s] Cooldown active â€” %.0fs / %ds since '%s' command â€” "
            "skipping IR control later this tick",
            room_id,
            secs_since_cmd, _COOLDOWN_SECS, st.last_command,
        )

    rst = _rt(room_id)
    if rst.last_schedule_slot != slot_label:
        if rst.last_schedule_slot is not None:
            logger.info(
                "[HawaAI][%s] Schedule slot boundary: %s â†’ %s",
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
    target_before_sleep = apply_effective_mode_engine_target(
        room_id=room_id,
        base_temp=float(base_temp),
        planned_with_ai=float(planned_with_ai),
        cfg=cfg,
        control_log=True,
    )
    sleep_result = _apply_sleep_optimizer_layer(
        room_id,
        cfg,
        now=now,
        indoor_temp=indoor_temp,
        target_before_sleep=target_before_sleep,
        log_change=True,
    )
    humidity_result = _apply_humidity_comfort_layer(
        room_id,
        cfg,
        indoor_temp=indoor_temp,
        humidity_percent=indoor_humidity,
        target_before_humidity=float(sleep_result.adjusted_target),
        ac_on=bool(ac_on),
        log_change=True,
    )
    engine_target = _apply_thermal_load_comfort_layer(
        room_id,
        cfg,
        now=now,
        indoor_temp=indoor_temp,
        outdoor_temp=outdoor_temp,
        humidity_percent=indoor_humidity,
        target_before_thermal=float(humidity_result.adjusted_target),
        ac_on=bool(ac_on),
        occupied=bool(is_occupied_bool),
        climate_data=climate_data or {},
        log_change=True,
    )

    manual_override_active, manual_target = _manual_override_resolve(
        room_id, cfg, climate_data or {}, indoor_temp, now, engine_target,
    )
    if manual_override_active:
        try:
            et_u = float(manual_target)
            logger.info(
                "[HawaAI][%s] User setpoint lock candidate %.1fÂ°C "
                "(control_effective=%.1fÂ°C)",
                room_id,
                et_u,
                float(engine_target),
            )
        except (TypeError, ValueError):
            pass

    manual_mode_active = str(temperature_mode_str or "manual").strip().lower() == "manual"
    if manual_override_active and manual_mode_active:
        et_eff = float(manual_target)
        target_source = "manual_setpoint"
    else:
        et_eff = float(engine_target)
        target_source = "control_effective"
    st.last_control_effective_target_temp = float(engine_target)
    st.effective_target_temp = et_eff
    st.effective_target_source = target_source
    log_with_room(
        "info",
        room_id,
        "[TARGET_SYNC] control_effective=%.2f",
        float(engine_target),
    )
    log_with_room(
        "info",
        room_id,
        "[TARGET_SYNC] runtime_target=%.2f",
        et_eff,
    )
    log_with_room(
        "info",
        room_id,
        "[TARGET_SYNC] source=%s",
        st.effective_target_source,
    )

    now_ts = now.timestamp()

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

    st.physical_ac_on = bool(st.ac_is_on or is_probably_on)
    confirmed_ac_on = bool(st.ac_is_on)

    if st.physical_ac_on:
        inferred_only = is_probably_on and not st.ac_is_on
        if inferred_only:
            st.ac_state_source = "inferred"
        else:
            st.ac_state_source = "system"
    else:
        st.ac_state_source = "system"

    if st.physical_ac_on:
        if st.effective_on_since_ts is None:
            st.effective_on_since_ts = now_ts
    else:
        st.effective_on_since_ts = None

    if await _handle_pending_off_confirmation(
        room_id,
        cfg,
        indoor_temp,
        st,
        now,
        telemetry_power_reading=telemetry_power_reading,
        climate_data=climate_data or {},
        room_is_occupied=bool(is_occupied_bool),
    ):
        ac_on = False
        confirmed_ac_on = False

    if st.ac_state == "on_failed":
        st.soft_start_ui = False
    if st.soft_start_ui:
        if (
            st.physical_ac_on
            or confirmed_ac_on
        ):
            st.soft_start_ui = False

    _clear_pending_when_physically_satisfied(
        st,
        manual_override_active=manual_override_active,
        confirmed_ac_on=confirmed_ac_on,
        physical_ac_on=st.physical_ac_on,
    )
    await _apply_runtime_self_heal(
        room_id,
        cfg,
        st,
        now=now,
        indoor_temp=indoor_temp,
        indoor_temp_raw=indoor_temp_raw,
        indoor_humidity=indoor_humidity,
        climate_data=climate_data or {},
        in_cooldown=in_cooldown,
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

    # Drop stale API "user" marker so thermostat lock still works after authority expires.
    if st.last_command_source == "user" and not _is_user_authority_active(st, cfg, now):
        st.last_command_source = "system"

    occ_res = bool(is_occupied_bool)

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
                    "[DECISION_LOCK][%s] HOLD â€” %.1fs since last decision (< %.0fs); "
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
        "[TICK] room=%s action=%s source=%s indoor=%.2fÂ°C target=%.2fÂ°C delta=%+.2fÂ°C "
        "power=%sW ir_cooldown_active=%s occupied=%s temp_mode=%s ha_mode=%s",
        room_id,
        action,
        source,
        indoor_temp,
        et_eff,
        delta_audit,
        f"{telemetry_power_watts:.0f}" if telemetry_power_valid else "n/a",
        in_cd_audit,
        occ_res,
        temperature_mode_str,
        ha_mode_tick or "â€”",
    )

    if control_action == "on":
        if bypass_actuation_delay:
            await _turn_ac_on(room_id, cfg, indoor_temp, et_eff, now=now)
            _clear_pending_command_state(st)
            st.last_command_source = "system"
        elif (
            st.ac_state == "on_failed"
            and not str(control_source).startswith("safety")
            and not _on_failed_retry_allowed(room_id, st, now)
        ):
            log_with_room(
                "info",
                room_id,
                "[DELAY_ON][%s] automated ON suppressed â€” ac_state=on_failed (await demand change / user)",
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
        if _should_suppress_duplicate_off(
            room_id,
            st,
            now,
            climate_data=climate_data or {},
        ):
            if st.off_finalized:
                _clear_pending_command_state(st)
            action = control_action = "hold"
            source = control_source = "off_settled"
        elif bypass_actuation_delay:
            if control_source == "thermostat_reached":
                session_logger.mark_cooled(room_id)
            await _turn_ac_off(
                room_id, cfg, indoor_temp, reason_off, now=now, force=force_off,
            )
            if not st.pending_off_confirmation:
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

    sleep_target_changed = abs(float(getattr(sleep_result, "offset", 0.0))) >= 0.01
    humidity_target_changed = abs(float(getattr(humidity_result, "humidity_offset", 0.0))) >= 0.01
    thermal_target_changed = abs(float(getattr(st, "thermal_load_compensation_offset", 0.0))) >= 0.01
    if (
        (smart_curve or sleep_target_changed or humidity_target_changed or thermal_target_changed)
        and climate_entity and ac_on and occ_res and not in_cooldown
    ):
        interval = _cfg_int(cfg, "setpoint_command_min_interval_seconds", 180, lo=0)
        meaningful = _cfg_float(cfg, "setpoint_min_delta_deg", 0.7, lo=0.0)
        await smart_cooling.apply_effective_target(
            room_id,
            climate_entity=climate_entity,
            effective_target=et_eff,
            current_target=climate_data.get("target_temp"),
            ac_on=ac_on,
            manual_override=manual_override_enabled(cfg) or manual_override_active,
            min_interval_seconds=interval,
            meaningful_delta_deg=meaningful,
        )

    if not manual_override_active:
        await _maybe_record_ai_user_adjustment(room_id, cfg, climate_data or {}, now)

    # After actuation: ac_is_on may have flipped this tick â€” session gate uses post-command truth.
    session_confirmed_ac_on = bool(st.ac_is_on)
    session_inferred_only = bool(st.physical_ac_on and not session_confirmed_ac_on)

    await _maintain_session_lifecycle(
        room_id,
        cfg,
        indoor_temp,
        now,
        et_eff,
        in_cooldown=in_cooldown,
        confirmed_ac_on=session_confirmed_ac_on,
        inferred_only_physical=session_inferred_only,
    )

    _sync_ac_display_fields(st)
    _maybe_finalize_terminal_off(
        room_id,
        st,
        now,
        climate_data=climate_data or {},
        in_cooldown=_is_in_cooldown(st, now),
    )

    if (
        session_logger.current_session_id(room_id)
        and st.telemetry_power_live_valid
        and not st.telemetry_gap
        and telemetry_power_reading is not None
    ):
        st.watts_samples.append(telemetry_power_watts)

    if manual_override_active:
        logger.info(
            "[HawaAI][%s] Manual setpoint lock active; runtime target source=%s target=%.1fÂ°C",
            room_id,
            st.effective_target_source,
            et_eff,
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
            "watt_draw": telemetry_power_watts if telemetry_power_reading is not None else None,
            "presence": bool(is_occupied_bool) if is_occupied_bool is not None else False,
            "setpoint": sp,
            "fan_mode": fm,
            "energy_kwh": energy_kwh_reading,
            "telemetry_gap": st.telemetry_gap,
            "ai_target_temp": ai_tgt,
            "ai_fan_mode": ai_fan,
            "ai_confidence": ai_conf,
            "schedule_slot": schedule_slot_snap,
            "schedule_base_temp": base_temp,
            "effective_after_weather": eff_aw,
            "effective_final_temp": et_eff,
            "sleep_offset": st.sleep_offset,
            "sleep_phase": st.sleep_phase,
            "sleep_optimization_active": st.sleep_optimization_active,
            "humidity_offset": st.humidity_offset,
            "comfort_score": st.comfort_score,
            "comfort_level": st.comfort_level,
            "humidity_band": st.humidity_band,
            "dry_mode_recommended": st.dry_mode_recommended,
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
            "[SNAPSHOT] session_active but no session row for room=%s after ensure â€” skipping",
            room_id,
        )


async def _maintain_session_lifecycle(
    room_id: str,
    cfg: dict,
    indoor_temp: float,
    now: datetime,
    et_eff: float,
    *,
    in_cooldown: bool,
    confirmed_ac_on: bool,
    inferred_only_physical: bool,
) -> None:
    """
    Open provisional session on command/runtime ON; never inferred-only pending path.

    Gating uses ``confirmed_ac_on`` / ``_session_creation_eligible`` (runtime / IR),
    not thermostat ``effective_target`` alone â€” switching comfort effective_mode does not start sessions by itself.
    """
    st = _rt(room_id)
    sid_open = session_logger.current_session_id(room_id)

    eligibility = _session_creation_eligible(st, now)
    eligible_confirmed_session = eligibility and confirmed_ac_on and not inferred_only_physical

    runtime_confirm = False
    if st.ac_is_on and st.last_ac_on_at is not None:
        since_ir = now.timestamp() - float(st.last_ac_on_at)
        if since_ir >= float(COMPRESSOR_STABLE_SECONDS):
            runtime_confirm = True

    if sid_open and runtime_confirm:
        _mark_session_runtime_confirmed(room_id, st, now, source="runtime")

    if sid_open and session_logger.current_session_is_provisional(room_id):
        start_ref = st.session_start_time or session_logger.session_start_time(room_id)
        if start_ref is not None:
            prov_age = (now - start_ref).total_seconds()
            if prov_age > float(MAX_PROVISIONAL_SECONDS):
                can_promote = _session_has_confirmation_evidence(
                    st,
                    start_ref,
                    prov_age,
                )
                if can_promote:
                    log_with_room(
                        "info",
                        room_id,
                        "[SESSION] provisional_promoted reason=timeout_reconciled age=%.0fs",
                        prov_age,
                    )
                    await session_logger.upgrade_current_session_to_confirmed(room_id)
                    st.session_state = "confirmed"
                else:
                    log_with_room(
                        "info",
                        room_id,
                        "[SESSION_PROVISIONAL_TIMEOUT] room=%s session=%s age=%.0fs (max %.0fs) - closing",
                        room_id,
                        sid_open,
                        prov_age,
                        MAX_PROVISIONAL_SECONDS,
                    )
                    await _close_session(room_id, cfg, indoor_temp, reason="provisional_timeout")
                    return

    if eligible_confirmed_session and session_logger.current_session_id(room_id) is None:
        await _start_provisional_session(room_id, cfg, indoor_temp, now, et_eff)

    if (
        session_logger.current_session_id(room_id) is not None
        and session_logger.current_session_is_provisional(room_id)
        and runtime_confirm
    ):
        await session_logger.upgrade_current_session_to_confirmed(room_id)
        log_with_room("info", room_id, "[SESSION] provisional_promoted")
        st.session_state = "confirmed"


async def _start_provisional_session(
    room_id: str,
    cfg: dict,
    indoor_temp: float,
    now: datetime,
    et_eff: float,
) -> None:
    """
    Start a DB session after runtime/IR ON intent.
    Same session_id is upgraded later when runtime remains stable (provisional=0).
    """
    if session_logger.current_session_id(room_id) is not None:
        return

    st = _rt(room_id)
    # No DB session until delayed ON executes; avoids phantom sessions.
    if st.pending_action == "on":
        return

    target = float(et_eff)
    start_kwh = st.energy_kwh if st.energy_kwh_entity else None

    st.session_start_kwh = start_kwh
    st.session_start_time = now
    st.session_start_temp = indoor_temp
    st.compressor_on_since = now
    st.compressor_off_since = None
    st.watts_samples = []
    st.session_state = "provisional"
    st.session_runtime_confirmed = False
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
        "[SESSION_START] room=%s session=%s provisional=1 indoor=%.1fÂ°C target=%.1fÂ°C",
        room_id,
        sid,
        indoor_temp,
        target,
    )


# â”€â”€ Delayed actuation (intent â†’ pending timer â†’ turn) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
            "[DELAY_ON][%s] runtime ON confirmed - clearing pending_on",
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
            "[DELAY_ON][%s] Skip duplicate ON â€” already sent at %s",
            room_canon,
            st.pending_on_ir_sent_at,
        )
        return

    if _decision_lock_blocks_delayed_emit(st, now):
        log_with_room(
            "info",
            room_canon,
            "[DECISION_LOCK][%s] delayed ON held â€” lock active pending_since=%.3f â€” wait for next tick",
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
    vacancy_off = _is_vacancy_off_reason(reason)
    vacancy_generation = st.vacancy_generation if vacancy_off else None

    if vacancy_off and (st.occupied or st.stable_occupied):
        log_with_room(
            "info",
            room_canon,
            "[VACANCY] stale_timer_ignored reason=reoccupancy generation=%s",
            st.vacancy_generation,
        )
        _clear_pending_command_state(st)
        return

    if not st.physical_ac_on:
        _clear_pending_command_state(st)
        return

    if delay <= 0:
        if _decision_lock_blocks_delayed_emit(st, now):
            logger.info(
                "[DECISION_LOCK][%s] immediate OFF deferred â€” %.0fs lock since last IR",
                room_canon,
                DECISION_LOCK_SECONDS,
            )
            return
        if reason == "target_reached":
            session_logger.mark_cooled(room_canon)
        logger.info("[DELAY_OFF][%s] TRIGGER _turn_ac_off (delay=0)", room_canon)
        await _turn_ac_off(room_canon, cfg, indoor_temp, reason, now=now, force=force)
        if not st.pending_off_confirmation:
            _clear_pending_command_state(st)
        return

    if st.pending_action != "off":
        st.pending_action = "off"
        st.pending_since = ts
        st.off_reason = reason
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
            vacancy_generation=vacancy_generation,
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
        if vacancy_off and (
            vacancy_generation is not None
            and vacancy_generation != st.vacancy_generation
            or st.occupied
            or st.stable_occupied
        ):
            log_with_room(
                "info",
                room_canon,
                "[VACANCY] stale_timer_ignored reason=reoccupancy generation=%s",
                st.vacancy_generation,
            )
            _clear_pending_command_state(st)
            return
        if _decision_lock_blocks_delayed_emit(st, now):
            logger.info(
                "[DECISION_LOCK][%s] delayed OFF held â€” elapsed=%.1fs pending_since=%.3f",
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
        if not st.pending_off_confirmation:
            _clear_pending_command_state(st)


# â”€â”€ Turn AC ON â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
            "[HawaAI][%s] Skip AC ON â€” delayed pending cycle already emitted IR; awaiting physical confirm",
            room_id,
        )
        return False

    climate_entity = (cfg.get("climate_entity") or "").strip()
    if not climate_entity:
        logger.error(
            "[HawaAI][%s] AC ON FAILED â€” no climate entity configured.",
            room_id,
        )
        return False

    target = effective_target if effective_target is not None else _cfg_float(cfg, "target_temp", 24.0)

    # Avoid redundant IR when runtime already reports ON.
    # never skip based on inferred-only transient ON.
    if st.physical_ac_on and st.ac_state_source != "inferred":
        logger.info(
            "[HawaAI][%s] Skip AC ON command â€” ON already confirmed (%s)",
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
        success = await ac_tuya_adapter.turn_on(
            climate_entity,
            target,
            fan_mode="auto",
            hvac_mode="cool",
        )
    elif ir_backend == "aerostate":
        success = await ac_aerostate_adapter.turn_on(
            climate_entity,
            target,
        )
    else:
        logger.error("[HawaAI][%s] AC ON FAILED: unsupported ir_backend=%s", room_id, ir_backend)
        return False
    if not success:
        logger.error(
            "[HawaAI][%s] AC ON via %s FAILED â€” not marking as ON; "
            "await physical confirmation or tick timeout (no automatic IR retry)",
            room_id,
            ir_backend,
        )
        return False

    st.ac_is_on = True
    cmd_ts = datetime.now(timezone.utc)
    st.ir_last_sent_ts = cmd_ts
    st.last_ac_on_at = cmd_ts.timestamp()
    st.last_confirmed_on_at = cmd_ts
    st.just_turned_on_until = cmd_ts + timedelta(seconds=float(POST_ON_STABILIZATION_SECONDS))
    _bump_last_command_ir_cooldown(st, cmd_ts)
    st.last_command = "on"
    st.off_reason = None
    st.last_sent_command_key = _fingerprint_turn_on(target)
    st.on_failed_retry_used = False
    st.compressor_on_since = None
    st.compressor_off_since = None
    record_setpoint_command(room_id, target, cmd_ts)
    st.last_decision_at = cmd_ts
    st.off_dispatch_pending = False
    st.off_dispatched_at = None
    st.off_finalized = False
    st.off_settled_at = None
    st.last_confirmed_off_at = None
    _clear_pending_off_confirmation(st)
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
        return _cfg_float(cfg, "target_temp", 24.0)
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
        logger.debug("[HawaAI][%s] _close_session(%s) â€” no open session, skipping", room_id, reason)
        return

    sl_start = session_logger.session_start_time(room_id)
    start_ref = st.session_start_time or sl_start
    now = datetime.now(timezone.utc)
    if start_ref is None:
        log_with_room(
            "warning",
            room_id,
            "[SESSION_END] room=%s session=%s â€” missing start anchor; using now",
            room_id,
            open_sid,
        )
        start_ref = now

    grace_secs = _session_finalization_grace_seconds(cfg, reason)
    if grace_secs > 0:
        log_with_room(
            "info",
            room_id,
            "[SESSION] reconciliation_window_started reason=%s grace=%.0fs",
            reason,
            grace_secs,
        )
        await asyncio.sleep(grace_secs)
        now = datetime.now(timezone.utc)

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
    if st.energy_kwh_entity and st.session_start_kwh is not None and st.energy_kwh is not None:
        try:
            end_k = float(st.energy_kwh)
            kwh_consumed = max(0.0, round(end_k - float(st.session_start_kwh), 4))
            energy_from_meter = True
        except (ValueError, TypeError):
            kwh_consumed = None

    if kwh_consumed is None:
        if st.watts_samples and avg_watts >= 100.0 and duration_secs > 0:
            kwh_consumed = max(0.0, (avg_watts * duration_secs) / 3_600_000.0)
            kwh_consumed = round(kwh_consumed, 4)

    try:
        temp_drop = (
            float(st.session_start_temp) - float(indoor_temp)
            if st.session_start_temp is not None and indoor_temp is not None
            else None
        )
    except (TypeError, ValueError):
        temp_drop = None
    confirmed_evidence = _session_has_confirmation_evidence(
        st,
        start_ref,
        duration_secs,
        avg_watts=avg_watts,
        peak_watts=peak_watts,
        kwh_consumed=kwh_consumed,
    )
    telemetry_evidence = bool(peak_watts is not None and peak_watts > _WATTS_COMPRESSOR)
    energy_evidence = bool(kwh_consumed is not None and kwh_consumed >= MIN_SESSION_ENERGY_KWH)
    cooling_evidence = bool(temp_drop is not None and temp_drop >= 0.2)
    meaningful_duration = duration_secs >= MEANINGFUL_SESSION_SECONDS
    short_invalid = not (
        duration_secs >= float(MIN_SESSION_SECONDS)
        and (
            confirmed_evidence
            or energy_evidence
            or cooling_evidence
            or meaningful_duration
        )
    )
    if short_invalid:
        log_with_room(
            "info",
            room_id,
            "[SESSION] finalized_invalid reason=insufficient_evidence duration=%.2fs energy=%s",
            duration_secs,
            f"{kwh_consumed:.4f}" if kwh_consumed is not None else "none",
        )
    else:
        if session_logger.current_session_is_provisional(room_id):
            await session_logger.upgrade_current_session_to_confirmed(room_id)
            log_with_room("info", room_id, "[SESSION] provisional_promoted reason=final_validation")
        log_with_room(
            "info",
            room_id,
            "[SESSION] validation_passed runtime=%s telemetry=%s energy=%s duration=%.0fs",
            confirmed_evidence,
            telemetry_evidence,
            energy_evidence,
            duration_secs,
        )

    tariff = _cfg_float(cfg, "energy_tariff_per_kwh", 8.0, lo=0.0)
    cost: Optional[float] = (
        round(kwh_consumed * tariff, 2) if kwh_consumed is not None else None
    )
    if kwh_consumed is not None:
        logger.info(
            "[HawaAI][%s] Session energy: %.4f kWh (%s) | Cost: â‚¹%.2f",
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
    log_with_room(
        "info",
        room_id,
        "[SESSION] finalized_%s reason=%s duration=%.0fs kwh=%s",
        "invalid" if short_invalid else "valid",
        reason,
        duration_secs,
        f"{kwh_consumed:.4f}" if kwh_consumed is not None else "none",
    )

    st.session_start_time = None
    st.session_start_temp = None
    st.session_start_kwh = None
    st.watts_samples = []
    st.session_state = "idle"
    st.session_runtime_confirmed = False
    # Don't clear setpoint tracking on provisional_timeout â€” the AC is still physically
    # running, so clearing last_applied_setpoint would cause a redundant IR command on
    # the next tick (should_send_setpoint_command returns "initial_setpoint").
    if reason != "provisional_timeout":
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
    close_session_on_send: bool = True,
) -> bool:
    st = _rt(room_id)
    climate_entity = (cfg.get("climate_entity") or "").strip()

    tnow = now if now is not None else datetime.now(timezone.utc)
    vacancy_off = _is_vacancy_off_reason(reason)

    if st.last_command == "off":
        if st.off_finalized:
            log_with_room("info", room_id, "[RUNTIME] duplicate_off_suppressed reason=settled_off")
            return False
        if st.pending_off_confirmation:
            log_with_room("info", room_id, "[RUNTIME] duplicate_off_suppressed reason=off_confirmation_pending")
            return False
        if st.off_confirmation_failed and vacancy_off:
            log_with_room("info", room_id, "[RUNTIME] duplicate_off_suppressed reason=off_confirmation_failed")
            return False
        if (
            st.off_dispatch_pending
            and _off_dispatch_elapsed(st, tnow) < float(OFF_TERMINAL_RECONCILE_SECONDS)
        ):
            log_with_room("info", room_id, "[RUNTIME] duplicate_off_suppressed reason=reconciliation_active")
            return False

    if reason not in (
        "manual",
        "manual_off",
        "power_off",
        "room_disabled",
        "room_deleted",
    ):
        on_secs = _seconds_since_effective_on_or_command(st, tnow)
        if on_secs < MIN_ON_TIME_SECONDS:
            logger.info(
                "[CONTROL] Block OFF â€” minimum ON time active (%.1fs < %.0fs)",
                on_secs,
                MIN_ON_TIME_SECONDS,
            )
            return False

    if not force:
        if not st.ac_is_on:
            return False
        if not _gate_turn_ac_off(room_id, cfg, tnow, force=False):
            return False
    elif not _gate_turn_ac_off(room_id, cfg, tnow, force=True):
        return False
    elif reason == "vacant":
        # Hard policy: vacancy must not be skipped for duplicate/cooldown â€” still log once.
        log_with_room("info", room_id, "[VACANCY] AC OFF forced")

    if not await _dispatch_off_ir(room_id, cfg, climate_entity):
        return False

    st.ir_last_sent_ts = tnow

    clear_setpoint_command_tracking(room_id)

    ts_off = time.time()
    cmd_ts = datetime.now(timezone.utc)
    if not vacancy_off:
        st.ac_is_on = False
        st.last_ac_off_at = ts_off
    if force:
        # Forced vacancy/safety OFF always anchors IR cooldown window (explicit command intent).
        st.last_command_time = cmd_ts
    else:
        _bump_last_command_ir_cooldown(st, cmd_ts)
    st.last_command = "off"
    st.off_reason = reason
    st.last_sent_command_key = _fingerprint_turn_off()
    if not vacancy_off:
        st.compressor_off_since = cmd_ts
        st.compressor_on_since = None
    st.just_turned_on_until = None
    st.last_decision_at = cmd_ts
    st.off_dispatch_pending = True
    st.off_dispatched_at = cmd_ts
    st.off_finalized = False
    st.off_settled_at = None
    st.off_confirmation_failed = False
    if vacancy_off:
        st.pending_off_confirmation = True
        st.pending_off_sent_at = cmd_ts
        st.pending_off_retry_count = 0
        st.pending_action = "off"
        st.pending_since = st.pending_since or time.time()
        st.ac_state = "pending_off"
        log_with_room("info", room_id, "[OFF_CONFIRM] pending reason=%s", reason)
    else:
        log_with_room("info", room_id, "[RUNTIME] entering_idle reason=%s", reason)

    if close_session_on_send and not vacancy_off:
        await _close_session(room_id, cfg, indoor_temp, reason)
    return True


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
    min_iv = _cfg_float(merged, "min_command_interval_seconds", 150.0, lo=0.0)

    mo_until = st.manual_override_until.isoformat() if st.manual_override_until else None
    mo_persisted = manual_override_enabled(merged)
    mo_started_at = merged.get("override_started_at") if mo_persisted else None
    mo_user_settings = _manual_override_user_settings(merged) if mo_persisted else {}
    mo_timed_active = bool(
        st.manual_override_until is not None
        and now < st.manual_override_until
        and st.manual_override_temp is not None
    )
    mo_active = bool(mo_persisted or mo_timed_active or st.manual_override_config_active)
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
        "hvac_control_confidence": st.hvac_control_confidence,
        "control_source":        st.effective_control_source,
        "control_mode":          normalize_control_mode(merged),
        "occupied":              st.occupied,
        "vacancy_active":        st.vacancy_active,
        "vacancy_hold":          st.vacancy_hold,
        "safety_vacant":         st.safety_vacant,
        "pending_vacancy":       st.pending_vacancy,
        "thermostat_blocked":    st.thermostat_blocked,
        "vacant_since":          st.vacant_since.isoformat() if st.vacant_since else None,
        "off_reason":            st.off_reason,
        "stale_idle":            st.stale_idle,
        "stable_occupied":       st.stable_occupied,
        "occupancy_cache":       st.last_known_presence,
        "target_temp":           st.effective_target_temp,
        "target_source":         st.effective_target_source,
        "control_effective_target": st.last_control_effective_target_temp,
        "sleep_offset":          st.sleep_offset,
        "sleep_phase":           st.sleep_phase,
        "sleep_optimization_active": st.sleep_optimization_active,
        "sleep_suspended_reason": st.sleep_suspended_reason,
        "humidity_percent":      st.humidity_percent,
        "feels_like_temp":       st.feels_like_temp,
        "dew_point":             st.dew_point,
        "humidity_offset":       st.humidity_offset,
        "comfort_score":         st.comfort_score,
        "comfort_level":         st.comfort_level,
        "humidity_band":         st.humidity_band,
        "dry_mode_recommended":  st.dry_mode_recommended,
        "thermal_load_level":    st.thermal_load_level,
        "thermal_load_confidence": st.thermal_load_confidence,
        "thermal_load_score":    st.thermal_load_score,
        "thermal_load_rise_rate": round(float(st.thermal_load_rise_rate_ema), 4),
        "thermal_load_offset":   st.thermal_load_compensation_offset,
        "thermal_load_active":   st.thermal_load_compensation_active,
        "thermal_load_summary":  st.thermal_load_summary,
        "cooling_saturated":     st.cooling_saturated,
        "max_comfort_cooling_active": st.cooling_saturated,
        "last_command_source":   st.last_command_source,
        "last_ac_on_at":         st.last_ac_on_at,
        "last_ac_off_at":        st.last_ac_off_at,
        "ir_last_sent_at":       st.ir_last_sent_ts.isoformat() if st.ir_last_sent_ts else None,
        "ir_send_lock_seconds":  float(IR_SEND_LOCK_SECONDS),
        "just_turned_on_until":  (
            st.just_turned_on_until.isoformat() if st.just_turned_on_until else None
        ),
        "energy_config_mode": st.energy_config_mode,
        "energy_configured": st.energy_configured,
        "energy_device_id": st.energy_device_id,
        "energy_device_name": st.energy_device_name,
        "energy_device_lookup_skipped": st.energy_device_lookup_skipped,
        "energy_power_entity": st.energy_power_entity,
        "energy_kwh_entity": st.energy_kwh_entity,
        "energy_power_raw_state": st.energy_power_raw_state,
        "energy_kwh_raw_state": st.energy_kwh_raw_state,
        "energy_watts": st.energy_watts,
        "energy_kwh_total": st.energy_kwh,
        "energy_power_unit": st.energy_power_unit,
        "energy_power_confidence": st.energy_power_confidence,
        "energy_power_validation_reason": st.energy_power_validation_reason,
        "energy_power_suspicious": st.energy_power_suspicious,
        "telemetry_power_live_valid": st.telemetry_power_live_valid,
        "telemetry_kwh_live_valid": st.telemetry_kwh_live_valid,
        "telemetry_status": st.telemetry_status,
        "telemetry_confidence": st.telemetry_confidence,
        "telemetry_gap": st.telemetry_gap,
        "telemetry_invalid_since": (
            st.telemetry_invalid_since.isoformat() if st.telemetry_invalid_since else None
        ),
        "telemetry_stale_after_seconds": float(TELEMETRY_STALE_SECONDS),
        "telemetry_offline_after_seconds": float(TELEMETRY_OFFLINE_SECONDS),
        "last_valid_power_watts": st.last_valid_power_watts,
        "last_valid_energy_kwh": st.last_valid_energy_kwh,
        "last_valid_timestamp": (
            st.last_valid_timestamp.isoformat() if st.last_valid_timestamp else None
        ),
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
        "manual_override": bool(mo_active),
        "manual_override_enabled": bool(mo_persisted),
        "manual_override_active": bool(mo_active),
        "manual_override_persisted": bool(mo_persisted),
        "automation_paused_by_user": bool(mo_active),
        "override_started_at": mo_started_at,
        "override_user_settings": mo_user_settings,
        "manual_override_expires_at": None if mo_persisted else (mo_until if mo_active else None),
        "manual_override_target_temp": (
            st.manual_override_temp
            if st.manual_override_temp is not None
            else mo_user_settings.get("target_temp")
        ) if mo_active else None,
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
        "pending_off_confirmation": bool(st.pending_off_confirmation),
        "pending_off_sent_at": (
            st.pending_off_sent_at.isoformat() if st.pending_off_sent_at else None
        ),
        "pending_off_retry_count": int(st.pending_off_retry_count),
        "max_off_retries": int(MAX_OFF_CONFIRM_RETRIES),
        "off_confirmation_failed": bool(st.off_confirmation_failed),
        "last_confirmed_off_at": (
            st.last_confirmed_off_at.isoformat() if st.last_confirmed_off_at else None
        ),
        "off_confirm_watts": _off_confirm_watts_threshold(merged),
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
        "zone_cache": {
            "present": st.zone_present,
            "confirmed": st.zone_confirmed,
            "confidence": st.zone_confidence,
            "sensor_usable": st.zone_sensor_usable,
        },
        "zone_ui_phase": zone_ui_phase,
        "zone_dwell_elapsed_seconds": zone_dwell_elapsed_seconds,
        "zone_dwell_remaining_seconds": zone_dwell_remaining_seconds,
        "effective_mode":             str(merged.get("effective_mode") or "auto"),
        "manual_effective_temp":      merged.get("manual_effective_temp"),
        "effective_max_delta_deg":    effective_max_delta_deg(merged),
        "self_heal": runtime_self_heal.report_to_dict(
            runtime_self_heal.global_state().latest_report(canonical)
        ),
    }
