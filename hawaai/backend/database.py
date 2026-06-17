"""SQLite database schema and query helpers for HawaAI."""

import aiosqlite
import json
import logging
import shutil
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DB_PATH = "/data/hawaai.db"
BACKUP_DIR = "/data/hawaai_db_backups"
MAX_DB_BACKUPS = 8


def backup_db(tag: str = "manual") -> None:
    """
    Copy SQLite DB to /data/hawaai_db_backups/ (survives addon image rebuilds when map data:rw).
    Prunes oldest backups beyond MAX_DB_BACKUPS. Never uses DROP.
    """
    try:
        src = Path(DB_PATH)
        if not src.is_file():
            logger.info("[DB] Skip backup — no database file yet at %s", DB_PATH)
            return
        dest_dir = Path(BACKUP_DIR)
        dest_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        dest = dest_dir / f"hawaai_{tag}_{ts}.db"
        shutil.copy2(src, dest)
        logger.info("[DB] Backup written: %s", dest)
        backups = sorted(dest_dir.glob("hawaai_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[MAX_DB_BACKUPS:]:
            try:
                old.unlink()
                logger.info("[DB] Pruned old backup %s", old.name)
            except OSError as exc:
                logger.warning("[DB] Could not remove %s: %s", old, exc)
    except Exception as exc:
        logger.error("[DB] Backup failed: %s", exc, exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

async def init_db() -> None:
    """Create all tables and indexes if they do not already exist."""
    Path("/data").mkdir(parents=True, exist_ok=True)
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

        await db.execute("""
            CREATE TABLE IF NOT EXISTS ai_decisions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             DATETIME NOT NULL,
                room_id        TEXT NOT NULL,
                session_id     TEXT,
                snapshot_id    INTEGER,
                target_temp    REAL,
                fan_mode       TEXT,
                confidence     REAL,
                action         TEXT,
                provider       TEXT,
                model          TEXT,
                raw_json       TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS auto_comfort_profiles (
                room_id      TEXT PRIMARY KEY,
                updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                profile_json TEXT NOT NULL
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
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_decisions_room ON ai_decisions(room_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_decisions_ts   ON ai_decisions(ts)"
        )

        # Analytics columns — added non-destructively so existing DBs keep working.
        # SQLite returns an error if the column already exists; we suppress it.
        for col_sql in (
            "ALTER TABLE sessions ADD COLUMN cooling_rate  REAL",   # °C / min
            "ALTER TABLE sessions ADD COLUMN cooling_type  TEXT",   # fast / normal / slow
            "ALTER TABLE sessions ADD COLUMN efficiency    REAL",   # °C / kWh
            "ALTER TABLE sessions ADD COLUMN room_id TEXT DEFAULT ''",
            "ALTER TABLE sessions ADD COLUMN cooling_time REAL",
            "ALTER TABLE sessions ADD COLUMN energy_used REAL",
            "ALTER TABLE sessions ADD COLUMN user_override INTEGER DEFAULT 0",
            "ALTER TABLE snapshots ADD COLUMN room_id TEXT DEFAULT ''",
            "ALTER TABLE snapshots ADD COLUMN outdoor_humidity REAL",
            "ALTER TABLE snapshots ADD COLUMN setpoint REAL",
            "ALTER TABLE snapshots ADD COLUMN fan_mode TEXT",
            "ALTER TABLE snapshots ADD COLUMN power_watts REAL",
            "ALTER TABLE snapshots ADD COLUMN energy_kwh REAL",
            "ALTER TABLE snapshots ADD COLUMN ai_target_temp REAL",
            "ALTER TABLE snapshots ADD COLUMN ai_fan_mode TEXT",
            "ALTER TABLE snapshots ADD COLUMN ai_confidence REAL",
            "ALTER TABLE snapshots ADD COLUMN indoor_humidity REAL",
            "ALTER TABLE sessions ADD COLUMN time_to_target_minutes REAL",
            "ALTER TABLE sessions ADD COLUMN temp_drop_rate REAL",
            "ALTER TABLE ai_decisions ADD COLUMN user_adjusted INTEGER DEFAULT 0",
            "ALTER TABLE ai_decisions ADD COLUMN user_target_temp REAL",
            "ALTER TABLE ai_decisions ADD COLUMN adjustment_delay_seconds REAL",
            "ALTER TABLE snapshots ADD COLUMN schedule_slot TEXT",
            "ALTER TABLE snapshots ADD COLUMN schedule_base_temp REAL",
            "ALTER TABLE snapshots ADD COLUMN effective_after_weather REAL",
            "ALTER TABLE snapshots ADD COLUMN effective_final_temp REAL",
            "ALTER TABLE snapshots ADD COLUMN ai_adjust_applied INTEGER",
        ):
            try:
                await db.execute(col_sql)
            except Exception:
                pass  # column already exists — safe to ignore

        for stmt in (
            "ALTER TABLE snapshots ADD COLUMN target_temp REAL",
            "ALTER TABLE snapshots ADD COLUMN control_source TEXT",
            "ALTER TABLE snapshots ADD COLUMN hvac_mode TEXT",
            "ALTER TABLE sessions ADD COLUMN provisional INTEGER DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN is_record_valid INTEGER DEFAULT 1",
            "ALTER TABLE sessions ADD COLUMN energy_source TEXT",
            "ALTER TABLE sessions ADD COLUMN energy_quality TEXT",
            "ALTER TABLE sessions ADD COLUMN telemetry_gap_seconds REAL",
            "ALTER TABLE sessions ADD COLUMN meter_start_kwh REAL",
            "ALTER TABLE sessions ADD COLUMN meter_end_kwh REAL",
        ):
            try:
                await db.execute(stmt)
            except Exception:
                pass  # column already exists — safe to ignore

        await db.commit()
    logger.info("Database ready at %s", DB_PATH)


async def get_auto_comfort_profile(room_id: str) -> Optional[Dict[str, Any]]:
    """Return persisted Auto Comfort learning profile for one room, if present."""
    rid = (room_id or "").strip()
    if not rid:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT profile_json FROM auto_comfort_profiles WHERE room_id = ?",
            (rid,),
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return None
    try:
        data = json.loads(row[0] or "{}")
    except (TypeError, json.JSONDecodeError):
        logger.warning("[AUTO_COMFORT] invalid persisted profile for room=%s", rid)
        return None
    return data if isinstance(data, dict) else None


async def upsert_auto_comfort_profile(room_id: str, profile: Dict[str, Any]) -> None:
    """Persist one room's Auto Comfort profile non-destructively."""
    rid = (room_id or "").strip()
    if not rid:
        return
    payload = dict(profile or {})
    payload["room_id"] = rid
    payload["updated_at"] = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO auto_comfort_profiles(room_id, updated_at, profile_json)
            VALUES (?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(room_id) DO UPDATE SET
                updated_at = excluded.updated_at,
                profile_json = excluded.profile_json
            """,
            (rid, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        await db.commit()


async def reset_auto_comfort_profile(room_id: str) -> None:
    """Delete persisted Auto Comfort learning for one room only."""
    rid = (room_id or "").strip()
    if not rid:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM auto_comfort_profiles WHERE room_id = ?", (rid,))
        await db.commit()


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
      energy_consumed_kwh — None stays None, negative→0, spikes > 10 kWh → None
      cost_estimate       — None when energy is missing, otherwise non-negative float
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
    s["duration_minutes"] = round(duration_min, 2) if duration_min is not None else None

    # ── Delta temperature ─────────────────────────────────────────────────────
    try:
        t_start = float(s["indoor_temp_start"]) if s.get("indoor_temp_start") is not None else None
        t_end   = float(s["indoor_temp_end"])   if s.get("indoor_temp_end")   is not None else None
        s["delta_temp"] = round(t_start - t_end, 2) if (t_start is not None and t_end is not None) else None
    except (TypeError, ValueError):
        s["delta_temp"] = None

    # ── Energy normalisation (API layer only) ─────────────────────────────────
    energy_missing = s.get("energy_consumed_kwh") is None
    try:
        e = float(s["energy_consumed_kwh"]) if not energy_missing else None
        if e is None:
            s["energy_consumed_kwh"] = None
        else:
            e = max(0.0, e)
            if e > 10.0:            # unrealistic spike — treat as missing data
                logger.debug("Session %s: energy spike %.2f kWh treated as missing", s.get("session_id"), e)
                e = None
            s["energy_consumed_kwh"] = round(e, 4) if e is not None else None
    except (TypeError, ValueError):
        s["energy_consumed_kwh"] = None

    # ── Cost normalisation ────────────────────────────────────────────────────
    try:
        if s["energy_consumed_kwh"] is None:
            cost_raw = None
        else:
            raw = s.get("cost_estimate")
            cost_raw = round(max(0.0, float(raw)), 2) if raw is not None else 0.0
    except (TypeError, ValueError):
        cost_raw = None
    s["cost_estimate"] = cost_raw

    # Backward-compatible aliases for UI/export consumers.
    s["energy_used"] = s["energy_consumed_kwh"]
    energy_quality = str(s.get("energy_quality") or "").strip().lower()
    if not energy_quality:
        if s["energy_consumed_kwh"] is None:
            energy_quality = "energy_unknown"
        elif s.get("energy_source") == "meter_delta":
            energy_quality = "good"
        else:
            energy_quality = "partial"
    s["energy_quality"] = energy_quality
    quality_labels = {
        "good": "Good",
        "partial": "Partial",
        "telemetry_gap": "Telemetry gap",
        "energy_unknown": "Energy unknown",
        "recovered": "Recovered",
        "invalid": "Invalid",
    }
    s["quality_label"] = quality_labels.get(energy_quality, "Partial")

    # ── Validity flag ─────────────────────────────────────────────────────────
    energy_ok = s["energy_consumed_kwh"] is None or s["energy_consumed_kwh"] >= 0
    try:
        stored_valid = int(s.get("is_record_valid", 1) or 0) != 0
    except (TypeError, ValueError):
        stored_valid = True
    try:
        provisional = int(s.get("provisional", 0) or 0) == 1
    except (TypeError, ValueError):
        provisional = False
    try:
        cooling_time = float(s.get("cooling_time")) if s.get("cooling_time") is not None else None
    except (TypeError, ValueError):
        cooling_time = None
    try:
        peak_watts = float(s.get("peak_watt_draw") or 0)
        avg_watts = float(s.get("avg_watt_draw") or 0)
    except (TypeError, ValueError):
        peak_watts = avg_watts = 0.0
    energy_positive = bool(
        s["energy_consumed_kwh"] is not None
        and s["energy_consumed_kwh"] >= 0.001
    )
    power_seen = bool(peak_watts > 0 or avg_watts > 0)
    evidence_ok = bool(
        (duration_min is not None and duration_min >= 3.0)
        or (s["delta_temp"] is not None and s["delta_temp"] >= 0.3)
        or energy_positive
        or power_seen
        or (cooling_time is not None and cooling_time >= 3.0)
    )
    s["valid"] = bool(
        s.get("end_time") is not None
        and stored_valid
        and not provisional
        and energy_ok
        and evidence_ok
    )
    if not s["valid"]:
        s["quality_label"] = "Invalid"
    elif s["energy_consumed_kwh"] is None:
        s["quality_label"] = "Energy unknown"
    elif energy_quality == "good":
        s["quality_label"] = "Good"

    return s


# ─────────────────────────────────────────────────────────────────────────────
# Sessions
# ─────────────────────────────────────────────────────────────────────────────

async def insert_session_start(session: Dict[str, Any]) -> None:
    rid = (session.get("room_id") or "").strip()
    if not rid:
        logger.error("[DB] insert_session_start rejected — missing room_id (session_id=%s)", session.get("session_id"))
        raise ValueError("room_id is required for insert_session_start")
    logger.info("[DB] insert session room_id=%s session_id=%s", rid, session.get("session_id"))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO sessions
                (session_id, start_time, indoor_temp_start, outdoor_temp_start,
                 outdoor_humidity_start, target_temp, ac_entity_id, ac_brand,
                 ac_model, room_name, presence_trigger, energy_start_kwh,
                 day_of_week, hour_of_day, room_id, provisional, is_record_valid,
                 meter_start_kwh, energy_source, energy_quality)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                rid,
                int(session.get("provisional", 0) or 0),
                int(session.get("is_record_valid", 1)),
                session.get("meter_start_kwh"),
                session.get("energy_source"),
                session.get("energy_quality"),
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
                efficiency           = ?,
                cooling_time         = ?,
                energy_used          = ?,
                user_override        = ?,
                time_to_target_minutes = ?,
                temp_drop_rate       = ?,
                energy_source        = ?,
                energy_quality       = ?,
                telemetry_gap_seconds = ?,
                meter_start_kwh      = COALESCE(meter_start_kwh, ?),
                meter_end_kwh        = ?,
                is_record_valid      = COALESCE(?, is_record_valid)
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
                end_data.get("cooling_time"),
                end_data.get("energy_used"),
                end_data.get("user_override"),
                end_data.get("time_to_target_minutes"),
                end_data.get("temp_drop_rate"),
                end_data.get("energy_source"),
                end_data.get("energy_quality"),
                end_data.get("telemetry_gap_seconds"),
                end_data.get("meter_start_kwh"),
                end_data.get("meter_end_kwh"),
                end_data.get("is_record_valid"),
                session_id,
            ),
        )
        await db.commit()


async def clear_session_provisional_flag(session_id: str) -> None:
    """Mark session as compressor-confirmed (no longer provisional in DB)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET provisional = 0 WHERE session_id = ?",
            (session_id,),
        )
        await db.commit()


async def get_sessions(
    room_id: str,
    limit: int = 50,
    offset: int = 0,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[Dict]:
    rid = (room_id or "").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM sessions WHERE is_archived = 0 AND end_time IS NOT NULL AND room_id = ?"
        params: list = [rid]
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
    room_id: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> int:
    rid = (room_id or "").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        query = "SELECT COUNT(*) FROM sessions WHERE is_archived = 0 AND end_time IS NOT NULL AND room_id = ?"
        params: list = [rid]
        if date_from:
            query += " AND start_time >= ?"
            params.append(date_from)
        if date_to:
            query += " AND start_time <= ?"
            params.append(date_to)
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def get_all_sessions_for_export(room_id: str) -> List[Dict]:
    rid = (room_id or "").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions WHERE room_id = ? ORDER BY start_time DESC",
            (rid,),
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


def _parse_row_ts(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    s = str(val).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(str(val)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


async def compute_cooling_snapshot_metrics(session_id: str) -> Dict[str, Optional[float]]:
    """
    Derive time_to_target_minutes and temp_drop_rate (°C/min) from session snapshots only.

    Target band: first snapshot time where indoor_temp <= session.target_temp + 0.25 °C.
    If never reached, fall back to (start indoor − last indoor) / snapshot span in minutes.
    """
    out: Dict[str, Optional[float]] = {
        "time_to_target_minutes": None,
        "temp_drop_rate": None,
    }
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT start_time, indoor_temp_start, target_temp
            FROM sessions WHERE session_id = ?
            """,
            (session_id,),
        ) as cur:
            sess = await cur.fetchone()
        if not sess:
            return out
        start_wall = _parse_row_ts(sess["start_time"])
        if start_wall is None:
            return out
        try:
            indoor_start = float(sess["indoor_temp_start"]) if sess["indoor_temp_start"] is not None else None
        except (TypeError, ValueError):
            indoor_start = None
        try:
            target = float(sess["target_temp"]) if sess["target_temp"] is not None else None
        except (TypeError, ValueError):
            target = None
        async with db.execute(
            """
            SELECT timestamp, indoor_temp FROM snapshots
            WHERE session_id = ?
            ORDER BY timestamp ASC
            """,
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()

    if not rows or indoor_start is None:
        return out

    band = 0.25
    if target is not None:
        threshold = target + band
        for r in rows:
            if r["indoor_temp"] is None:
                continue
            try:
                ti = float(r["indoor_temp"])
            except (TypeError, ValueError):
                continue
            ts = _parse_row_ts(r["timestamp"])
            if ts is None:
                continue
            if ti <= threshold:
                minutes = max(0.0, (ts - start_wall).total_seconds() / 60.0)
                if minutes > 0.05:
                    drop = indoor_start - ti
                    out["time_to_target_minutes"] = round(minutes, 2)
                    out["temp_drop_rate"] = round(drop / minutes, 4) if drop > 0 else None
                return out

    # Fallback: net drop over observed snapshot window
    parsed: List[Tuple[datetime, float]] = []
    for r in rows:
        if r["indoor_temp"] is None:
            continue
        ts = _parse_row_ts(r["timestamp"])
        if ts is None:
            continue
        try:
            parsed.append((ts, float(r["indoor_temp"])))
        except (TypeError, ValueError):
            continue
    if len(parsed) < 2:
        return out
    t0, v0 = parsed[0]
    t1, v1 = parsed[-1]
    span = (t1 - t0).total_seconds() / 60.0
    if span < 0.5:
        return out
    drop = v0 - v1
    if drop <= 0:
        return out
    out["temp_drop_rate"] = round(drop / span, 4)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Snapshots
# ─────────────────────────────────────────────────────────────────────────────

async def insert_snapshot(snapshot: Dict[str, Any]) -> int:
    rid = (snapshot.get("room_id") or "").strip()
    if not rid:
        logger.error("[DB] insert_snapshot rejected — missing room_id (session_id=%s)", snapshot.get("session_id"))
        raise ValueError("room_id is required for insert_snapshot")
    sid = snapshot.get("session_id")
    if sid is None or (isinstance(sid, str) and not sid.strip()):
        logger.debug("[DB] insert_snapshot skipped — missing session_id (room_id=%s)", rid)
        return 0
    logger.info("[DB] writing snapshot for room_id=%s session_id=%s", rid, snapshot.get("session_id"))
    w = snapshot.get("watt_draw")
    if w is None:
        w = snapshot.get("power_watts")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO snapshots
                (session_id, timestamp, indoor_temp, outdoor_temp, outdoor_humidity,
                 indoor_humidity, ac_state, watt_draw, power_watts, presence, room_id,
                 setpoint, fan_mode, energy_kwh,
                 ai_target_temp, ai_fan_mode, ai_confidence,
                 schedule_slot, schedule_base_temp, effective_after_weather,
                 effective_final_temp, ai_adjust_applied,
                 target_temp, control_source, hvac_mode)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot.get("session_id"),
                snapshot.get("timestamp"),
                snapshot.get("indoor_temp"),
                snapshot.get("outdoor_temp"),
                snapshot.get("outdoor_humidity"),
                snapshot.get("indoor_humidity"),
                1 if snapshot.get("ac_state") else 0,
                w,
                w,
                1 if snapshot.get("presence") else 0,
                rid,
                snapshot.get("setpoint"),
                snapshot.get("fan_mode"),
                snapshot.get("energy_kwh"),
                snapshot.get("ai_target_temp"),
                snapshot.get("ai_fan_mode"),
                snapshot.get("ai_confidence"),
                snapshot.get("schedule_slot"),
                snapshot.get("schedule_base_temp"),
                snapshot.get("effective_after_weather"),
                snapshot.get("effective_final_temp"),
                snapshot.get("ai_adjust_applied"),
                snapshot.get("target_temp"),
                snapshot.get("control_source"),
                snapshot.get("hvac_mode"),
            ),
        )
        await db.commit()
        return int(cur.lastrowid)


async def insert_ai_decision(row: Dict[str, Any]) -> int:
    """Persist one AI model output for ML / audit. Returns new row id."""
    rid = (row.get("room_id") or "").strip()
    if not rid:
        logger.error("[DB] insert_ai_decision rejected — missing room_id (session_id=%s)", row.get("session_id"))
        raise ValueError("room_id is required for insert_ai_decision")
    logger.info("[DB] insert ai_decision room_id=%s session_id=%s", rid, row.get("session_id"))
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO ai_decisions
                (ts, room_id, session_id, snapshot_id, target_temp, fan_mode, confidence,
                 action, provider, model, raw_json,
                 user_adjusted, user_target_temp, adjustment_delay_seconds)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row.get("ts"),
                rid,
                row.get("session_id"),
                row.get("snapshot_id"),
                row.get("target_temp"),
                row.get("fan_mode"),
                row.get("confidence"),
                row.get("action"),
                row.get("provider"),
                row.get("model"),
                row.get("raw_json"),
                int(row.get("user_adjusted") or 0),
                row.get("user_target_temp"),
                row.get("adjustment_delay_seconds"),
            ),
        )
        await db.commit()
        return int(cur.lastrowid)


async def update_ai_decision_ml_labels(
    decision_id: int,
    *,
    user_adjusted: int,
    user_target_temp: Optional[float],
    adjustment_delay_seconds: float,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE ai_decisions SET
                user_adjusted = ?,
                user_target_temp = ?,
                adjustment_delay_seconds = ?
            WHERE id = ?
            """,
            (user_adjusted, user_target_temp, adjustment_delay_seconds, decision_id),
        )
        await db.commit()


async def get_ai_decisions_recent(
    room_id: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    rid = (room_id or "").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM ai_decisions
            WHERE room_id = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (rid, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_snapshots_recent(minutes: int = 120, room_id: str = "") -> List[Dict]:
    since = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
    rid = (room_id or "").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM snapshots
            WHERE timestamp >= ? AND room_id = ?
            ORDER BY timestamp ASC
            """,
            (since, rid),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_snapshots_for_ml(room_id: str, limit: int = 10000) -> List[Dict[str, Any]]:
    """Rows with non-null ML-critical fields for export."""
    rid = (room_id or "").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                session_id, room_id, timestamp, indoor_temp, outdoor_temp,
                indoor_humidity, outdoor_humidity, target_temp, effective_final_temp,
                ac_state, power_watts, fan_mode, hvac_mode, presence,
                control_source, schedule_slot, ai_adjust_applied
            FROM snapshots
            WHERE room_id = ?
              AND session_id IS NOT NULL
              AND indoor_temp IS NOT NULL
              AND ac_state IS NOT NULL
              AND presence IS NOT NULL
              AND control_source IS NOT NULL
              AND effective_final_temp IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (rid, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────────────────────

def _today_utc_bounds(timezone_name: str = "UTC") -> Tuple[str, str]:
    try:
        tz = ZoneInfo(str(timezone_name or "UTC"))
    except Exception:
        tz = timezone.utc
    local_today = datetime.now(tz).date()
    local_start = datetime.combine(local_today, datetime.min.time(), tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).isoformat(),
        local_end.astimezone(timezone.utc).isoformat(),
    )


async def get_today_stats(
    room_id: str,
    tariff_per_kwh: float = 8.0,
    timezone_name: str = "UTC",
) -> Dict[str, Any]:
    rid = (room_id or "").strip()
    try:
        tariff = float(tariff_per_kwh)
        if tariff < 0:
            tariff = 8.0
    except (TypeError, ValueError):
        tariff = 8.0
    today, tomorrow = _today_utc_bounds(timezone_name)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT
                COUNT(*)                              AS session_count,
                SUM(energy_consumed_kwh)              AS total_kwh,
                COUNT(energy_consumed_kwh)            AS energy_session_count,
                COALESCE(SUM(
                    (JULIANDAY(end_time) - JULIANDAY(start_time)) * 1440
                ), 0)                                 AS total_ac_minutes
            FROM sessions
            WHERE start_time >= ? AND start_time < ?
              AND is_archived = 0
              AND end_time IS NOT NULL
              AND room_id = ?
              AND COALESCE(is_record_valid, 1) != 0
              AND COALESCE(provisional, 0) != 1
            """,
            (today, tomorrow, rid),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                total_kwh = None if row[1] is None else max(0.0, float(row[1]))
                return {
                    "session_count": row[0],
                    "total_kwh": round(total_kwh, 3) if total_kwh is not None else None,
                    "total_cost": round(total_kwh * tariff, 2) if total_kwh is not None else None,
                    "energy_session_count": int(row[2] or 0),
                    "total_ac_minutes": round(row[3], 1),
                }
    return {
        "session_count": 0,
        "total_kwh": None,
        "total_cost": None,
        "energy_session_count": 0,
        "total_ac_minutes": 0.0,
    }


async def get_daily_stats(days: int = 7, room_id: str = "", tariff_per_kwh: float = 8.0) -> List[Dict]:
    rid = (room_id or "").strip()
    try:
        tariff = float(tariff_per_kwh)
        if tariff < 0:
            tariff = 8.0
    except (TypeError, ValueError):
        tariff = 8.0
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT
                DATE(start_time)                       AS date,
                COUNT(*)                               AS sessions,
                SUM(energy_consumed_kwh)               AS kwh,
                COUNT(energy_consumed_kwh)             AS energy_sessions,
                COALESCE(AVG(
                    (JULIANDAY(end_time) - JULIANDAY(start_time)) * 1440
                ), 0)                                  AS avg_cool_time
            FROM sessions
            WHERE start_time >= DATE('now', ?)
              AND is_archived = 0
              AND end_time IS NOT NULL
              AND room_id = ?
              AND COALESCE(is_record_valid, 1) != 0
              AND COALESCE(provisional, 0) != 1
            GROUP BY DATE(start_time)
            ORDER BY date ASC
            """,
            (f"-{days} days", rid),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "date": r[0],
                    "sessions": r[1],
                    "kwh": (
                        round(max(0.0, float(r[2])), 3)
                        if r[2] is not None
                        else None
                    ),
                    "cost": (
                        round(max(0.0, float(r[2])) * tariff, 2)
                        if r[2] is not None
                        else None
                    ),
                    "energy_sessions": int(r[3] or 0),
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


async def get_insights(room_id: str) -> Dict[str, Any]:
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
        rid = (room_id or "").strip()
        # ── Fetch recent completed sessions ───────────────────────────────────
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM sessions
                WHERE end_time IS NOT NULL
                  AND is_archived = 0
                  AND room_id = ?
                ORDER BY start_time DESC
                LIMIT 200
                """,
                (rid,),
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


async def get_ml_stats(room_id: str) -> Dict[str, Any]:
    rid = (room_id or "").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT
                COUNT(*) AS total,
                AVG((JULIANDAY(end_time) - JULIANDAY(start_time)) * 1440) AS avg_cool,
                COUNT(CASE WHEN COALESCE(is_record_valid, 1) != 0 AND COALESCE(provisional, 0) != 1 THEN 1 END) * 100.0 / MAX(COUNT(*), 1) AS completeness
            FROM sessions
            WHERE room_id = ?
              AND end_time IS NOT NULL
            """,
            (rid,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "total_sessions": row[0] or 0,
                    "avg_cool_time": round(row[1] or 0, 1),
                    "data_completeness": round(row[2] or 0, 1),
                }
    return {"total_sessions": 0, "avg_cool_time": 0.0, "data_completeness": 0.0}


async def delete_room_data(room_id: str) -> None:
    """Remove all persisted analytics rows for one room (sessions, snapshots, AI audit)."""
    rid = (room_id or "").strip().lower()
    if not rid:
        return
    match = "LOWER(TRIM(COALESCE(room_id, ''))) = ?"
    async with aiosqlite.connect(DB_PATH) as conn:
        try:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(f"DELETE FROM snapshots WHERE {match}", (rid,))
            await conn.execute(f"DELETE FROM ai_decisions WHERE {match}", (rid,))
            await conn.execute(f"DELETE FROM sessions WHERE {match}", (rid,))
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    logger.warning("[DB] Purged all data for room_id=%s", rid)
