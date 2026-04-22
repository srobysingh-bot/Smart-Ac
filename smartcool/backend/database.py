"""SQLite database schema and query helpers for SmartCool."""

import aiosqlite
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = "/data/smartcool.db"


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

async def init_db() -> None:
    """Create all tables and indexes if they do not already exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id             TEXT PRIMARY KEY,
                start_time             DATETIME NOT NULL,
                end_time               DATETIME,
                indoor_temp_start      REAL,
                indoor_temp_end        REAL,
                outdoor_temp_start     REAL,
                outdoor_humidity_start REAL,
                target_temp            REAL,
                ac_entity_id           TEXT,
                ac_brand               TEXT,
                ac_model               TEXT,
                room_name              TEXT,
                presence_trigger       TEXT,
                energy_start_kwh       REAL,
                energy_consumed_kwh    REAL,
                time_to_cool_minutes   REAL,
                cost_estimate          REAL,
                reason_stopped         TEXT,
                peak_watt_draw         REAL,
                avg_watt_draw          REAL,
                day_of_week            INTEGER,
                hour_of_day            INTEGER,
                is_archived            INTEGER DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT,
                timestamp   DATETIME NOT NULL,
                indoor_temp REAL,
                outdoor_temp REAL,
                ac_state    INTEGER,
                watt_draw   REAL,
                presence    INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS config_store (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Performance indexes
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_start    ON sessions(start_time)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_archived ON sessions(is_archived)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_session ON snapshots(session_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_ts      ON snapshots(timestamp)"
        )

        await db.commit()
    logger.info("Database ready at %s", DB_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# Sessions
# ─────────────────────────────────────────────────────────────────────────────

async def insert_session_start(session: Dict[str, Any]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO sessions
                (session_id, start_time, indoor_temp_start, outdoor_temp_start,
                 outdoor_humidity_start, target_temp, ac_entity_id, ac_brand,
                 ac_model, room_name, presence_trigger, energy_start_kwh,
                 day_of_week, hour_of_day)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session["session_id"],
                session["start_time"],
                session.get("indoor_temp_start"),
                session.get("outdoor_temp_start"),
                session.get("outdoor_humidity_start"),
                session.get("target_temp"),
                session.get("ac_entity_id"),
                session.get("ac_brand"),
                session.get("ac_model"),
                session.get("room_name"),
                session.get("presence_trigger"),
                session.get("energy_start_kwh"),
                session.get("day_of_week"),
                session.get("hour_of_day"),
            ),
        )
        await db.commit()


async def update_session_end(session_id: str, end_data: Dict[str, Any]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE sessions SET
                end_time             = ?,
                indoor_temp_end      = ?,
                time_to_cool_minutes = ?,
                energy_consumed_kwh  = ?,
                cost_estimate        = ?,
                reason_stopped       = ?,
                peak_watt_draw       = ?,
                avg_watt_draw        = ?
            WHERE session_id = ?
            """,
            (
                end_data.get("end_time"),
                end_data.get("indoor_temp_end"),
                end_data.get("time_to_cool_minutes"),
                end_data.get("energy_consumed_kwh"),
                end_data.get("cost_estimate"),
                end_data.get("reason_stopped"),
                end_data.get("peak_watt_draw"),
                end_data.get("avg_watt_draw"),
                session_id,
            ),
        )
        await db.commit()


