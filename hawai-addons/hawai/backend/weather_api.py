"""
Outdoor weather data with a 10-minute in-memory cache.

Supported providers:
  - openweathermap  (default)
  - weatherapi
  - tomorrow        (tomorrow.io)
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600  # 10 minutes

_cache: Dict[str, Any] = {}
_cached_at: Optional[float] = None


async def get_cached() -> Dict[str, Any]:
    """Return cached weather data dict, or empty dict if none fetched yet."""
    return dict(_cache) if _cache else {}


async def refresh(cfg: Dict[str, Any]) -> None:
    """Fetch fresh weather data and update the cache."""
    global _cache, _cached_at

    api_key = cfg.get("weather_api_key", "")
    city = cfg.get("weather_city", "")
    provider = cfg.get("weather_provider", "openweathermap")

    if not api_key or not city:
        logger.warning("[HawaAI] Weather API not configured (key=%s, city=%s) — skipping", bool(api_key), city or "(empty)")
        return

    try:
        if provider == "openweathermap":
            data = await _fetch_openweathermap(api_key, city)
        elif provider == "weatherapi":
            data = await _fetch_weatherapi(api_key, city)
        elif provider == "tomorrow":
            data = await _fetch_tomorrow(api_key, city)
        else:
            logger.warning("[HawaAI] Unknown weather provider: %s", provider)
            return

        if data:
            _cache = data
            _cached_at = datetime.now(timezone.utc).timestamp()
            logger.info("[HawaAI] Weather updated: %.1f°C, %d%% humidity", data.get("temp", 0), data.get("humidity", 0))

    except Exception as e:
        logger.error("[HawaAI] Weather fetch error (%s, city=%s): %s", provider, city, e)


async def _fetch_openweathermap(api_key: str, city: str) -> Optional[Dict[str, Any]]:
    if "," in city and _looks_like_latlon(city):
        lat, lon = city.split(",", 1)
        url = (
            f"https://api.openweathermap.org/data/2.5/weather"
            f"?lat={lat.strip()}&lon={lon.strip()}&appid={api_key}&units=metric"
        )
    else:
        url = (
            f"https://api.openweathermap.org/data/2.5/weather"
            f"?q={city}&appid={api_key}&units=metric"
        )

    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            raw = await resp.json()

    return {
        "temp": raw["main"]["temp"],
        "humidity": raw["main"]["humidity"],
        "feels_like": raw["main"].get("feels_like"),
        "description": raw["weather"][0]["description"] if raw.get("weather") else "",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def _fetch_weatherapi(api_key: str, city: str) -> Optional[Dict[str, Any]]:
    url = f"https://api.weatherapi.com/v1/current.json?key={api_key}&q={city}&aqi=no"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            raw = await resp.json()

    current = raw["current"]
    return {
        "temp": current["temp_c"],
        "humidity": current["humidity"],
        "feels_like": current.get("feelslike_c"),
        "description": current.get("condition", {}).get("text", ""),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def _fetch_tomorrow(api_key: str, city: str) -> Optional[Dict[str, Any]]:
    url = (
        f"https://api.tomorrow.io/v4/timelines"
        f"?location={city}&fields=temperature,humidity"
        f"&timesteps=current&units=metric&apikey={api_key}"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            raw = await resp.json()

    values = raw["data"]["timelines"][0]["intervals"][0]["values"]
    return {
        "temp": values["temperature"],
        "humidity": values["humidity"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _looks_like_latlon(s: str) -> bool:
    parts = s.split(",")
    if len(parts) != 2:
        return False
    try:
        float(parts[0])
        float(parts[1])
        return True
    except ValueError:
        return False
