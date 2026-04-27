"""Build prompts: model must return STRICT JSON only (Ollama format=json)."""

from __future__ import annotations

from typing import Any, Dict, Optional

SCHEMA = """{
  "action": "none|pre_cool|boost|normal",
  "target_temp": <number 16-30>,
  "fan_mode": "auto|f1|f2|f3|f4|f5",
  "confidence": <number 0-1>
}"""


def build_system_prompt() -> str:
    return (
        "You are a home cooling advisor for an AC automation system. "
        "Reply with ONE JSON object and NOTHING else — no markdown, no prose, no code fences. "
        "The JSON must match this schema exactly:\n"
        f"{SCHEMA}\n"
        "action meanings: none=no change; pre_cool=aggressive setpoint; boost=higher fan; "
        "normal=balanced fan. confidence is your certainty. "
    )


def build_user_prompt(
    indoor_temp: float,
    target_temp: float,
    effective_target: float,
    outdoor_temp: Optional[float],
    is_occupied: bool,
) -> str:
    out = f"{outdoor_temp:.1f}C" if outdoor_temp is not None else "unknown"
    occ = "occupied" if is_occupied else "vacant"
    return (
        f"indoor_celsius={indoor_temp:.1f} "
        f"user_target={target_temp:.1f} "
        f"effective_setpoint_suggested={effective_target:.1f} "
        f"outdoor_celsius={out} "
        f"room={occ}. "
        f"Propose a single JSON object per the schema. "
    )


def ollama_payload(model: str, system: str, user: str) -> Dict[str, Any]:
    return {
        "model":  model,
        "prompt": f"{system}\n\n{user}",
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.2,
        },
    }
