import { useEffect, useRef, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { getStatus, getSessionStats, getSnapshots, getClimateState, setClimateTemperature, setHvacMode, setFanMode, setSwingMode } from '../api/smartcool.js'
import ACStatusCard  from '../components/ACStatusCard.jsx'
import TempGauge     from '../components/TempGauge.jsx'
import EnergyChart   from '../components/EnergyChart.jsx'
import PresenceBadge from '../components/PresenceBadge.jsx'
import SessionTable  from '../components/SessionTable.jsx'
import { Thermometer, Wind, Zap, Cloud, AlertTriangle, Minus, Plus, Loader } from 'lucide-react'

// ── Config warning banner ─────────────────────────────────────────────────────
function ConfigWarning() {
  const navigate = useNavigate()
  return (
    <div className="flex items-center gap-3 mx-6 mt-4 px-4 py-3 bg-yellow-900/40 border border-yellow-700 rounded-lg text-sm">
      <AlertTriangle size={16} className="text-yellow-400 shrink-0" />
      <span className="text-yellow-200 flex-1">
        Sensors not configured — go to Settings to set up your devices
      </span>
      <button
        onClick={() => navigate('/settings')}
        className="px-3 py-1 bg-yellow-700 hover:bg-yellow-600 rounded text-yellow-100 text-xs font-medium transition-colors"
      >
        Go to Settings
      </button>
    </div>
  )
}

// ── Live status bar ───────────────────────────────────────────────────────────
function LiveStatusBar({ status }) {
  const { indoor_temp, outdoor_temp, presence, ac_on, watt_draw, ac_current_temp } = status || {}

  // When WiFi switch sensor is offline, fall back to AC unit's own thermistor
  const displayTemp   = indoor_temp ?? ac_current_temp
  const tempFromAC    = indoor_temp == null && ac_current_temp != null

  return (
    <div className="flex flex-wrap items-center gap-3 px-6 py-3 bg-gray-900 border-b border-gray-800 text-sm">
      <span className="flex items-center gap-1.5">
        <Thermometer size={15} className={tempFromAC ? 'text-blue-400' : 'text-orange-400'} />
        Indoor:{' '}
        <strong>
          {displayTemp != null ? `${displayTemp.toFixed(1)}°C` : '—'}
        </strong>
        {tempFromAC && (
          <span className="text-xs text-blue-400 ml-0.5" title="Reading from AC unit (WiFi sensor offline)">
            ⁽ᴬᶜ⁾
          </span>
        )}
      </span>
      <span className="text-gray-700">|</span>
      <span className="flex items-center gap-1.5">
        <Cloud size={15} className="text-sky-400" />
        Outside:{' '}
        <strong>{outdoor_temp != null ? `${outdoor_temp.toFixed(1)}°C` : '—'}</strong>
      </span>
      <span className="text-gray-700">|</span>
      <PresenceBadge present={presence} />
      <span className="text-gray-700">|</span>
      <span className="flex items-center gap-1.5">
        <Zap size={15} className={ac_on ? 'text-green-400' : 'text-gray-500'} />
        AC:{' '}
        <strong className={ac_on ? 'text-green-400' : 'text-gray-500'}>
          {ac_on ? 'ON' : 'OFF'}
        </strong>
        {ac_on && watt_draw > 0 && (
          <span className="text-gray-400">· {watt_draw.toFixed(0)} W</span>
        )}
      </span>
    </div>
  )
}

// ── Today / ML quality strip ──────────────────────────────────────────────────
function StatsStrip({ stats }) {
  const today = stats?.today || {}
  const ml    = stats?.ml    || {}
  return (
    <div className="grid grid-cols-2 gap-4">
      <div className="card">
        <p className="text-xs text-gray-500 uppercase tracking-wide mb-3">Today</p>
        <div className="grid grid-cols-2 gap-y-2 text-sm">
          <span className="text-gray-400">Sessions</span>
          <span className="font-semibold">{today.session_count ?? 0}</span>
          <span className="text-gray-400">Total AC time</span>
          <span className="font-semibold">{formatMinutes(today.total_ac_minutes)}</span>
          <span className="text-gray-400">Energy used</span>
          <span className="font-semibold">{(today.total_kwh ?? 0).toFixed(2)} kWh</span>
          <span className="text-gray-400">Cost</span>
          <span className="font-semibold text-yellow-400">₹{(today.total_cost ?? 0).toFixed(2)}</span>
        </div>
      </div>

      <div className="card">
        <p className="text-xs text-gray-500 uppercase tracking-wide mb-3">ML Data Quality</p>
        <div className="grid grid-cols-2 gap-y-2 text-sm">
          <span className="text-gray-400">Total sessions</span>
          <span className="font-semibold">{ml.total_sessions ?? 0}</span>
          <span className="text-gray-400">Avg cool time</span>
          <span className="font-semibold">{(ml.avg_cool_time ?? 0).toFixed(1)} min</span>
          <span className="text-gray-400">Completeness</span>
          <span className="font-semibold text-green-400">{(ml.data_completeness ?? 0).toFixed(1)}%</span>
        </div>
      </div>
    </div>
  )
}

function formatMinutes(mins) {
  if (!mins) return '0m'
  const h = Math.floor(mins / 60)
  const m = Math.round(mins % 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

// ── Climate card ──────────────────────────────────────────────────────────────
const HVAC_MODE_COLORS = {
  cool:     'bg-blue-600 text-white',
  heat:     'bg-orange-600 text-white',
  auto:     'bg-purple-600 text-white',
  dry:      'bg-yellow-600 text-white',
  fan_only: 'bg-teal-600 text-white',
  off:      'bg-gray-700 text-gray-300',
}
const HVAC_MODE_LABELS = {
  cool: 'Cool', heat: 'Heat', auto: 'Auto',
  dry: 'Dry', fan_only: 'Fan', off: 'Off',
}

function ClimateCard({ entityId }) {
  const [climate,  setClimate]  = useState(null)
  const [error,    setError]    = useState(null)
  const [busy,     setBusy]     = useState(false)   // pending control command

  const fetchClimate = useCallback(() => {
    getClimateState(entityId)
      .then(d => { setClimate(d); setError(null) })
      .catch(e => setError(e.message || String(e)))
  }, [entityId])

  // Initial fetch + 8-second polling
  useEffect(() => {
    fetchClimate()
    const id = setInterval(fetchClimate, 8_000)
    return () => clearInterval(id)
  }, [fetchClimate])

  const sendCommand = async (fn) => {
    setBusy(true)
    try {
      await fn()
      // Short delay then re-fetch so UI reflects confirmed state
      setTimeout(fetchClimate, 800)
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const adjustTemp = (delta) => {
    if (!climate) return
    const step = climate.target_temp_step || 1
    const next = Math.round(((climate.temperature ?? 24) + delta) / step) * step
    const clamped = Math.max(climate.min_temp ?? 16, Math.min(climate.max_temp ?? 30, next))
    sendCommand(() => setClimateTemperature(entityId, clamped))
  }

  if (error) {
    return (
      <div className="card">
        <p className="text-xs text-gray-500 uppercase tracking-wide mb-3">AC Climate</p>
        <div className="flex items-center gap-2 text-xs text-red-400">
          <AlertTriangle size={13} /> {error}
        </div>
        <p className="text-xs text-gray-600 mt-1">entity: {entityId}</p>
      </div>
    )
  }

  if (!climate) {
    return (
      <div className="card flex items-center gap-2 text-xs text-gray-500">
        <Loader size={13} className="animate-spin" /> Loading climate data…
      </div>
    )
  }

  const { hvac_mode, current_temperature, temperature, fan_mode, swing_mode,
          hvac_modes, fan_modes, swing_modes, friendly_name } = climate

  return (
    <div className="card space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs text-gray-500 uppercase tracking-wide">AC Climate</p>
          <p className="text-sm text-gray-300 mt-0.5">{friendly_name}</p>
        </div>
        <span className={`px-3 py-1 rounded-full text-xs font-semibold ${HVAC_MODE_COLORS[hvac_mode] ?? 'bg-gray-700 text-gray-300'}`}>
          {HVAC_MODE_LABELS[hvac_mode] ?? hvac_mode ?? '—'}
        </span>
      </div>

      {/* Temperature row */}
      <div className="flex items-center justify-between">
        {/* Current temp */}
        <div className="text-center">
          <p className="text-3xl font-bold text-blue-400">
            {current_temperature != null ? `${current_temperature.toFixed(1)}°` : '—'}
          </p>
          <p className="text-xs text-gray-500 mt-0.5">Current</p>
        </div>

        {/* Setpoint with ±controls */}
        <div className="flex flex-col items-center gap-1">
          <p className="text-xs text-gray-500">Setpoint</p>
          <div className="flex items-center gap-2">
            <button
              disabled={busy || hvac_mode === 'off'}
              onClick={() => adjustTemp(-1)}
              className="w-8 h-8 rounded-lg bg-gray-700 hover:bg-gray-600 disabled:opacity-40 flex items-center justify-center transition-colors"
            >
              <Minus size={14} />
            </button>
            <span className="text-2xl font-bold w-14 text-center">
              {temperature != null ? `${temperature}°` : '—'}
            </span>
            <button
              disabled={busy || hvac_mode === 'off'}
              onClick={() => adjustTemp(+1)}
              className="w-8 h-8 rounded-lg bg-gray-700 hover:bg-gray-600 disabled:opacity-40 flex items-center justify-center transition-colors"
            >
              <Plus size={14} />
            </button>
          </div>
        </div>

        {/* Fan mode */}
        <div className="text-center">
          <p className="text-xs text-gray-500 mb-1">Fan</p>
          {fan_modes && fan_modes.length > 0 ? (
            <select
              disabled={busy || hvac_mode === 'off'}
              value={fan_mode ?? ''}
              onChange={e => sendCommand(() => setFanMode(entityId, e.target.value))}
              className="bg-gray-700 border border-gray-600 rounded-lg px-2 py-1 text-xs text-gray-100 focus:outline-none focus:border-blue-500 disabled:opacity-40"
            >
              {fan_modes.map(m => <option key={m} value={m}>{m}</option>)}
            </select>
          ) : (
            <span className="text-sm font-semibold">{fan_mode ?? '—'}</span>
          )}
        </div>
      </div>

      {/* HVAC mode buttons */}
      {hvac_modes && hvac_modes.length > 0 && (
        <div>
          <p className="text-xs text-gray-500 mb-2">Mode</p>
          <div className="flex flex-wrap gap-2">
            {hvac_modes.map(mode => (
              <button
                key={mode}
                disabled={busy}
                onClick={() => sendCommand(() => setHvacMode(entityId, mode))}
                className={`px-3 py-1 rounded-lg text-xs font-medium transition-colors disabled:opacity-40 ${
                  mode === hvac_mode
                    ? HVAC_MODE_COLORS[mode] ?? 'bg-blue-600 text-white'
                    : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
                }`}
              >
                {HVAC_MODE_LABELS[mode] ?? mode}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Swing mode (if supported) */}
      {swing_modes && swing_modes.length > 1 && (
        <div className="flex items-center justify-between">
          <p className="text-xs text-gray-500">Swing</p>
          <select
            disabled={busy || hvac_mode === 'off'}
            value={swing_mode ?? ''}
            onChange={e => sendCommand(() => setSwingMode(entityId, e.target.value))}
            className="bg-gray-700 border border-gray-600 rounded-lg px-2 py-1 text-xs text-gray-100 focus:outline-none disabled:opacity-40"
          >
            {swing_modes.map(m => <option key={m} value={m}>{m}</option>)}
          </select>
        </div>
      )}

      {busy && (
        <div className="flex items-center gap-1.5 text-xs text-gray-400">
          <Loader size={11} className="animate-spin" /> Sending command…
        </div>
      )}
    </div>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────
export default function Dashboard() {
  const [status,    setStatus]    = useState(null)
  const [snapshots, setSnapshots] = useState([])
  const [stats,     setStats]     = useState(null)
  const pollRef = useRef(null)

  const fetchStatus = () => {
    getStatus()
      .then(setStatus)
      .catch(err => console.warn('[HawaAI] Status poll error:', err))
  }

  // Initial data load + polling every 10 seconds
  useEffect(() => {
    fetchStatus()
    getSessionStats().then(setStats).catch(console.error)
    getSnapshots(120).then(setSnapshots).catch(console.error)

    pollRef.current = setInterval(fetchStatus, 10_000)
    return () => clearInterval(pollRef.current)
  }, [])

  // Refresh snapshots every 30 seconds
  useEffect(() => {
    const id = setInterval(() => {
      getSnapshots(120).then(setSnapshots).catch(console.error)
    }, 30_000)
    return () => clearInterval(id)
  }, [])

  const configIncomplete = status && status.config_complete === false

  return (
    <div className="flex flex-col h-full">
      <LiveStatusBar status={status} />

      {configIncomplete && <ConfigWarning />}

      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        {/* Top cards row */}
        <div className="grid grid-cols-3 gap-4">
          <TempGauge
            indoor={status?.indoor_temp ?? status?.ac_current_temp}
            outdoor={status?.outdoor_temp}
            target={status?.target_temp}
            indoorFromAC={status?.indoor_temp == null && status?.ac_current_temp != null}
          />
          <ACStatusCard
            acOn={status?.ac_on}
            sessionStart={status?.session_start}
            wattDraw={status?.watt_draw}
            sessionKwh={status?.session_kwh}
            hasClimateEntity={!!status?.climate_entity}
            acCurrentTemp={status?.ac_current_temp}
            acTargetTemp={status?.ac_target_temp}
            acMode={status?.ac_mode}
            acFanMode={status?.ac_fan_mode}
            acSwingMode={status?.ac_swing_mode}
          />
          <div className="card flex flex-col gap-3">
            <p className="text-xs text-gray-500 uppercase tracking-wide">Energy Now</p>
            <div className="flex-1 flex flex-col justify-center items-center gap-1">
              {status?.energy_watts != null ? (
                <>
                  <span className="text-4xl font-bold text-yellow-400">
                    {status.energy_watts.toFixed(0)} W
                  </span>
                  <span className="text-xs text-gray-500">Room total consumption</span>
                  {status.energy_kwh_total != null && (
                    <span className="text-xs text-gray-400 mt-1">
                      Meter: {status.energy_kwh_total.toFixed(2)} kWh
                    </span>
                  )}
                  {status.session_start
                    ? <span className="text-xs text-blue-400 mt-1">Session: tracking kWh…</span>
                    : <span className="text-xs text-gray-600">No active session</span>
                  }
                </>
              ) : (
                <>
                  <span className="text-2xl font-bold text-gray-600">— W</span>
                  <span className="text-xs text-gray-600 text-center">
                    Configure Live Power Sensor in Settings
                  </span>
                </>
              )}
            </div>
          </div>
        </div>

        {/* Climate card — only shown when a climate entity is configured */}
        {status?.climate_entity && (
          <ClimateCard entityId={status.climate_entity} />
        )}

        {/* Real-time chart */}
        <div className="card">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-4">
            Real-time · Last 2 hours
          </p>
          {snapshots.length === 0 ? (
            <p className="text-sm text-gray-600 py-8 text-center">
              Waiting for first session to start
            </p>
          ) : (
            <EnergyChart snapshots={snapshots} />
          )}
        </div>

        {/* Session table + today/ML stats */}
        <StatsStrip stats={stats} />

        <div className="card">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-4">Recent Sessions</p>
          <SessionTable limit={10} />
        </div>
      </div>
    </div>
  )
}
