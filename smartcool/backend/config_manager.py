"""Read and write add-on configuration from /data/hawaai_config.json."""

import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

CONFIG_PATH = "/data/hawaai_config.json"

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
    "vacancy_timeout_minutes": 5,
    "use_presence": True,
    "use_outdoor_temp": True,
    "smart_temp_adjustment": True,   # raise/lower effective target based on outdoor temp
    "smart_cooling_enabled": False,  # fan boost/normal via climate entity when enabled
    "manual_override": False,
    "weather_api_key": "",
    "weather_city": "",
    "weather_provider": "openweathermap",
    "energy_tariff_per_kwh": 8.0,
    "currency": "INR",
    "logic_interval_seconds": 60,
}


def load_config() -> Dict[str, Any]:
    """Always read fresh from disk. Merges defaults so new keys always have values."""
    # First try the persisted UI config
    saved: Dict[str, Any] = {}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
    except Exception as e:
        logger.error("[HawaAI] Failed to load config: %s", e)

    # Also layer in /data/options.json written by HA supervisor (lower priority)
    options: Dict[str, Any] = {}
    try:
        options_path = "/data/options.json"
        if os.path.exists(options_path):
            with open(options_path, "r", encoding="utf-8") as f:
                options = json.load(f)
    except Exception:
        pass

    # Merge: defaults < supervisor options < persisted UI config
    merged = {**DEFAULT_CONFIG, **options, **saved}

    # Drop legacy Broadlink / IR keys — old JSON may still contain them; never used.
    for _k in _LEGACY_IR_KEYS:
        merged.pop(_k, None)

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
