# HawaAI add-on — release notes

## 1.4.94

- **Tuya IR ON reliability:** Automated Tuya ON now uses the proven `set_hvac_mode(cool)` physical command, confirms via power telemetry, preserves fan/swing by default, and avoids extra temperature/fan/swing IR beeps unless a real change is needed.

## 1.4.91

- **LG fan guard:** Added a room-scoped guard for LG F1-F5/Turbo fan modes so automation never sends Turbo, while explicit user Turbo remains allowed and can time out back to the last safe fan.

## 1.4.90

- **Physical remote state:** Reconciles trusted live power telemetry into physical AC ON/OFF state so remote-driven AC starts show as ON without faking occupancy or bypassing safety logic.

## 1.4.89

- **Pre-cool:** Added user-triggered arrival cooling holds that block vacancy OFF while active, hand off to normal occupied logic on arrival, and expire back to normal vacancy behavior on no-show.
- **Vacancy timeout:** Settings now allows `0 min`, which turns AC off as soon as vacancy is confirmed while preserving debounce and pre-cool protection.

## 1.4.88

- **Climate controls:** Removed pre-dispatch debounce from climate-card commands, added latest-state-wins command coalescing, and protected pending user setpoints from stale status rollback.

## 1.4.87

- **Auto Comfort:** Fixed repeated manual-override clearing/log spam by only clearing on real temperature-mode transitions, refreshed Auto Comfort reasoning logs, and corrected misleading power telemetry labels.

## 1.4.85

- **Manual Override persistence:** Made Manual Override a durable per-room user-authority state that survives restarts, updates, reloads, and reconnects until the user explicitly disables it.

## 1.4.84

- **Dashboard reliability:** Improved live refresh lifecycle with WebSocket stale detection, safe fallback polling, focus/visibility refreshes, and background session updates so the dashboard no longer needs manual browser refreshes.

## 1.4.83

- **Comfort intelligence:** Added adaptive room thermal-load comfort compensation with saturation awareness, subtle target offsets, and a compact room-load status indicator.
- **Health UI:** Refined learning-mode presentation so AC Health shows stable-session progress instead of a misleading percentage ring.

## 1.4.82

- **Dashboard polish:** Refined AC Health and Recent Sessions with a more premium compact visual hierarchy, dense session rows, sticky grouped scrolling, and a stable fixed-height sessions panel.

## 1.4.81

- **AC health:** Added advisory-only per-room AC health analytics with room-local baselines, learning mode, telemetry quality, filter runtime tracking, and a compact dashboard health card.

## 1.4.80

- **Climate controls UI:** Compact mode, fan, and swing controls into single-tap selectors while preserving optimistic updates, debounce, and existing climate API commands.

## 1.4.79

- **Climate controls UI:** Redesigned the dashboard AC controls into a premium thermostat-style card with debounced setpoint control, dynamic mode/fan/swing chips, and visual-only runtime status badges.

## 1.4.78

- **Config migration:** Persisted upgraded schema versions after the first successful migration so future restarts report only the current schema.
- **Startup stabilization:** Added a startup hydrate-only window that restores runtime/entity/telemetry state while suppressing active HVAC ON/OFF decisions until stabilization completes.

## 1.4.77

- **Config persistence:** Added schema-versioned, idempotent config migrations that preserve user-selected room entities and scrub transient runtime state from saved config.
- **Startup hydration:** Startup now waits briefly for HA entity/device hydration and audits saved entities without clearing or rewriting config during temporary unavailable states.
- **Analytics/UI:** Session history and analytics now compute durations only from finalized persisted session timestamps and calculate cost from numeric kWh x tariff.

## 1.4.76

- **OFF reliability:** Vacancy shutdown now enters OFF confirmation, verifies physical OFF by power/climate state, retries missed IR sends with a cap, and only finalizes runtime/session after confirmation.
- **Safety:** Re-entry cancels pending OFF retries, max retry failure leaves runtime ON instead of faking idle, and duplicate OFF dispatches are suppressed once pending, failed, or confirmed.

## 1.4.75

