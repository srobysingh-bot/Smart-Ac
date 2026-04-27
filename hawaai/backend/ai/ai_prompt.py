"""Build prompts: model must return STRICT JSON only (Ollama format=json)."""

from __future__ import annotations

from typing import Any, Dict, Optional

# Short schema = fewer input tokens = less CPU on Pi
SCHEMA = """{
  "target_temp": <number 16-30>,
  "fan_mode": "auto|f1|f2|f3|f4|f5",
  "confidence": <number 0-1>
}"""


def build_system_prompt() -> str:
    return (
        "Output: one JSON object only. No prose, no explanation, no markdown, no code fences, no extra keys. "
        f"Schema: {SCHEMA}"
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
    return f"i={indoor_temp:.1f} t={target_temp:.1f} e={effective_target:.1f} o={out} {occ[0].upper()}"


def ollama_payload(model: str, system: str, user: str) -> Dict[str, Any]:
    """Request shape; add generation options in ai_worker (num_predict, temperature)."""
    return {
        "model":  model,
        "prompt": f"{system}\n{user}",
        "stream": False,
        "format": "json",
    }
