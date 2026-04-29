"""Rate-limit AI calls; store last validated result per room."""

from __future__ import annotations

import logging
import random
import time
from collections import defaultdict
from typing import Any, Dict, Optional, Tuple

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
        "_last_ai_invoke_mono": None,
    }


_stores: Dict[str, Dict[str, Any]] = defaultdict(_new_store)


def _s(room_id: str) -> Dict[str, Any]:
    return _stores[room_id]


def _roll_next_interval() -> float:
    return time.monotonic() + _MIN_INTERVAL + random.random() * (_MAX_INTERVAL - _MIN_INTERVAL)


def mark_ai_infer_scheduled(room_id: str, cfg: Dict[str, Any]) -> None:
    """Record dispatch time — enforces minimum wall time between outbound AI calls."""
    st = _s(room_id)
    now_m = time.monotonic()
    st["_last_ai_invoke_mono"] = now_m
    try:
        min_gap = float(cfg.get("ai_fetch_min_interval_seconds", 60) or 60)
    except (TypeError, ValueError):
        min_gap = 60.0
    min_gap = max(10.0, min(min_gap, 3600.0))
    nf = float(st.get("_next_fetch_at", 0.0))
    st["_next_fetch_at"] = max(nf, now_m + min_gap)


def should_run_ai(
    room_id: str,
    cfg: Dict[str, Any],
    is_occupied: bool,
    indoor_temp: float,
    *,
    control_base_temp: Optional[float] = None,
) -> bool:
    st = _s(room_id)
    if not bool(cfg.get("ai_enabled", False)) or not is_occupied:
        st["_last_tick_occupied"] = is_occupied
        return False

    became_occupied = st["_last_tick_occupied"] is False and is_occupied

    now = time.monotonic()

    try:
        min_invoke = float(cfg.get("ai_fetch_min_interval_seconds", 60) or 60)
    except (TypeError, ValueError):
        min_invoke = 60.0
    min_invoke = max(10.0, min(min_invoke, 3600.0))
    last_inv = st.get("_last_ai_invoke_mono")
    if not became_occupied and last_inv is not None and (now - float(last_inv)) < min_invoke:
        logger.debug(
            "[AI][%s] Skipped fetch: min interval (%.0fs since last invoke)",
            room_id, min_invoke,
        )
        st["_last_tick_occupied"] = is_occupied
        return False

    if now < st["_next_fetch_at"]:
        st["_last_tick_occupied"] = is_occupied
        return False

    st["_last_tick_occupied"] = is_occupied

    if st["_last_valid"] is None:
        return True

    if st["_context_indoor"] is None:
        return True

    if (
        control_base_temp is not None
        and not became_occupied
    ):
        try:
            near_deg = float(cfg.get("ai_indoor_near_setpoint_deg", 0.5) or 0.5)
            near_deg = max(0.05, min(near_deg, 5.0))
            prox = abs(float(indoor_temp) - float(control_base_temp))
            if prox < near_deg:
                logger.debug(
                    "[AI][%s] Skipped fetch: indoor %.2f°C within %.2f°C of schedule base %.2f°C",
                    room_id, float(indoor_temp), near_deg, float(control_base_temp),
                )
                return False
        except (TypeError, ValueError):
            pass

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


# Latest AI decision row awaiting user-adjustment labeling (ML).
_PENDING_ML: Dict[str, Optional[Tuple[int, str, float]]] = {}


def set_pending_ml_label(room_id: str, decision_id: int, ts_iso: str, ai_target_temp: float) -> None:
    _PENDING_ML[room_id] = (decision_id, ts_iso, float(ai_target_temp))


def get_pending_ml_label(room_id: str) -> Optional[Tuple[int, str, float]]:
    return _PENDING_ML.get(room_id)


def clear_pending_ml_label(room_id: str) -> None:
    _PENDING_ML.pop(room_id, None)
