# HawaAI add-on — release notes

## 1.3.3

- **UX:** `RoomContext` — active room persists across Dashboard, Sessions, Analytics, and Settings (`localStorage` + `?room_id=` on all sidebar links).
- **ML / data quality:** Every `ai_decisions` row has a **`snapshot_id`** (minimal snapshot if needed); optional **`indoor_humidity_entity`** and **`indoor_humidity`** on snapshots; **`time_to_target_minutes`** and **`temp_drop_rate`** from session snapshots; full **`raw_json`** (no truncation); **`user_adjusted`**, **`user_target_temp`**, **`adjustment_delay_seconds`** on AI rows (heuristic from climate vs AI setpoint).
- API: room CRUD accepts **`indoor_humidity_entity`**.

## 1.3.2

- Persistent SQLite at `/data/hawaai.db` with `map: data:rw`; automatic DB backup on add-on startup and shutdown (under `/data/hawaai_db_backups/`, pruned).
- Non-destructive schema evolution (`ALTER TABLE` only); snapshots extended for ML (humidity, setpoint, fan, power, meter kWh, AI fields); sessions include `cooling_time`, `energy_used`, `user_override`; new `ai_decisions` table linked by `room_id` and `session_id`.
- Session end prefers **meter delta** kWh when `energy_kwh_entity` + start reading exist; else estimates from power samples.
- API: `GET /api/ai/decisions?room_id=&limit=`.
- Dashboard: separate **temperature vs time** and **energy vs time** charts; **AI decision history** card.

## 1.3.1

- Production multi-room isolation: room-scoped WebSocket subscribe/broadcast, required `room_id` on status, sessions, analytics, snapshots, exports, and AI status APIs.
- Per-room analytics and dashboard health (climate, sensors, AI); stricter `merge_room_config` (no `rooms` blob leakage); `GET /api/weather` for uncoupled outdoor preview in Settings.

## 1.2.1

- Fix: add-on build directory renamed to **`hawaai/`** to match the `slug: hawaai` in `config.yaml` (ensures `Dockerfile` and `config.yaml` are on the build path Home Assistant uses).

## 1.2.0

**New: Optional AI Optimization (Gemma)**

- AI can improve cooling decisions (soft setpoint / fan hints via optional Ollama HTTP layer).
- **Disabled by default** — existing installations behave exactly as before until you enable AI in Settings.
- Requires the separate **Ollama AI** add-on only if you want this feature; HawaAI runs fully without it.
- No impact on existing users who do not enable AI.

Rollback: install add-on version **1.1.28** (or your previous backup) from a saved backup or git tag; config remains forward-compatible (new keys are additive).
