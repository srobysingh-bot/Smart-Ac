"""Rate-limit AI calls; store last validated result."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Call interval: 10–15 minutes (seconds) after a completed fetch
_MIN_INTERVAL = 600.0
_MAX_INTERVAL = 900.0

# Only refetch when indoor moved this much vs last successful AI context (°C), or occupancy edge
_TEMP_REFETCH_DELTA = 1.0
# When cache is still "valid" but timer fired — check again later (seconds)
_DEFER_STABLE_S = 300.0

_last_valid: Optional[Dict[str, Any]] = None
_last_ollama_model: Optional[str] = None
_context_indoor: Optional[float] = None
_last_tick_occupied: Optional[bool] = None

_next_fetch_at: float = 0.0
_last_cache_info_log: float = 0.0
_boot = time.monotonic()
_STARTUP_BLOCK_S = 60.0
_next_fetch_at = _boot + _STARTUP_BLOCK_S

_THROTTLE_LOG_SEC = 300.0


def _roll_next_interval() -> float:
    return time.monotonic() + _MIN_INTERVAL + random.random() * (_MAX_INTERVAL - _MIN_INTERVAL)


def should_run_ai(
    cfg: Dict[str, Any], is_occupied: bool, indoor_temp: float,
) -> bool:
    """
    Gate Ollama calls: time window + need new context (ΔT ≥ 1°C, vacant→occupied edge, or no cache).
    Skips fetch if a prior decision still fits a stable room (defers next check).
    """
    global _last_tick_occupied, _next_fetch_at

    if not bool(cfg.get("ai_enabled", False)) or not is_occupied:
        _last_tick_occupied = is_occupied
        return False

    now = time.monotonic()
    if now < _next_fetch_at:
        _last_tick_occupied = is_occupied
        return False

    became_occupied = _last_tick_occupied is False and is_occupied
    _last_tick_occupied = is_occupied

    if _last_valid is None:
        return True

    if _context_indoor is None:
        return True

    if became_occupied:
        return True

    if abs(float(indoor_temp) - float(_context_indoor)) >= _TEMP_REFETCH_DELTA:
        return True

    _next_fetch_at = now + _DEFER_STABLE_S
    logger.debug("[AI] Skipped fetch: stable room — using cache")
    return False


def mark_fetch_done() -> None:
    global _next_fetch_at
    _next_fetch_at = _roll_next_interval()


def get_cached() -> Optional[Dict[str, Any]]:
    return _last_valid


def set_validated(v: Dict[str, Any], indoor_temp: Optional[float] = None) -> None:
    global _last_valid, _context_indoor
    _last_valid = v
    if indoor_temp is not None:
        _context_indoor = float(indoor_temp)


def invalidate_if_ollama_model_changed(resolved_model: str) -> None:
    """Clear cached AI output when the configured Ollama model changes (UI vs backend stay aligned)."""
    global _last_ollama_model, _last_valid, _next_fetch_at, _context_indoor
    r = (resolved_model or "").strip()
    if _last_ollama_model is not None and r != _last_ollama_model:
        _last_valid = None
        _context_indoor = None
        _next_fetch_at = 0.0
        logger.debug("[AI] Ollama model changed — cache invalidated")
    _last_ollama_model = r


def throttle_cache_use_log() -> bool:
    """True if we should log cache use (max once / 5 min)."""
    global _last_cache_info_log
    now = time.monotonic()
    if now - _last_cache_info_log >= _THROTTLE_LOG_SEC:
        _last_cache_info_log = now
        return True
    return False
