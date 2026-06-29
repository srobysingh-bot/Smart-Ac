"""Read and write add-on configuration from /data/hawaai_config.json."""

import copy
import glob
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .energy_config import static_energy_entity_rejection_reason
from . import room_registry
from .temperature_schedule import validate_timezone_optional

logger = logging.getLogger(__name__)

CONFIG_PATH = "/data/hawaai_config.json"
CONFIG_SCHEMA_VERSION = 4

# Last successful merged config (survives single bad load so rooms are not silently wiped).
_last_known_good_config: Optional[Dict[str, Any]] = None
_last_load_sanitized_energy_entities = False
_last_logged_config_load_sig: Optional[tuple] = None
_logged_migration_steps: set[tuple[int, int]] = set()

# Default matches Ollama add-on pull (gemma:2b); override in Settings if needed.
DEFAULT_OLLAMA_MODEL = "gemma:2b"

# Legacy keys from pre–climate-only installs — stripped from merged config (ignored safely).
_LEGACY_IR_KEYS = frozenset({
    "broadlink_entity", "ir_device_name", "ir_command_on", "ir_command_off",
})

DEFAULT_CONFIG: Dict[str, Any] = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "presence_entity": "",
    "indoor_temp_entity": "",
    "ac_entity": "",
    "climate_entity": "",
    "energy_power_entity": "",
    "energy_kwh_entity": "",
    "energy_device_id": "",
    "energy_device_name": "",
    "ac_brand": "",
    "ac_model": "",
    "room_name": "Living Room",
    "control_mode": "thermostat",
    "target_temp": 24,
    "hysteresis": 1.5,
    "control_hysteresis_half_deg": 0.5,
    "min_command_interval_seconds": 150,
    "manual_override_duration_minutes": 10,
    "manual_override_detect_delta_deg": 0.5,
    "manual_override_exit_within_deg": 0.5,
    "meaningful_setpoint_delta_deg": 0.5,
    "setpoint_min_delta_deg": 0.7,
    "setpoint_command_min_interval_seconds": 180,
    "compressor_min_on_seconds": 300,
    "compressor_min_off_seconds": 180,
    "on_delay_seconds": 0,
    "off_delay_seconds": 0,
    "physical_state_from_power": True,
    "physical_on_watts": 100,
    "physical_off_watts": 30,
    "physical_state_confirm_seconds": 5,
    "max_reasonable_runtime_hours": 12,
    "vacancy_timeout_minutes": 5,
    "presence_only_on_dwell_seconds": 20,
    "presence_only_max_runtime_minutes": 240,
    "pre_cool_enabled": False,
    "pre_cool_duration_minutes": 25,
    "pre_cool_min_temp_gap_deg": 1.0,
    "pre_cool_target_offset_deg": 1.0,
    "pre_cool_arrival_grace_seconds": 120,
    "pre_cool_no_show_action": "off",
    "pre_cool_geofence_enabled": False,
    "pre_cool_geofence_mode": "suggest_only",
    "pre_cool_geofence_radius_km": 2.0,
    "pre_cool_home_latitude": None,
    "pre_cool_home_longitude": None,
    "pre_cool_allowed_people": [],
    "pre_cool_geofence_cooldown_minutes": 30,
    "pre_cool_one_shot_per_window": True,
    "pre_cool_allow_extension": True,
    "pre_cool_extension_minutes": 10,
    "pre_cool_max_total_minutes": 45,
    "pre_cool_stop_if_user_leaves_geofence": True,
    "thermostat_on_delta_deg": 0.7,
    "thermostat_off_delta_deg": 0.3,
    "user_authority_lock_secs": 120,
    "snapshot_interval_secs": 60,
    "use_presence": True,
    "use_outdoor_temp": True,
    "smart_temp_adjustment": True,
    "smart_cooling_enabled": False,
    "lg_fan_guard_enabled": False,
    "fan_guard_profile": "",
    "auto_turbo_allowed": False,
    "allow_manual_turbo": True,
    "default_safe_fan_mode": "f3",
    "preserve_last_non_turbo_fan": True,
    "turbo_auto_timeout_minutes": 10,
    "sleep_optimization_enabled": True,
    "sleep_start_hour": 22,
    "sleep_end_hour": 6,
    "sleep_max_offset": 1.5,
    "sleep_curve_mode": "gradual",
    "humidity_entity_id": "",
    "humidity_comfort_enabled": True,
    "humidity_ideal_min": 40,
    "humidity_ideal_max": 60,
    "humidity_warning_threshold": 65,
    "humidity_critical_threshold": 75,
    "humidity_min_offset": -1.0,
    "humidity_max_offset": 0.5,
    "manual_override": False,
    "manual_override_enabled": False,
    "override_started_at": None,
    "override_user_settings": {},
    "auto_comfort_profile": "comfort",
    "auto_comfort_learning_enabled": True,
    "auto_comfort_min_target": 16.0,
    "auto_comfort_max_target": 25.0,
    "auto_comfort_max_step_deg": 0.5,
    "auto_comfort_max_total_offset_deg": 2.0,
    "auto_comfort_min_change_seconds": 900,
    "ai_enabled": False,
    "ai_provider": "ollama",
    "ai_ollama_url": "http://172.30.32.1:11434",
    "ai_ollama_model": "",
    "ai_api_key": "",
    "ai_api_base_url": "",
    "ai_api_model": "",
    "ai_api_timeout": 60,
    "ai_api_json_object_format": False,
    "ai_fetch_min_interval_seconds": 60,
    "ai_indoor_near_setpoint_deg": 0.5,
    "weather_api_key": "",
    "weather_city": "",
    "weather_provider": "openweathermap",
    "energy_tariff_per_kwh": 8.0,
    "currency": "INR",
    "logic_interval_seconds": 60,
    "timezone": "",
    "rooms": [],
}

