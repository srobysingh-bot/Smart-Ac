"""Passive runtime self-healing diagnostics for HawaAI.

This module is deliberately not a control engine. It never sends IR, never
decides thermostat intent, and never bypasses cooldown or hysteresis. It only
validates runtime consistency, scores confidence, and emits bounded recovery
recommendations that the existing runtime may apply idempotently.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


UNAVAILABLE_STATES = {"", "unknown", "unavailable", "none"}


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DESYNCED = "desynced"


class RecoveryAction(str, Enum):
    CLEAR_STALE_PENDING_ON = "clear_pending_on"
    CLEAR_STALE_PENDING_OFF = "clear_pending_off"
    REBUILD_RUNTIME = "runtime_rebuilt"
    CLOSE_ORPHAN_SESSION = "close_orphan_session"
    DOWNGRADE_CONFIDENCE = "downgrade_confidence"
    RESTORE_CACHED_STATE = "restore_cached_state"
    RELEASE_FAILED_ON_RETRY = "release_failed_on_retry"


@dataclass(frozen=True)
class SelfHealConfig:
    """Bounded thresholds for passive runtime validation."""

    pending_stale_seconds: float = 180.0
    pending_off_stale_seconds: float = 180.0
    mismatch_grace_seconds: float = 120.0
    orphan_session_grace_seconds: float = 180.0
    climate_cache_ttl_seconds: float = 45.0
    sensor_cache_ttl_seconds: float = 45.0
    sensor_stale_seconds: float = 300.0
    failed_on_retry_release_seconds: float = 120.0
    high_power_watts: float = 500.0
    idle_power_watts: float = 50.0
    min_rebuild_confidence: float = 0.65
    min_recovery_confidence: float = 0.60


@dataclass(frozen=True)
class RuntimeSnapshot:
    room_id: str
    ac_is_on: bool = False
    physical_ac_on: bool = False
    effective_ac_on: bool = False
    ac_state: str = "off"
    power_source: str = "internal"
    pending_action: Optional[str] = None
    pending_since: Optional[float] = None
    pending_on_ir_sent: bool = False
    pending_on_ir_sent_at: Optional[datetime] = None
    last_command: str = ""
    last_command_time: Optional[datetime] = None
    last_decision_at: Optional[datetime] = None
    startup_state_loaded: bool = False
    session_id: Optional[Any] = None
    session_start_time: Optional[datetime] = None
    session_state: str = "idle"
    on_failed_retry_used: bool = False


@dataclass(frozen=True)
class SensorSnapshot:
    entity_id: str
    value: Any = None
    available: bool = True
    kind: str = "sensor"


@dataclass(frozen=True)
class ObservationSnapshot:
    climate_entity: str = ""
    climate_state: Optional[str] = None
    climate_available: bool = True
    climate_last_updated: Optional[datetime] = None
    power_entity: str = ""
    power_watts: Optional[float] = None
    power_available: bool = True
    sensors: Tuple[SensorSnapshot, ...] = ()


@dataclass(frozen=True)
class ConfidenceScores:
    runtime: float
    climate: float
    power: float
    sensor: float

    @property
    def overall(self) -> float:
        return round(min(self.runtime, self.climate, self.power, self.sensor), 3)

    @property
    def label(self) -> str:
        score = self.overall
        if score >= 0.75:
            return "high"
        if score >= 0.45:
            return "medium"
        return "low"


@dataclass(frozen=True)
class HealthIssue:
    code: str
    detail: str
    severity: str = "warning"
    age_seconds: float = 0.0


@dataclass(frozen=True)
class RecoveryRecommendation:
    action: RecoveryAction
    reason: str
    confidence: float
    idempotency_key: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HealthReport:
    room_id: str
    status: HealthStatus
    confidence: ConfidenceScores
    issues: Tuple[HealthIssue, ...] = ()
    recommendations: Tuple[RecoveryRecommendation, ...] = ()
    used_cached_values: Tuple[str, ...] = ()


@dataclass
class _CacheEntry:
    value: Any
    observed_at: datetime


@dataclass
class _SensorTrack:
    value: Any
    first_seen_at: datetime
    last_seen_at: datetime


class SelfHealState:
    """Small in-memory ledger for delayed detection and no-spam logging."""

    def __init__(self) -> None:
        self._issue_first_seen: Dict[Tuple[str, str], datetime] = {}
        self._cache: Dict[Tuple[str, str], _CacheEntry] = {}
        self._sensor_tracks: Dict[Tuple[str, str], _SensorTrack] = {}
        self._log_signature_by_room: Dict[str, Tuple[Any, ...]] = {}
        self._latest_report_by_room: Dict[str, HealthReport] = {}

    def issue_age(self, room_id: str, code: str, active: bool, now: datetime) -> float:
        key = (room_id, code)
        if not active:
            self._issue_first_seen.pop(key, None)
            return 0.0
        if key not in self._issue_first_seen:
            self._issue_first_seen[key] = now
        return max(0.0, (now - self._issue_first_seen[key]).total_seconds())

    def remember(self, kind: str, entity_id: str, value: Any, now: datetime) -> None:
        eid = (entity_id or "").strip()
        if not eid:
            return
        self._cache[(kind, eid)] = _CacheEntry(value=value, observed_at=now)

    def cached(
        self,
        kind: str,
        entity_id: str,
        now: datetime,
        ttl_seconds: float,
    ) -> Optional[Any]:
        eid = (entity_id or "").strip()
        if not eid:
            return None
        entry = self._cache.get((kind, eid))
        if entry is None:
            return None
        age = (now - entry.observed_at).total_seconds()
        if age <= float(ttl_seconds):
            return entry.value
        return None

    def sensor_age_if_frozen(self, sensor: SensorSnapshot, now: datetime) -> float:
        eid = (sensor.entity_id or "").strip()
        if not eid or not sensor.available:
            self._sensor_tracks.pop((sensor.kind, eid), None)
            return 0.0
        val = _normalized_value(sensor.value)
        key = (sensor.kind, eid)
        track = self._sensor_tracks.get(key)
        if track is None or _normalized_value(track.value) != val:
            self._sensor_tracks[key] = _SensorTrack(val, now, now)
            return 0.0
        track.last_seen_at = now
        return max(0.0, (now - track.first_seen_at).total_seconds())

    def should_log(self, report: HealthReport) -> bool:
        sig = (
            report.status.value,
            report.confidence.label,
            tuple(issue.code for issue in report.issues),
            tuple(rec.action.value for rec in report.recommendations),
            report.used_cached_values,
        )
        prev = self._log_signature_by_room.get(report.room_id)
        if sig == prev:
            return False
        self._log_signature_by_room[report.room_id] = sig
        return True

    def set_latest_report(self, report: HealthReport) -> None:
        self._latest_report_by_room[report.room_id] = report

    def latest_report(self, room_id: str) -> Optional[HealthReport]:
        return self._latest_report_by_room.get((room_id or "").strip().lower())


_GLOBAL_STATE = SelfHealState()


def global_state() -> SelfHealState:
    return _GLOBAL_STATE


def evaluate(
    runtime: RuntimeSnapshot,
    observation: ObservationSnapshot,
    *,
    now: Optional[datetime] = None,
    cfg: SelfHealConfig = SelfHealConfig(),
    state: Optional[SelfHealState] = None,
) -> HealthReport:
    """Evaluate runtime consistency and emit passive recovery recommendations."""

    tnow = _as_utc(now or datetime.now(timezone.utc))
    ledger = state or _GLOBAL_STATE
    room_id = (runtime.room_id or "").strip().lower()

    climate_state, used_climate_cache = _resolved_climate_state(observation, ledger, tnow, cfg)
    power_watts, used_power_cache = _resolved_power_watts(observation, ledger, tnow, cfg)
    power_valid = power_watts is not None and math.isfinite(float(power_watts))
    climate_mode = _norm_state(climate_state)
    climate_available = _is_available(climate_state) and bool(observation.climate_available)

    observed_power_on = bool(power_valid and float(power_watts) >= cfg.idle_power_watts)
    observed_power_high = bool(power_valid and float(power_watts) >= cfg.high_power_watts)
    observed_power_off = bool(power_valid and float(power_watts) < cfg.idle_power_watts)
    observed_climate_on = climate_mode == "cool"
    observed_climate_off = climate_mode == "off"
    runtime_on = bool(runtime.ac_is_on or runtime.physical_ac_on)

    issues: List[HealthIssue] = []
    recs: List[RecoveryRecommendation] = []

    pending_age = _pending_age_seconds(runtime, tnow)
    if runtime.pending_action == "on":
        active = pending_age >= cfg.pending_stale_seconds
        age = ledger.issue_age(room_id, "pending_on_stuck", active, tnow)
        if active:
            issues.append(HealthIssue("pending_on_stuck", "pending_on exceeded bounded timeout", "warning", age))
            recs.append(_rec(
                room_id,
                RecoveryAction.CLEAR_STALE_PENDING_ON,
                "pending_on timed out without confirmation",
                0.82,
                pending_age=pending_age,
            ))
    else:
        ledger.issue_age(room_id, "pending_on_stuck", False, tnow)

    if runtime.pending_action == "off":
        active = pending_age >= cfg.pending_off_stale_seconds
        age = ledger.issue_age(room_id, "pending_off_stuck", active, tnow)
        if active:
            issues.append(HealthIssue("pending_off_stuck", "pending_off exceeded bounded timeout", "warning", age))
            recs.append(_rec(
                room_id,
                RecoveryAction.CLEAR_STALE_PENDING_OFF,
                "pending_off timed out",
                0.80,
                pending_age=pending_age,
            ))
    else:
        ledger.issue_age(room_id, "pending_off_stuck", False, tnow)

    cool_power_low = observed_climate_on and observed_power_off
    age = ledger.issue_age(room_id, "climate_cool_power_low", cool_power_low, tnow)
    if cool_power_low:
        issues.append(HealthIssue("climate_cool_power_low", "ha_mode=cool but power is low", "warning", age))
        if age >= cfg.mismatch_grace_seconds:
            recs.append(_rec(
                room_id,
                RecoveryAction.REBUILD_RUNTIME,
                "climate/power mismatch settled as off",
                0.70,
                observed_on=False,
                source="power",
                mismatch_age=age,
            ))

    climate_off_power_high = observed_climate_off and observed_power_high
    age = ledger.issue_age(room_id, "climate_off_power_high", climate_off_power_high, tnow)
    if climate_off_power_high:
        issues.append(HealthIssue("climate_off_power_high", "ha_mode=off but power is high", "warning", age))
        if age >= cfg.mismatch_grace_seconds:
            recs.append(_rec(
                room_id,
                RecoveryAction.REBUILD_RUNTIME,
                "climate/power mismatch settled as on",
                0.78,
                observed_on=True,
                source="power",
                mismatch_age=age,
            ))

    runtime_on_ha_unavailable = runtime_on and observation.climate_entity and not climate_available
    age = ledger.issue_age(room_id, "runtime_on_ha_unavailable", runtime_on_ha_unavailable, tnow)
    if runtime_on_ha_unavailable:
        issues.append(HealthIssue("runtime_on_ha_unavailable", "runtime is on while HA climate is unavailable", "info", age))
        recs.append(_rec(
            room_id,
            RecoveryAction.DOWNGRADE_CONFIDENCE,
            "HA climate unavailable; preserve runtime with lower confidence",
            0.68,
        ))

    runtime_on_power_low = runtime_on and observed_power_off and not _recent_command(runtime, tnow, cfg.mismatch_grace_seconds)
    age = ledger.issue_age(room_id, "runtime_on_power_low", runtime_on_power_low, tnow)
    if runtime_on_power_low:
        issues.append(HealthIssue("runtime_on_power_low", "runtime says on but power is low", "warning", age))
        if age >= cfg.mismatch_grace_seconds:
            recs.append(_rec(
                room_id,
                RecoveryAction.REBUILD_RUNTIME,
                "runtime/power mismatch settled as off",
                0.76,
                observed_on=False,
                source="power",
                mismatch_age=age,
            ))

    orphan_session = bool(runtime.session_id) and observed_power_off and observed_climate_off
    age = ledger.issue_age(room_id, "orphan_session", orphan_session, tnow)
    if orphan_session:
        issues.append(HealthIssue("orphan_session", "open session while climate and power are off", "warning", age))
        if age >= cfg.orphan_session_grace_seconds:
            recs.append(_rec(
                room_id,
                RecoveryAction.CLOSE_ORPHAN_SESSION,
                "session no longer matches physical state",
                0.82,
                mismatch_age=age,
            ))

    if not runtime.startup_state_loaded:
        issues.append(HealthIssue("startup_unreconciled", "runtime has not completed startup reconciliation", "info", 0.0))
        if power_valid or climate_available:
            recs.append(_rec(
                room_id,
                RecoveryAction.REBUILD_RUNTIME,
                "startup runtime truth can be rebuilt from observations",
                0.66,
                observed_on=bool(observed_power_on or (not power_valid and observed_climate_on)),
                source="startup",
            ))

    failed_age = _seconds_since(runtime.last_command_time, tnow)
    if (
        runtime.ac_state == "on_failed"
        and runtime.on_failed_retry_used
        and failed_age >= cfg.failed_on_retry_release_seconds
        and runtime.pending_action is None
    ):
        issues.append(HealthIssue("failed_on_retry_locked", "failed ON retry lock aged out safely", "info", failed_age))
        recs.append(_rec(
            room_id,
            RecoveryAction.RELEASE_FAILED_ON_RETRY,
            "failed ON retry lock exceeded release timeout",
            0.72,
            failed_age=failed_age,
        ))

    sensor_issues, sensor_score = _sensor_health(observation.sensors, ledger, tnow, cfg)
    issues.extend(sensor_issues)

    used_cached_values: List[str] = []
    if used_climate_cache:
        used_cached_values.append("climate")
        recs.append(_rec(
            room_id,
            RecoveryAction.RESTORE_CACHED_STATE,
            "using short-lived cached climate state",
            0.55,
            entity_id=observation.climate_entity,
        ))
    if used_power_cache:
        used_cached_values.append("power")

    confidence = ConfidenceScores(
        runtime=_runtime_confidence(runtime, issues),
        climate=_climate_confidence(observation, climate_state, used_climate_cache, issues),
        power=_power_confidence(observation, power_watts, used_power_cache, issues),
        sensor=sensor_score,
    )

    recs = tuple(
        rec for rec in recs
        if rec.confidence >= cfg.min_recovery_confidence
        or rec.action in (RecoveryAction.DOWNGRADE_CONFIDENCE, RecoveryAction.RESTORE_CACHED_STATE)
    )
    status = _status_for(issues, confidence, cfg)
    report = HealthReport(
        room_id=room_id,
        status=status,
        confidence=confidence,
        issues=tuple(issues),
        recommendations=recs,
        used_cached_values=tuple(used_cached_values),
    )
    ledger.set_latest_report(report)
    return report


def log_report_changes(
    report: HealthReport,
    *,
    state: Optional[SelfHealState] = None,
    log: Optional[logging.Logger] = None,
) -> None:
    """Emit structured self-heal logs only when the report signature changes."""

    ledger = state or _GLOBAL_STATE
    if not ledger.should_log(report):
        return
    out = log or logger
    out.info("[SELF_HEAL] state=%s", report.status.value)
    out.info("[SELF_HEAL] confidence=%s", report.confidence.label)
    for cached in report.used_cached_values:
        out.info("[SELF_HEAL] using_cached_value entity=%s", cached)
    for issue in report.issues:
        if issue.code.startswith("sensor_"):
            out.info("[SELF_HEAL] sensor=stale code=%s", issue.code)
    for rec in report.recommendations:
        out.info("[SELF_HEAL] recovery=%s", rec.action.value)


def runtime_snapshot_from_object(
    room_id: str,
    runtime: Any,
    *,
    session_id: Optional[Any] = None,
) -> RuntimeSnapshot:
    """Build a snapshot from logic_engine.RoomRuntime without importing it."""

    return RuntimeSnapshot(
        room_id=room_id,
        ac_is_on=bool(getattr(runtime, "ac_is_on", False)),
        physical_ac_on=bool(getattr(runtime, "physical_ac_on", False)),
        effective_ac_on=bool(getattr(runtime, "effective_ac_on", False)),
        ac_state=str(getattr(runtime, "ac_state", "off") or "off"),
        power_source=str(getattr(runtime, "effective_power_source", "internal") or "internal"),
        pending_action=getattr(runtime, "pending_action", None),
        pending_since=getattr(runtime, "pending_since", None),
        pending_on_ir_sent=bool(getattr(runtime, "pending_on_ir_sent", False)),
        pending_on_ir_sent_at=getattr(runtime, "pending_on_ir_sent_at", None),
        last_command=str(getattr(runtime, "last_command", "") or ""),
        last_command_time=getattr(runtime, "last_command_time", None),
        last_decision_at=getattr(runtime, "last_decision_at", None),
        startup_state_loaded=bool(getattr(runtime, "startup_state_loaded", False)),
        session_id=session_id,
        session_start_time=getattr(runtime, "session_start_time", None),
        session_state=str(getattr(runtime, "session_state", "idle") or "idle"),
        on_failed_retry_used=bool(getattr(runtime, "on_failed_retry_used", False)),
    )


def report_to_dict(report: Optional[HealthReport]) -> Dict[str, Any]:
    if report is None:
        return {
            "status": "unknown",
            "confidence": {
                "runtime": None,
                "climate": None,
                "power": None,
                "sensor": None,
                "overall": None,
                "label": "unknown",
            },
            "issues": [],
            "recommendations": [],
            "used_cached_values": [],
        }
    return {
        "status": report.status.value,
        "confidence": {
            "runtime": report.confidence.runtime,
            "climate": report.confidence.climate,
            "power": report.confidence.power,
            "sensor": report.confidence.sensor,
            "overall": report.confidence.overall,
            "label": report.confidence.label,
        },
        "issues": [
            {
                "code": issue.code,
                "detail": issue.detail,
                "severity": issue.severity,
                "age_seconds": round(issue.age_seconds, 1),
            }
            for issue in report.issues
        ],
        "recommendations": [
            {
                "action": rec.action.value,
                "reason": rec.reason,
                "confidence": rec.confidence,
                "idempotency_key": rec.idempotency_key,
                "metadata": rec.metadata,
            }
            for rec in report.recommendations
        ],
        "used_cached_values": list(report.used_cached_values),
    }


def _status_for(
    issues: Iterable[HealthIssue],
    confidence: ConfidenceScores,
    cfg: SelfHealConfig,
) -> HealthStatus:
    issue_list = list(issues)
    mismatch_codes = {
        "climate_cool_power_low",
        "climate_off_power_high",
        "runtime_on_power_low",
    }
    if any(i.code in mismatch_codes and i.age_seconds >= cfg.mismatch_grace_seconds for i in issue_list):
        return HealthStatus.DESYNCED
    if any(i.code == "orphan_session" and i.age_seconds >= cfg.orphan_session_grace_seconds for i in issue_list):
        return HealthStatus.DESYNCED
    if issue_list or confidence.overall < 0.75:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


def _rec(room_id: str, action: RecoveryAction, reason: str, confidence: float, **metadata: Any) -> RecoveryRecommendation:
    key_bits = [room_id, action.value]
    if "source" in metadata:
        key_bits.append(str(metadata["source"]))
    return RecoveryRecommendation(
        action=action,
        reason=reason,
        confidence=round(float(confidence), 3),
        idempotency_key=":".join(key_bits),
        metadata=dict(metadata),
    )


def _resolved_climate_state(
    observation: ObservationSnapshot,
    ledger: SelfHealState,
    now: datetime,
    cfg: SelfHealConfig,
) -> Tuple[Optional[str], bool]:
    state = observation.climate_state
    if observation.climate_entity and observation.climate_available and _is_available(state):
        ledger.remember("climate", observation.climate_entity, state, now)
        return state, False
    cached = ledger.cached("climate", observation.climate_entity, now, cfg.climate_cache_ttl_seconds)
    if cached is not None:
        return cached, True
    return state, False


def _resolved_power_watts(
    observation: ObservationSnapshot,
    ledger: SelfHealState,
    now: datetime,
    cfg: SelfHealConfig,
) -> Tuple[Optional[float], bool]:
    watts = observation.power_watts
    if observation.power_entity and observation.power_available and _valid_float(watts):
        val = float(watts)
        ledger.remember("power", observation.power_entity, val, now)
        return val, False
    cached = ledger.cached("power", observation.power_entity, now, cfg.sensor_cache_ttl_seconds)
    if cached is not None and _valid_float(cached):
        return float(cached), True
    return None, False


def _sensor_health(
    sensors: Iterable[SensorSnapshot],
    ledger: SelfHealState,
    now: datetime,
    cfg: SelfHealConfig,
) -> Tuple[List[HealthIssue], float]:
    issues: List[HealthIssue] = []
    configured = 0
    healthy = 0
    for sensor in sensors:
        if not (sensor.entity_id or "").strip():
            continue
        configured += 1
        raw = sensor.value
        available = bool(sensor.available) and _is_available(raw)
        if sensor.kind == "humidity":
            valid = _valid_humidity(raw)
        elif sensor.kind in ("temperature", "power"):
            valid = _valid_float(raw)
        else:
            valid = available

        if not available or not valid:
            issues.append(HealthIssue(f"sensor_invalid_{sensor.kind}", f"{sensor.entity_id} invalid", "warning", 0.0))
            continue

        frozen_age = ledger.sensor_age_if_frozen(sensor, now)
        if sensor.kind in ("temperature", "power") and frozen_age >= cfg.sensor_stale_seconds:
            issues.append(HealthIssue(f"sensor_frozen_{sensor.kind}", f"{sensor.entity_id} frozen", "warning", frozen_age))
            continue

        ledger.remember(sensor.kind, sensor.entity_id, raw, now)
        healthy += 1

    if configured == 0:
        return issues, 1.0
    return issues, round(max(0.0, min(1.0, healthy / configured)), 3)


def _runtime_confidence(runtime: RuntimeSnapshot, issues: Iterable[HealthIssue]) -> float:
    score = 1.0
    if runtime.pending_action:
        score -= 0.08
    if runtime.ac_state == "on_failed":
        score -= 0.22
    for issue in issues:
        if issue.code in ("runtime_on_power_low", "orphan_session", "pending_on_stuck", "pending_off_stuck"):
            score -= 0.25
        elif issue.code == "startup_unreconciled":
            score -= 0.12
    return round(max(0.0, min(1.0, score)), 3)


def _climate_confidence(
    observation: ObservationSnapshot,
    climate_state: Optional[str],
    used_cache: bool,
    issues: Iterable[HealthIssue],
) -> float:
    score = 1.0
    if observation.climate_entity and (not observation.climate_available or not _is_available(climate_state)):
        score -= 0.55
    if used_cache:
        score = min(score, 0.55)
    for issue in issues:
        if issue.code in ("climate_cool_power_low", "climate_off_power_high", "runtime_on_ha_unavailable"):
            score -= 0.18
    return round(max(0.0, min(1.0, score)), 3)


def _power_confidence(
    observation: ObservationSnapshot,
    power_watts: Optional[float],
    used_cache: bool,
    issues: Iterable[HealthIssue],
) -> float:
    score = 1.0
    if observation.power_entity and (not observation.power_available or power_watts is None):
        score -= 0.55
    if used_cache:
        score = min(score, 0.60)
    for issue in issues:
        if issue.code in ("climate_cool_power_low", "climate_off_power_high", "runtime_on_power_low"):
            score -= 0.16
    return round(max(0.0, min(1.0, score)), 3)


def _pending_age_seconds(runtime: RuntimeSnapshot, now: datetime) -> float:
    if runtime.pending_since is None:
        return 0.0
    try:
        return max(0.0, now.timestamp() - float(runtime.pending_since))
    except (TypeError, ValueError):
        return 0.0


def _seconds_since(moment: Optional[datetime], now: datetime) -> float:
    if moment is None:
        return float("inf")
    try:
        return max(0.0, (now - _as_utc(moment)).total_seconds())
    except Exception:
        return float("inf")


def _recent_command(runtime: RuntimeSnapshot, now: datetime, window_seconds: float) -> bool:
    return _seconds_since(runtime.last_command_time, now) < float(window_seconds)


def _norm_state(raw: Any) -> str:
    return str(raw or "").strip().lower()


def _is_available(raw: Any) -> bool:
    return _norm_state(raw) not in UNAVAILABLE_STATES


def _valid_float(raw: Any) -> bool:
    try:
        return math.isfinite(float(raw))
    except (TypeError, ValueError):
        return False


def _valid_humidity(raw: Any) -> bool:
    if not _valid_float(raw):
        return False
    val = float(raw)
    return 0.0 <= val <= 100.0


def _normalized_value(raw: Any) -> Any:
    if _valid_float(raw):
        return round(float(raw), 3)
    return str(raw)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
