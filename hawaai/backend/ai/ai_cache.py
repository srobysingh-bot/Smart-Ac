"""Rate-limit AI calls; store last validated result."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Call interval: 10–15 minutes (seconds)
_MIN_INTERVAL = 600.0
_MAX_INTERVAL = 900.0

_last_valid: Optional[Dict[str, Any]] = None
_last_ollama_model: Optional[str] = None
# No AI for first 60s after this process start (Ollama model load / addon settle; Raspberry Pi safe)
_next_fetch_at: float = 0.0
_last_cache_info_log: float = 0.0
_boot = time.monotonic()
_STARTUP_BLOCK_S = 60.0
_next_fetch_at = _boot + _STARTUP_BLOCK_S

_THROTTLE_LOG_SEC = 300.0


def _roll_next_interval() -> float:
    return time.monotonic() + _MIN_INTERVAL + random.random() * (_MAX_INTERVAL - _MIN_INTERVAL)


def should_run_ai(cfg: Dict[str, Any], is_occupied: bool) -> bool:
    if not bool(cfg.get("ai_enabled", False)) or not is_occupied:
        return False
    return time.monotonic() >= _next_fetch_at


def mark_fetch_done() -> None:
    global _next_fetch_at
    _next_fetch_at = _roll_next_interval()


def get_cached() -> Optional[Dict[str, Any]]:
    return _last_valid


def set_validated(v: Dict[str, Any]) -> None:
    global _last_valid
    _last_valid = v


def invalidate_if_ollama_model_changed(resolved_model: str) -> None:
    """Clear cached AI output when the configured Ollama model changes (UI vs backend stay aligned)."""
    global _last_ollama_model, _last_valid, _next_fetch_at
    r = (resolved_model or "").strip()
    if _last_ollama_model is not None and r != _last_ollama_model:
        _last_valid = None
        _next_fetch_at = 0.0
        logger.info("[AI] Ollama model changed — cache invalidated")
    _last_ollama_model = r


def throttle_cache_use_log() -> bool:
    """True if we should log [AI] Cached used (max once / 5 min)."""
    global _last_cache_info_log
    now = time.monotonic()
    if now - _last_cache_info_log >= _THROTTLE_LOG_SEC:
        _last_cache_info_log = now
        return True
    return False
