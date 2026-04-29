"""Read and write add-on configuration from /data/hawaai_config.json."""

import json
import logging
import os
from typing import Any, Dict

from . import room_registry

logger = logging.getLogger(__name__)

CONFIG_PATH = "/data/hawaai_config.json"

# Default matches Ollama add-on pull (gemma:2b); override in Settings if needed.
DEFAULT_OLLAMA_MODEL = "gemma:2b"

# Legacy keys from pre–climate-only installs — stripped from merged config (ignored safely).
_LEGACY_IR_KEYS = frozenset({
    "broadlink_entity", "ir_device_name", "ir_command_on", "ir_command_off",
})

DEFAULT_CONFIG: Dict[str, Any] = {
    "presence_entity": "",
    "indoor_temp_entity": "",
    # Primary AC entity (Aerostate). Synced to climate_entity for engine/API compatibility.
    "ac_entity": "",
    "climate_entity": "",
    "energy_power_entity": "",   # live watts sensor  (e.g. sensor.study_sensor_power)
    "energy_kwh_entity": "",     # cumulative kWh sensor (e.g. sensor.study_sensor_power_usage)
    "ac_brand": "",
    "ac_model": "",
    "room_name": "Living Room",
    "target_temp": 24,
    "hysteresis": 1.5,
    # Control band: ON above target+half, OFF below target−half (°C each side)
    "control_hysteresis_half_deg": 0.5,
    "min_command_interval_seconds": 150,
    "manual_override_duration_minutes": 30,
    "manual_override_detect_delta_deg": 0.5,
    "manual_override_exit_within_deg": 0.5,
    "meaningful_setpoint_delta_deg": 0.5,
    "compressor_min_on_seconds": 300,
    "compressor_min_off_seconds": 180,
    "vacancy_timeout_minutes": 5,
    "use_presence": True,
    "use_outdoor_temp": True,
    "smart_temp_adjustment": True,   # raise/lower effective target based on outdoor temp
    "smart_cooling_enabled": False,  # fan boost/normal via climate entity when enabled
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


def load_config() -> Dict[str, Any]:
    """Always read fresh from disk. Merges defaults so new keys always have values."""
    try:
        return _load_config_merged()
    except Exception:
        logger.exception("[HawaAI] Config load failed — using DEFAULT_CONFIG")
        return dict(DEFAULT_CONFIG)


def _load_config_merged() -> Dict[str, Any]:
    """Merge disk + supervisor options + defaults. Raises on unexpected data; caller wraps."""
    # First try the persisted UI config
    saved: Dict[str, Any] = {}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                saved = raw
            else:
                logger.error("[HawaAI] Config file is not a JSON object — ignoring")
    except json.JSONDecodeError as e:
        logger.error("[HawaAI] Failed to parse config JSON: %s", e)
    except Exception as e:
        logger.error("[HawaAI] Failed to load config: %s", e)

    # Also layer in /data/options.json written by HA supervisor (lower priority)
    options: Dict[str, Any] = {}
    try:
        options_path = "/data/options.json"
        if os.path.exists(options_path):
            with open(options_path, "r", encoding="utf-8") as f:
                raw_o = json.load(f)
            if isinstance(raw_o, dict):
                options = raw_o
    except Exception:
        pass

    # Merge: defaults < supervisor options < persisted UI config
    # Upgrade-safe: new keys (e.g. ai_enabled, ai_ollama_url) appear without wiping user data.
    merged: Dict[str, Any] = {**DEFAULT_CONFIG, **options, **saved}

    # Drop legacy Broadlink / IR keys — old JSON may still contain them; never used.
    for _k in _LEGACY_IR_KEYS:
        merged.pop(_k, None)

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

    # Ollama URL: only apply default when using Ollama. API mode does not assume Ollama exists.
    _ou_stripped = (str(merged.get("ai_ollama_url") or "")).strip()
    if merged["ai_provider"] == "ollama":
        if not _ou_stripped:
            merged["ai_ollama_url"] = (str(DEFAULT_CONFIG.get("ai_ollama_url") or "")).strip()
        else:
            merged["ai_ollama_url"] = _ou_stripped
    else:
        merged["ai_ollama_url"] = _ou_stripped

    # Single AC entity: ac_entity wins, else climate_entity (supervisor / old saves).
    _ace = (merged.get("ac_entity") or merged.get("climate_entity") or "").strip()
    merged["ac_entity"] = _ace
    merged["climate_entity"] = _ace

    # Migration: rename legacy energy_sensor_entity → energy_power_entity
    if "energy_sensor_entity" in merged and "energy_power_entity" not in merged:
        merged["energy_power_entity"] = merged.pop("energy_sensor_entity")
        merged.setdefault("energy_kwh_entity", "")
    elif "energy_sensor_entity" in merged:
        merged.pop("energy_sensor_entity", None)

    room_registry.ensure_migrated(merged)

    return merged


def save_config(data: Dict[str, Any]) -> bool:
    """Write config to /data/ which persists across HA addon restarts."""
    try:
        data = {k: v for k, v in data.items() if k not in _LEGACY_IR_KEYS}
        current = load_config()
        current.update(data)
        _ace = (current.get("ac_entity") or current.get("climate_entity") or "").strip()
        current["ac_entity"] = _ace
        current["climate_entity"] = _ace
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
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