async def get_sessions(
    limit: int = 50,
    offset: int = 0,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM sessions WHERE is_archived = 0"
        params: list = []
        if date_from:
            query += " AND start_time >= ?"
            params.append(date_from)
        if date_to:
            query += " AND start_time <= ?"
            params.append(date_to)
        query += " ORDER BY start_time DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_session_count(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        query = "SELECT COUNT(*) FROM sessions WHERE is_archived = 0"
        params: list = []
        if date_from:
            query += " AND start_time >= ?"
            params.append(date_from)
        if date_to:
            query += " AND start_time <= ?"
            params.append(date_to)
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def get_all_sessions_for_export() -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions ORDER BY start_time DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def archive_old_sessions(days: int = 90) -> int:
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        result = await db.execute(
            "UPDATE sessions SET is_archived = 1 WHERE start_time < ? AND is_archived = 0",
            (cutoff,),
        )
        await db.commit()
        logger.info("Archived %d sessions older than %d days", result.rowcount, days)
        return result.rowcount


# ─────────────────────────────────────────────────────────────────────────────
# Snapshots
# ─────────────────────────────────────────────────────────────────────────────

async def insert_snapshot(snapshot: Dict[str, Any]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO snapshots
                (session_id, timestamp, indoor_temp, outdoor_temp, ac_state, watt_draw, presence)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                snapshot.get("session_id"),
                snapshot.get("timestamp"),
                snapshot.get("indoor_temp"),
                snapshot.get("outdoor_temp"),
                1 if snapshot.get("ac_state") else 0,
                snapshot.get("watt_draw"),
                1 if snapshot.get("presence") else 0,
            ),
        )
        await db.commit()


async def get_snapshots_recent(minutes: int = 120) -> List[Dict]:
    since = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM snapshots WHERE timestamp >= ? ORDER BY timestamp ASC",
            (since,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────────────────────

async def get_today_stats() -> Dict[str, Any]:
    today = datetime.utcnow().date().isoformat()
    tomorrow = (datetime.utcnow().date() + timedelta(days=1)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT
                COUNT(*)                              AS session_count,
                COALESCE(SUM(energy_consumed_kwh), 0) AS total_kwh,
                COALESCE(SUM(cost_estimate), 0)       AS total_cost,
                COALESCE(SUM(
                    CASE WHEN end_time IS NOT NULL
                    THEN (JULIANDAY(end_time) - JULIANDAY(start_time)) * 1440
                    ELSE (JULIANDAY('now')    - JULIANDAY(start_time)) * 1440
                    END
                ), 0)                                 AS total_ac_minutes
            FROM sessions
            WHERE start_time >= ? AND start_time < ? AND is_archived = 0
            """,
            (today, tomorrow),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "session_count": row[0],
                    "total_kwh": round(row[1], 3),
                    "total_cost": round(row[2], 2),
                    "total_ac_minutes": round(row[3], 1),
                }
    return {"session_count": 0, "total_kwh": 0.0, "total_cost": 0.0, "total_ac_minutes": 0.0}


async def get_daily_stats(days: int = 7) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT
                DATE(start_time)                       AS date,
                COUNT(*)                               AS sessions,
                COALESCE(SUM(energy_consumed_kwh), 0)  AS kwh,
                COALESCE(SUM(cost_estimate), 0)        AS cost,
                COALESCE(AVG(time_to_cool_minutes), 0) AS avg_cool_time
            FROM sessions
            WHERE start_time >= DATE('now', ?) AND is_archived = 0
            GROUP BY DATE(start_time)
            ORDER BY date ASC
            """,
            (f"-{days} days",),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "date": r[0],
                    "sessions": r[1],
                    "kwh": round(r[2], 3),
                    "cost": round(r[3], 2),
                    "avg_cool_time": round(r[4], 1),
                }
                for r in rows
            ]


async def get_ml_stats() -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT
                COUNT(*) AS total,
                AVG(time_to_cool_minutes) AS avg_cool,
                COUNT(CASE WHEN end_time IS NOT NULL THEN 1 END) * 100.0 / MAX(COUNT(*), 1) AS completeness
            FROM sessions
            """
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "total_sessions": row[0] or 0,
                    "avg_cool_time": round(row[1] or 0, 1),
                    "data_completeness": round(row[2] or 0, 1),
                }
    return {"total_sessions": 0, "avg_cool_time": 0.0, "data_completeness": 0.0}