_RUNTIME_TRANSIENT_KEYS = frozenset({
    "ac_is_on",
    "ac_phase",
    "ac_state",
    "ac_state_source",
    "comfort_level",
    "effective_ac_on",
    "energy_config_mode",
    "energy_configured",
    "energy_kwh_total",
    "energy_power_unit",
    "energy_watts",
    "health",
    "humidity_band",
    "last_command",
    "pending_action",
    "pending_off_confirmation",
    "pending_remaining_seconds",
    "pending_since_ts",
    "physical_ac_on",
    "pre_cool_active",
    "pre_cool_requested_at",
    "pre_cool_until",
    "pre_cool_target",
    "pre_cool_reason",
    "pre_cool_result",
    "pre_cool_remaining_seconds",
    "pre_cool_trigger_source",
    "pre_cool_geofence_trigger_person",
    "pre_cool_started_at",
    "pre_cool_extension_count",
    "pre_cool_total_runtime_seconds",
    "pre_cool_snoozed_until",
    "pre_cool_suppressed_visit_id",
    "vacancy_off_blocked_reason",
    "runtime",
    "runtime_energy_mode",
    "session_kwh",
    "status",
    "telemetry_status",
    "watt_draw",
    "zone_status",
})

_ROOM_RUNTIME_TRANSIENT_KEYS = _RUNTIME_TRANSIENT_KEYS | frozenset({
    "effective_on_since_ts",
    "last_ac_on_at",
    "off_dispatched_at",
    "pending_off_sent_at",
    "pending_off_retry_count",
    "session_start_time",
    "session_state",
})

