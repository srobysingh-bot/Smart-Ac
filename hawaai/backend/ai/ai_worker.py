"""AI inference: OpenAI-compatible API or Ollama (local). Non-blocking via asyncio.create_task."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Any, Dict, Optional, Tuple

import aiohttp

from . import ai_cache, ai_prompt, ai_validator
from .. import config_manager, ha_client

logger = logging.getLogger(__name__)

# Bumped with each AI transport change — look for "AI worker initialized" in add-on logs
AI_WORKER_VERSION = "1.2.19"

# Ollama structured output: JSON Schema only under request "format" (no separate top-level required)
OLLAMA_RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_temp": {"type": "number"},
        "fan_mode": {
            "type": "string",
            "enum": ["auto", "f1", "f2", "f3", "f4", "f5"],
        },
        "confidence": {"type": "number"},
    },
    "required": ["target_temp", "fan_mode", "confidence"],
}

_DEFAULT_URL = config_manager.DEFAULT_CONFIG["ai_ollama_url"]
_ollama_url_logged: bool = False
_GENERATE = "/api/generate"
_TIMEOUT_S = 30.0
_RETRY_DELAY_S = 4.0
_API_RETRY_DELAY_S = 2.0
# Safe HVAC hint when API returns 200 but body is unusable (validated before cache).
_API_CONSERVATIVE_DEFAULT: Dict[str, Any] = {
    "target_temp": 24,
    "fan_mode": "auto",
    "confidence": 0.5,
}
_OLLAMA_GEN_OPTIONS: Dict[str, Any] = {
    "num_predict":  20,
    "temperature": 0.0,
}
_MAX_RESPONSE_TEXT_LEN = 200
_FAN_COOLDOWN = 60.0

_last_fan_cmd_at: float = 0.0
_last_applied_action: str = ""
_last_same_mode_log_at: float = 0.0
_SAME_MODE_LOG_THROTTLE = 300.0
_skip_unavailable_log_at: float = 0.0
_SKIP_UNAVAILABLE_THROTTLE = 300.0

logger.info("AI worker initialized v%s", AI_WORKER_VERSION)


def _log_skipped_not_available() -> None:
    global _skip_unavailable_log_at
    now = time.perf_counter()
    if now - _skip_unavailable_log_at < _SKIP_UNAVAILABLE_THROTTLE:
        return
    _skip_unavailable_log_at = now
    logger.info("[AI] Skipped")
    logger.debug("[AI] Skipped reason: Ollama unreachable or bad response (throttled detail)")


def last_ai_log_state() -> Dict[str, Any]:
    return {
        "last_fan_cmd_at": _last_fan_cmd_at,
        "last_action":     _last_applied_action,
    }


def _base_url(cfg: Dict[str, Any]) -> str:
    global _ollama_url_logged
    u = (cfg.get("ai_ollama_url") or "").strip()
    if not u or "ollama_ai" in u.lower():
        u = _DEFAULT_URL
    else:
        u = u.rstrip("/")
    if not _ollama_url_logged:
        logger.debug("[AI] Ollama URL: %s", u)
        _ollama_url_logged = True
    return u


def _model(cfg: Dict[str, Any]) -> str:
    m = (cfg.get("ai_ollama_model") or "").strip()
    return m or config_manager.DEFAULT_OLLAMA_MODEL


def _ai_provider(cfg: Dict[str, Any]) -> str:
    p = (str(cfg.get("ai_provider") or "ollama")).strip().lower()
    return "api" if p == "api" else "ollama"


def _api_model(cfg: Dict[str, Any]) -> str:
    return (cfg.get("ai_api_model") or "").strip()


def _api_timeout_s(cfg: Dict[str, Any]) -> float:
    try:
        t = float(cfg.get("ai_api_timeout", 20))
    except (TypeError, ValueError):
        t = 20.0
    return max(5.0, min(120.0, t))


def mask_api_key_for_log(secret: str) -> str:
    """Never log raw keys; use only for 'configured' hints (not full secret)."""
    s = (secret or "").strip()
    if not s:
        return "(not set)"
    if len(s) <= 8:
        return "****"
    return f"{s[:3]}****{s[-4:]}"


def _normalize_message_content(content: Any) -> Any:
    """OpenAI-compatible message content: str or multimodal list."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list = []
        for p in content:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts) if parts else None
    return content


def _extract_chat_completion_content(resp: Dict[str, Any]) -> Any:
    try:
        ch = resp.get("choices") or []
        if not ch:
            return None
        msg = (ch[0] or {}).get("message") or {}
        return msg.get("content")
    except (TypeError, IndexError, AttributeError):
        return None


