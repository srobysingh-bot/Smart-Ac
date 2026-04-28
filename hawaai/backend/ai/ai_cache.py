"""Rate-limit AI calls; store last validated result per room."""

from __future__ import annotations

import logging
import random
import time
from collections import defaultdict
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_MIN_INTERVAL = 600.0
_MAX_INTERVAL = 900.0
_TEMP_REFETCH_DELTA = 1.0
_DEFER_STABLE_S = 300.0
_STARTUP_BLOCK_S = 60.0
_THROTTLE_LOG_SEC = 300.0


def _new_store() -> Dict[str, Any]:
    boot = time.monotonic()
    return {
        "_last_valid": None,
        "_last_ai_identity": None,
        "_context_indoor": None,
        "_last_tick_occupied": None,
        "_next_fetch_at": boot + _STARTUP_BLOCK_S,
        "_last_cache_info_log": 0.0,
    }


_stores: Dict[str, Dict[str, Any]] = defaultdict(_new_store)


def _s(room_id: str) -> Dict[str, Any]:
    return _stores[room_id]


def _roll_next_interval() -> float:
    return time.monotonic() + _MIN_INTERVAL + random.random() * (_MAX_INTERVAL - _MIN_INTERVAL)


def should_run_ai(
    room_id: str,
    cfg: Dict[str, Any],
    is_occupied: bool,
    indoor_temp: float,
) -> bool:
    st = _s(room_id)
    if not bool(cfg.get("ai_enabled", False)) or not is_occupied:
        st["_last_tick_occupied"] = is_occupied
        return False

    now = time.monotonic()
    if now < st["_next_fetch_at"]:
        st["_last_tick_occupied"] = is_occupied
        return False

    became_occupied = st["_last_tick_occupied"] is False and is_occupied
    st["_last_tick_occupied"] = is_occupied

    if st["_last_valid"] is None:
        return True

    if st["_context_indoor"] is None:
        return True

    if became_occupied:
        return True

    if abs(float(indoor_temp) - float(st["_context_indoor"])) >= _TEMP_REFETCH_DELTA:
        return True

    st["_next_fetch_at"] = now + _DEFER_STABLE_S
    logger.debug("[AI][%s] Skipped fetch: stable room — using cache", room_id)
    return False


def mark_fetch_done(room_id: str) -> None:
    st = _s(room_id)
    st["_next_fetch_at"] = _roll_next_interval()


def get_cached(room_id: str) -> Optional[Dict[str, Any]]:
    return _s(room_id)["_last_valid"]


def set_validated(room_id: str, v: Dict[str, Any], indoor_temp: Optional[float] = None) -> None:
    st = _s(room_id)
    st["_last_valid"] = v
    if indoor_temp is not None:
        st["_context_indoor"] = float(indoor_temp)


def invalidate_if_ai_identity_changed(room_id: str, provider: str, resolved_model: str) -> None:
    st = _s(room_id)
    p = (provider or "ollama").strip().lower()
    if p not in ("ollama", "api"):
        p = "ollama"
    r = f"{p}:{(resolved_model or '').strip()}"
    if st["_last_ai_identity"] is not None and r != st["_last_ai_identity"]:
        st["_last_valid"] = None
        st["_context_indoor"] = None
        st["_next_fetch_at"] = 0.0
        logger.debug("[AI][%s] Provider/model changed — cache invalidated", room_id)
    st["_last_ai_identity"] = r


def invalidate_if_ollama_model_changed(room_id: str, resolved_model: str) -> None:
    invalidate_if_ai_identity_changed(room_id, "ollama", resolved_model)


def throttle_cache_use_log(room_id: str) -> bool:
    st = _s(room_id)
    now = time.monotonic()
    if now - st["_last_cache_info_log"] >= _THROTTLE_LOG_SEC:
        st["_last_cache_info_log"] = now
        return True
    return False
