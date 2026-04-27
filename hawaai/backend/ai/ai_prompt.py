"""HVAC control prompt — Ollama must emit JSON only; no chat content."""

from __future__ import annotations

from typing import Any, Dict, Optional


def build_hvac_control_prompt(
    indoor_temp: float,
    outdoor_temp: Optional[float],
    is_occupied: bool,
) -> str:
    """
    Return EXACT instruction block (placeholders only: indoor, outdoor, occupied).
    No system/user split — this entire string is the only `prompt` sent to Ollama.
    """
    out = f"{outdoor_temp:.1f}" if outdoor_temp is not None else "unknown"
    occ = "true" if is_occupied else "false"
    return f"""You are an HVAC control system.

Return ONLY valid JSON. No explanation. No text. No markdown.

Format:
{{"target_temp": number, "fan_mode": "auto|f1|f2|f3|f4|f5", "confidence": number}}

Rules:

* target_temp must be between 22 and 26
* fan_mode must be one of allowed values
* confidence must be between 0 and 1
* Output must be a single JSON object
* If unsure, still return valid JSON

Input:
indoor_temp={indoor_temp:.1f}
outdoor_temp={out}
occupied={occ}

Return JSON only."""


def ollama_payload(model: str, prompt: str) -> Dict[str, Any]:
    """Minimal body — `options` and `stop` are merged in ai_worker."""
    return {
        "model":  model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }
