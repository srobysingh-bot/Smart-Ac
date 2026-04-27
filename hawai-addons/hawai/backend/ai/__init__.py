"""Optional Ollama-based AI layer (HTTP only, soft overrides)."""

from .ai_cache import get_cached, mark_fetch_done, set_validated, should_run_ai, throttle_cache_use_log
from .ai_validator import validate_ai_payload
from .ai_worker import apply_ai_fan, fetch_ai_in_background

__all__ = [
    "apply_ai_fan",
    "fetch_ai_in_background",
    "get_cached",
    "mark_fetch_done",
    "set_validated",
    "should_run_ai",
    "throttle_cache_use_log",
    "validate_ai_payload",
]