def _parse_json_from_model_output(raw: Any) -> Any:
    """Turn model output (string or dict) into parsed JSON object or None."""
    if isinstance(raw, dict):
        try:
            blob = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            _log_invalid_json_full_raw("dict serialize failed", raw)
            return None
        logger.info("RAW AI RESPONSE: %s", blob)
        if len(blob) > _MAX_RESPONSE_TEXT_LEN:
            _log_invalid_json_full_raw("dict too long", raw)
            logger.info("[AI] response too long → rejecting (object >%d chars)", _MAX_RESPONSE_TEXT_LEN)
            return None
        _emit_ai_response_line(raw)
        return raw

    if isinstance(raw, list):
        _log_invalid_json_full_raw("unexpected list", raw)
        logger.debug("[AI] unexpected list response")
        return None

    if not isinstance(raw, str):
        _log_invalid_json_full_raw("unexpected type", raw)
        return None

    s = raw.strip()
    if not s:
        _log_invalid_json_full_raw("empty string", raw)
        return None

    logger.info("RAW AI RESPONSE: %s", s)

    if not s.startswith("{"):
        _log_invalid_json_full_raw("does not start with brace", raw)
        logger.info("AI INVALID — NOT JSON")
        return None

    match = re.search(r"\{.*\}", s, re.DOTALL)
    if not match:
        _log_invalid_json_full_raw("regex miss", raw)
        logger.info("[AI] JSON block regex miss → rejecting")
        return None

    response_text = match.group()
    if len(response_text) > _MAX_RESPONSE_TEXT_LEN:
        _log_invalid_json_full_raw("extracted block too long", raw)
        logger.info("[AI] response too long → rejecting (>%d chars)", _MAX_RESPONSE_TEXT_LEN)
        return None

    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError:
        _log_invalid_json_full_raw("json.loads failed", raw)
        logger.info("[AI] response not valid JSON after extract → rejecting")
        return None

    _emit_ai_response_line(parsed)
    return parsed


async def _post_generate(
    base: str, body: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    url = f"{base}{_GENERATE}"
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=body) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    logger.debug("[AI] HTTP %s %s", resp.status, txt[:200])
                    return None
                data = await resp.json()
                logger.debug("[AI] response time %.2fs", time.perf_counter() - t0)
                return data
    except asyncio.TimeoutError:
        logger.debug("[AI] request timeout %ss", _TIMEOUT_S)
        return None
    except aiohttp.ClientError as e:
        logger.debug("[AI] client error: %s", e)
        return None


