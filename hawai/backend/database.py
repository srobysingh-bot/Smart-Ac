"""SQLite database schema and query helpers for HawaAI."""

import aiosqlite
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = "/data/hawaai.db"


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

        # Analytics columns — added non-destructively so existing DBs keep working.
        # SQLite returns an error if the column already exists; we suppress it.
        for col_sql in (
            "ALTER TABLE sessions ADD COLUMN cooling_rate  REAL",   # °C / min
            "ALTER TABLE sessions ADD COLUMN cooling_type  TEXT",   # fast / normal / slow
            "ALTER TABLE sessions ADD COLUMN efficiency    REAL",   # °C / kWh
        ):
            try:
                await db.execute(col_sql)
            except Exception:
                pass  # column already exists — safe to ignore

        await db.commit()
    logger.info("Database ready at %s", DB_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# Session enrichment  (API-layer only — never writes to the database)
# ─────────────────────────────────────────────────────────────────────────────

def _enrich_session(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Add computed fields to a session dict at API-response time.

    RULES (strictly followed):
    - Never modifies the database
    - Never changes session start/stop triggers
    - Only normalises values and adds derived read-only fields
    - All operations are safe — no crashes on missing / None values

    Added fields:
      duration_minutes  float | None   — wall-clock session length
      delta_temp        float | None   — indoor_temp_start − indoor_temp_end
      valid             bool           — session is analytically useful
        criteria: duration >= 3 min, delta_temp >= 0.3 °C, session completed

    Normalised fields (not modified in DB):
      energy_consumed_kwh — None→0, negative→0, spikes > 10 kWh → 0
      cost_estimate       — None→0
    """
    s = dict(row)

    # ── Duration ──────────────────────────────────────────────────────────────
    duration_min: Optional[float] = None
    try:
        if s.get("start_time") and s.get("end_time"):
            def _parse(ts: str) -> datetime:
                ts = str(ts).replace("Z", "+00:00")
                try:
                    return datetime.fromisoformat(ts)
                except ValueError:
                    return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")

            start = _parse(s["start_time"])
            end   = _parse(s["end_time"])
            secs  = (end - start).total_seconds()
            duration_min = max(0.0, secs / 60.0)
    except Exception:
        pass
    # Fall back to stored time_to_cool_minutes if timestamps unparseable
    if duration_min is None and s.get("time_to_cool_minutes") is not None:
        try:
            duration_min = max(0.0, float(s["time_to_cool_minutes"]))
        except (TypeError, ValueError):
            pass

    s["duration_minutes"] = round(duration_min, 2) if duration_min is not None else None

    # ── Delta temperature ─────────────────────────────────────────────────────
    try:
        t_start = float(s["indoor_temp_start"]) if s.get("indoor_temp_start") is not None else None
        t_end   = float(s["indoor_temp_end"])   if s.get("indoor_temp_end")   is not None else None
        s["delta_temp"] = round(t_start - t_end, 2) if (t_start is not None and t_end is not None) else None
    except (TypeError, ValueError):
        s["delta_temp"] = None

    # ── Energy normalisation (API layer only) ─────────────────────────────────
    try:
        e = float(s["energy_consumed_kwh"]) if s.get("energy_consumed_kwh") is not None else 0.0
        e = max(0.0, e)
        if e > 10.0:            # unrealistic spike — treat as missing data
            logger.debug("Session %s: energy spike %.2f kWh clamped to 0", s.get("session_id"), e)
            e = 0.0
        s["energy_consumed_kwh"] = round(e, 4)
    except (TypeError, ValueError):
        s["energy_consumed_kwh"] = 0.0

    # ── Cost normalisation ────────────────────────────────────────────────────
    # Rule: cost MUST be 0 if energy is 0 (guards against stale DB rows where
    # the kWh-meter calculation was wrong and produced a high cost with 0 energy).
    try:
        cost_raw = round(float(s["cost_estimate"]), 2) if s.get("cost_estimate") is not None else 0.0
    except (TypeError, ValueError):
        cost_raw = 0.0
    s["cost_estimate"] = 0.0 if s["energy_consumed_kwh"] == 0.0 else cost_raw

    # ── Validity flag ─────────────────────────────────────────────────────────
    s["valid"] = bool(
        s.get("end_time") is not None          # session completed
        and duration_min is not None
        and duration_min >= 3.0                # at least 3 minutes
        and s["delta_temp"] is not None
        and s["delta_temp"] >= 0.3             # room cooled by at least 0.3 °C
        and s["energy_consumed_kwh"] >= 0      # no negative energy
    )

    return s


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
                avg_watt_draw        = ?,
                cooling_rate         = ?,
                cooling_type         = ?,
                efficiency           = ?
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
                end_data.get("cooling_rate"),
                end_data.get("cooling_type"),
                end_data.get("efficiency"),
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
            # Enrich at API layer — adds valid, delta_temp, duration_minutes
            return [_enrich_session(dict(r)) for r in rows]


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
            return [_enrich_session(dict(r)) for r in rows]


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


def _safe_round(val, digits: int) -> Optional[float]:
    """round() that returns None instead of raising when val is None."""
    try:
        return round(float(val), digits) if val is not None else None
    except (TypeError, ValueError):
        return None


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Division that returns default instead of raising ZeroDivisionError."""
    try:
        if denominator == 0:
            return default
        return numerator / denominator
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def _build_empty_insights(reason: str) -> Dict[str, Any]:
    return {
        "has_data":           False,
        "reason":             reason,
        "sessions_analyzed":  0,
        "fallback_used":      False,
        # Flat keys (backward-compatible with old InsightsCard / callers)
        "avg_cooling_rate":   0.0,
        "avg_efficiency":     0.0,
        "best_target_temp":   None,
        "best_outdoor_range": None,
        "cooling_type_counts": {"fast": 0, "normal": 0, "slow": 0},
        "trend":              None,
        # New structured metrics block
        "metrics": {
            "avg_cooling_rate":    0.0,
            "avg_efficiency":      0.0,
            "avg_cool_time_min":   0.0,
            "best_target_temp":    None,
            "best_outdoor_range":  None,
            "cooling_type_counts": {"fast": 0, "normal": 0, "slow": 0},
            "trend":               None,
        },
    }


async def get_insights() -> Dict[str, Any]:
    """
    Compute analytics insights at the API layer from enriched completed sessions.

    All computation is done in Python — no reliance on stored cooling_rate column.
    This means insights work even for sessions logged before v1.1.15.

    Selection logic:
      1. Prefer "valid" sessions: duration >= 3 min, delta_temp >= 0.3 °C
      2. If none exist, fall back to sessions with duration >= 2 min + delta_temp > 0
         (at most last 5) — flagged with fallback_used=True
      3. If still none, return has_data=False with a human-readable reason

    Never raises. Always returns valid JSON.
    """
    try:
        # ── Fetch recent completed sessions ───────────────────────────────────
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM sessions
                WHERE end_time IS NOT NULL
                  AND is_archived = 0
                ORDER BY start_time DESC
                LIMIT 200
                """
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            return _build_empty_insights("no_sessions")

        sessions = [_enrich_session(dict(r)) for r in rows]

        # ── Select analysis pool ──────────────────────────────────────────────
        valid_pool = [s for s in sessions if s.get("valid")]
        fallback_used = False

        if not valid_pool:
            # Relaxed fallback: duration >= 2 min, positive cooling
            valid_pool = [
                s for s in sessions
                if (s.get("duration_minutes") or 0) >= 2.0
                and (s.get("delta_temp") or 0) > 0.0
            ][:5]
            if valid_pool:
                fallback_used = True
                logger.info(
                    "[HawaAI] Insights: no strict-valid sessions; using %d fallback sessions",
                    len(valid_pool),
                )
            else:
                return _build_empty_insights("insufficient_data")

        # ── Compute metrics in Python ─────────────────────────────────────────
        cooling_rates: List[float] = []
        efficiencies:  List[float] = []
        cool_times:    List[float] = []
        type_counts: Dict[str, int] = {"fast": 0, "normal": 0, "slow": 0}
        temp_rates:    Dict[float,  List[float]] = defaultdict(list)
        range_rates:   Dict[str,    List[float]] = defaultdict(list)

        for s in valid_pool:
            dur = s.get("duration_minutes") or 0.0
            dt  = s.get("delta_temp")       or 0.0
            kwh = s.get("energy_consumed_kwh") or 0.0

            if dur <= 0 or dt <= 0:
                continue

            rate = _safe_div(dt, dur)
            if rate > 0:
                cooling_rates.append(rate)
                if   rate > 0.5:  type_counts["fast"]   += 1
                elif rate >= 0.2: type_counts["normal"] += 1
                else:             type_counts["slow"]   += 1

            # Efficiency: kWh per °C cooled (lower = more efficient)
            if kwh > 0:
                efficiencies.append(_safe_div(kwh, max(dt, 0.1)))

            cool_times.append(dur)

            # Best target temperature accumulator
            tgt = s.get("target_temp")
            if tgt is not None:
                try:
                    temp_rates[float(tgt)].append(rate)
                except (TypeError, ValueError):
                    pass

            # Best outdoor range accumulator
            out = s.get("outdoor_temp_start")
            if out is not None:
                try:
                    out_f = float(out)
                    if   out_f < 30: label = "Below 30°C"
                    elif out_f < 35: label = "30-35°C"
                    elif out_f < 40: label = "35-40°C"
                    else:            label = "Above 40°C"
                    range_rates[label].append(rate)
                except (TypeError, ValueError):
                    pass

        if not cooling_rates:
            return _build_empty_insights("no_usable_data")

        n            = len(valid_pool)
        avg_rate     = _safe_div(sum(cooling_rates), len(cooling_rates))
        avg_eff      = _safe_div(sum(efficiencies),  len(efficiencies)) if efficiencies else 0.0
        avg_cool_min = _safe_div(sum(cool_times),    len(cool_times))   if cool_times   else 0.0

        # Best target temp: highest average cooling rate
        best_temp: Optional[float] = None
        if temp_rates:
            best_temp = max(
                temp_rates,
                key=lambda t: _safe_div(sum(temp_rates[t]), len(temp_rates[t]))
            )

        # Best outdoor range: highest average cooling rate
        best_outdoor: Optional[str] = None
        if range_rates:
            best_outdoor = max(
                range_rates,
                key=lambda l: _safe_div(sum(range_rates[l]), len(range_rates[l]))
            )

        # Trend: compare last 3 vs rest
        trend: Optional[str] = None
        if len(cooling_rates) >= 5:
            recent_avg = _safe_div(sum(cooling_rates[:3]), 3)
            older_pool = cooling_rates[3:]
            older_avg  = _safe_div(sum(older_pool), max(len(older_pool), 1))
            if older_avg > 0:
                if   recent_avg > older_avg * 1.1: trend = "improving"
                elif recent_avg < older_avg * 0.9: trend = "declining"
                else:                              trend = "stable"

        metrics = {
            "avg_cooling_rate":    round(avg_rate, 4),
            "avg_efficiency":      round(avg_eff,  4),
            "avg_cool_time_min":   round(avg_cool_min, 1),
            "best_target_temp":    best_temp,
            "best_outdoor_range":  best_outdoor,
            "cooling_type_counts": type_counts,
            "trend":               trend,
        }

        return {
            "has_data":           True,
            "reason":             None,
            "sessions_analyzed":  n,
            "fallback_used":      fallback_used,
            # Flat backward-compatible keys
            "avg_cooling_rate":   metrics["avg_cooling_rate"],
            "avg_efficiency":     metrics["avg_efficiency"],
            "best_target_temp":   best_temp,
            "best_outdoor_range": best_outdoor,
            "cooling_type_counts": type_counts,
            "trend":              trend,
            # Structured block for new InsightsCard
            "metrics":            metrics,
        }

    except Exception as exc:
        logger.error("[HawaAI] get_insights() failed: %s", exc, exc_info=True)
        return _build_empty_insights("error")


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
