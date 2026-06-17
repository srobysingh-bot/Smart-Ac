import { useEffect, useRef, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { getClimateState, setClimateTemperature, setHvacMode, setFanMode, setSwingMode, createRoom, getRoomLogs, clearRoomLogs, startPreCool, cancelPreCool, snoozePreCool, disableGeofencePreCool } from '../api/smartcool.js'
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
import ACHealthCard from '../components/ACHealthCard.jsx'
import {
  Thermometer,
  Wind,
  Zap,
  Cloud,
  AlertTriangle,
  Minus,
  Plus,
  Loader,
  Brain,
  Snowflake,
  Flame,
  Fan,
  Droplets,
  Power,
  RotateCw,
  Gauge,
  WifiOff,
  TimerReset,
  ChevronDown,
  PauseCircle,
} from 'lucide-react'

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

function normalizeRoomKey(id) {
  return String(id || '').trim().toLowerCase()
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
const TELEMETRY_STYLE = {
  healthy: 'text-emerald-300 bg-emerald-950/35 border-emerald-800/55',
  recovering: 'text-sky-200 bg-sky-950/35 border-sky-800/55',
  stale: 'text-amber-200 bg-amber-950/35 border-amber-800/55',
  offline: 'text-red-200 bg-red-950/35 border-red-800/55',
  not_configured: 'text-gray-400 bg-gray-900/55 border-gray-800',
  unconfigured: 'text-gray-400 bg-gray-900/55 border-gray-800',
}

function telemetryLabel(status) {
  const key = String(status || 'unconfigured').toLowerCase()
  return {
    healthy: 'Healthy',
    recovering: 'Recovering',
    stale: 'Stale',
    offline: 'Offline',
    not_configured: 'Not configured',
    unconfigured: 'Unconfigured',
  }[key] || 'Unknown'
}

function LiveStatusBar({ status }) {
  const {
    indoor_temp, outdoor_temp, presence, ac_on, ac_idle, watt_draw,
    ac_current_temp, cooldown_active, last_command, secs_since_cmd, power_source,
    effective_ac_on,
    ac_state_source,
    ac_state: acPhase,
    physical_ac_on,
    manual_override_active,
    manual_override_enabled,
    manual_override_persisted,
    automation_paused_by_user,
  } = status || {}
  const overrideActive = Boolean(
    automation_paused_by_user || manual_override_active || manual_override_enabled || manual_override_persisted
  )

  const physicalCore = Boolean(
    physical_ac_on != null ? physical_ac_on : (ac_on ?? effective_ac_on)
  )

  const displayTemp = indoor_temp
  const tempFromAC  = status != null && indoor_temp == null && ac_current_temp != null

  // State display — prefer explicit ac_phase; pending_on never shows as green ON
  const phase = acPhase || 'off'
  const acColor =
    phase === 'on_failed'
      ? 'text-red-400'
      : phase === 'pending_on'
      ? 'text-amber-400'
      : physicalCore && !ac_idle
        ? 'text-green-400'
        : ac_idle
          ? 'text-yellow-400'
          : 'text-gray-500'
  const acLabel =
    phase === 'on_failed'
      ? 'ON FAIL'
      : phase === 'pending_on'
      ? 'WAIT ON'
      : physicalCore && !ac_idle
        ? 'ON'
        : ac_idle
          ? 'IDLE'
          : 'OFF'

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
      {/* AC state comes from HVAC runtime; telemetry health is shown separately. */}
      <span className="flex items-center gap-1.5">
        <Zap size={15} className={acColor} />
        AC:{' '}
        <strong className={acColor}>{acLabel}</strong>
        {ac_state_source === 'inferred' && physicalCore && (
          <span className="text-xs text-purple-400/90 ml-1" title="Room hotter than target while runtime settles">
            🧠 est.
          </span>
        )}
        {ac_state_source === 'system' && physicalCore && (
          <span className="text-xs text-gray-500 ml-1" title="Runtime state from IR/control path">
            🎛 system
          </span>
        )}
        {watt_draw != null && Number.isFinite(Number(watt_draw)) && Number(watt_draw) > 0 && (
          <span className="text-gray-400">· {Number(watt_draw).toFixed(0)} W</span>
        )}
        {power_source === 'internal' && !physicalCore && phase !== 'pending_on' && phase !== 'on_failed' && (
          <span className="text-xs text-gray-600 ml-1" title="Runtime state from IR/control path">
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
      {overrideActive && (
        <>
          <span className="hidden sm:inline text-gray-700" aria-hidden>|</span>
          <span
            className="inline-flex items-center gap-1.5 rounded-full border border-violet-700/60 bg-violet-950/35 px-2.5 py-1 text-xs font-semibold text-violet-200"
            title="Manual Override is persisted. Automation stays paused until you turn it off."
          >
            <PauseCircle size={13} />
            Override active · persisted
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
          <span className="font-semibold">
            {today.total_kwh == null ? 'Unknown' : `${Number(today.total_kwh).toFixed(2)} kWh`}
          </span>
          <span className="text-gray-400">Cost</span>
          <span className="font-semibold text-yellow-400">
            {today.total_cost == null ? 'Unknown' : `${Number(today.total_cost).toFixed(2)}`}
          </span>
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
  const [pendingTemperature, setPendingTemperature] = useState(null)
  const pendingTemperatureRef = useRef(null)
  const pendingTemperatureStartedRef = useRef(0)

  const fetchClimate = useCallback(() => {
    getClimateState(entityId)
      .then(d => {
        const pending = pendingTemperatureRef.current
        if (
          pending != null
          && (
            sameClimateTemperature(d.temperature, pending)
            || Date.now() - pendingTemperatureStartedRef.current >= DEFAULT_CLIMATE_COMMAND_TIMEOUT_MS
          )
        ) {
          setPendingTemperature(null)
        }
        setClimate(d)
        setError(null)
      })
      .catch(e => setError(e.message || String(e)))
  }, [entityId])

  // Initial fetch + 8-second polling
  useEffect(() => {
    fetchClimate()
    const id = setInterval(fetchClimate, 8_000)
    return () => clearInterval(id)
  }, [fetchClimate])

  useEffect(() => {
    setPendingTemperature(null)
  }, [entityId])

  useEffect(() => {
    pendingTemperatureRef.current = pendingTemperature
    if (pendingTemperature != null) {
      const remaining = Math.max(
        0,
        pendingTemperatureStartedRef.current + DEFAULT_CLIMATE_COMMAND_TIMEOUT_MS - Date.now(),
      )
      const id = window.setTimeout(() => {
        setPendingTemperature(current => (current === pendingTemperature ? null : current))
      }, remaining)
      return () => window.clearTimeout(id)
    }
    return undefined
  }, [pendingTemperature])

  const sendCommand = async (fn) => {
    setBusy(true)
    try {
      const result = await fn()
      if (result && result.success === false) {
        throw new Error(result.error || 'Climate command failed')
      }
      // Short delay then re-fetch so UI reflects confirmed state
      setTimeout(fetchClimate, 150)
      return result
    } catch (e) {
      setError(e.message || String(e))
      throw e
    } finally {
      setBusy(false)
    }
  }

  const adjustTemp = (delta) => {
    if (!climate) return
    const step = climate.target_temp_step || 1
    const current = pendingTemperature ?? climate.temperature ?? 24
    const next = Math.round((current + delta) / step) * step
    const clamped = Math.max(climate.min_temp ?? 16, Math.min(climate.max_temp ?? 30, next))
    pendingTemperatureStartedRef.current = Date.now()
    setPendingTemperature(clamped)
    sendCommand(() => setClimateTemperature(entityId, clamped)).catch(() => {
      setPendingTemperature(current => (current === clamped ? null : current))
    })
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
  const displayTemperature = pendingTemperature ?? temperature

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
              disabled={hvac_mode === 'off'}
              onClick={() => adjustTemp(-1)}
              className="w-11 h-11 shrink-0 rounded-lg bg-gray-700 hover:bg-gray-600 disabled:opacity-40 flex items-center justify-center transition-colors tap-highlight-none"
              type="button"
            >
              <Minus size={14} aria-hidden />
            </button>
            <span className="text-2xl font-bold w-14 text-center tabular-nums">
              {displayTemperature != null ? `${displayTemperature}°` : '—'}
            </span>
            <button
              disabled={hvac_mode === 'off'}
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
const PREMIUM_HVAC_MODE_META = {
  cool: { label: 'Cool', Icon: Snowflake, active: 'border-sky-400/70 bg-sky-500/20 text-sky-100 shadow-[0_0_24px_rgba(56,189,248,0.18)]' },
  heat: { label: 'Heat', Icon: Flame, active: 'border-orange-400/70 bg-orange-500/20 text-orange-100 shadow-[0_0_24px_rgba(251,146,60,0.16)]' },
  auto: { label: 'Auto', Icon: Gauge, active: 'border-violet-400/70 bg-violet-500/20 text-violet-100 shadow-[0_0_24px_rgba(167,139,250,0.16)]' },
  dry: { label: 'Dry', Icon: Droplets, active: 'border-amber-300/70 bg-amber-400/15 text-amber-100 shadow-[0_0_24px_rgba(251,191,36,0.13)]' },
  fan_only: { label: 'Fan', Icon: Fan, active: 'border-teal-300/70 bg-teal-400/15 text-teal-100 shadow-[0_0_24px_rgba(45,212,191,0.14)]' },
  off: { label: 'Off', Icon: Power, active: 'border-slate-500/80 bg-slate-700/35 text-slate-200' },
}

function clampClimateNumber(value, min, max) {
  return Math.max(min, Math.min(max, value))
}

function roundClimateToStep(value, step, min) {
  const safeStep = Number(step) > 0 ? Number(step) : 1
  return Math.round((Number(value) - min) / safeStep) * safeStep + min
}

function premiumModeLabel(value) {
  const key = String(value || '').trim()
  return HVAC_MODE_LABELS[key] || key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) || '-'
}

function premiumTempLabel(value) {
  if (value == null || Number.isNaN(Number(value))) return '-'
  const n = Number(value)
  return Number.isInteger(n) ? String(n) : n.toFixed(1)
}

const DEFAULT_CLIMATE_COMMAND_TIMEOUT_MS = 5_000

function sameClimateTemperature(a, b) {
  const na = Number(a)
  const nb = Number(b)
  return Number.isFinite(na) && Number.isFinite(nb) && Math.abs(na - nb) < 0.01
}

function PremiumClimateStatePills({ status, climate, busy }) {
  const phase = String(status?.ac_state || '').toLowerCase()
  const telemetry = String(status?.telemetry_status || 'unconfigured').toLowerCase()
  const overrideActive = Boolean(
    status?.automation_paused_by_user
    || status?.manual_override_active
    || status?.manual_override_enabled
    || status?.manual_override_persisted
  )
  const climateUnavailable = status?.health?.climate?.available === false
    || ['unavailable', 'unknown'].includes(String(climate?.hvac_mode || '').toLowerCase())
  const pills = []

  if (climateUnavailable) {
    pills.push({ key: 'offline', label: 'Offline', Icon: WifiOff, cls: 'border-red-700/60 bg-red-950/35 text-red-200' })
  }
  if (overrideActive) {
    pills.push({ key: 'override', label: 'Automation paused by user', Icon: PauseCircle, cls: 'border-violet-700/60 bg-violet-950/35 text-violet-200' })
  } else if (!climateUnavailable && phase === 'pending_on') {
    pills.push({ key: 'pending-on', label: 'Waiting ON', Icon: TimerReset, cls: 'border-amber-700/60 bg-amber-950/30 text-amber-200' })
  } else if (phase === 'pending_off') {
    pills.push({ key: 'pending-off', label: 'Pending OFF', Icon: TimerReset, cls: 'border-amber-700/60 bg-amber-950/30 text-amber-200' })
  } else if (phase === 'on' || status?.ac_on || status?.effective_ac_on) {
    pills.push({ key: 'running', label: 'Running', Icon: Zap, cls: 'border-emerald-700/60 bg-emerald-950/30 text-emerald-200' })
  }

  if (status?.cooldown_active) {
    pills.push({ key: 'cooldown', label: 'IR cooldown', Icon: Loader, spin: true, cls: 'border-yellow-700/60 bg-yellow-950/30 text-yellow-200' })
  }
  if (telemetry && !['healthy', 'unconfigured'].includes(telemetry)) {
    pills.push({ key: 'telemetry', label: `Telemetry ${telemetryLabel(telemetry)}`, Icon: AlertTriangle, cls: 'border-orange-700/60 bg-orange-950/30 text-orange-200' })
  }
  if (busy) {
    pills.push({ key: 'busy', label: 'Sending', Icon: Loader, spin: true, cls: 'border-sky-700/60 bg-sky-950/30 text-sky-200' })
  }

  if (pills.length === 0) return null
  return (
    <div className="flex flex-wrap gap-2">
      {pills.map(({ key, label, Icon, cls, spin }) => (
        <span
          key={key}
          className={`inline-flex min-h-[28px] items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold ${cls}`}
        >
          <Icon size={12} className={spin ? 'animate-spin' : ''} aria-hidden />
          {label}
        </span>
      ))}
    </div>
  )
}

function PremiumControlButton({
  active,
  disabled,
  Icon,
  label,
  value,
  onClick,
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      aria-expanded={active}
      onClick={onClick}
      className={`tap-highlight-none flex min-h-[58px] min-w-0 items-center justify-between gap-3 rounded-lg border px-3 py-2 text-left transition-all duration-200 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-45 ${
        active
          ? 'border-sky-400/60 bg-sky-500/15 shadow-[0_0_18px_rgba(56,189,248,0.12)]'
          : 'border-gray-800 bg-gray-950/45 hover:border-gray-600 hover:bg-gray-900/75'
      }`}
    >
      <span className="flex min-w-0 items-center gap-2.5">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-white/10 text-sky-200">
          <Icon size={15} aria-hidden />
        </span>
        <span className="min-w-0">
          <span className="block text-[10px] font-semibold uppercase tracking-[0.16em] text-gray-500">{label}</span>
          <span className="block truncate text-sm font-semibold text-gray-100">{value || 'Unsupported'}</span>
        </span>
      </span>
      <ChevronDown
        size={15}
        className={`shrink-0 text-gray-500 transition-transform duration-200 ${active ? 'rotate-180 text-sky-300' : ''}`}
        aria-hidden
      />
    </button>
  )
}

function PremiumSelectorPanel({
  title,
  options,
  currentValue,
  disabled,
  emptyLabel = 'Unsupported',
  onSelect,
  optionMeta,
}) {
  if (!options || options.length === 0) {
    return (
      <div className="rounded-lg border border-gray-800 bg-gray-950/50 p-3 text-sm font-semibold text-gray-500">
        {emptyLabel}
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-sky-500/20 bg-gray-950/70 p-3 shadow-xl shadow-black/20">
      <p className="mb-3 text-[11px] font-semibold uppercase tracking-[0.16em] text-gray-500">{title}</p>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {options.map(option => {
          const meta = optionMeta?.(option)
          const Icon = meta?.Icon || Gauge
          const label = meta?.label || premiumModeLabel(option)
          const active = option === currentValue
          return (
            <button
              key={option}
              type="button"
              disabled={disabled || active}
              aria-pressed={active}
              onClick={() => onSelect(option)}
              className={`tap-highlight-none min-h-[46px] rounded-lg border px-3 py-2 text-xs font-semibold transition-all duration-200 active:scale-[0.98] disabled:cursor-not-allowed ${
                active
                  ? meta?.active || 'border-sky-400/60 bg-sky-500/15 text-white'
                  : 'border-gray-700/75 bg-gray-900/70 text-gray-300 hover:border-gray-500 hover:bg-gray-800/85'
              }`}
            >
              <span className="flex items-center justify-center gap-2">
                <Icon size={15} aria-hidden />
                {label}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

function PremiumClimateCard({ entityId, status }) {
  const [climate, setClimate] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [pendingDesiredTarget, setPendingDesiredTarget] = useState(null)
  const [localDraftTarget, setLocalDraftTarget] = useState(null)
  const [isDraggingTemp, setIsDraggingTemp] = useState(false)
  const [activeSelector, setActiveSelector] = useState(null)
  const commandSeqRef = useRef(0)
  const pendingDesiredRef = useRef(null)
  const busyCountRef = useRef(0)
  const tempDragRef = useRef(false)

  const fetchClimate = useCallback(() => {
    const requestSeq = commandSeqRef.current
    getClimateState(entityId)
      .then(d => {
        if (d?.error) throw new Error(d.error)
        const pending = pendingDesiredRef.current
        if (pending && requestSeq >= pending.seq) {
          const confirmed = sameClimateTemperature(d.temperature, pending.target)
          const expired = Date.now() - pending.startedAt >= pending.timeoutMs
          if (confirmed || expired) {
            setPendingDesiredTarget(prev => (prev?.seq === pending.seq ? null : prev))
          }
        }
        setClimate(d)
        setError(null)
      })
      .catch(e => setError(e.message || String(e)))
  }, [entityId])

  useEffect(() => {
    fetchClimate()
    const id = setInterval(fetchClimate, 8_000)
    return () => clearInterval(id)
  }, [fetchClimate])

  useEffect(() => {
    setPendingDesiredTarget(null)
    setLocalDraftTarget(null)
    setIsDraggingTemp(false)
    setActiveSelector(null)
    commandSeqRef.current = 0
    pendingDesiredRef.current = null
    busyCountRef.current = 0
    tempDragRef.current = false
    setBusy(false)
  }, [entityId])

  useEffect(() => {
    pendingDesiredRef.current = pendingDesiredTarget
  }, [pendingDesiredTarget])

  useEffect(() => {
    if (!pendingDesiredTarget) return undefined
    const remaining = Math.max(0, pendingDesiredTarget.startedAt + pendingDesiredTarget.timeoutMs - Date.now())
    const id = window.setTimeout(() => {
      setPendingDesiredTarget(prev => (prev?.seq === pendingDesiredTarget.seq ? null : prev))
    }, remaining)
    return () => window.clearTimeout(id)
  }, [pendingDesiredTarget])

  const setCommandBusy = (active) => {
    busyCountRef.current = Math.max(0, busyCountRef.current + (active ? 1 : -1))
    setBusy(busyCountRef.current > 0)
  }

  const sendCommand = async (fn, optimisticPatch = null) => {
    if (optimisticPatch) {
      setClimate(prev => prev ? { ...prev, ...optimisticPatch } : prev)
    }
    setCommandBusy(true)
    try {
      const result = await fn()
      if (result && result.success === false) {
        throw new Error(result.error || 'Climate command failed')
      }
      setTimeout(fetchClimate, 150)
      return result
    } catch (e) {
      setError(e.message || String(e))
      throw e
    } finally {
      setCommandBusy(false)
    }
  }

  const normalizeTarget = (value) => {
    if (!climate) return
    const min = Number(climate.min_temp ?? 16)
    const max = Number(climate.max_temp ?? 30)
    const step = Number(climate.target_temp_step || 1)
    const rounded = roundClimateToStep(value, step, min)
    return clampClimateNumber(rounded, min, max)
  }

  const commitTemperature = (value) => {
    const target = normalizeTarget(value)
    if (target == null) return
    const seq = commandSeqRef.current + 1
    commandSeqRef.current = seq
    setLocalDraftTarget(null)
    setPendingDesiredTarget({
      target,
      seq,
      startedAt: Date.now(),
      timeoutMs: DEFAULT_CLIMATE_COMMAND_TIMEOUT_MS,
    })
    sendCommand(
      () => setClimateTemperature(entityId, target),
      { temperature: target },
    ).then(result => {
      const timeoutMs = Number(result?.pending_timeout_ms)
      if (Number.isFinite(timeoutMs) && timeoutMs > 0) {
        setPendingDesiredTarget(prev => (prev?.seq === seq ? { ...prev, timeoutMs } : prev))
      }
    }).catch(() => {
      setPendingDesiredTarget(prev => (prev?.seq === seq ? null : prev))
    })
  }

  const adjustTemp = (delta) => {
    if (!climate) return
    const current = localDraftTarget ?? pendingDesiredTarget?.target ?? climate.temperature ?? 24
    commitTemperature(Number(current) + Number(delta))
  }

  const beginTempDrag = (value) => {
    tempDragRef.current = true
    setIsDraggingTemp(true)
    const target = normalizeTarget(value)
    if (target != null) setLocalDraftTarget(target)
  }

  const updateTempInput = (value) => {
    const target = normalizeTarget(value)
    if (target == null) return
    if (tempDragRef.current) {
      setLocalDraftTarget(target)
      return
    }
    commitTemperature(target)
  }

  const commitTempDrag = (value) => {
    if (!tempDragRef.current) return
    tempDragRef.current = false
    setIsDraggingTemp(false)
    commitTemperature(value)
  }

  const toggleSelector = (name) => {
    setActiveSelector(current => current === name ? null : name)
  }

  const selectHvacMode = (mode) => {
    setActiveSelector(null)
    sendCommand(
      () => setHvacMode(entityId, mode),
      { hvac_mode: mode },
    )
  }

  const selectFanMode = (mode) => {
    setActiveSelector(null)
    sendCommand(
      () => setFanMode(entityId, mode),
      { fan_mode: mode },
    )
  }

  const selectSwingMode = (mode) => {
    setActiveSelector(null)
    sendCommand(
      () => setSwingMode(entityId, mode),
      { swing_mode: mode },
    )
  }

  if (error) {
    return (
      <div className="card border-red-900/60 bg-red-950/10">
        <p className="text-xs text-gray-500 uppercase tracking-wide mb-3">AC Climate</p>
        <div className="flex items-center gap-2 text-sm text-red-300">
          <AlertTriangle size={15} /> {error}
        </div>
        <p className="text-xs text-gray-600 mt-1">entity: {entityId}</p>
      </div>
    )
  }

  if (!climate) {
    return (
      <div className="card flex items-center gap-2 text-xs text-gray-500">
        <Loader size={13} className="animate-spin" /> Loading climate data...
      </div>
    )
  }

  const { hvac_mode, current_temperature, temperature, fan_mode, swing_mode,
          hvac_modes, fan_modes, swing_modes, friendly_name } = climate
  const minTemp = Number(climate.min_temp ?? 16)
  const maxTemp = Number(climate.max_temp ?? 30)
  const tempStep = Number(climate.target_temp_step || 1)
  const displayTemperature = clampClimateNumber(
    Number(localDraftTarget ?? pendingDesiredTarget?.target ?? temperature ?? 24),
    minTemp,
    maxTemp,
  )
  const tempPercent = clampClimateNumber(((displayTemperature - minTemp) / Math.max(1, maxTemp - minTemp)) * 100, 0, 100)
  const ringRadius = 62
  const ringCircumference = 2 * Math.PI * ringRadius
  const activeMode = PREMIUM_HVAC_MODE_META[hvac_mode] || {
    label: premiumModeLabel(hvac_mode),
    Icon: Gauge,
    active: 'border-gray-500/70 bg-gray-800 text-gray-100',
  }
  const ActiveIcon = activeMode.Icon
  const FanIcon = Fan
  const SwingIcon = RotateCw
  const controlsDisabled = hvac_mode === 'off'
  const modeOptions = Array.isArray(hvac_modes) ? hvac_modes : []
  const fanOptions = Array.isArray(fan_modes) ? fan_modes : []
  const swingOptions = Array.isArray(swing_modes) ? swing_modes : []

  return (
    <div className="card relative overflow-hidden border-gray-700/80 bg-[radial-gradient(circle_at_50%_0%,rgba(14,165,233,0.14),transparent_34%),linear-gradient(180deg,rgba(17,24,39,0.96),rgba(3,7,18,0.94))] p-0 shadow-2xl shadow-black/25">
      <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-sky-300/50 to-transparent" aria-hidden />
      <div className="grid gap-5 p-4 sm:p-5 lg:grid-cols-[minmax(260px,0.9fr)_minmax(0,1.1fr)]">
        <section className="flex min-w-0 flex-col items-center justify-between gap-4 rounded-lg border border-white/10 bg-black/20 p-4">
          <div className="flex w-full items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-gray-500">AC Climate</p>
              <p className="mt-1 truncate text-sm font-semibold text-gray-100" title={friendly_name || entityId}>
                {friendly_name || entityId}
              </p>
            </div>
            <span className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-semibold ${activeMode.active}`}>
              <ActiveIcon size={13} aria-hidden />
              {activeMode.label}
            </span>
          </div>

          <div className="relative grid aspect-square w-full max-w-[270px] place-items-center">
            <svg className="absolute inset-0 h-full w-full -rotate-90 drop-shadow-[0_0_18px_rgba(56,189,248,0.16)]" viewBox="0 0 160 160" aria-hidden>
              <circle cx="80" cy="80" r={ringRadius} fill="none" stroke="rgba(51,65,85,0.78)" strokeWidth="10" />
              <circle
                cx="80"
                cy="80"
                r={ringRadius}
                fill="none"
                stroke="url(#premium-climate-temp-ring)"
                strokeLinecap="round"
                strokeWidth="10"
                strokeDasharray={`${(tempPercent / 100) * ringCircumference} ${ringCircumference}`}
                className="transition-[stroke-dasharray] duration-300 ease-out"
              />
              <defs>
                <linearGradient id="premium-climate-temp-ring" x1="20" x2="140" y1="20" y2="140">
                  <stop offset="0%" stopColor="#38bdf8" />
                  <stop offset="52%" stopColor="#22c55e" />
                  <stop offset="100%" stopColor="#f59e0b" />
                </linearGradient>
              </defs>
            </svg>
            <div className="absolute inset-[17%] rounded-full border border-white/10 bg-gray-950/85 shadow-[inset_0_0_30px_rgba(15,23,42,0.9)]" aria-hidden />
            <div className="relative z-10 flex flex-col items-center text-center">
              <span className="text-[11px] font-semibold uppercase tracking-[0.18em] text-gray-500">Setpoint</span>
              <span className={`mt-1 tabular-nums text-6xl font-black tracking-normal text-white transition-transform duration-200 ${isDraggingTemp ? 'scale-[1.04]' : ''}`}>
                {premiumTempLabel(displayTemperature)}
              </span>
              <span className="-mt-1 text-sm font-semibold text-sky-200">deg C</span>
              <span className="mt-3 text-xs text-gray-400">
                Current {current_temperature != null ? `${premiumTempLabel(current_temperature)} deg` : 'unavailable'}
              </span>
            </div>
          </div>

          <div className="grid w-full grid-cols-[44px_minmax(0,1fr)_44px] items-center gap-3">
            <button
              disabled={controlsDisabled}
              onClick={() => adjustTemp(-tempStep)}
              className="touch-target rounded-lg border border-white/10 bg-white/10 text-gray-100 transition-all hover:bg-white/15 active:scale-95 disabled:opacity-35"
              type="button"
              aria-label="Decrease target temperature"
            >
              <Minus size={16} aria-hidden />
            </button>
            <input
              type="range"
              aria-label="Target temperature"
              min={minTemp}
              max={maxTemp}
              step={tempStep}
              value={displayTemperature}
              disabled={controlsDisabled}
              onPointerDown={e => beginTempDrag(Number(e.currentTarget.value))}
              onPointerUp={e => commitTempDrag(Number(e.currentTarget.value))}
              onPointerCancel={e => commitTempDrag(Number(e.currentTarget.value))}
              onBlur={e => commitTempDrag(Number(e.currentTarget.value))}
              onChange={e => updateTempInput(Number(e.target.value))}
              className="climate-range h-8 w-full disabled:opacity-40"
              style={{ '--climate-range': `${tempPercent}%` }}
            />
            <button
              disabled={controlsDisabled}
              onClick={() => adjustTemp(tempStep)}
              className="touch-target rounded-lg border border-white/10 bg-white/10 text-gray-100 transition-all hover:bg-white/15 active:scale-95 disabled:opacity-35"
              type="button"
              aria-label="Increase target temperature"
            >
              <Plus size={16} aria-hidden />
            </button>
          </div>
        </section>

        <section className="min-w-0 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <PremiumClimateStatePills status={status} climate={climate} busy={busy} />
            <span className="max-w-full truncate rounded-full border border-gray-800 bg-black/25 px-2.5 py-1 text-[11px] font-mono text-gray-500" title={entityId}>
              {entityId}
            </span>
          </div>

          <div className="grid gap-2 sm:grid-cols-3">
            <PremiumControlButton
              active={activeSelector === 'mode'}
              disabled={modeOptions.length === 0}
              Icon={ActiveIcon}
              label="Mode"
              value={activeMode.label}
              onClick={() => toggleSelector('mode')}
            />
            <PremiumControlButton
              active={activeSelector === 'fan'}
              disabled={fanOptions.length === 0 || hvac_mode === 'off'}
              Icon={FanIcon}
              label="Fan"
              value={fan_mode ? premiumModeLabel(fan_mode) : 'Unsupported'}
              onClick={() => toggleSelector('fan')}
            />
            <PremiumControlButton
              active={activeSelector === 'swing'}
              disabled={swingOptions.length <= 1 || hvac_mode === 'off'}
              Icon={SwingIcon}
              label="Swing"
              value={swing_mode ? premiumModeLabel(swing_mode) : 'Unsupported'}
              onClick={() => toggleSelector('swing')}
            />
          </div>

          {activeSelector === 'mode' && (
            <PremiumSelectorPanel
              title="Select mode"
              options={modeOptions}
              currentValue={hvac_mode}
              disabled={busy}
              onSelect={selectHvacMode}
              optionMeta={(mode) => PREMIUM_HVAC_MODE_META[mode] || {
                label: premiumModeLabel(mode),
                Icon: Gauge,
                active: 'border-sky-400/60 bg-sky-500/15 text-white',
              }}
            />
          )}

          {activeSelector === 'fan' && (
            <PremiumSelectorPanel
              title="Select fan"
              options={fanOptions}
              currentValue={fan_mode}
              disabled={controlsDisabled}
              onSelect={selectFanMode}
              optionMeta={(mode) => ({
                label: premiumModeLabel(mode),
                Icon: FanIcon,
              })}
            />
          )}

          {activeSelector === 'swing' && (
            <PremiumSelectorPanel
              title="Select vertical swing"
              options={swingOptions}
              currentValue={swing_mode}
              disabled={controlsDisabled}
              onSelect={selectSwingMode}
              optionMeta={(mode) => ({
                label: premiumModeLabel(mode),
                Icon: SwingIcon,
              })}
            />
          )}

        </section>
      </div>
    </div>
  )
}

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

function RoomLogsCard({ activeRoomId, rooms }) {
  const [roomFilter, setRoomFilter] = useState('active')
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [logs, setLogs] = useState([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const logsRef = useRef(null)
  const shouldAutoScrollRef = useRef(true)

  const selectedRoomId = roomFilter === 'active' ? (activeRoomId || '') : roomFilter

  const dedupeAndSort = useCallback((items) => {
    const seen = new Set()
    const out = []
    for (const log of items || []) {
      if (String(log?.scope || 'runtime').toLowerCase() !== 'runtime') continue
      if (selectedRoomId && normalizeRoomKey(log?.room_id) !== normalizeRoomKey(selectedRoomId)) continue
      const key = `${log.ts}-${log.room_id || selectedRoomId}-${log.message}`
      if (!seen.has(key)) {
        seen.add(key)
        out.push(log)
      }
    }
    out.sort((a, b) => (a?.ts || 0) - (b?.ts || 0))
    return out
  }, [selectedRoomId])

  const loadLogs = useCallback(async () => {
    if (!activeRoomId) return
    const el = logsRef.current
    shouldAutoScrollRef.current = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 50
    setBusy(true)
    setErr('')
    try {
      if (selectedRoomId) {
        const res = await getRoomLogs(selectedRoomId, 250)
        const roomName = rooms.find((r) => normalizeRoomKey(r.id) === normalizeRoomKey(selectedRoomId))?.name || selectedRoomId
        const oneRoom = (res?.logs || []).map((it) => ({
          ...it,
          room_id: normalizeRoomKey(it?.room_id || selectedRoomId),
          room_name: roomName,
        }))
        setLogs(dedupeAndSort(oneRoom))
      } else {
        setLogs([])
      }
    } catch (e) {
      setErr(e?.message || 'Failed to fetch logs')
    } finally {
      setBusy(false)
    }
  }, [activeRoomId, rooms, selectedRoomId, dedupeAndSort])

  useEffect(() => {
    const el = logsRef.current
    if (!el || !shouldAutoScrollRef.current) return
    el.scrollTop = el.scrollHeight
  }, [logs])

  useEffect(() => {
    loadLogs()
  }, [loadLogs])

  useEffect(() => {
    if (!autoRefresh) return
    const t = setInterval(loadLogs, 3000)
    return () => clearInterval(t)
  }, [autoRefresh, loadLogs])

  const onClear = async () => {
    if (!selectedRoomId) return
    setBusy(true)
    setErr('')
    try {
      await clearRoomLogs(selectedRoomId)
      await loadLogs()
    } catch (e) {
      setErr(e?.message || 'Failed to clear logs')
    } finally {
      setBusy(false)
    }
  }

  const fmtTime = (ts) => {
    if (!ts) return '--:--:--'
    const n = Number(ts)
    // Backward compatibility: old entries may still be epoch-seconds.
    const ms = n < 1e12 ? n * 1000 : n
    return new Date(ms).toLocaleTimeString()
  }

  const levelClass = (level) => {
    const lv = String(level || 'INFO').toUpperCase()
    if (lv === 'ERROR') return 'text-red-300'
    if (lv === 'WARNING' || lv === 'WARN') return 'text-yellow-300'
    return 'text-gray-300'
  }

  return (
    <div className="card">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-3">
        <p className="text-xs text-gray-500 uppercase tracking-wide">Room Logs</p>
        <div className="flex flex-wrap items-center gap-2">
          <select
            className="bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-xs"
            value={roomFilter}
            onChange={(e) => setRoomFilter(e.target.value)}
          >
            <option value="active">Active Room</option>
            {rooms.map((r) => (
              <option key={r.id} value={r.id}>{r.name || r.id}</option>
            ))}
          </select>
          <label className="text-xs text-gray-400 flex items-center gap-1">
            <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
            Auto
          </label>
          <button
            type="button"
            onClick={onClear}
            disabled={busy || !selectedRoomId}
            className="px-2 py-1.5 text-xs rounded bg-gray-800 border border-gray-600 disabled:opacity-40"
          >
            Clear
          </button>
        </div>
      </div>
      {err ? <p className="text-xs text-red-400 mb-2">{err}</p> : null}
      <div
        ref={logsRef}
        onScroll={(e) => {
          const el = e.currentTarget
          shouldAutoScrollRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 50
        }}
        className="overflow-y-auto overflow-x-hidden rounded border border-gray-800 bg-gray-950/70 p-2 font-mono text-xs"
      >
        {busy && logs.length === 0 ? <p className="text-gray-500">Loading logs...</p> : null}
        {!busy && logs.length === 0 ? <p className="text-gray-600">No room logs yet.</p> : null}
        {logs.map((l, idx) => (
          <div key={`${l.ts}-${idx}`} className={`${levelClass(l.level)} break-words`}>
            <span className="text-gray-500">[{fmtTime(l.ts)}]</span>{' '}
            <span className="text-gray-500">[{String(l.level || 'INFO').toUpperCase()}] </span>
            <span>{l.message}</span>
          </div>
        ))}
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
  const handlePreCoolStart = useCallback(
    (durationMinutes) => startPreCool(activeRoomId, durationMinutes),
    [activeRoomId],
  )
  const handlePreCoolCancel = useCallback(
    () => cancelPreCool(activeRoomId),
    [activeRoomId],
  )
  const handlePreCoolSnooze = useCallback(
    () => snoozePreCool(activeRoomId, 1440),
    [activeRoomId],
  )
  const handlePreCoolDisableGeofence = useCallback(
    () => disableGeofencePreCool(activeRoomId),
    [activeRoomId],
  )

  return (
    <div className="flex flex-col min-w-0">
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

          <div className="container-app overflow-x-hidden px-4 sm:px-6 py-3 sm:py-4 pb-8 space-y-4 min-w-0">
            {/* Cards — 1 col · 2 cols tablet · 3 lg · 4 xl */}
            <div className="grid grid-cols-1 items-start sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4 min-w-0">
          <TempGauge
            indoor={displayStatus?.indoor_temp ?? displayStatus?.ac_current_temp}
            outdoor={displayStatus?.outdoor_temp}
            target={displayStatus?.effective_target ?? displayStatus?.target_temp}
            indoorFromAC={displayStatus?.indoor_temp == null && displayStatus?.ac_current_temp != null}
          />
          <ACStatusCard
            acPhase={displayStatus?.ac_state || 'off'}
            acIdle={displayStatus?.ac_idle ?? false}
            acStateSource={displayStatus?.ac_state_source}
            sessionStart={
              displayStatus?.active_session_started_at
              || displayStatus?.runtime?.active_session_started_at
              || displayStatus?.session_start
              || displayStatus?.runtime?.session_start
            }
            runtime={displayStatus?.runtime}
            wattDraw={displayStatus?.watt_draw}
            sessionKwh={displayStatus?.session_kwh}
            lastAcOnAt={displayStatus?.last_ac_on_at}
            lastAcOffAt={displayStatus?.last_ac_off_at}
            pendingAction={displayStatus?.pending_action}
            pendingRemainSec={displayStatus?.pending_remaining_seconds}
            preCoolEnabled={displayStatus?.pre_cool_enabled}
            preCoolActive={displayStatus?.pre_cool_active}
            preCoolDurationMinutes={displayStatus?.pre_cool_duration_minutes}
            preCoolRemainingSeconds={displayStatus?.pre_cool_remaining_seconds}
            preCoolTarget={displayStatus?.pre_cool_target}
            preCoolResult={displayStatus?.pre_cool_result}
            preCoolTriggerSource={displayStatus?.pre_cool_trigger_source}
            preCoolPerson={displayStatus?.pre_cool_geofence_trigger_person}
            preCoolSnoozedUntil={displayStatus?.pre_cool_snoozed_until}
            preCoolExtensionCount={displayStatus?.pre_cool_extension_count}
            vacancyOffBlockedReason={displayStatus?.vacancy_off_blocked_reason}
            onPreCoolStart={handlePreCoolStart}
            onPreCoolCancel={handlePreCoolCancel}
            onPreCoolSnooze={handlePreCoolSnooze}
            onPreCoolDisableGeofence={handlePreCoolDisableGeofence}
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
            sleepOptimizationActive={displayStatus?.sleep_optimization_active}
            sleepPhase={displayStatus?.sleep_phase}
            sleepOffset={displayStatus?.sleep_offset}
            humidityPercent={displayStatus?.humidity_percent}
            feelsLikeTemp={displayStatus?.feels_like_temp}
            dewPoint={displayStatus?.dew_point}
            humidityOffset={displayStatus?.humidity_offset}
            comfortLevel={displayStatus?.comfort_level}
            humidityBand={displayStatus?.humidity_band}
            dryModeRecommended={displayStatus?.dry_mode_recommended}
            thermalLoadLevel={displayStatus?.thermal_load_level}
            thermalLoadConfidence={displayStatus?.thermal_load_confidence}
            thermalLoadActive={displayStatus?.thermal_load_active}
            thermalLoadSummary={displayStatus?.thermal_load_summary}
            thermalLoadOffset={displayStatus?.thermal_load_offset}
            coolingSaturated={displayStatus?.cooling_saturated}
          />
          <SmartAdjustmentCard
            smartAdjustment={displayStatus?.smart_adjustment ?? displayStatus?.smart_temp_adjustment}
            targetTemp={displayStatus?.target_temp}
            effectiveTarget={displayStatus?.effective_target}
            reason={displayStatus?.smart_adjustment_reason}
          />
          <div className="card flex flex-col gap-3">
            <p className="text-xs text-gray-500 uppercase tracking-wide">Energy Now</p>
            {displayStatus && (
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-gray-500">Telemetry</span>
                <span
                  className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-[11px] ${
                    TELEMETRY_STYLE[String(displayStatus.telemetry_status || 'unconfigured').toLowerCase()]
                    || TELEMETRY_STYLE.unconfigured
                  }`}
                >
                  <Zap size={11} aria-hidden />
                  {telemetryLabel(displayStatus.telemetry_status)}
                </span>
              </div>
            )}
            <div className="flex flex-col items-center justify-center gap-1 py-3">
              {displayStatus?.energy_watts != null ? (
                <>
                  <span className="text-4xl font-bold text-yellow-400">
                    {displayStatus.energy_watts.toFixed(0)} W
                  </span>
                  <span className="text-xs text-gray-500">
                    Live power reading
                  </span>
                  {displayStatus.energy_kwh_total != null && (
                    <span className="text-xs text-gray-400 mt-1">
                      Meter: {displayStatus.energy_kwh_total.toFixed(2)} kWh
                    </span>
                  )}
                  {(displayStatus.active_session_started_at || displayStatus.session_start)
                    ? <span className="text-xs text-blue-400 mt-1">Session: tracking kWh…</span>
                    : <span className="text-xs text-gray-600">No active session</span>
                  }
                </>
              ) : (
                <>
                  <span className="text-2xl font-bold text-gray-600">— W</span>
                  <span className="text-xs text-gray-600 text-center">
                    {displayStatus?.energy_configured
                      ? telemetryLabel(displayStatus?.telemetry_status)
                      : 'Configure Live Power Sensor in Settings'}
                  </span>
                  {displayStatus?.last_valid_power_watts != null && (
                    <span className="text-xs text-gray-500">
                      Last known: {Number(displayStatus.last_valid_power_watts).toFixed(0)} W
                    </span>
                  )}
                </>
              )}
            </div>
          </div>
        </div>

        <AiStatusCard ai={ai} />
        <RoomHealthCard health={displayStatus?.health} />
        <RoomLogsCard activeRoomId={activeRoomId} rooms={rooms} />

        {/* Climate card — only shown when a climate entity is configured */}
        {(displayStatus?.climate_entity || displayStatus?.ac_entity) && (
          <PremiumClimateCard
            entityId={displayStatus.climate_entity || displayStatus.ac_entity}
            status={displayStatus}
          />
        )}

        {/* Live session card — visible only when a session is active */}
        <ACHealthCard roomId={activeRoomId} />

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