- **Energy config:** Broadened cumulative energy sensor discovery so kWh/usage/consumption entities can be selected even when Home Assistant metadata is incomplete.
- **Selection:** Added deterministic ranking for energy sensors, preferring explicit total-energy style entities while keeping entity id ordering stable for ties.

## 1.4.74

- **Zone authority:** In zone-gated rooms, generic presence can hold vacancy escalation but cannot promote runtime occupancy or arm thermostat ON until FP2 zone presence is confirmed.
- **Safety:** Required zone-gated thermostat ON now waits for confirmed zone presence, including during transient unusable zone sensor states.
- **Tests:** Added regressions for the UI/runtime split where the dashboard showed vacant while HVAC runtime could still treat the room as occupied.

## 1.4.73

- **Telemetry isolation:** Finalized breaker telemetry as an observational-only layer with independent telemetry confidence, cache, stale/offline status, and debounced invalid-state logging.
- **Runtime authority:** Preserved IR/runtime state as the AC ON/OFF authority; breaker watts no longer affect occupancy, vacancy, thermostat decisions, pending actions, or runtime state.
- **UI/API:** Added telemetry health fields and dashboard status while keeping HVAC runtime state separate from telemetry health.
- **Cleanup:** Removed legacy watt-authority remnants, duplicate runtime fields, and mixed telemetry naming.

## 1.4.61

- **Energy runtime:** Removed the remaining duplicate dashboard/status energy read path so runtime energy has one canonical source: `_read_runtime_energy()`.
- **UI consistency:** `/api/status` now reports current parsed runtime energy values instead of fetching and parsing energy sensors independently.
- **Tests:** Added coverage proving current invalid readings stay distinct from last-valid diagnostics.

## 1.4.60

- **Energy runtime:** Hardened room-scoped runtime power resolution so ticks use canonical `energy_power_entity` / `energy_kwh_entity` from the merged effective room config only.
- **Runtime safety:** Added safe numeric parsing that keeps invalid/unavailable sensor states distinct from real `0 W`, tracks last valid readings separately, and avoids poisoning AC/session state.
- **Dynamic updates:** Room saves now trigger an immediate non-blocking runtime tick so energy config changes apply without an add-on restart.
- **Compatibility:** Removed legacy energy field reads from runtime paths; aliases remain only at ingestion/migration boundaries.

## 1.4.59

- **Energy config:** Fixed room-scoped Energy Monitoring persistence so breaker device, live power sensor, and kWh sensor survive save, reload, restart, and room edits.
- **Compatibility:** Migrates legacy/alternate energy field names into canonical room fields without silently dropping values.
- **Diagnostics:** Added `[ENERGY_CONFIG]` received, normalized, persisted, and loaded logs plus validation warnings for invalid energy entity ids.
- **Tests:** Added regressions for energy config save, reload, room merge, and alias migration.

## 1.4.58

- **Occupancy recovery:** Confirmed FP2 zone re-entry now immediately clears stale vacancy runtime state, safety-vacant holds, pending vacant OFF state, and thermostat suppression.
- **Diagnostics:** Added structured `[OCCUPANCY] recovery_triggered` and `[RUNTIME] vacancy_cleared reason=zone_reentry` logs plus runtime vacancy audit fields.
- **Tests:** Added regressions proving zone-confirmed occupancy overrides stale vacancy and resumes thermostat control without repeated IR spam.

## 1.4.57

- **Runtime:** Added an OFF dispatch settlement latch so vacancy/manual OFF commands are idempotent after a successful OFF.
- **Control:** Suppresses duplicate OFF IR sends during reconciliation or after terminal OFF settlement, preventing cooldown refresh loops and stuck IDLE UI state.
- **Tests:** Added regressions proving AeroState/Tuya OFF dispatches do not repeat, cooldown expiry does not redispatch OFF, and UI-facing runtime settles to OFF.

## 1.4.56

