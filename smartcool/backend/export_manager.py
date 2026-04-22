"""CSV and JSON export for ML training data."""

import csv
import io
import json
import logging
from datetime import datetime

from . import database

logger = logging.getLogger(__name__)

_CSV_COLUMNS = [
    "session_id",
    "date",
    "start_time",
    "end_time",
    "indoor_temp_start",
    "indoor_temp_end",
    "outdoor_temp_start",
    "outdoor_humidity_start",
    "target_temp",
    "time_to_cool_minutes",
    "energy_consumed_kwh",
    "peak_watt_draw",
    "avg_watt_draw",
    "ac_brand",
    "ac_model",
    "reason_stopped",
    "room_name",
    "day_of_week",
    "hour_of_day",
]


async def export_csv() -> str:
    """Return all sessions as a UTF-8 CSV string."""
    rows = await database.get_all_sessions_for_export()

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()

    for row in rows:
        # Derive date column from start_time
        start = row.get("start_time") or ""
        row["date"] = start[:10] if len(start) >= 10 else ""
        writer.writerow(row)

    return buf.getvalue()


async def export_json() -> str:
    """Return all sessions as a JSON array string."""
    rows = await database.get_all_sessions_for_export()
    return json.dumps(rows, indent=2, ensure_ascii=False, default=str)


def export_filename(ext: str = "csv") -> str:
    """Generate a timestamped export filename."""
    ts = datetime.utcnow().strftime("%Y%m%d")
    return f"smartcool_data_{ts}.{ext}"
