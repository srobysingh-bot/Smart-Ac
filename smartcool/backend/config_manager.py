"""Read and write add-on configuration from /data/options.json."""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

OPTIONS_PATH = Path("/data/options.json")

# Defaults — must mirror config.yaml options block
_DEFAULTS: Dict[str, Any] = {
    "ha_token": "",
    "weather_api_key": "",
    "weather_city": "",
    "weather_provider": "openweathermap",
    "target_temp": 24,
    "hysteresis": 1.5,
    "vacancy_timeout_minutes": 5,
    "energy_tariff_per_kwh": 8.0,
    "logic_interval_seconds": 60,
    "currency": "INR",
    "presence_entity": "",
    "indoor_temp_entity": "",
    "ac_switch_entity": "",
    "energy_sensor_entity": "",
    "broadlink_entity": "",
    "ac_brand": "",
    "ac_model": "",
    "room_name": "Living Room",
    "use_presence": True,
    "use_outdoor_temp": True,
    "manual_override": False,
}

_cache: Dict[str, Any] = {}


def load() -> Dict[str, Any]:
    """Load options.json, falling back to defaults for missing keys."""
    global _cache
    try:
        if OPTIONS_PATH.exists():
            raw = OPTIONS_PATH.read_text(encoding="utf-8")
            data = json.loads(raw)
        else:
            logger.warning("options.json not found — using defaults")
            data = {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read options.json: %s", exc)
        data = {}

    _cache = {**_DEFAULTS, **data}
    return _cache


def get(key: str, default: Any = None) -> Any:
    """Return a single config value (lazy-loads on first access)."""
    if not _cache:
        load()
    return _cache.get(key, default)


def get_all() -> Dict[str, Any]:
    """Return a copy of the full config dict."""
    if not _cache:
        load()
    return dict(_cache)


def update(patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge *patch* into the current config and persist to options.json.

    HA will override options.json on add-on restart, so this is useful
    for runtime toggles (e.g. manual_override) that the UI can change
    without a full add-on restart.
    """
    if not _cache:
        load()

    # Validate only known keys are being set
    unknown = set(patch) - set(_DEFAULTS)
    if unknown:
        logger.warning("Ignoring unknown config keys: %s", unknown)
        patch = {k: v for k, v in patch.items() if k in _DEFAULTS}

    _cache.update(patch)

    try:
        OPTIONS_PATH.write_text(
            json.dumps(_cache, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Config saved to %s", OPTIONS_PATH)
    except OSError as exc:
        logger.error("Failed to write options.json: %s", exc)

    return dict(_cache)


def reload() -> Dict[str, Any]:
    """Force-reload from disk (used after add-on config change in HA UI)."""
    global _cache
    _cache = {}
    return load()