_ENERGY_FIELD_ALIASES = {
    "energy_power_entity": (
        "energy_sensor_entity",
        "power_sensor",
        "live_power_sensor",
        "live_power_entity",
        "power_sensor_entity",
    ),
    "energy_kwh_entity": (
        "energy_usage_sensor",
        "energy_meter_entity",
        "energy_usage_entity",
        "kwh_sensor",
        "kwh_sensor_entity",
        "energy_kwh_sensor",
    ),
    "energy_device_id": (
        "breaker_device_id",
        "circuit_breaker_device_id",
        "power_device_id",
        "energy_monitor_device_id",
    ),
    "energy_device_name": (
        "breaker_device_name",
        "circuit_breaker_name",
        "power_device_name",
        "energy_monitor_device_name",
    ),
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _energy_config_snapshot(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "energy_device_id": cfg.get("energy_device_id") or "",
        "energy_device_name": cfg.get("energy_device_name") or "",
        "energy_power_entity": cfg.get("energy_power_entity") or "",
        "energy_kwh_entity": cfg.get("energy_kwh_entity") or "",
    }


def _energy_config_log_summary(cfg: Dict[str, Any]) -> Dict[str, Any]:
    rooms = room_registry.list_room_dicts(cfg) if isinstance(cfg, dict) else []
    return {
        "rooms": len(rooms),
        "global_power_configured": bool(str(cfg.get("energy_power_entity") or "").strip())
        if isinstance(cfg, dict)
        else False,
        "global_kwh_configured": bool(str(cfg.get("energy_kwh_entity") or "").strip())
        if isinstance(cfg, dict)
        else False,
        "room_power_configured": sum(
            1 for room in rooms if str(room.get("energy_power_entity") or "").strip()
        ),
        "room_kwh_configured": sum(
            1 for room in rooms if str(room.get("energy_kwh_entity") or "").strip()
        ),
        "room_device_configured": sum(
            1 for room in rooms if str(room.get("energy_device_id") or "").strip()
        ),
    }


def _energy_config_log_signature(summary: Dict[str, Any]) -> tuple:
    return tuple(sorted(summary.items()))


def _log_loaded_config_summary_if_changed(cfg: Dict[str, Any]) -> None:
    global _last_logged_config_load_sig
    summary = _energy_config_log_summary(cfg)
    sig = _energy_config_log_signature(summary)
    if sig == _last_logged_config_load_sig:
        logger.debug("[ENERGY_CONFIG] load unchanged")
        return
    _last_logged_config_load_sig = sig
    logger.info("[ENERGY_CONFIG] loaded %s", summary)


def _normalize_energy_fields_in_place(cfg: Dict[str, Any]) -> None:
    if not isinstance(cfg, dict):
        return
    for canonical, aliases in _ENERGY_FIELD_ALIASES.items():
        if not str(cfg.get(canonical) or "").strip():
            for alias in aliases:
                raw = cfg.get(alias)
                if raw is not None and str(raw).strip():
                    cfg[canonical] = str(raw).strip()
                    break
        for alias in aliases:
            cfg.pop(alias, None)

    for key in _ENERGY_FIELD_ALIASES:
        if key in cfg and cfg[key] is not None:
            cfg[key] = str(cfg[key]).strip()


def _normalize_room_energy_fields(rooms: Any) -> None:
    if not isinstance(rooms, list):
        return
    for room in rooms:
        if not isinstance(room, dict):
            continue
        _normalize_energy_fields_in_place(room)
        settings = room.get("settings")
        if isinstance(settings, dict):
            _normalize_energy_fields_in_place(settings)
            for key in _ENERGY_FIELD_ALIASES:
                if key in settings and not str(room.get(key) or "").strip():
                    room[key] = settings.pop(key)


def _strip_transient_state_in_place(cfg: Dict[str, Any]) -> bool:
    """Remove runtime/UI state that must never be persisted as configuration."""
    if not isinstance(cfg, dict):
        return False
    changed = False
    for key in tuple(_RUNTIME_TRANSIENT_KEYS):
        if key in cfg:
            cfg.pop(key, None)
            changed = True
    rooms = cfg.get("rooms")
    if isinstance(rooms, list):
        for room in rooms:
            if not isinstance(room, dict):
                continue
            for key in tuple(_ROOM_RUNTIME_TRANSIENT_KEYS):
                if key in room:
                    room.pop(key, None)
                    changed = True
            settings = room.get("settings")
            if isinstance(settings, dict):
                for key in tuple(_ROOM_RUNTIME_TRANSIENT_KEYS):
                    if key in settings:
                        settings.pop(key, None)
                        changed = True
    return changed


def _normalize_manual_override_fields_in_place(cfg: Dict[str, Any]) -> bool:
    """
    Keep persisted Manual Override as durable user-authority config.

    `manual_override_enabled` is the canonical v4 field. The legacy
    `manual_override` flag is kept in sync so older UI/config payloads remain
    compatible and never silently unpause automation.
    """
    if not isinstance(cfg, dict):
        return False
    changed = False
    has_new = "manual_override_enabled" in cfg
    has_legacy = "manual_override" in cfg
    if has_new and has_legacy:
        enabled = bool(cfg.get("manual_override_enabled")) or bool(cfg.get("manual_override"))
    elif has_new:
        enabled = bool(cfg.get("manual_override_enabled"))
    else:
        enabled = bool(cfg.get("manual_override"))
    if cfg.get("manual_override_enabled") is not enabled:
        cfg["manual_override_enabled"] = enabled
        changed = True
    if cfg.get("manual_override") is not enabled:
        cfg["manual_override"] = enabled
        changed = True
    if enabled:
        started = cfg.get("override_started_at")
        if started in (None, ""):
            cfg["override_started_at"] = _utc_now_iso()
            changed = True
        if not isinstance(cfg.get("override_user_settings"), dict):
            cfg["override_user_settings"] = {}
            changed = True
    else:
        if cfg.get("override_started_at") not in (None, ""):
            cfg["override_started_at"] = None
            changed = True
        if cfg.get("override_user_settings") not in ({}, None):
            cfg["override_user_settings"] = {}
            changed = True
        elif cfg.get("override_user_settings") is None:
            cfg["override_user_settings"] = {}
            changed = True
    return changed


def _normalize_manual_override_tree_in_place(cfg: Dict[str, Any]) -> bool:
    changed = _normalize_manual_override_fields_in_place(cfg)
    rooms = cfg.get("rooms")
    if isinstance(rooms, list):
        for room in rooms:
            if not isinstance(room, dict):
                continue
            settings = room.get("settings")
            if isinstance(settings, dict):
                changed = _normalize_manual_override_fields_in_place(settings) or changed
    return changed


def _coerce_schema_version(raw: Any) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(n, CONFIG_SCHEMA_VERSION))


