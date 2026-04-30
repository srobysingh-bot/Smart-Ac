import { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { getClimateState, setClimateTemperature, setHvacMode, setFanMode, setSwingMode, createRoom } from '../api/smartcool.js'
import { useRoom } from '../context/RoomContext.jsx'
import { useRoomData } from '../context/RoomDataContext.jsx'
import ACStatusCard    from '../components/ACStatusCard.jsx'
import TempGauge       from '../components/TempGauge.jsx'
import EnergyChart from '../components/EnergyChart.jsx'
import AiDecisionsCard from '../components/AiDecisionsCard.jsx'
import PresenceBadge   from '../components/PresenceBadge.jsx'
import SessionTable    from '../components/SessionTable.jsx'
import InsightsCard    from '../components/InsightsCard.jsx'
import LiveSessionCard from '../components/LiveSessionCard.jsx'
import SmartAdjustmentCard from '../components/SmartAdjustmentCard.jsx'
import TemperaturePlanCard from '../components/TemperaturePlanCard.jsx'
import { Thermometer, Wind, Zap, Cloud, AlertTriangle, Minus, Plus, Loader, Brain } from 'lucide-react'

function formatAiTime(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString()
  } catch {
    return iso
  }
}

/** Dim previous room content while fetching the next room — avoids blank flash. */
function SoftLoadingOverlay({ show, children }) {
  if (!show) return children
  return (
    <div className="relative min-w-0 flex flex-col flex-1 min-h-0">
      <div
        className="pointer-events-none absolute inset-0 z-10 flex justify-center bg-gray-950/40 pt-6 sm:pt-8"
        aria-busy="true"
        aria-label="Loading room data"
      >
        <span className="inline-flex h-fit items-center gap-2 rounded-lg border border-gray-600 bg-gray-900/95 px-4 py-2 text-sm text-gray-100 shadow-lg">
          <Loader size={18} className="animate-spin text-blue-400 shrink-0" aria-hidden />
          Loading room…
        </span>
      </div>
      <div className="min-h-0 min-w-0 flex flex-col opacity-[0.72]">{children}</div>
    </div>
  )
}

function AiStatusCard({ ai }) {
  const st = ai?.status ?? 'idle'
  const border =
    st === 'success'
      ? 'border-green-600/80 bg-green-950/25'
      : st === 'running'
        ? 'border-yellow-600/80 bg-yellow-950/20'
        : st === 'idle'
          ? 'border-gray-700 bg-gray-900/50'
          : 'border-red-600/80 bg-red-950/20'

  const rt = ai?.response_time
  const row = (label, val) => (
    <div className="flex justify-between gap-2 text-xs">
      <span className="text-gray-500">{label}</span>
      <span className="text-gray-200 font-medium text-right truncate max-w-[60%]" title={val != null ? String(val) : ''}>
        {val != null && val !== '' ? val : '—'}
      </span>
    </div>
  )

  return (
    <div className={`card border ${border}`}>
      <div className="flex items-center gap-2 mb-3">
        <Brain size={18} className="text-violet-400 shrink-0" aria-hidden />
        <p className="text-xs text-gray-500 uppercase tracking-wide">🧠 AI Status</p>
      </div>
      {ai?.circuit_open && (
        <div className="mb-2 text-xs text-amber-400 font-medium">
          Circuit open — API paused {ai.circuit_seconds_remaining != null ? `(${ai.circuit_seconds_remaining}s)` : ''}
        </div>
      )}
      <div className="space-y-2">
        {row('Status', st)}
        {row('Provider', ai?.provider)}
        {row('Model', ai?.model)}
        {row('Last call', formatAiTime(ai?.last_call))}
        {row('Response time', rt != null ? `${rt} ms` : '—')}
        {row('Failures (streak)', ai?.api_consecutive_failures ?? '—')}
        {row('Last error', ai?.last_error)}
      </div>
      <p className="text-[10px] text-gray-600 mt-3">Room-scoped · refreshes every 5s</p>
    </div>
  )
}

