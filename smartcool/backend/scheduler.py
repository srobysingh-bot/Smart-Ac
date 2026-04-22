"""
Periodic background tasks.

  • Weather refresh   — every 10 min
  • Logic tick        — every N seconds (config: logic_interval_seconds)
  • Archive old data  — once per day at 02:00
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import config_manager, database
from .logic_engine import LogicEngine
from .weather_api import get_weather

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def start(engine: LogicEngine) -> None:
    """Start the APScheduler with all background jobs."""
    global _scheduler

    _scheduler = AsyncIOScheduler(timezone="UTC")

    # ── Logic tick ────────────────────────────────────────────────────────────
    interval = config_manager.get("logic_interval_seconds", 60)
    _scheduler.add_job(
        engine.tick,
        trigger=IntervalTrigger(seconds=interval),
        id="logic_tick",
        max_instances=1,
        coalesce=True,
    )
    logger.info("Logic tick scheduled every %d seconds", interval)

    # ── Weather refresh ───────────────────────────────────────────────────────
    _scheduler.add_job(
        _refresh_weather,
        trigger=IntervalTrigger(minutes=10),
        id="weather_refresh",
        max_instances=1,
    )

    # ── Daily archival at 02:00 UTC ───────────────────────────────────────────
    _scheduler.add_job(
        _archive_sessions,
        trigger=CronTrigger(hour=2, minute=0),
        id="archive_sessions",
    )

    _scheduler.start()
    logger.info("Scheduler started")


def stop() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


async def _refresh_weather() -> None:
    try:
        data = await get_weather(force=True)
        if data:
            logger.debug("Weather refreshed: %.1f°C", data.temp_c)
    except Exception as exc:
        logger.error("Weather refresh error: %s", exc)


async def _archive_sessions() -> None:
    try:
        archived = await database.archive_old_sessions(days=90)
        logger.info("Archived %d old sessions", archived)
    except Exception as exc:
        logger.error("Session archival error: %s", exc)
