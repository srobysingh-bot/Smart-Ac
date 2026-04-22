# SmartCool AC Optimizer

> AI-powered AC optimization for Home Assistant — presence-aware, energy-tracking, and ML-ready.

[![HACS Badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Addon](https://img.shields.io/badge/Home%20Assistant-Add--on-blue)](https://www.home-assistant.io/addons/)

---

## Features

- **Presence-aware automation** — Uses Aqara FP2 or any binary sensor to detect occupancy
- **Smart temperature control** — Target temp + hysteresis band prevents rapid cycling
- **Energy monitoring** — Tracks kWh per session, calculates cost in any currency
- **Outside temperature logic** — Skips cooling when outdoor temp is already comfortable
- **Broadlink IR control** — Supports 10+ AC brands with full IR profiles
- **Session logging** — Every cooling event recorded with full environmental context
- **ML data export** — Download clean CSV for training predictive models
- **Live dashboard** — Real-time temp chart, AC status, energy gauges
- **HA sensor publishing** — SmartCool sensors available in your HA automations

---

## Installation via HACS

1. In HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add `https://github.com/yourusername/smartcool` as **Integration**
3. Install **SmartCool AC Optimizer**
4. Restart Home Assistant
5. Go to **Settings → Add-ons → SmartCool** and configure

---

## Manual Installation

```bash
# Clone into your HA add-ons directory
cd /config/addons
git clone https://github.com/yourusername/smartcool
```

Then in HA: **Settings → Add-ons → Add-on Store → ⋮ → Check for updates**

---

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `ha_token` | string | `""` | Long-lived HA access token |
| `weather_api_key` | string | `""` | OpenWeatherMap / WeatherAPI key |
| `weather_city` | string | `""` | City name or `lat,lon` |
| `weather_provider` | string | `openweathermap` | `openweathermap` \| `weatherapi` \| `tomorrow` |
| `target_temp` | int | `24` | Target temperature °C |
| `hysteresis` | float | `1.5` | Dead-band around target (°C) |
| `vacancy_timeout_minutes` | int | `5` | Minutes vacant before AC turns off |
| `energy_tariff_per_kwh` | float | `8.0` | Cost per kWh |
| `currency` | string | `INR` | Currency symbol |
| `logic_interval_seconds` | int | `60` | Decision loop frequency |
| `presence_entity` | string | `""` | HA entity id of presence sensor |
| `indoor_temp_entity` | string | `""` | HA entity id of indoor temp sensor |
| `ac_switch_entity` | string | `""` | HA entity id of AC smart switch |
| `energy_sensor_entity` | string | `""` | HA entity id of power/energy sensor |
| `broadlink_entity` | string | `""` | HA entity id of Broadlink remote |
| `ac_brand` | string | `""` | Selected AC brand id |
| `ac_model` | string | `""` | Selected AC model id |
| `room_name` | string | `Living Room` | Room label |
| `use_presence` | bool | `true` | Enable presence-based control |
| `use_outdoor_temp` | bool | `true` | Factor in outdoor temperature |
| `manual_override` | bool | `false` | Disable all automation |

---

## Entities Published to HA

| Entity | Type | Description |
|---|---|---|
| `sensor.smartcool_indoor_temp` | sensor | Current indoor temp |
| `sensor.smartcool_outdoor_temp` | sensor | API outdoor temp |
| `sensor.smartcool_session_kwh` | sensor | Current session energy |
| `binary_sensor.smartcool_ac_active` | binary | AC on/off |
| `sensor.smartcool_daily_cost` | sensor | Today's AC cost |
| `sensor.smartcool_time_to_cool` | sensor | Last session cool time |

---

## Supported AC Brands

Daikin · LG · Samsung · Voltas · Carrier · Hitachi · Mitsubishi Electric · Panasonic · Haier · Blue Star

---

## ML Data Export

Navigate to **Analytics** in the SmartCool panel and click **Export CSV**.  
Each row = one cooling session with full environmental and energy context, ready for ML training.

---

## Architecture

```
HA WebSocket ──► ha_client.py ──► presence_handler / temp_handler
                                         │
                                  logic_engine.py (60s tick)
                                         │
                          ┌──────────────┼──────────────┐
                          ▼              ▼               ▼
                   ac_controller  session_logger   weather_api
                   (Broadlink IR)  (SQLite DB)     (10min cache)
                          │
                   FastAPI /api/* ──► React Dashboard
```

---

## License

MIT © 2024
