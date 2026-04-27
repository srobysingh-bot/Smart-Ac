"""HVAC control prompt: model must return ONLY JSON (Ollama format=json)."""

from __future__ import annotations

from typing import Any, Dict, Optional


def build_hvac_control_prompt(
    indoor_temp: float,
    outdoor_temp: Optional[float],
    is_occupied: bool,
) -> str:
    """Single strict control prompt — not a chat template."""
    out = f"{outdoor_temp:.1f}" if outdoor_temp is not None else "unknown"
    occ = "true" if is_occupied else "false"
    return f"""You are an HVAC control system.

Return ONLY JSON. No explanation.

Format:
{{
  "target_temp": number,
  "fan_mode": "auto|f1|f2|f3|f4|f5",
  "confidence": number
}}

Rules:
* target_temp must be between 22 and 26
* fan_mode must be valid
* confidence between 0 and 1
* NO text outside JSON
* NO explanation
* NO markdown

Input:
indoor_temp={indoor_temp:.1f}
outdoor_temp={out}
occupied={occ}

Return JSON only."""


def ollama_payload(model: str, prompt: str) -> Dict[str, Any]:
    """Request shape; options and stop list are set in ai_worker."""
    return {
        "model":  model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }
