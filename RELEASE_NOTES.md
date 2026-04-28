# HawaAI add-on — release notes

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
