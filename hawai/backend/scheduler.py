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

    # Startup configuration — climate-only control path (Aerostate / ac_adapter)
    try:
        cfg = config_manager.load_config()
        ac_ent = (cfg.get("ac_entity") or cfg.get("climate_entity") or "").strip() or "(not set)"
        smart_on = logic_engine.smart_temp_adjustment_enabled(cfg)
        logger.info("[HawaAI] --- Startup configuration ---")
        logger.info("[HawaAI]   AC entity         : %s", ac_ent)
        logger.info("[HawaAI]   Control mode      : climate_adapter (Aerostate)")
        logger.info("[HawaAI]   presence_entity   : %s", cfg.get("presence_entity") or "(not set)")
        logger.info("[HawaAI]   indoor_temp_entity: %s", cfg.get("indoor_temp_entity") or "(not set)")
        logger.info("[HawaAI]   energy_power      : %s", cfg.get("energy_power_entity") or "(not set)")
        logger.info("[HawaAI]   target_temp       : %s°C", cfg.get("target_temp", 24))
        logger.info("[HawaAI]   Smart mode        : %s", "enabled" if smart_on else "disabled")
        logger.info("[HawaAI]   hysteresis        : ±%s°C", cfg.get("hysteresis", 1.5))
        logger.info("[HawaAI]   vacancy_timeout   : %s min", cfg.get("vacancy_timeout_minutes", 5))
        logger.info("[HawaAI]   logic_interval    : %s sec", cfg.get("logic_interval_seconds", 60))
        logger.info("[HawaAI] ---------------------------------")
    except Exception as e:
        logger.error("[HawaAI] Could not log startup config: %s", e)

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
