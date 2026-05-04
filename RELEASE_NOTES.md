# HawaAI add-on — release notes

## 1.4.31

- **UI:** Fixed room logs panel height at 300px with internal scrolling and bottom-aware auto-scroll. Sessions now show full datetimes, date grouping, Stored/Invalid badges, null-safe cost display, and clearer empty states.
- **Control API:** Replaced user climate-command 429s with a short coalescing command queue. Rapid temperature changes are debounced in the UI and merged on the backend so only the latest command is sent.

## 1.4.30

- **Control:** Protect the pending ON transition from premature OFF decisions. After the single ON emit, OFF is blocked until confirmation or the pending ON timeout, preventing `ON → OFF → ON` command thrashing while the compressor ramps up.
- **Tests:** Added regression coverage for OFF blocking during pending ON and release after timeout.

## 1.4.29

- **Control:** Prevent duplicate delayed ON commands while a pending ON is awaiting confirmation. The engine now locks repeated thermostat ON decisions after the single IR emit, preserves the pending cycle until power/HA confirmation or timeout, and clears the pending lock on confirmation, manual override, or `on_failed` timeout.
- **Tests:** Added regression coverage for single-shot delayed ON, pending ON decision locking, timeout recovery, and pending state cleanup.

## 1.4.13

- **Frontend:** `RoomDataContext` — clearer `setRoomData` naming; room reset stays a proper object/updater (`status` / `snapshots` / `ai` / `stats` plus loading + previous\* for soft load). Ensures reproducible addon builds cache no stale JSX.

## 1.4.12

- **Runtime / telemetry:** Ensure a cooling **session exists before telemetry snapshots** (including HA startup when the AC is already ON), with a reconcile path before writing snapshots — avoids dropped ML/telemetry rows.
- **Control:** User API authority window **bypasses the global IR cooldown** for ON gating (`_gate_turn_ac_on`); other guards unchanged.
- **Safety:** Critical OFF paths (**safety**, **thermostat_reached**) always use **`force=True`** to `_turn_ac_off` explicitly.
- **Presence:** Unknown/unavailable presence falls back to **last known** occupancy, otherwise **occupied=True**.
- **Rooms:** **`room_id`** normalized (**lower**, **strip**) for runtime, scheduler, **`get_runtime_state`**, API and WebSocket room resolution (**case-insensitive** config lookup via `resolve_room_definition`).
- **Logging:** `[TICK]` lines include **`temp_mode`** (schedule/temperature mode) and **`ha_mode`** (climate entity mode).
- **Tests:** Unit coverage for normalize/resolve and mixed-case **`get_runtime_state`**.

## 1.4.0

- **UI:** Fully responsive Dashboard and app shell — bottom navigation on phones, top navigation on tablets/desktop; grids and cards (`container-app`); charts use fluid heights; larger touch targets; no horizontal overflow.
- **Control:** Manual setpoint lock when HA differs from schedule/AI intent (timed); command cooldown, duplicate suppression, ±0.5 °C hysteresis band, compressor min ON/OFF, meaningful setpoint deltas; logs skip reasons (`logic_engine`, `smart_cooling`, narrower Aerostate deadband).
- **Scheduling:** Temperature schedule slots, modes (`manual` / `schedule` / `schedule_ai`), timezone-aware bases, bounded ±1 °C AI; Settings + `TemperaturePlanCard` telemetry.

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
