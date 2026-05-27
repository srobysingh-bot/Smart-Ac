"""Read-only per-room AC health analytics.

This module observes completed session telemetry only. It never calls climate
commands, never mutates runtime state, and never participates in HVAC decisions.
"""

import json
import math
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, List, Optional

import aiosqlite

from . import database

MIN_BASELINE_SESSIONS = 30
MIN_BASELINE_DAYS = 7
CACHE_SECONDS = 15 * 60
FILTER_RECOMMEND_HOURS = 180.0
FILTER_SERVICE_HOURS = 260.0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    raw = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _num(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        n = float(value)
        if not math.isfinite(n):
            return None
        return n
    except (TypeError, ValueError):
        return None


def _safe_div(a: float, b: float) -> Optional[float]:
    if b <= 0:
        return None
    return a / b


def _hour_bucket(hour: Optional[int]) -> str:
    if hour is None:
        return "unknown"
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _session_feature(row: Dict[str, Any], presence_stable: Optional[bool] = None) -> Optional[Dict[str, Any]]:
    s = database._enrich_session(row)
    if presence_stable is False:
        return None
    if not s.get("valid"):
        return None
    try:
        if int(s.get("user_override", 0) or 0) != 0:
            return None
    except (TypeError, ValueError):
        return None
    try:
        if int(s.get("provisional", 0) or 0) != 0:
            return None
    except (TypeError, ValueError):
        return None
    try:
        if int(s.get("is_record_valid", 1) or 0) == 0:
            return None
    except (TypeError, ValueError):
        return None

    reason = str(s.get("reason_stopped") or "").lower()
    noisy_reason_tokens = (
        "manual",
        "override",
        "power_off",
        "startup",
        "telemetry_failed",
        "sensor_unavailable",
        "unavailable",
        "invalid",
    )
    if any(token in reason for token in noisy_reason_tokens):
        return None

    duration = _num(s.get("duration_minutes"))
    delta = _num(s.get("delta_temp"))
    start_temp = _num(s.get("indoor_temp_start"))
    end_temp = _num(s.get("indoor_temp_end"))
    if duration is None or delta is None or start_temp is None or end_temp is None:
        return None
    if duration < 5.0 or duration > 360.0 or delta < 0.3:
        return None

    energy = _num(s.get("energy_consumed_kwh"))
    avg_watts = _num(s.get("avg_watt_draw"))
    peak_watts = _num(s.get("peak_watt_draw"))
    if energy is not None and (energy < 0 or energy > 10):
        return None
    if avg_watts is not None and avg_watts < 0:
        return None
    if peak_watts is not None and peak_watts < 0:
        return None

    start = _parse_ts(s.get("start_time"))
    end = _parse_ts(s.get("end_time"))
    if not start or not end:
        return None

    rate = _safe_div(delta, duration)
    runtime_per_degree = _safe_div(duration, delta)
    if not rate or not runtime_per_degree:
        return None

    hour = s.get("hour_of_day")
    try:
        hour_int = int(hour) if hour is not None else start.hour
    except (TypeError, ValueError):
        hour_int = start.hour

    return {
        "session_id": s.get("session_id"),
        "start_time": start,
        "end_time": end,
        "duration_minutes": duration,
        "delta_temp": delta,
        "cooling_rate": rate,
        "runtime_per_degree": runtime_per_degree,
        "energy_per_degree": _safe_div(energy, delta) if energy and energy > 0 else None,
        "outdoor_temp": _num(s.get("outdoor_temp_start")),
        "humidity": _num(s.get("outdoor_humidity_start")),
        "start_temp": start_temp,
        "target_temp": _num(s.get("target_temp")),
        "hour_bucket": _hour_bucket(hour_int),
        "avg_watts": avg_watts,
        "peak_watts": peak_watts,
        "has_power": avg_watts is not None or peak_watts is not None or energy is not None,
    }


def _similar(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    checks = 0
    passed = 0
    for key, tolerance in (
        ("outdoor_temp", 4.0),
        ("humidity", 12.0),
        ("start_temp", 1.5),
        ("target_temp", 1.0),
    ):
        av = a.get(key)
        bv = b.get(key)
        if av is None or bv is None:
            continue
        checks += 1
        if abs(float(av) - float(bv)) <= tolerance:
            passed += 1
    if a.get("hour_bucket") != "unknown" and b.get("hour_bucket") != "unknown":
        checks += 1
        if a.get("hour_bucket") == b.get("hour_bucket"):
            passed += 1
    return checks >= 2 and passed >= max(2, math.ceil(checks * 0.6))


def _median(values: List[Optional[float]]) -> Optional[float]:
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not clean:
        return None
    return float(median(clean))


def _ratio(recent: Optional[float], baseline: Optional[float]) -> Optional[float]:
    if recent is None or baseline is None or baseline <= 0:
        return None
    return recent / baseline


async def _ensure_schema() -> None:
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ac_health_profiles (
                room_id TEXT PRIMARY KEY,
                computed_at DATETIME NOT NULL,
                stable_session_count INTEGER DEFAULT 0,
                profile_json TEXT NOT NULL
            )
            """
        )
        await db.commit()


async def _read_cached(room_id: str) -> Optional[Dict[str, Any]]:
    cutoff = _now_utc() - timedelta(seconds=CACHE_SECONDS)
    async with aiosqlite.connect(database.DB_PATH) as db:
        async with db.execute(
            """
            SELECT computed_at, profile_json
            FROM ac_health_profiles
            WHERE room_id = ?
            """,
            (room_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    computed = _parse_ts(row[0])
    if not computed or computed < cutoff:
        return None
    try:
        return json.loads(row[1])
    except Exception:
        return None


async def _write_profile(room_id: str, profile: Dict[str, Any]) -> None:
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO ac_health_profiles(room_id, computed_at, stable_session_count, profile_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(room_id) DO UPDATE SET
                computed_at = excluded.computed_at,
                stable_session_count = excluded.stable_session_count,
                profile_json = excluded.profile_json
            """,
            (
                room_id,
                profile.get("computed_at"),
                int(profile.get("stable_session_count") or 0),
                json.dumps(profile, ensure_ascii=False),
            ),
        )
        await db.commit()


async def _fetch_sessions(room_id: str) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(database.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT *
            FROM sessions
            WHERE room_id = ?
              AND is_archived = 0
              AND end_time IS NOT NULL
            ORDER BY start_time DESC
            LIMIT 300
            """,
            (room_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def _fetch_presence_stability(session_ids: List[str]) -> Dict[str, Optional[bool]]:
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    try:
        async with aiosqlite.connect(database.DB_PATH) as db:
            async with db.execute(
                f"""
                SELECT session_id, COUNT(*) AS n, MIN(presence) AS min_presence, MAX(presence) AS max_presence
                FROM snapshots
                WHERE session_id IN ({placeholders})
                  AND presence IS NOT NULL
                GROUP BY session_id
                """,
                ids,
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return {}
    stability: Dict[str, Optional[bool]] = {}
    for sid, n, min_presence, max_presence in rows:
        if not n:
            stability[str(sid)] = None
        elif int(n) >= 2 and min_presence != max_presence:
            stability[str(sid)] = False
        else:
            stability[str(sid)] = True
    return stability


def _build_learning(room_id: str, total: int, stable: List[Dict[str, Any]], telemetry_quality: float) -> Dict[str, Any]:
    days = 0
    if stable:
        starts = [s["start_time"] for s in stable if s.get("start_time")]
        if starts:
            days = max(1, (_now_utc() - min(starts)).days + 1)
    session_progress = min(1.0, len(stable) / MIN_BASELINE_SESSIONS)
    day_progress = min(1.0, days / MIN_BASELINE_DAYS)
    progress = max(session_progress, day_progress)
    runtime_hours = sum(float(s.get("duration_minutes") or 0) for s in stable) / 60.0
    return {
        "room_id": room_id,
        "computed_at": _now_utc().isoformat(),
        "phase": "learning",
        "status": "learning",
        "status_label": "Learning",
        "confidence": "low",
        "summary": "Learning room cooling profile",
        "stable_session_count": len(stable),
        "total_session_count": total,
        "learning": {
            "progress": round(progress, 2),
            "sessions_needed": max(0, MIN_BASELINE_SESSIONS - len(stable)),
            "days_observed": days,
            "minimum_sessions": MIN_BASELINE_SESSIONS,
            "minimum_days": MIN_BASELINE_DAYS,
            "message": "Collecting baseline telemetry",
        },
        "telemetry_quality": {
            "score": round(telemetry_quality, 2),
            "label": "Limited" if telemetry_quality < 0.7 else "Good",
        },
        "filter": {
            "runtime_hours": round(runtime_hours, 1),
            "status": "tracking",
            "progress": round(min(1.0, runtime_hours / FILTER_RECOMMEND_HOURS), 2),
        },
        "metrics": {},
        "trends": {},
        "runtime_statistics": {
            "stable_runtime_hours": round(runtime_hours, 1),
            "stable_sessions": len(stable),
        },
        "degradation_history": [],
        "advisories": [
            {
                "type": "learning",
                "severity": "info",
                "confidence": "low",
                "title": "Learning room cooling profile",
                "message": "Health advisories remain informational until this room has enough stable completed sessions.",
            }
        ],
    }


def _build_profile(room_id: str, total: int, stable: List[Dict[str, Any]], telemetry_quality: float) -> Dict[str, Any]:
    recent = stable[: min(6, max(3, len(stable) // 5))]
    older = stable[len(recent):]
    matched: List[Dict[str, Any]] = []
    for r in recent:
        room_matches = [o for o in older if _similar(r, o)]
        if room_matches:
            matched.extend(room_matches[:8])
    if len(matched) < 8:
        matched = older

    baseline_rate = _median([s.get("cooling_rate") for s in matched])
    recent_rate = _median([s.get("cooling_rate") for s in recent])
    baseline_runtime = _median([s.get("runtime_per_degree") for s in matched])
    recent_runtime = _median([s.get("runtime_per_degree") for s in recent])
    baseline_energy = _median([s.get("energy_per_degree") for s in matched])
    recent_energy = _median([s.get("energy_per_degree") for s in recent])

    cooling_ratio = _ratio(recent_rate, baseline_rate)
    runtime_ratio = _ratio(recent_runtime, baseline_runtime)
    energy_ratio = _ratio(recent_energy, baseline_energy)

    runtime_hours = sum(float(s.get("duration_minutes") or 0) for s in stable) / 60.0
    power_covered = sum(1 for s in stable if s.get("has_power"))
    matched_count = len(matched)
    confidence = "high" if matched_count >= 18 and telemetry_quality >= 0.75 else "medium"
    if matched_count < 8 or telemetry_quality < 0.55:
        confidence = "low"

    advisories: List[Dict[str, Any]] = []
    status = "excellent"
    summary = "Cooling performance is tracking near this room's learned baseline."

    degraded = bool(
        cooling_ratio is not None
        and runtime_ratio is not None
        and cooling_ratio < 0.82
        and runtime_ratio > 1.18
    )
    if degraded:
        severity = "warning" if confidence != "high" else "attention"
        status = "attention" if confidence != "low" else "watch"
        summary = "Cooling efficiency reduced compared to this room's historical baseline."
        advisories.append({
            "type": "cooling_degradation",
            "severity": severity,
            "confidence": confidence,
            "title": "Cooling efficiency reduced",
            "message": "Recent sessions are cooling more slowly than this room's own similar historical sessions.",
        })

    if runtime_ratio is not None and runtime_ratio > 1.25 and confidence != "low":
        status = "attention" if status == "excellent" else status
        advisories.append({
            "type": "runtime_trend",
            "severity": "warning",
            "confidence": confidence,
            "title": "Long runtime trend detected",
            "message": "Recent runtime per degree cooled is higher than this room's learned baseline under similar conditions.",
        })

    if energy_ratio is not None and energy_ratio > 1.25 and confidence != "low":
        advisories.append({
            "type": "efficiency_trend",
            "severity": "warning",
            "confidence": confidence,
            "title": "Cooling energy trend increased",
            "message": "Recent energy per degree cooled is higher than this room's historical baseline.",
        })

    filter_status = "good"
    if runtime_hours >= FILTER_SERVICE_HOURS:
        filter_status = "service"
        advisories.append({
            "type": "filter_runtime",
            "severity": "warning",
            "confidence": "medium",
            "title": "Filter cleaning recommended",
            "message": "Tracked cooling runtime is high. Cleaning the AC filter may improve airflow.",
        })
        if status == "excellent":
            status = "watch"
    elif runtime_hours >= FILTER_RECOMMEND_HOURS:
        filter_status = "due_soon"
        advisories.append({
            "type": "filter_runtime",
            "severity": "info",
            "confidence": "medium",
            "title": "Filter maintenance due soon",
            "message": "Tracked cooling runtime is approaching the filter cleaning advisory window.",
        })

    if not advisories:
        advisories.append({
            "type": "normal",
            "severity": "info",
            "confidence": confidence,
            "title": "No maintenance advisory",
            "message": "Recent performance is consistent with this room's learned baseline.",
        })

    return {
        "room_id": room_id,
        "computed_at": _now_utc().isoformat(),
        "phase": "active",
        "status": status,
        "status_label": {
            "excellent": "Excellent",
            "watch": "Watch",
            "attention": "Attention",
            "service": "Service",
        }.get(status, "Good"),
        "confidence": confidence,
        "summary": summary,
        "stable_session_count": len(stable),
        "total_session_count": total,
        "learning": {
            "progress": 1.0,
            "sessions_needed": 0,
            "days_observed": max(1, (_now_utc() - min(s["start_time"] for s in stable)).days + 1),
            "minimum_sessions": MIN_BASELINE_SESSIONS,
            "minimum_days": MIN_BASELINE_DAYS,
            "message": "Room-specific baseline active",
        },
        "telemetry_quality": {
            "score": round(telemetry_quality, 2),
            "label": "Strong" if telemetry_quality >= 0.8 else ("Fair" if telemetry_quality >= 0.55 else "Limited"),
        },
        "filter": {
            "runtime_hours": round(runtime_hours, 1),
            "status": filter_status,
            "progress": round(min(1.0, runtime_hours / FILTER_RECOMMEND_HOURS), 2),
        },
        "metrics": {
            "baseline_cooling_rate": round(baseline_rate, 3) if baseline_rate is not None else None,
            "recent_cooling_rate": round(recent_rate, 3) if recent_rate is not None else None,
            "baseline_runtime_per_degree": round(baseline_runtime, 2) if baseline_runtime is not None else None,
            "recent_runtime_per_degree": round(recent_runtime, 2) if recent_runtime is not None else None,
            "similar_sessions": matched_count,
        },
        "trends": {
            "cooling_rate_ratio": round(cooling_ratio, 2) if cooling_ratio is not None else None,
            "runtime_ratio": round(runtime_ratio, 2) if runtime_ratio is not None else None,
            "energy_ratio": round(energy_ratio, 2) if energy_ratio is not None else None,
        },
        "runtime_statistics": {
            "stable_runtime_hours": round(runtime_hours, 1),
            "stable_sessions": len(stable),
            "power_covered_sessions": power_covered,
        },
        "degradation_history": [
            {
                "period": "recent",
                "cooling_rate_ratio": round(cooling_ratio, 2) if cooling_ratio is not None else None,
                "runtime_ratio": round(runtime_ratio, 2) if runtime_ratio is not None else None,
                "similar_sessions": matched_count,
            }
        ],
        "advisories": advisories[:3],
    }


async def get_room_health(room_id: str) -> Dict[str, Any]:
    """Return a room-isolated advisory health profile from completed sessions."""
    rid = (room_id or "").strip()
    await _ensure_schema()
    cached = await _read_cached(rid)
    if cached:
        return cached

    rows = await _fetch_sessions(rid)
    total = len(rows)
    presence_stability = await _fetch_presence_stability([str(r.get("session_id") or "") for r in rows])
    stable = [_session_feature(r, presence_stability.get(str(r.get("session_id") or ""))) for r in rows]
    stable = [s for s in stable if s is not None]
    stable.sort(key=lambda s: s["start_time"], reverse=True)
    telemetry_quality = (len(stable) / max(total, 1)) if total else 0.0

    if len(stable) < MIN_BASELINE_SESSIONS:
        profile = _build_learning(rid, total, stable, telemetry_quality)
    else:
        oldest = min(s["start_time"] for s in stable)
        days = (_now_utc() - oldest).days + 1
        if days < MIN_BASELINE_DAYS:
            profile = _build_learning(rid, total, stable, telemetry_quality)
        else:
            profile = _build_profile(rid, total, stable, telemetry_quality)

    await _write_profile(rid, profile)
    return profile