- **Runtime:** Added terminal OFF reconciliation so transitional IDLE state clears after OFF cooldown, session finalization, HA OFF, and below-compressor power settle.
- **Diagnostics:** Added `[RUNTIME]` lifecycle logs for idle entry, reconciliation completion, stale idle cleanup, and finalized OFF.
- **Tests:** Added regressions for vacancy OFF, AeroState OFF, Tuya delayed power drop, cooldown expiry, manual OFF, and finalized-session runtime cleanup.

## 1.4.55

- **Sessions:** Hardened Session History persistence so confirmed cooling sessions promote from provisional, wait briefly for delayed power/meter updates, and persist stable duration/energy/validity metrics.
- **Validation:** Relaxed read-side quality checks to accept real runtime evidence such as confirmed runtime, power samples, energy delta, cooling duration, or temperature drop.
- **Tests:** Added regressions for delayed power confirmation, presence-only shutdown, AeroState runtime, Tuya power lag, runtime reset, and short accidental ON handling.

## 1.4.54

- **Runtime resilience:** Added a passive Self-Healing Runtime Engine that validates runtime, HA climate, power, sensor, pending, and session consistency without sending IR or changing thermostat decisions.
- **Recovery:** Safely clears stale pending flags, rebuilds runtime truth from settled HA/power observations, closes orphan sessions, releases aged failed-ON retry locks, and uses short-lived cached HA values during instability.
- **Diagnostics:** Added confidence scores, structured `[SELF_HEAL]` change-based logs, runtime `self_heal` status, and regression coverage for stale/desynced runtime states.

## 1.4.53

- **Dashboard:** Added a read-only comfort intelligence panel showing sleep optimization, humidity, feels-like temperature, dew point, comfort band, humidity adjustment, and dry-mode recommendation from live status data.
- **Versioning:** Aligned add-on metadata, frontend package metadata, and the FastAPI advertised version.

## 1.4.52

- **Comfort:** Added passive sleep optimization and humidity-aware comfort layers that adjust effective targets without changing ON/OFF, cooldown, delay, adapter, presence, or session logic.
- **Analytics:** Exposes sleep phase/offset plus humidity, feels-like temperature, dew point, comfort score, comfort band, and dry-mode recommendation.
- **Tests:** Added regression coverage for sleep progression, high-heat suspension, humidity offsets, dew point, dry-mode recommendation, and sleep/humidity target stacking.

## 1.4.51

- **Presence-only:** Vacant rooms that are still physically running now send OFF immediately, wait for power-confirmed OFF, then finalize runtime/session cleanup and enter `presence_idle`.

## 1.4.50

- **Presence-only:** Vacant rooms that are already physically OFF now finalize once, clear stale pending/session/runtime state, and enter `presence_idle` instead of looping forever as `hold/presence_only`.
- **Presence-only:** Reuses the main tick's stabilized occupancy decision so vacancy debounce is not evaluated twice in the same cycle.
- **Diagnostics:** Added `[PRESENCE_ONLY]` target logs for OFF finalization, runtime reset, idle entry, vacancy timestamp, and duplicate OFF-block detection.

## 1.4.49

- **Control:** Fixed thermostat target synchronization so schedule/AI/manual effective targets are the single runtime source for TICK decisions.
- **Runtime:** Clears stale manual target/session references when the configured temperature plan changes, preventing old climate setpoints from causing false `thermostat_reached` shutdowns.
- **Hardening:** Added safe numeric config fallbacks and regression coverage for malformed config values.

## 1.4.46

- **IR:** Broadlink ON now follows AeroState staging: `set_hvac_mode(cool)`, wait 2s, then `set_temperature(target)` without bundling `hvac_mode` into the temperature call.
- **Control:** IR send lock now records after the full ON sequence completes successfully.

## 1.4.45

- **IR:** Broadlink ON now dispatches one combined `climate.set_temperature` payload with `hvac_mode` and target temperature, avoiding split IR packets that can cause ON/OFF/ON toggles.
- **Control:** Added a 10s IR send lock and 20s post-ON stabilization window to suppress immediate OFF/ON retriggers while HA climate state settles.

## 1.4.44

