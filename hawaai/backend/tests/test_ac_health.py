"""AC health analytics stays advisory and room-isolated."""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import aiosqlite

_HAWAAI = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _HAWAAI not in sys.path:
    sys.path.insert(0, _HAWAAI)

from backend import ac_health, database  # noqa: E402


CREATE_SESSIONS = """
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    start_time DATETIME NOT NULL,
    end_time DATETIME,
    indoor_temp_start REAL,
    indoor_temp_end REAL,
    outdoor_temp_start REAL,
    outdoor_humidity_start REAL,
    target_temp REAL,
    reason_stopped TEXT,
    peak_watt_draw REAL,
    avg_watt_draw REAL,
    hour_of_day INTEGER,
    is_archived INTEGER DEFAULT 0,
    room_id TEXT DEFAULT '',
    cooling_time REAL,
    energy_consumed_kwh REAL,
    energy_used REAL,
    user_override INTEGER DEFAULT 0,
    provisional INTEGER DEFAULT 0,
    is_record_valid INTEGER DEFAULT 1
)
"""


async def _insert_session(db, room_id, idx, *, days_ago, duration_min, delta):
    start = datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc) + timedelta(days=idx - days_ago)
    end = start + timedelta(minutes=duration_min)
    await db.execute(
        """
        INSERT INTO sessions(
            session_id, start_time, end_time, indoor_temp_start, indoor_temp_end,
            outdoor_temp_start, outdoor_humidity_start, target_temp, reason_stopped,
            peak_watt_draw, avg_watt_draw, hour_of_day, is_archived, room_id,
            cooling_time, energy_consumed_kwh, energy_used, user_override,
            provisional, is_record_valid
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 0, 0, 1)
        """,
        (
            f"{room_id}-{idx}",
            start.isoformat(),
            end.isoformat(),
            28.0,
            28.0 - delta,
            34.0,
            55.0,
            24.0,
            "thermostat_reached",
            900.0,
            650.0,
            14,
            room_id,
            duration_min,
            max(0.01, duration_min * 0.010),
            max(0.01, duration_min * 0.010),
        ),
    )


class TestACHealth(unittest.IsolatedAsyncioTestCase):
    async def test_health_is_room_isolated_and_learning_until_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "hawaai.db")
            async with aiosqlite.connect(db_path) as db:
                await db.execute(CREATE_SESSIONS)
                for i in range(36):
                    await _insert_session(
                        db,
                        "master",
                        i,
                        days_ago=36 - i,
                        duration_min=60 if i >= 30 else 30,
                        delta=3.0,
                    )
                for i in range(8):
                    await _insert_session(
                        db,
                        "media",
                        i,
                        days_ago=8 - i,
                        duration_min=30,
                        delta=3.0,
                    )
                await db.commit()

            with mock.patch.object(database, "DB_PATH", db_path):
                master = await ac_health.get_room_health("master")
                media = await ac_health.get_room_health("media")

            self.assertEqual(master["room_id"], "master")
            self.assertEqual(master["phase"], "active")
            self.assertGreaterEqual(master["stable_session_count"], 30)
            self.assertTrue(
                any(a["type"] == "cooling_degradation" for a in master["advisories"])
            )

            self.assertEqual(media["room_id"], "media")
            self.assertEqual(media["phase"], "learning")
            self.assertLess(media["stable_session_count"], 30)


if __name__ == "__main__":
    unittest.main()