async def _post_chat_completions(
    url: str,
    api_key: str,
    body: Dict[str, Any],
    timeout_s: float,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """
    POST chat/completions. Returns (data, retryable).
    retryable is True only for timeout / transport errors — outer wrapper may retry once.
    HTTP 429: waits 2–3s and repeats the request once inside this function.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(2):
                async with session.post(url, json=body, headers=headers) as resp:
                    if resp.status == 429:
                        await resp.text()
                        if attempt == 0:
                            delay = 2.0 + random.random()
                            logger.warning(
                                "[AI] API rate limited (HTTP 429); retry in %.1fs",
                                delay,
                            )
                            await asyncio.sleep(delay)
                            continue
                        logger.warning("[AI] API HTTP 429 after retry")
                        return None, False
                    if resp.status != 200:
                        txt = await resp.text()
                        logger.warning(
                            "[AI] API HTTP %s (%.2fs) body[:200]=%r",
                            resp.status,
                            time.perf_counter() - t0,
                            (txt or "")[:200],
                        )
                        return None, False
                    try:
                        data = await resp.json()
                    except (aiohttp.ContentTypeError, json.JSONDecodeError) as e:
                        logger.warning("[AI] API response body not valid JSON: %s", e)
                        return None, False
                    logger.debug(
                        "[AI] API response time %.2fs",
                        time.perf_counter() - t0,
                    )
                    return data, False
            return None, False
    except asyncio.TimeoutError:
        logger.warning("[AI] API timeout after %s seconds", timeout_s)
        return None, True
    except aiohttp.ClientError as e:
        logger.warning("[AI] API client error: %s", e)
        return None, True


async def _post_chat_completions_with_one_retry(
    url: str,
    api_key: str,
    body: Dict[str, Any],
    timeout_s: float,
) -> Optional[Dict[str, Any]]:
    """On transport/timeout failure only: wait 2s and retry once. HTTP errors: no retry."""
    data, retryable = await _post_chat_completions(url, api_key, body, timeout_s)
    if data is not None:
        return data
    if not retryable:
        return None
    logger.debug("[AI] API transport/timeout — retry in %.0fs", _API_RETRY_DELAY_S)
    await asyncio.sleep(_API_RETRY_DELAY_S)
    data, _ = await _post_chat_completions(url, api_key, body, timeout_s)
    return data


async def _post_generate_with_one_retry(
    base: str, body: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    r = await _post_generate(base, body)
    if r is not None:
        return r
    logger.debug("[AI] retry in %.0fs", _RETRY_DELAY_S)
    await asyncio.sleep(_RETRY_DELAY_S)
    return await _post_generate(base, body)


def _emit_ai_response_line(obj: Any) -> None:
    try:
        txt = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    logger.info("AI RESPONSE: %s", txt)


def _log_invalid_json_full_raw(context: str, raw: Any) -> None:
    """Debug: full model output when parse/validation path fails (no truncation)."""
    logger.warning("[AI] INVALID JSON (%s) — full raw: %r", context, raw)


def _parse_api_chat_response(resp: Dict[str, Any]) -> Any:
    """Parse OpenAI-compatible chat completion JSON only (no Ollama fields)."""
    if not isinstance(resp, dict) or "choices" not in resp:
        logger.warning("[AI] API response missing choices[]")
        return None
    raw = _normalize_message_content(_extract_chat_completion_content(resp))
    if raw is None:
        logger.warning("[AI] API message content empty — invalid")
        return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            logger.warning("[AI] API message content empty — invalid")
            return None
        if not s.startswith("{"):
            logger.warning("[AI] API message content does not start with '{' — invalid")
            return None
    return _parse_json_from_model_output(raw)


def _parse_ollama_response_body(resp: Dict[str, Any]) -> Any:
    """Normalize Ollama /api/generate response → JSON object for ai_validator."""
    if not isinstance(resp, dict):
        return None
    raw = (resp or {}).get("response")
    if not raw and "message" in resp:
        c = (resp.get("message") or {}).get("content")
        if c:
            raw = c
    if raw is None:
        logger.warning("[AI] INVALID JSON (empty response field) — full message: %r", resp)
        return None
    return _parse_json_from_model_output(raw)


def _api_fallback_to_logic_engine() -> None:
    logger.info("[AI] API failed → fallback to logic_engine")
    ai_cache.mark_fetch_done()


def _try_apply_api_conservative_defaults(
    is_occupied: bool,
    indoor_temp: float,
) -> bool:
    """
    When the API returns HTTP 200 but the model output is unusable, apply a
    validated conservative hint so behavior stays deterministic. If validation
    fails (e.g. occupancy edge), return False and let logic_engine run alone.
    """
    validated = ai_validator.validate_ai_payload(
        dict(_API_CONSERVATIVE_DEFAULT),
        is_occupied,
    )
    if not validated:
        logger.warning(
            "[AI] Conservative API default rejected by validator — logic_engine only",
        )
        return False
    ai_cache.set_validated(validated, indoor_temp)
    ai_cache.mark_fetch_done()
    logger.info(
        "[AI] API output invalid — applied conservative default "
        "(target_temp=24, fan_mode=auto, confidence=0.5)",
    )
    return True


async def run_ai_and_cache(
    cfg: Dict[str, Any],
    indoor_temp: float,
    target_temp: float,
    base_effective: float,
    outdoor_temp: Optional[float],
    is_occupied: bool,
) -> None:
    if not bool(cfg.get("ai_enabled", False)) or not is_occupied:
        return

    prompt = ai_prompt.build_hvac_control_prompt(
        indoor_temp, outdoor_temp, is_occupied,
    )
    provider = _ai_provider(cfg)

    if provider == "ollama":
        logger.info("[AI] Provider: Ollama")
        base   = _base_url(cfg)
        model  = _model(cfg)
        ai_cache.invalidate_if_ai_identity_changed("ollama", model)
        logger.debug("[AI] model=%s", model)
        logger.info("PROMPT SENT TO OLLAMA: %s", prompt)

        body = ai_prompt.ollama_payload(model, prompt)
        body["format"] = dict(OLLAMA_RESPONSE_FORMAT)
        body["raw"] = True
        body["stream"] = False
        body["options"] = dict(_OLLAMA_GEN_OPTIONS)

        logger.info("OLLAMA PAYLOAD: %s", json.dumps(body, ensure_ascii=False))

        logger.info("[AI] Called")
        resp = await _post_generate_with_one_retry(base, body)
        if resp is None:
            _log_skipped_not_available()
            ai_cache.mark_fetch_done()
            return

        parsed = _parse_ollama_response_body(resp)
        if parsed is None:
            logger.info("[AI] Skipped")
            logger.debug("[AI] Skipped reason: unparseable output")
            ai_cache.mark_fetch_done()
            return

        validated = ai_validator.validate_ai_payload(parsed, is_occupied)
        if not validated:
            logger.info("[AI] Skipped")
            logger.debug("[AI] Skipped reason: validation failed")
            ai_cache.mark_fetch_done()
            return

        ai_cache.set_validated(validated, indoor_temp)
        ai_cache.mark_fetch_done()
        logger.info("[AI] Applied")
        return

    # OpenAI-compatible HTTP API
    logger.info("[AI] Provider: API")
    key = (cfg.get("ai_api_key") or "").strip()
    base_u = (cfg.get("ai_api_base_url") or "").strip().rstrip("/")
    model_a = _api_model(cfg)
    timeout_a = _api_timeout_s(cfg)

    if not key or not base_u or not model_a:
        logger.error(
            "[AI] API mode requires non-empty ai_api_key, ai_api_base_url, and ai_api_model",
        )
        _api_fallback_to_logic_engine()
        return

    ai_cache.invalidate_if_ai_identity_changed("api", model_a)
    logger.debug("[AI] model=%s timeout=%.0fs", model_a, timeout_a)
    logger.info("[AI] API key fingerprint (masked): %s", mask_api_key_for_log(key))
    logger.info("PROMPT SENT TO AI (API): %s", prompt)

    chat_url = f"{base_u}/chat/completions"
    # OpenAI-compatible; omit if provider returns 400 (not all endpoints support json_object).
    req_body: Dict[str, Any] = {
        "model": model_a,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 50,
        "response_format": {"type": "json_object"},
    }
    logger.info("[AI] Request sent to API (url=%s model=%s)", chat_url, model_a)

    resp = await _post_chat_completions_with_one_retry(chat_url, key, req_body, timeout_a)
    if resp is None:
        _api_fallback_to_logic_engine()
        return

    logger.info("[AI] Response received")

    parsed = _parse_api_chat_response(resp)
    if parsed is None:
        if not _try_apply_api_conservative_defaults(is_occupied, indoor_temp):
            _api_fallback_to_logic_engine()
        return

    validated = ai_validator.validate_ai_payload(parsed, is_occupied)
    if not validated:
        if not _try_apply_api_conservative_defaults(is_occupied, indoor_temp):
            _api_fallback_to_logic_engine()
        return

    ai_cache.set_validated(validated, indoor_temp)
    ai_cache.mark_fetch_done()
    logger.info("[AI] Applied")


def fetch_ai_in_background(
    cfg: Dict[str, Any],
    indoor_temp: float,
    target_temp: float,
    base_effective: float,
    outdoor_temp: Optional[float],
    is_occupied: bool,
) -> None:
    asyncio.create_task(
        run_ai_and_cache(
            cfg, indoor_temp, target_temp, base_effective, outdoor_temp, is_occupied,
        ),
    )


def _map_logical_fan_to_ha(
    supported: list,
    fan_mode: str,
) -> Tuple[Optional[str], str]:
    if not supported or not isinstance(supported, list):
        return None, "empty_supported"

    def _find(mode: str) -> Optional[str]:
        if not mode:
            return None
        if mode in supported:
            return mode
        m = mode.lower()
        for s in supported:
            if s is not None and str(s).lower() == m:
                return s
        return None

    if fan_mode == "auto":
        r = _find("auto")
        return (r, "ok") if r else (None, "unsupported")

    from .. import smart_cooling
    m = smart_cooling.FAN_ALIAS_MAP.get(fan_mode, fan_mode)
    hit = _find(m) or _find(fan_mode)
    return (hit, "ok" if hit else "unsupported")


async def apply_ai_fan(
    climate_entity: str,
    fan_mode: str,
    action: str,
) -> None:
    global _last_fan_cmd_at, _last_applied_action, _last_same_mode_log_at

    if not climate_entity or not fan_mode or action in (None, "none"):
        return

    now = time.perf_counter()
    if now - _last_fan_cmd_at < _FAN_COOLDOWN:
        return

    cstate     = await ha_client.get_climate_state(climate_entity)
    supported  = cstate.get("fan_modes")
    if not isinstance(supported, list):
        supported = []

    resolved, reason = _map_logical_fan_to_ha(supported, fan_mode)
    if resolved is None:
        logger.debug("[AI] fan %r not on entity (%s)", fan_mode, reason)
        return

    cur = cstate.get("fan_mode")
    if cur is not None and str(cur).lower() == str(resolved).lower():
        tnow = time.perf_counter()
        if tnow - _last_same_mode_log_at >= _SAME_MODE_LOG_THROTTLE:
            _last_same_mode_log_at = tnow
            logger.debug("[AI] Fan skipped (same mode)")
        return

    ok = await ha_client.call_service("climate", "set_fan_mode", {
        "entity_id":  climate_entity,
        "fan_mode":   resolved,
    })
    if ok:
        _last_fan_cmd_at   = time.perf_counter()
        _last_applied_action = action
        logger.debug("[AI] Fan mode %r → %r (action=%s)", fan_mode, resolved, action)
    else:
        logger.debug("[AI] set_fan_mode failed for %s", climate_entity)
