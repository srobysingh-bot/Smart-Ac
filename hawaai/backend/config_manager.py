"""Read and write add-on configuration from /data/hawaai_config.json."""

import copy
import glob
import json
import logging
import os
from typing import Any, Dict, Optional

from .energy_config import static_energy_entity_rejection_reason
from . import room_registry
from .temperature_schedule import validate_timezone_optional

logger = logging.getLogger(__name__)

CONFIG_PATH = "/data/hawaai_config.json"

# Last successful merged config (survives single bad load so rooms are not silently wiped).
_last_known_good_config: Optional[Dict[str, Any]] = None
_last_load_sanitized_energy_entities = False

# Default matches Ollama add-on pull (gemma:2b); override in Settings if needed.
DEFAULT_OLLAMA_MODEL = "gemma:2b"

# Legacy keys from pre–climate-only installs — stripped from merged config (ignored safely).
_LEGACY_IR_KEYS = frozenset({
    "broadlink_entity", "ir_device_name", "ir_command_on", "ir_command_off",
})

DEFAULT_CONFIG: Dict[str, Any] = {
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
    "vacancy_timeout_minutes": 5,
    "presence_only_on_dwell_seconds": 20,
    "presence_only_max_runtime_minutes": 240,
    "thermostat_on_delta_deg": 0.7,
    "thermostat_off_delta_deg": 0.3,
    "user_authority_lock_secs": 120,
    "snapshot_interval_secs": 60,
    "use_presence": True,
    "use_outdoor_temp": True,
    "smart_temp_adjustment": True,
    "smart_cooling_enabled": False,
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


def _energy_config_snapshot(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "energy_device_id": cfg.get("energy_device_id") or "",
        "energy_device_name": cfg.get("energy_device_name") or "",
        "energy_power_entity": cfg.get("energy_power_entity") or "",
        "energy_kwh_entity": cfg.get("energy_kwh_entity") or "",
    }


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


def _sanitize_energy_entities_in_place(cfg: Dict[str, Any], *, room_id: str = "global") -> bool:
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
            "[ENERGY_VALIDATE] room=%s entity=%s reason=%s action=clear_saved_field",
            room_id,
            entity_id,
            reason,
        )
        cfg[key] = ""
        changed = True

    device_id = str(cfg.get("energy_device_id") or "").strip()
    if cfg.get("energy_device_id") != device_id:
        cfg["energy_device_id"] = device_id
        changed = True
    return changed


def sanitize_energy_entities(config: Dict[str, Any]) -> Dict[str, Any]:
    """Clear statically-invalid saved energy entity ids without touching valid fields."""
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
        clean = sanitize_energy_entities(raw)
        global _last_load_sanitized_energy_entities
        if clean != raw:
            _last_load_sanitized_energy_entities = True
        return clean
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


def _assemble_merged_config(saved: Dict[str, Any], options: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deep-copy defaults, then layer supervisor options and saved UI config.
    Never mutate DEFAULT_CONFIG in place.
    """
    merged: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
    merged.update(options or {})
    merged.update(saved or {})

    merged["timezone"] = validate_timezone_optional(merged.get("timezone"))

    for _k in _LEGACY_IR_KEYS:
        merged.pop(_k, None)

    _normalize_energy_fields_in_place(merged)
    _normalize_room_energy_fields(merged.get("rooms"))

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
    return merged


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
        logger.info(
            "[ENERGY_CONFIG] loaded_from_disk=%s",
            {
                "global": _energy_config_snapshot(merged),
                "rooms": [
                    {
                        "id": r.get("id"),
                        **_energy_config_snapshot(r),
                    }
                    for r in room_registry.list_room_dicts(merged)
                ],
            },
        )
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
        logger.info(
            "[ENERGY_CONFIG] received_payload=%s",
            {
                "global": _energy_config_snapshot(data),
                "rooms": [
                    {
                        "id": r.get("id"),
                        **_energy_config_snapshot(r),
                    }
                    for r in data.get("rooms", [])
                    if isinstance(r, dict)
                ],
            },
        )
        _normalize_energy_fields_in_place(data)
        _normalize_room_energy_fields(data.get("rooms"))
        logger.info(
            "[ENERGY_CONFIG] normalized=%s",
            {
                "global": _energy_config_snapshot(data),
                "rooms": [
                    {
                        "id": r.get("id"),
                        **_energy_config_snapshot(r),
                    }
                    for r in data.get("rooms", [])
                    if isinstance(r, dict)
                ],
            },
        )
        current = load_config()
        current.update(data)
        _normalize_energy_fields_in_place(current)
        _normalize_room_energy_fields(current.get("rooms"))
        current["timezone"] = validate_timezone_optional(current.get("timezone"))
        _ace = (current.get("ac_entity") or current.get("climate_entity") or "").strip()
        current["ac_entity"] = _ace
        current["climate_entity"] = _ace
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
        logger.info(
            "[ENERGY_CONFIG] persisted=%s",
            {
                "global": _energy_config_snapshot(current),
                "rooms": [
                    {
                        "id": r.get("id"),
                        **_energy_config_snapshot(r),
                    }
                    for r in room_registry.list_room_dicts(current)
                ],
            },
        )
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