- **IR:** Broadlink ON now sends `climate.set_hvac_mode` before the temperature command when the climate entity is off, preventing rapid double-IR power cycling.
- **Sessions:** Provisional session timeout no longer clears setpoint tracking while the AC may still be running, avoiding a redundant follow-up temperature command.

## 1.4.43

- **IR:** Fixed Tuya AC power-on by sending `climate.set_hvac_mode` before temperature and fan commands.

## 1.4.42

- **Control:** Prevent overlapping event-triggered ticks, extend the decision lock past the scheduler interval, and fix presence-only session lifecycle argument order.

## 1.4.41

- **Control:** Added explicit presence enter/exit hysteresis state with stable occupancy, confirmed vacancy, and confirmed ON anchors.
- **Control:** Vacancy OFF now logs and holds for unstable vacancy, pending ON, and post-ON protection before allowing `safety_vacant`.
- **Tests:** Added regression coverage for brief vacancy flicker after ON, post-ON OFF blocking, and stable vacancy eventually allowing OFF.

## 1.4.40

- **Control:** Added a 90s minimum ON-time OFF block using effective ON time or recent ON command time, so startup vacancy noise cannot force OFF while power is still ramping.
- **Tests:** Added regression coverage for forced vacancy OFF being blocked during the minimum ON window.

## 1.4.39

- **Runtime:** Fixed `_tick_impl` crash by initializing timezone-aware `now` before presence stabilization.
- **Tests:** Added regression coverage for tick-time initialization before stabilized presence is evaluated.

## 1.4.38

- **Presence:** Added a 60s stabilization layer so false vacancy spikes are held as occupied before control decisions see them.
- **Tests:** Added regression coverage for ignored false spikes and stable vacancy after the stabilization window.

## 1.4.37

- **Control:** Fan-only / idle power now counts as ON for physical control state, pending ON confirmation, and vacancy protection.
- **Presence:** Vacancy debounce now returns an explicit `vacancy_debounce` hold during the first 60s instead of allowing early OFF evaluation.
- **Tests:** Added regression coverage for fan-only wattage being treated as ON.

## 1.4.36

- **IR:** Added Tuya delayed-ON double emit: the first command wakes/syncs the IR climate state, and a second ON emit follows after 2s if physical power has not confirmed.
- **Control:** Running OFF protection now uses `effective_on_since_ts` or recent ON command time, so Broadlink startup power delay still blocks unstable vacancy OFF.
- **Presence:** Added a 60s minimum vacancy confirmation floor before vacancy OFF can fire.
- **Tests:** Added regression coverage for Tuya double emit, ON-command-based running protection, and minimum vacancy confirmation.

## 1.4.35

- **IR:** Added per-room `ir_backend` profile with default `broadlink`. Broadlink rooms keep the existing adapter path; Tuya rooms send ON as one combined `set_temperature` payload (`temperature` + `hvac_mode`) with optional supported `set_fan_mode`.
- **Control:** Added a 180s running-state OFF block while HA reports cooling, preventing unstable vacancy signals from shutting down a freshly running AC.
- **UI:** Added an IR backend selector on each room's AC Control card.
- **Tests:** Added regression coverage for Broadlink dispatch, Tuya combined ON, no Broadlink combined call for Tuya, duplicate pending ON guard, and unchanged OFF path.

## 1.4.33

- **Control:** Block every OFF source during the pending ON confirmation window, including `safety_vacant`. The pending ON startup phase is now atomic even if presence briefly flips false while the AC is still confirming.
- **Tests:** Added regression coverage for vacancy OFF protection during pending ON, including the conservative missing-timestamp case.

## 1.4.32

- **Control:** Added `presence_only` room mode for rooms without indoor temperature sensors. In this mode HawaAI uses occupancy only, with confirmed-presence dwell before ON, vacancy grace before OFF, max runtime failsafe, and safe hold on unavailable presence.
- **UI:** Added a room control-mode selector, hides thermostat/AI/weather temperature fields in presence-only mode, and shows a mode helper note.
- **Tests:** Added regression coverage for no-temp presence-only decisions, dwell, vacancy OFF, unavailable presence safety, max runtime failsafe, and thermostat default behavior.

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