def migrate_v1_to_v2(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    v2 canonicalizes energy fields and promotes room-scoped energy aliases out of
    settings. It never validates against HA and never drops selected entities.
    """
    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    _normalize_energy_fields_in_place(out)
    _normalize_room_energy_fields(out.get("rooms"))
    out["schema_version"] = 2
    return out


def migrate_v2_to_v3(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    v3 makes persisted config authoritative by scrubbing transient runtime/status
    keys that can leak in from API payloads or UI caches.
    """
    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    _strip_transient_state_in_place(out)
    out["schema_version"] = 3
    return out


def migrate_v3_to_v4(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    v4 promotes Manual Override into explicit durable user-authority fields.
    Existing legacy `manual_override` values are preserved and mirrored.
    """
    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    _normalize_manual_override_tree_in_place(out)
    out["schema_version"] = 4
    return out


def _log_migration_once(from_version: int, to_version: int) -> None:
    key = (int(from_version), int(to_version))
    if key in _logged_migration_steps:
        return
    _logged_migration_steps.add(key)
    logger.info("[CONFIG] migration_applied from=%s to=%s", from_version, to_version)


def migrate_config(saved: Dict[str, Any], *, log_migrations: bool = True) -> Dict[str, Any]:
    """Apply idempotent migrations without consulting Home Assistant."""
    cfg = copy.deepcopy(saved) if isinstance(saved, dict) else {}
    if not cfg:
        cfg["schema_version"] = CONFIG_SCHEMA_VERSION
        return cfg
    original_version = _coerce_schema_version(cfg.get("schema_version"))
    version = original_version

    if version < 2:
        cfg = migrate_v1_to_v2(cfg)
        if log_migrations:
            _log_migration_once(version, 2)
        version = 2
    if version < 3:
        cfg = migrate_v2_to_v3(cfg)
        if log_migrations:
            _log_migration_once(version, 3)
        version = 3
    if version < 4:
        cfg = migrate_v3_to_v4(cfg)
        if log_migrations:
            _log_migration_once(version, 4)
        version = 4

    _strip_transient_state_in_place(cfg)
    _normalize_manual_override_tree_in_place(cfg)
    cfg["schema_version"] = CONFIG_SCHEMA_VERSION
    if original_version == CONFIG_SCHEMA_VERSION:
        logger.debug("[CONFIG] migration_check schema_version=%s", CONFIG_SCHEMA_VERSION)
    return cfg


def _sanitize_energy_entities_in_place(cfg: Dict[str, Any], *, room_id: str = "global") -> bool:
    """Legacy compatibility wrapper: preserve entity ids; only trim device ids."""
    changed = False
    for key, kind in (
        ("energy_power_entity", "power"),
        ("energy_kwh_entity", "energy"),
    ):
        entity_id = str(cfg.get(key) or "").strip()
        if not entity_id:
            continue
        reason = static_energy_entity_rejection_reason(entity_id, kind=kind)
        if not reason:
            continue
        logger.warning(
            "[CONFIG] preserved_entity despite unavailable state room=%s field=%s entity=%s reason=%s",
            room_id,
            key,
            entity_id,
            reason,
        )

    device_id = str(cfg.get("energy_device_id") or "").strip()
    if cfg.get("energy_device_id") != device_id:
        cfg["energy_device_id"] = device_id
        changed = True
    return changed


def sanitize_energy_entities(config: Dict[str, Any]) -> Dict[str, Any]:
    """Compatibility helper that preserves saved entity ids while normalizing whitespace."""
    clean = copy.deepcopy(config) if isinstance(config, dict) else {}
    _sanitize_energy_entities_in_place(clean)
    rooms = clean.get("rooms")
    if isinstance(rooms, list):
        for room in rooms:
            if isinstance(room, dict):
                _sanitize_energy_entities_in_place(
                    room,
                    room_id=str(room.get("id") or room.get("name") or "unknown"),
                )
    return clean


def consume_energy_sanitized_load_flag() -> bool:
    global _last_load_sanitized_energy_entities
    value = _last_load_sanitized_energy_entities
    _last_load_sanitized_energy_entities = False
    return value


def _read_json_dict(path: str) -> Dict[str, Any]:
    """Read JSON object from path; return {} if missing / invalid."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return raw
    except json.JSONDecodeError as e:
        logger.error("[HawaAI] Invalid JSON at %s: %s", path, e)
        return {}
    except OSError as e:
        logger.error("[HawaAI] Cannot read %s: %s", path, e)
        return {}


def _backup_config_paths_newest_first() -> list:
    paths: list = []
    for p in glob.glob("/data/hawaai_backup*.json"):
        paths.append(p)
    # Optional static backup alongside primary config
    bak = "/data/hawaai_config.json.bak"
    if os.path.isfile(bak):
        paths.append(bak)
    for p in glob.glob("/data/hawaai_backup*.yaml"):
        paths.append(p)
    for p in glob.glob("/data/hawaai_backup*.yml"):
        paths.append(p)
    # Newest first (best effort recover)
    def _mtime(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    paths.sort(key=_mtime, reverse=True)
    return paths


def _read_config_dict_from_backup_path(path: str) -> Dict[str, Any]:
    """JSON or YAML (if PyYAML not available, skip yaml)."""
    low = path.lower()
    if low.endswith(".json"):
        return _read_json_dict(path)
    if low.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError:
            logger.warning("[HawaAI] Skipping YAML backup %s — PyYAML not installed", path)
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            return raw if isinstance(raw, dict) else {}
        except Exception as e:
            logger.error("[HawaAI] Failed to read YAML backup %s: %s", path, e)
            return {}
    return {}


def _write_json_dict(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def _assemble_merged_config(
    saved: Dict[str, Any],
    options: Dict[str, Any],
    *,
    log_migrations: bool = True,
) -> Dict[str, Any]:
    """
    Deep-copy defaults, then layer supervisor options and saved UI config.
    Never mutate DEFAULT_CONFIG in place.
    """
    saved_migrated = migrate_config(saved, log_migrations=log_migrations)

    merged: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
    merged.update(options or {})
    merged.update(saved_migrated or {})
    merged["schema_version"] = CONFIG_SCHEMA_VERSION

    merged["timezone"] = validate_timezone_optional(merged.get("timezone"))

    for _k in _LEGACY_IR_KEYS:
        merged.pop(_k, None)

    _normalize_energy_fields_in_place(merged)
    _normalize_room_energy_fields(merged.get("rooms"))
    _strip_transient_state_in_place(merged)
    _normalize_manual_override_tree_in_place(merged)

    if merged.get("ai_enabled") is None:
        merged["ai_enabled"] = False

    _prov = (str(merged.get("ai_provider") or "ollama")).strip().lower()
    merged["ai_provider"] = "api" if _prov == "api" else "ollama"

    try:
        _to = int(merged.get("ai_api_timeout", 60))
    except (TypeError, ValueError):
        _to = 60
    merged["ai_api_timeout"] = max(5, min(120, _to))

    if merged.get("ai_api_json_object_format") is None:
        merged["ai_api_json_object_format"] = False
    else:
        merged["ai_api_json_object_format"] = bool(merged.get("ai_api_json_object_format"))

    _ou_stripped = (str(merged.get("ai_ollama_url") or "")).strip()
    if merged["ai_provider"] == "ollama":
        if not _ou_stripped:
            merged["ai_ollama_url"] = (str(DEFAULT_CONFIG.get("ai_ollama_url") or "")).strip()
        else:
            merged["ai_ollama_url"] = _ou_stripped
    else:
        merged["ai_ollama_url"] = _ou_stripped

    _ace = (merged.get("ac_entity") or merged.get("climate_entity") or "").strip()
    merged["ac_entity"] = _ace
    merged["climate_entity"] = _ace

    room_registry.ensure_migrated(merged)
    _normalize_manual_override_tree_in_place(merged)
    merged["schema_version"] = CONFIG_SCHEMA_VERSION
    return merged


def persist_migrated_config_if_needed() -> bool:
    """
    Persist an upgraded primary config exactly once after migration.
    The write is schema-only/normalization-preserving and never consults HA state.
    """
    try:
        saved = _read_json_dict(CONFIG_PATH)
        if not saved:
            return False
        saved_version = _coerce_schema_version(saved.get("schema_version"))
        if saved_version >= CONFIG_SCHEMA_VERSION:
            return False

        opts = _read_json_dict("/data/options.json")
        upgraded = _assemble_merged_config(saved, opts, log_migrations=False)
        upgraded["schema_version"] = CONFIG_SCHEMA_VERSION
        _write_json_dict(CONFIG_PATH, upgraded)
        logger.debug("[CONFIG] schema_version=%s persisted=true", CONFIG_SCHEMA_VERSION)
        return True
    except Exception:
        logger.exception("[CONFIG] migration_persist_failed path=%s", CONFIG_PATH)
        return False


def load_config() -> Dict[str, Any]:
    """
    Load merged config. On primary failure: try backup files, then last in-memory snapshot.
    Only uses DEFAULT_CONFIG alone if nothing else is recoverable (logged CRITICAL).
    """
    global _last_known_good_config

    def _remember(merged: Dict[str, Any]) -> Dict[str, Any]:
        global _last_known_good_config
        _last_known_good_config = copy.deepcopy(merged)
        return merged

    try:
        saved = _read_json_dict(CONFIG_PATH)
        opts = _read_json_dict("/data/options.json")
        merged = _assemble_merged_config(saved, opts)
        _log_loaded_config_summary_if_changed(merged)
        return _remember(merged)

    except Exception:
        logger.exception(
            "[HawaAI] CRITICAL: Primary config assembly failed (%s) — attempting backup files",
            CONFIG_PATH,
        )

    opts_sup = _read_json_dict("/data/options.json")

    for bpath in _backup_config_paths_newest_first():
        try:
            raw = _read_config_dict_from_backup_path(bpath)
            if not raw:
                continue
            merged = _assemble_merged_config(raw, opts_sup)
            logger.error("[HawaAI] Recovered configuration from backup file: %s", bpath)
            return _remember(merged)
        except Exception:
            logger.warning("[HawaAI] Backup config unusable: %s", bpath, exc_info=True)

    if _last_known_good_config is not None:
        logger.critical(
            "[HawaAI] CRITICAL: Disk config failed — serving last known good in-memory config "
            "(rooms and settings preserved until primary file is fixed).",
        )
        return copy.deepcopy(_last_known_good_config)

    logger.critical(
        "[HawaAI] CRITICAL: No valid config on disk, no usable backup, no in-memory cache — "
        "using DEFAULT_CONFIG (empty rooms). Repair %s or restore a backup.",
        CONFIG_PATH,
    )
    merged = _assemble_merged_config({}, opts_sup)
    _last_known_good_config = copy.deepcopy(merged)
    return merged


def save_config(data: Dict[str, Any]) -> bool:
    """Write config to /data/ which persists across HA addon restarts."""
    try:
        data = copy.deepcopy(data)
        data = {k: v for k, v in data.items() if k not in _LEGACY_IR_KEYS}
        _strip_transient_state_in_place(data)
        logger.info("[ENERGY_CONFIG] received %s", _energy_config_log_summary(data))
        _normalize_energy_fields_in_place(data)
        _normalize_room_energy_fields(data.get("rooms"))
        logger.info("[ENERGY_CONFIG] normalized %s", _energy_config_log_summary(data))
        current = load_config()
        current.update(data)
        current = migrate_config(current)
        _normalize_energy_fields_in_place(current)
        _normalize_room_energy_fields(current.get("rooms"))
        _strip_transient_state_in_place(current)
        _normalize_manual_override_tree_in_place(current)
        current["schema_version"] = CONFIG_SCHEMA_VERSION
        current["timezone"] = validate_timezone_optional(current.get("timezone"))
        _ace = (current.get("ac_entity") or current.get("climate_entity") or "").strip()
        current["ac_entity"] = _ace
        current["climate_entity"] = _ace
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        _write_json_dict(CONFIG_PATH, current)
        logger.info("[ENERGY_CONFIG] persisted %s", _energy_config_log_summary(current))
        logger.info("[HawaAI] Config saved to %s", CONFIG_PATH)
        return True
    except Exception as e:
        logger.error("[HawaAI] Failed to save config: %s", e)
        return False


# Aliases for backward compatibility with any code still using old API
def get(key: str, default: Any = None) -> Any:
    return load_config().get(key, default)


def get_all() -> Dict[str, Any]:
    return load_config()


def update(patch: Dict[str, Any]) -> Dict[str, Any]:
    save_config(patch)
    return load_config()


def load() -> Dict[str, Any]:
    return load_config()


def reload() -> Dict[str, Any]:
    return load_config()
