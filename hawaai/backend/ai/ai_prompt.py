"""HVAC control prompt for Ollama structured output (JSON schema in request body)."""

from __future__ import annotations

from typing import Any, Dict, Optional


def build_hvac_control_prompt(
    indoor_temp: float,
    outdoor_temp: Optional[float],
    is_occupied: bool,
) -> str:
    """Minimal prompt; schema enforcement is in API `format` + `raw` (see ai_worker)."""
    inn = int(round(indoor_temp))
    out = int(round(outdoor_temp)) if outdoor_temp is not None else "unknown"
    occ = "true" if is_occupied else "false"
    return f"""You are an HVAC controller.
Return ONLY valid JSON.

Example:
{{"target_temp":24,"fan_mode":"f2","confidence":0.8}}

Input:
indoor_temp={inn}
outdoor_temp={out}
occupied={occ}"""


def ollama_payload(model: str, prompt: str) -> Dict[str, Any]:
    """Body extended in ai_worker with format (JSON schema), raw, stream, options."""
    return {
        "model":  model,
        "prompt": prompt,
    }
