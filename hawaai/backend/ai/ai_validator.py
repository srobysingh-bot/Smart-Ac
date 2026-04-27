"""Validate and sanitize Ollama JSON output. Returns dict or None."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

VALID_ACTIONS = frozenset({"none", "pre_cool", "boost", "normal"})
VALID_FANS = frozenset({"auto", "f1", "f2", "f3", "f4", "f5"})
MIN_T = 16.0
MAX_T = 30.0
MIN_CONF = 0.6


def validate_ai_payload(data: Any, is_occupied: bool) -> Optional[Dict[str, Any]]:
    """
    Accept compact JSON: target_temp, fan_mode, confidence (optional legacy: action).
    If action is omitted, default "normal" for downstream apply_ai_fan / setpoint.
    """
    if not isinstance(data, dict):
        logger.debug("[AI] Invalid response: not a JSON object")
        return None

    action: Optional[str] = data.get("action")
    if action is not None and (not isinstance(action, str) or action not in VALID_ACTIONS):
        logger.debug("[AI] Invalid response: bad action %r", action)
        return None
    if action is None:
        action = "normal"

    try:
        t = float(data.get("target_temp"))
    except (TypeError, ValueError):
        logger.debug("[AI] Invalid response: target_temp not numeric")
        return None
    if t < MIN_T or t > MAX_T:
        logger.debug("[AI] Invalid response: target_temp %.1f out of range", t)
        return None

    fan = data.get("fan_mode")
    if not isinstance(fan, str) or fan not in VALID_FANS:
        logger.debug("[AI] Invalid response: bad fan_mode %r", fan)
        return None

    try:
        conf = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        logger.debug("[AI] Invalid response: confidence not numeric")
        return None
    if conf < MIN_CONF:
        logger.debug("[AI] Invalid response: confidence %.2f < %.1f", conf, MIN_CONF)
        return None

    if not is_occupied and action in ("boost", "pre_cool"):
        logger.debug("[AI] Invalid response: %s when room empty", action)
        return None

    t = min(MAX_T, max(MIN_T, t))
    return {
        "action":       action,
        "target_temp":  round(t, 1),
        "fan_mode":     fan,
        "confidence":   conf,
    }