function RoomHealthCard({ health }) {
  if (!health) return null
  const { climate, sensors, ai, fetched_at } = health
  const row = (name, v) => (
    <div key={name} className="flex items-center justify-between text-xs gap-2">
      <span className="text-gray-500 shrink-0">{name}</span>
      <span>
        {v === null ? <span className="text-gray-600">n/a</span>
          : v ? <span className="text-green-400">OK</span> : <span className="text-red-400">Unavailable</span>}
      </span>
    </div>
  )
  return (
    <div className="card border border-gray-800/80">
      <div className="flex items-center justify-between mb-3 gap-2">
        <p className="text-xs text-gray-500 uppercase tracking-wide">Room health</p>
        <span className="text-[10px] text-gray-600 font-mono truncate max-w-[200px]" title={fetched_at || ''}>
          Fetched {fetched_at ? new Date(fetched_at).toLocaleString() : '—'}
        </span>
      </div>
      <div className="grid sm:grid-cols-2 gap-4">
        <div className="space-y-2">
          <p className="text-[10px] text-gray-600 uppercase tracking-wide">Climate entity</p>
          <div className="text-xs space-y-1.5">
            <div className="flex justify-between gap-2">
              <span className="text-gray-500">HA availability</span>
              <span className={climate?.available ? 'text-green-400' : 'text-red-400'}>
                {climate?.available ? 'Available' : 'Unavailable'}
              </span>
            </div>
            <div className="flex justify-between gap-2">
              <span className="text-gray-500">State</span>
              <span className="font-mono text-gray-200 truncate" title={climate?.state}>{climate?.state ?? '—'}</span>
            </div>
            <div className="flex justify-between gap-2">
              <span className="text-gray-500">Last HA update</span>
              <span className="text-gray-300 text-right truncate" title={climate?.last_updated}>
                {climate?.last_updated ? new Date(climate.last_updated).toLocaleString() : '—'}
              </span>
            </div>
          </div>
        </div>
        <div className="space-y-2">
          <p className="text-[10px] text-gray-600 uppercase tracking-wide">Sensors · AI</p>
          <div className="space-y-1">
            {row('Indoor temp', sensors?.indoor_temp)}
            {row('Presence', sensors?.presence)}
            {row('Power', sensors?.energy_power)}
            {row('Energy meter', sensors?.energy_kwh)}
            <div className="flex justify-between text-xs pt-1.5 mt-1 border-t border-gray-800 gap-2">
              <span className="text-gray-500">AI</span>
              <span className="text-violet-300 font-medium truncate" title={ai?.last_error || ''}>
                {ai?.status ?? '—'}{ai?.circuit_open ? ' · circuit open' : ''}
              </span>
            </div>
            {ai?.last_call && (
              <p className="text-[10px] text-gray-600">Last inference: {formatAiTime(ai.last_call)}</p>
            )}
            {ai?.last_error && (
              <p className="text-[10px] text-red-400 break-words">{ai.last_error}</p>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Room selector (multi-room) ────────────────────────────────────────────────
function RoomStrip({ rooms, activeId, onSelect, onRoomAdded }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [entity, setEntity] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = (e) => {
    e.preventDefault()
    if (!entity.trim()) return
    setBusy(true)
    createRoom({ name: name.trim() || 'Room', climate_entity: entity.trim() })
      .then(() => {
        setName('')
        setEntity('')
        setOpen(false)
        onRoomAdded?.()
      })
      .catch(console.error)
      .finally(() => setBusy(false))
  }

  if (!rooms?.length && !open) {
    return (
      <div className="container-app px-4 sm:px-6 py-3 bg-gray-900 border-b border-gray-800 flex flex-col sm:flex-row flex-wrap items-stretch sm:items-center gap-3 text-sm min-w-0">
        <span className="text-amber-400">No rooms configured.</span>
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="touch-target sm:min-h-0 sm:min-w-0 sm:px-4 sm:py-2 text-sm text-blue-400 bg-gray-800 rounded-lg border border-gray-700 shrink-0 self-start"
        >
          Add room
        </button>
        {open && (
          <form onSubmit={submit} className="flex flex-col sm:flex-row flex-wrap items-stretch gap-2 text-xs w-full sm:w-auto">
            <input
              className="bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 min-h-[44px] sm:min-h-0 w-full sm:w-36"
              placeholder="Name"
              value={name}
              onChange={e => setName(e.target.value)}
            />
            <input
              className="bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 min-h-[44px] sm:min-h-0 w-full sm:min-w-[12rem] flex-1"
              placeholder="climate.entity_id"
              value={entity}
              onChange={e => setEntity(e.target.value)}
              required
            />
            <button type="submit" disabled={busy} className="min-h-[44px] px-4 py-2 bg-blue-600 rounded-lg disabled:opacity-40 text-sm">
              Save
            </button>
          </form>
        )}
      </div>
    )
  }

  const active = rooms.find(r => r.id === activeId)
  const selClass = `bg-gray-800 border rounded-lg px-3 text-sm text-gray-100 w-full min-h-[44px] md:min-h-0 md:max-w-[min(100%,280px)] md:w-auto ${
    activeId ? 'border-blue-500/70 ring-1 ring-blue-500/25' : 'border-gray-600'
  }`

  return (
    <div className="container-app px-4 sm:px-6 py-3 bg-gray-900/95 border-b border-gray-800 flex flex-col lg:flex-row lg:flex-wrap lg:items-end gap-3 lg:gap-4 text-sm min-w-0">
      <div className="flex flex-col min-w-0 w-full lg:flex-1 lg:max-w-md">
        <span className="text-[10px] text-gray-500 uppercase tracking-wide">Active room</span>
        <span className="text-base font-semibold text-gray-100 truncate" title={active?.name}>
          {active?.name || '—'}
        </span>
        {active?.climate_entity && (
          <span className="text-[11px] text-gray-500 font-mono truncate hidden sm:block" title={active.climate_entity}>
            {active.climate_entity}
          </span>
        )}
      </div>

      {/* Mobile / narrow: full-width dropdown only */}
      <div className="w-full min-w-0 md:hidden">
        <label className="sr-only" htmlFor="room-select-mobile">Switch room</label>
        <select
          id="room-select-mobile"
          className={selClass}
          value={activeId || ''}
          onChange={e => onSelect(e.target.value || null)}
        >
          <option value="">Select room…</option>
          {rooms.map(r => (
            <option key={r.id} value={r.id}>{r.name || r.id}</option>
          ))}
        </select>
      </div>

      {/* Tablet+ : row with select + actions */}
      <div className="hidden md:flex flex-wrap items-center gap-3 min-w-0">
        <label className="text-gray-500 text-xs uppercase tracking-wide shrink-0 hidden lg:inline">Switch</label>
        <select
          className={`${selClass.replace('w-full ', '')} min-w-[11rem]`}
          value={activeId || ''}
          onChange={e => onSelect(e.target.value || null)}
          aria-label="Switch room"
        >
          <option value="">Select room…</option>
          {rooms.map(r => (
            <option key={r.id} value={r.id}>{r.name || r.id}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => setOpen(v => !v)}
          className="min-h-[40px] px-3 py-2 rounded-lg text-sm text-blue-400 border border-blue-500/30 hover:bg-blue-950/40 transition-colors"
        >
          {open ? 'Cancel' : '+ Add room'}
        </button>
        {open && (
          <form onSubmit={submit} className="flex flex-wrap items-center gap-2 w-full xl:w-auto mt-1 xl:mt-0">
            <input
              className="bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm w-28 min-w-0"
              placeholder="Name"
              value={name}
              onChange={e => setName(e.target.value)}
            />
            <input
              className="bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm min-w-[10rem] flex-1 max-w-xs"
              placeholder="climate.entity"
              value={entity}
              onChange={e => setEntity(e.target.value)}
            />
            <button type="submit" disabled={busy} className="min-h-[40px] px-4 py-2 bg-blue-600 rounded-lg disabled:opacity-40 text-sm">
              Add
            </button>
          </form>
        )}
      </div>
    </div>
  )
}
function ConfigWarning({ roomId }) {
  const navigate = useNavigate()
  const go = () => {
    const search = roomId ? `?room_id=${encodeURIComponent(roomId)}` : ''
    navigate({ pathname: '/settings', search })
  }
  return (
    <div className="container-app flex flex-col sm:flex-row sm:items-center gap-3 mx-4 sm:mx-auto mt-4 px-4 py-3 bg-yellow-900/40 border border-yellow-700 rounded-xl text-sm max-w-full min-w-0">
      <AlertTriangle size={16} className="text-yellow-400 shrink-0" aria-hidden />
      <span className="text-yellow-200 flex-1 min-w-0">
        Sensors not configured — go to Settings to set up your devices
      </span>
      <button
        type="button"
        onClick={go}
        className="shrink-0 min-h-[44px] sm:min-h-[40px] px-4 py-2 bg-yellow-700 hover:bg-yellow-600 rounded-lg text-yellow-100 text-sm font-medium transition-colors w-full sm:w-auto tap-highlight-none"
      >
        Go to Settings
      </button>
    </div>
  )
}

// ── Live status bar ───────────────────────────────────────────────────────────
function LiveStatusBar({ status }) {
  const {
    indoor_temp, outdoor_temp, presence, ac_on, ac_idle, watt_draw,
    ac_current_temp, cooldown_active, last_command, secs_since_cmd, power_source,
    effective_ac_on,
    ac_state_source,
  } = status || {}

  const acCore = Boolean(effective_ac_on ?? ac_on)

  const displayTemp = indoor_temp
  const tempFromAC  = status != null && indoor_temp == null && ac_current_temp != null

  // State display: ON (green) / IDLE (amber) / OFF (gray) — server-derived
  const acColor  = acCore && !ac_idle ? 'text-green-400'
                 : ac_idle           ? 'text-yellow-400'
                 :                    'text-gray-500'
  const acLabel  = acCore && !ac_idle ? 'ON'
                 : ac_idle           ? 'IDLE'
                 :                    'OFF'

  return (
    <nav
      className="flex flex-col gap-y-3 gap-x-2 px-4 sm:px-6 py-3 bg-gray-900 border-b border-gray-800 text-sm min-w-0 sm:flex-row sm:flex-wrap sm:items-center"
      aria-label="Live environment"
    >
      <span className="flex items-center gap-1.5">
        <Thermometer size={15} className={tempFromAC ? 'text-blue-400' : 'text-orange-400'} />
        Indoor:{' '}
        <strong>
          {displayTemp != null ? `${Number(displayTemp).toFixed(1)}°C` : '—'}
        </strong>
        {tempFromAC && (
          <span className="text-xs text-blue-400 ml-0.5" title="Reading from AC unit (WiFi sensor offline)">
            ⁽ᴬᶜ⁾
          </span>
        )}
      </span>
      <span className="hidden sm:inline text-gray-700" aria-hidden>|</span>
      <span className="flex items-center gap-1.5">
        <Cloud size={15} className="text-sky-400" />
        Outside:{' '}
        <strong>{outdoor_temp != null ? `${Number(outdoor_temp).toFixed(1)}°C` : '—'}</strong>
      </span>
      <span className="hidden sm:inline text-gray-700" aria-hidden>|</span>
      <PresenceBadge present={presence} />
      <span className="hidden sm:inline text-gray-700" aria-hidden>|</span>
      {/* AC state — from power sensor (watts) or internal flag */}
      <span className="flex items-center gap-1.5">
        <Zap size={15} className={acColor} />
        AC:{' '}
        <strong className={acColor}>{acLabel}</strong>
        {ac_state_source === 'power' && acCore && (
          <span className="text-xs text-yellow-400/90 ml-1" title="Watts crossed compressor threshold">
            ⚡ confirmed
          </span>
        )}
        {ac_state_source === 'inferred' && acCore && (
          <span className="text-xs text-purple-400/90 ml-1" title="Room hotter than target with no watt spike yet">
            🧠 est.
          </span>
        )}
        {ac_state_source === 'system' && acCore && (
          <span className="text-xs text-gray-500 ml-1" title="Cooldown or no power meter — trusting command state">
            🎛 system
          </span>
        )}
        {watt_draw > 0 && (
          <span className="text-gray-400">· {Number(watt_draw).toFixed(0)} W</span>
        )}
        {power_source === 'internal' && !acCore && (
          <span className="text-xs text-gray-600 ml-1" title="No power sensor — state from IR command flag">
            (flag)
          </span>
        )}
      </span>
      {/* Cooldown indicator — shows briefly after every IR command */}
      {cooldown_active && (
        <>
          <span className="hidden sm:inline text-gray-700" aria-hidden>|</span>
          <span className="text-xs text-yellow-400 flex items-center gap-1"
                title={`${secs_since_cmd?.toFixed(0)}s since ${last_command} command`}>
            <Loader size={11} className="animate-spin" />
            Cooldown {secs_since_cmd != null ? `${Math.round(secs_since_cmd)}s` : ''}
          </span>
        </>
      )}
    </nav>
  )
}
function StatsStrip({ stats, roomName }) {
  const today = stats?.today || {}
  const ml    = stats?.ml    || {}
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
      <div className="card">
        <p className="text-xs text-gray-500 uppercase tracking-wide mb-3">
          Today{roomName ? <span className="normal-case text-gray-400 font-medium"> · {roomName}</span> : null}
        </p>
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
        <p className="text-xs text-gray-500 uppercase tracking-wide mb-3">
          ML Data Quality{roomName ? <span className="normal-case text-gray-400 font-medium"> · {roomName}</span> : null}
        </p>
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
    <div className="card space-y-4 min-w-0">
      {/* Header */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <p className="text-xs text-gray-500 uppercase tracking-wide">AC Climate</p>
          <p className="text-sm text-gray-300 mt-0.5 break-words">{friendly_name}</p>
        </div>
        <span className={`shrink-0 px-3 py-1 rounded-full text-xs font-semibold ${HVAC_MODE_COLORS[hvac_mode] ?? 'bg-gray-700 text-gray-300'}`}>
          {HVAC_MODE_LABELS[hvac_mode] ?? hvac_mode ?? '—'}
        </span>
      </div>

      {/* Temperature row — stacks on narrow viewports */}
      <div className="flex flex-col gap-6 sm:flex-row sm:flex-wrap sm:items-start sm:justify-between">
        <div className="text-center sm:text-left min-w-[6rem]">
          <p className="text-3xl font-bold text-blue-400">
            {current_temperature != null ? `${current_temperature.toFixed(1)}°` : '—'}
          </p>
          <p className="text-xs text-gray-500 mt-0.5">Current</p>
        </div>

        <div className="flex flex-col items-center gap-2 mx-auto sm:mx-0 min-w-[8rem]">
          <p className="text-xs text-gray-500">Setpoint</p>
          <div className="flex items-center gap-2">
            <button
              disabled={busy || hvac_mode === 'off'}
              onClick={() => adjustTemp(-1)}
              className="w-11 h-11 shrink-0 rounded-lg bg-gray-700 hover:bg-gray-600 disabled:opacity-40 flex items-center justify-center transition-colors tap-highlight-none"
              type="button"
            >
              <Minus size={14} aria-hidden />
            </button>
            <span className="text-2xl font-bold w-14 text-center tabular-nums">
              {temperature != null ? `${temperature}°` : '—'}
            </span>
            <button
              disabled={busy || hvac_mode === 'off'}
              onClick={() => adjustTemp(+1)}
              className="w-11 h-11 shrink-0 rounded-lg bg-gray-700 hover:bg-gray-600 disabled:opacity-40 flex items-center justify-center transition-colors tap-highlight-none"
              type="button"
            >
              <Plus size={14} aria-hidden />
            </button>
          </div>
        </div>

        <div className="text-center sm:text-right min-w-0 flex-1 sm:flex-initial">
          <p className="text-xs text-gray-500 mb-1">Fan</p>
          {fan_modes && fan_modes.length > 0 ? (
            <select
              disabled={busy || hvac_mode === 'off'}
              value={fan_mode ?? ''}
              onChange={e => sendCommand(() => setFanMode(entityId, e.target.value))}
              className="bg-gray-700 border border-gray-600 rounded-lg px-2 py-2 min-h-[44px] max-w-full text-xs text-gray-100 focus:outline-none focus:border-blue-500 disabled:opacity-40"
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
                className={`min-h-[40px] px-3 py-2 rounded-lg text-xs font-medium transition-colors disabled:opacity-40 tap-highlight-none ${
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
            className="bg-gray-700 border border-gray-600 rounded-lg px-2 py-2 min-h-[44px] text-xs text-gray-100 focus:outline-none disabled:opacity-40 max-w-full"
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

// ── Dashboard: missing room ───────────────────────────────────────────────────
/** Shown when no room is selected (or none exist). Avoids ghost empty widgets. */
function DashboardNeedsRoomGate({ rooms, onSelectRoom, onOpenSettings }) {
  const multi = rooms && rooms.length > 1
  if (!rooms || rooms.length === 0) {
    return (
      <div className="container-app px-4 sm:px-6 py-10 max-w-xl mx-auto">
        <div className="rounded-xl border border-gray-700 bg-gray-900/80 p-6 sm:p-8 text-center shadow-lg">
          <Wind size={40} className="mx-auto text-blue-400 mb-4" aria-hidden />
          <h2 className="text-lg font-semibold text-white mb-2">No rooms configured</h2>
          <p className="text-sm text-gray-400 mb-6 leading-relaxed">
            The dashboard loads live data per room. Add a room in Settings, then pick it from the strip above.
            Nothing is shown until a room is selected so data is never mixed between spaces.
          </p>
          <button
            type="button"
            onClick={onOpenSettings}
            className="min-h-[44px] px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium transition-colors w-full sm:w-auto"
          >
            Open Settings
          </button>
        </div>
      </div>
    )
  }
  return (
    <div className="container-app px-4 sm:px-6 py-8 max-w-xl mx-auto">
      <div className="rounded-xl border border-amber-700/60 bg-amber-950/30 p-6 sm:p-8 text-center">
        <h2 className="text-lg font-semibold text-amber-100 mb-2">
          {multi ? 'Select a room' : 'Choose this room'}
        </h2>
        <p className="text-sm text-amber-200/90 mb-6 leading-relaxed">
          {multi
            ? 'Pick a room from the strip above. All API calls include room_id — sessions, snapshots, and AI status stay isolated per room.'
            : 'Select the room below to load telemetry. Your last choice is remembered in this browser.'}
        </p>
        <div className="flex flex-wrap justify-center gap-2">
          {rooms.map((r) => (
            <button
              key={r.id}
              type="button"
              onClick={() => onSelectRoom(r.id)}
              className="min-h-[44px] px-4 py-2 rounded-lg bg-gray-800 border border-gray-600 hover:border-blue-500 hover:bg-gray-700 text-gray-100 text-sm font-medium transition-colors"
            >
              {r.name || r.id}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────
export default function Dashboard() {
  const navigate = useNavigate()
  const { activeRoomId, setActiveRoom, rooms, refreshRooms, roomsLoading, roomsEmpty } = useRoom()
  const {
    status,
    snapshots,
    stats,
    ai,
    loading: roomLoading,
    loadError,
    displayStatus,
    displaySnapshots,
    displayStats,
    showSoftLoading,
  } = useRoomData()

  const activeRoom = rooms.find(r => r.id === activeRoomId)
  const configIncomplete = Boolean(
    activeRoomId && status && status.config_complete === false && !showSoftLoading,
  )
  const hasRoom = Boolean(activeRoomId)
  const showDashboardBody = !loadError && (!roomLoading || showSoftLoading)

  return (
    <div className="flex flex-col h-full min-w-0">
      <RoomStrip
        rooms={rooms}
        activeId={activeRoomId}
        onSelect={setActiveRoom}
        onRoomAdded={() => refreshRooms().then(list => {
          const last = list[list.length - 1]
          if (last?.id) {
            setActiveRoom(last.id)
            navigate({ pathname: '/settings' })
          }
        })}
      />

      {!hasRoom && roomsLoading ? (
        <div className="container-app px-4 py-12 text-center text-sm text-gray-500">
          Loading rooms…
        </div>
      ) : !hasRoom && roomsEmpty ? (
        <DashboardNeedsRoomGate
          rooms={[]}
          onSelectRoom={setActiveRoom}
          onOpenSettings={() => navigate({ pathname: '/settings' })}
        />
      ) : !hasRoom ? (
        <DashboardNeedsRoomGate
          rooms={rooms}
          onSelectRoom={setActiveRoom}
          onOpenSettings={() => navigate({ pathname: '/settings' })}
        />
      ) : (
        <>
          {roomLoading && !showSoftLoading && !loadError && (
            <div className="container-app px-4 py-10 flex flex-col items-center justify-center gap-3 text-sm text-gray-500">
              <Loader size={24} className="animate-spin text-blue-400" aria-hidden />
              <span>Loading room data…</span>
            </div>
          )}

          {!roomLoading && loadError && (
            <div className="container-app px-4 py-10 text-center max-w-md mx-auto">
              <p className="text-sm text-red-300 mb-2">Could not load this room.</p>
              <p className="text-xs text-gray-500">Check the connection and try switching rooms or reopening the dashboard.</p>
            </div>
          )}

          {showDashboardBody && (
            <SoftLoadingOverlay show={showSoftLoading}>
            <>
          <LiveStatusBar status={displayStatus} />

          {configIncomplete && <ConfigWarning roomId={activeRoomId} />}

          {activeRoomId && status && (
            <div className="container-app px-4 sm:px-6 pb-2 shrink-0 min-w-0">
              <TemperaturePlanCard status={status} />
            </div>
          )}

          <div className="container-app flex-1 overflow-y-auto overflow-x-hidden px-4 sm:px-6 py-4 sm:py-6 pb-8 space-y-6 min-w-0">
            {/* Cards — 1 col · 2 cols tablet · 3 lg · 4 xl */}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4 min-w-0">
          <TempGauge
            indoor={displayStatus?.indoor_temp ?? displayStatus?.ac_current_temp}
            outdoor={displayStatus?.outdoor_temp}
            target={displayStatus?.effective_target ?? displayStatus?.target_temp}
            indoorFromAC={displayStatus?.indoor_temp == null && displayStatus?.ac_current_temp != null}
          />
          <ACStatusCard
            acOn={Boolean(displayStatus?.effective_ac_on ?? displayStatus?.ac_on)}
            acIdle={displayStatus?.ac_idle ?? false}
            acStateSource={displayStatus?.ac_state_source}
            sessionStart={displayStatus?.session_start || displayStatus?.runtime?.session_start}
            runtime={displayStatus?.runtime}
            wattDraw={displayStatus?.watt_draw}
            sessionKwh={displayStatus?.session_kwh}
            lastAcOnAt={displayStatus?.last_ac_on_at}
            lastAcOffAt={displayStatus?.last_ac_off_at}
            hasClimateEntity={!!(displayStatus?.climate_entity || displayStatus?.ac_entity)}
            acCurrentTemp={displayStatus?.ac_current_temp}
            acTargetTemp={displayStatus?.ac_target_temp}
            acMode={displayStatus?.ac_mode}
            acFanMode={displayStatus?.ac_fan_mode}
            acSwingMode={displayStatus?.ac_swing_mode}
            smartCoolingEnabled={displayStatus?.smart_cooling_enabled ?? false}
            smartMode={displayStatus?.smart_mode}
            smartFanMode={displayStatus?.smart_fan_mode}
            smartDelta={displayStatus?.smart_delta}
          />
          <SmartAdjustmentCard
            smartAdjustment={displayStatus?.smart_adjustment ?? displayStatus?.smart_temp_adjustment}
            targetTemp={displayStatus?.target_temp}
            effectiveTarget={displayStatus?.effective_target}
            reason={displayStatus?.smart_adjustment_reason}
          />
          <div className="card flex flex-col gap-3">
            <p className="text-xs text-gray-500 uppercase tracking-wide">Energy Now</p>
            <div className="flex-1 flex flex-col justify-center items-center gap-1">
              {displayStatus?.energy_watts != null ? (
                <>
                  <span className="text-4xl font-bold text-yellow-400">
                    {displayStatus.energy_watts.toFixed(0)} W
                  </span>
                  <span className="text-xs text-gray-500">Room total consumption</span>
                  {displayStatus.energy_kwh_total != null && (
                    <span className="text-xs text-gray-400 mt-1">
                      Meter: {displayStatus.energy_kwh_total.toFixed(2)} kWh
                    </span>
                  )}
                  {displayStatus.session_start
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

        <AiStatusCard ai={ai} />
        <RoomHealthCard health={displayStatus?.health} />

        {/* Climate card — only shown when a climate entity is configured */}
        {(displayStatus?.climate_entity || displayStatus?.ac_entity) && (
          <ClimateCard entityId={displayStatus.climate_entity || displayStatus.ac_entity} />
        )}

        {/* Live session card — visible only when a session is active */}
        <LiveSessionCard status={displayStatus} />

        <AiDecisionsCard roomId={activeRoomId} />

        {/* Insights — read-only analytics from completed sessions */}
        <InsightsCard roomId={activeRoomId} />

        {/* Real-time chart */}
        <div className="card">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-4">
            Real-time · Last 2 hours
          </p>
          {displaySnapshots.length === 0 ? (
            <p className="text-sm text-gray-600 py-8 text-center">
              Waiting for telemetry — snapshots appear once the engine runs for this room
            </p>
          ) : (
            <EnergyChart snapshots={displaySnapshots} targetTemp={displayStatus?.target_temp} />
          )}
        </div>

        {/* Session table + today/ML stats */}
        <StatsStrip stats={displayStats} roomName={activeRoom?.name} />

        <div className="card">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-4">Recent Sessions</p>
          <SessionTable limit={10} roomId={activeRoomId} />
        </div>
      </div>
            </>
            </SoftLoadingOverlay>
          )}
        </>
      )}
    </div>
  )
}
