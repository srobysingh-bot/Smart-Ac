"""Read and write add-on configuration from /data/options.json."""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

OPTIONS_PATH = Path("/data/options.json")
PERSIST_PATH = Path("/data/hawaai_config.json")

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

    # Also load any runtime-saved config from /data which persists across restarts
    try:
        if PERSIST_PATH.exists():
            raw_p = PERSIST_PATH.read_text(encoding="utf-8")
            data_p = json.loads(raw_p)
        else:
            data_p = {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read persisted config %s: %s", PERSIST_PATH, exc)
        data_p = {}

    # Merge: defaults <- supervisor options.json <- runtime persisted config
    _cache = {**_DEFAULTS, **data, **data_p}
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

    # Persist runtime changes to a dedicated file under /data so they survive restarts
    try:
        PERSIST_PATH.write_text(
            json.dumps(_cache, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Runtime config saved to %s", PERSIST_PATH)
    except OSError as exc:
        logger.error("Failed to write persisted config %s: %s", PERSIST_PATH, exc)

    return dict(_cache)


def reload() -> Dict[str, Any]:
    """Force-reload from disk (used after add-on config change in HA UI)."""
    global _cache
    _cache = {}
    return load()


def load_config() -> Dict[str, Any]:
    """Explicit helper to reload configuration from disk and return it.

    Use this from long-running components to pick up runtime changes.
    """
    return reload()
