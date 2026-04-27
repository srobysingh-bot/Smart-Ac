# HawaAI add-on — release notes

## 1.2.0

The add-on lives in the repository folder **`hawaai/`** (same name as the add-on `slug` in `config.yaml`). Home Assistant builds from that directory; it must contain `Dockerfile` and `config.yaml`.

**New: Optional AI Optimization (Gemma)**

- AI can improve cooling decisions (soft setpoint / fan hints via optional Ollama HTTP layer).
- **Disabled by default** — existing installations behave exactly as before until you enable AI in Settings.
- Requires the separate **Ollama AI** add-on only if you want this feature; HawaAI runs fully without it.
- No impact on existing users who do not enable AI.

Rollback: install add-on version **1.1.28** (or your previous backup) from a saved backup or git tag; config remains forward-compatible (new keys are additive).
