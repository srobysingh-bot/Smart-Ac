"""
Outdoor weather data with a 10-minute in-memory cache.

Supported providers:
  - openweathermap  (default)
  - weatherapi
  - tomorrow        (tomorrow.io)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from . import config_manager

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600  # 10 minutes


@dataclass
class WeatherData:
    temp_c: float
    humidity_pct: float
    description: str = ""
    fetched_at: float = field(default_factory=time.monotonic)

    def is_fresh(self) -> bool:
        return (time.monotonic() - self.fetched_at) < CACHE_TTL_SECONDS


_cache: Optional[WeatherData] = None


async def get_weather(force: bool = False) -> Optional[WeatherData]:
    """Return cached weather data, fetching fresh data if stale or forced."""
    global _cache
    if not force and _cache and _cache.is_fresh():
        return _cache

    provider = config_manager.get("weather_provider", "openweathermap")
    api_key = config_manager.get("weather_api_key", "")
    city = config_manager.get("weather_city", "")

    if not api_key or not city:
        logger.debug("Weather API not configured — skipping fetch")
        return _cache  # Return stale data rather than None if available

    try:
        if provider == "openweathermap":
            _cache = await _fetch_openweathermap(api_key, city)
        elif provider == "weatherapi":
            _cache = await _fetch_weatherapi(api_key, city)
        elif provider == "tomorrow":
            _cache = await _fetch_tomorrow(api_key, city)
        else:
            logger.warning("Unknown weather provider: %s", provider)
            return _cache
    except Exception as exc:
        logger.error("Weather fetch failed (%s): %s", provider, exc)
        # Return stale cache rather than propagating the error

    return _cache


# ── Provider implementations ──────────────────────────────────────────────────

async def _fetch_openweathermap(api_key: str, city: str) -> WeatherData:
    # city can be "Chennai" or "13.08,80.27" (lat,lon)
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
            data = await resp.json()

    temp_c = data["main"]["temp"]
    humidity = data["main"]["humidity"]
    description = data["weather"][0]["description"] if data.get("weather") else ""
    logger.info("OWM weather: %.1f°C, %d%% humidity — %s", temp_c, humidity, description)
    return WeatherData(temp_c=temp_c, humidity_pct=humidity, description=description)


async def _fetch_weatherapi(api_key: str, city: str) -> WeatherData:
    url = f"https://api.weatherapi.com/v1/current.json?key={api_key}&q={city}&aqi=no"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()

    current = data["current"]
    return WeatherData(
        temp_c=current["temp_c"],
        humidity_pct=current["humidity"],
        description=current.get("condition", {}).get("text", ""),
    )


async def _fetch_tomorrow(api_key: str, city: str) -> WeatherData:
    # Requires lat,lon format
    location = city if "," in city else city
    url = (
        f"https://api.tomorrow.io/v4/timelines"
        f"?location={location}&fields=temperature,humidity"
        f"&timesteps=current&units=metric&apikey={api_key}"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()

    values = (
        data["data"]["timelines"][0]["intervals"][0]["values"]
    )
    return WeatherData(
        temp_c=values["temperature"],
        humidity_pct=values["humidity"],
    )


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
