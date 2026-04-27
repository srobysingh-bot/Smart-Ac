"""Build prompts: model must return STRICT JSON only (Ollama format=json)."""

from __future__ import annotations

from typing import Any, Dict, Optional

# Short schema = fewer tokens = lower latency on CPU
SCHEMA = """{
  "target_temp": <number 16-30>,
  "fan_mode": "auto|f1|f2|f3|f4|f5",
  "confidence": <number 0-1>
}"""


def build_system_prompt() -> str:
    return (
        "You are a compact JSON emitter for an AC controller. "
        "Return ONLY a single JSON object. No text before or after. No markdown, no keys beyond the schema.\n"
        f"{SCHEMA}\n"
        "Use confidence for how sure you are; fan_mode and target_temp for your recommendation."
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
        f"in={indoor_temp:.1f} target={target_temp:.1f} eff={effective_target:.1f} "
        f"out={out} {occ}"
    )


def ollama_payload(model: str, system: str, user: str) -> Dict[str, Any]:
    return {
        "model":  model,
        "prompt": f"{system}\n\n{user}",
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.2,
            "num_predict":  50,  # cap tokens — critical for <15s on Pi CPU
        },
    }
