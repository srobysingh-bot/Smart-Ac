"""Periodic background tasks — simple asyncio loop, no external scheduler."""

import asyncio
import logging

from . import config_manager, logic_engine, weather_api

logger = logging.getLogger(__name__)


async def start() -> None:
    """
    Main scheduler loop. Runs forever:
      - logic_engine.tick()  every logic_interval_seconds
      - weather_api.refresh() every 10 minutes
    """
    logger.info("[HawaAI] Scheduler started")
    weather_accumulator = 0

    # Initial weather fetch
    try:
        cfg = config_manager.load_config()
        await weather_api.refresh(cfg)
    except Exception as e:
        logger.error("[HawaAI] Initial weather fetch error: %s", e)

    while True:
        cfg = config_manager.load_config()
        interval = int(cfg.get("logic_interval_seconds", 60))

        try:
            await logic_engine.tick()
        except Exception as e:
            logger.error("[HawaAI] Logic tick error: %s", e)

        weather_accumulator += interval
        if weather_accumulator >= 600:
            try:
                await weather_api.refresh(cfg)
            except Exception as e:
                logger.error("[HawaAI] Weather refresh error: %s", e)
            weather_accumulator = 0

        await asyncio.sleep(interval)
