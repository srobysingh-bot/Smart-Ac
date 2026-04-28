import { useEffect, useRef, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { getStatus, getSessionStats, getSnapshots, getClimateState, setClimateTemperature, setHvacMode, setFanMode, setSwingMode, getAiStatus, getRooms, createRoom, connectLive } from '../api/smartcool.js'
import ACStatusCard    from '../components/ACStatusCard.jsx'
import TempGauge       from '../components/TempGauge.jsx'
import EnergyChart from '../components/EnergyChart.jsx'
import AiDecisionsCard from '../components/AiDecisionsCard.jsx'
import PresenceBadge   from '../components/PresenceBadge.jsx'
import SessionTable    from '../components/SessionTable.jsx'
import InsightsCard    from '../components/InsightsCard.jsx'
import LiveSessionCard from '../components/LiveSessionCard.jsx'
import SmartAdjustmentCard from '../components/SmartAdjustmentCard.jsx'
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

const ROOM_LS = 'hawaai_active_room'

function AiStatusCard({ roomId }) {
  const [ai, setAi] = useState(null)
  const load = useCallback(() => {
    if (!roomId) {
      setAi(null)
      return
    }
    getAiStatus(roomId).then(setAi).catch(() => setAi(null))
  }, [roomId])

  useEffect(() => {
    load()
    const id = setInterval(load, 5_000)
    return () => clearInterval(id)
  }, [load])

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
      <div className="px-6 py-2 bg-gray-900 border-b border-gray-800 flex flex-wrap items-center gap-3 text-sm">
        <span className="text-amber-400">No rooms configured.</span>
        <button type="button" onClick={() => setOpen(true)} className="text-blue-400 hover:underline text-xs">Add room</button>
        {open && (
          <form onSubmit={submit} className="flex flex-wrap items-center gap-2 text-xs">
            <input
              className="bg-gray-800 border border-gray-600 rounded px-2 py-1 w-32"
              placeholder="Name"
              value={name}
              onChange={e => setName(e.target.value)}
            />
            <input
              className="bg-gray-800 border border-gray-600 rounded px-2 py-1 w-48"
              placeholder="climate.entity_id"
              value={entity}
              onChange={e => setEntity(e.target.value)}
              required
            />
            <button type="submit" disabled={busy} className="px-2 py-1 bg-blue-600 rounded disabled:opacity-40">Save</button>
          </form>
        )}
      </div>
    )
  }

  const active = rooms.find(r => r.id === activeId)

  return (
    <div className="px-6 py-3 bg-gray-900/95 border-b border-gray-800 flex flex-wrap items-center gap-3 text-sm">
      <div className="flex flex-col min-w-0 flex-1 sm:flex-none sm:max-w-[min(100%,280px)]">
        <span className="text-[10px] text-gray-500 uppercase tracking-wide">Active room</span>
        <span className="text-base font-semibold text-gray-100 truncate" title={active?.name}>
          {active?.name || '—'}
        </span>
        {active?.climate_entity && (
          <span className="text-[11px] text-gray-500 font-mono truncate" title={active.climate_entity}>
            {active.climate_entity}
          </span>
        )}
      </div>
      <label className="text-gray-500 text-xs uppercase tracking-wide shrink-0 hidden sm:block">Switch</label>
      <select
        className={`bg-gray-800 border rounded-lg px-2 py-1.5 text-xs text-gray-100 max-w-[200px] ${
          activeId ? 'border-blue-500/70 ring-1 ring-blue-500/25' : 'border-gray-600'
        }`}
        value={activeId || ''}
        onChange={e => onSelect(e.target.value || null)}
      >
        <option value="">Select room…</option>
        {rooms.map(r => (
          <option key={r.id} value={r.id}>{r.name || r.id}</option>
        ))}
      </select>
      <button type="button" onClick={() => setOpen(v => !v)} className="text-xs text-blue-400 hover:underline">
        {open ? 'Cancel' : '+ Add room'}
      </button>
      {open && (
        <form onSubmit={submit} className="flex flex-wrap items-center gap-2 text-xs">
          <input
            className="bg-gray-800 border border-gray-600 rounded px-2 py-1 w-28"
            placeholder="Name"
            value={name}
            onChange={e => setName(e.target.value)}
          />
          <input
            className="bg-gray-800 border border-gray-600 rounded px-2 py-1 w-44"
            placeholder="climate.entity"
            value={entity}
            onChange={e => setEntity(e.target.value)}
          />
          <button type="submit" disabled={busy} className="px-2 py-1 bg-blue-600 rounded disabled:opacity-40">Add</button>
        </form>
      )}
    </div>
  )
}
function ConfigWarning({ roomId }) {
  const navigate = useNavigate()
  const go = () => {
    const q = roomId
      ? `?room_id=${encodeURIComponent(roomId)}`
      : (typeof localStorage !== 'undefined' && localStorage.getItem(ROOM_LS))
        ? `?room_id=${encodeURIComponent(localStorage.getItem(ROOM_LS))}`
        : ''
    navigate(`/settings${q}`)
  }
  return (
    <div className="flex items-center gap-3 mx-6 mt-4 px-4 py-3 bg-yellow-900/40 border border-yellow-700 rounded-lg text-sm">
      <AlertTriangle size={16} className="text-yellow-400 shrink-0" />
      <span className="text-yellow-200 flex-1">
        Sensors not configured — go to Settings to set up your devices
      </span>
      <button
        type="button"
        onClick={go}
        className="px-3 py-1 bg-yellow-700 hover:bg-yellow-600 rounded text-yellow-100 text-xs font-medium transition-colors"
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
  } = status || {}

  const displayTemp = indoor_temp
  const tempFromAC  = status != null && indoor_temp == null && ac_current_temp != null

  // State display: ON (green) / IDLE (amber) / OFF (gray)
  const acColor  = ac_on && !ac_idle ? 'text-green-400'
                 : ac_idle           ? 'text-yellow-400'
                 :                    'text-gray-500'
  const acLabel  = ac_on && !ac_idle ? 'ON'
                 : ac_idle           ? 'IDLE'
                 :                    'OFF'

  return (
    <div className="flex flex-wrap items-center gap-3 px-6 py-3 bg-gray-900 border-b border-gray-800 text-sm">
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
      <span className="text-gray-700">|</span>
      <span className="flex items-center gap-1.5">
        <Cloud size={15} className="text-sky-400" />
        Outside:{' '}
        <strong>{outdoor_temp != null ? `${Number(outdoor_temp).toFixed(1)}°C` : '—'}</strong>
      </span>
      <span className="text-gray-700">|</span>
      <PresenceBadge present={presence} />
      <span className="text-gray-700">|</span>
      {/* AC state — from power sensor (watts) or internal flag */}
      <span className="flex items-center gap-1.5">
        <Zap size={15} className={acColor} />
        AC:{' '}
        <strong className={acColor}>{acLabel}</strong>
        {watt_draw > 0 && (
          <span className="text-gray-400">· {Number(watt_draw).toFixed(0)} W</span>
        )}
        {power_source === 'internal' && (
          <span className="text-xs text-gray-600 ml-1" title="No power sensor — state from IR command flag">
            (flag)
          </span>
        )}
      </span>
      {/* Cooldown indicator — shows briefly after every IR command */}
      {cooldown_active && (
        <>
          <span className="text-gray-700">|</span>
          <span className="text-xs text-yellow-400 flex items-center gap-1"
                title={`${secs_since_cmd?.toFixed(0)}s since ${last_command} command`}>
            <Loader size={11} className="animate-spin" />
            Cooldown {secs_since_cmd != null ? `${Math.round(secs_since_cmd)}s` : ''}
          </span>
        </>
      )}
    </div>
  )
}

// ── Today / ML quality strip ──────────────────────────────────────────────────
function StatsStrip({ stats, roomName }) {
  const today = stats?.today || {}
  const ml    = stats?.ml    || {}
  return (
    <div className="grid grid-cols-2 gap-4">
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
  const navigate = useNavigate()
  const [status,    setStatus]    = useState(null)
  const [snapshots, setSnapshots] = useState([])
  const [stats,     setStats]     = useState(null)
  const [rooms,     setRooms]     = useState([])
  const [activeRoomId, setActiveRoomId] = useState(null)
  const pollRef = useRef(null)

  const reloadRooms = useCallback(() => {
    return getRooms()
      .then(r => {
        const list = r.rooms || []
        setRooms(list)
        return list
      })
      .catch(err => {
        console.warn('[HawaAI] Rooms load error:', err)
        return []
      })
  }, [])

  useEffect(() => {
    reloadRooms().then(list => {
      const stored = typeof localStorage !== 'undefined' ? localStorage.getItem(ROOM_LS) : null
      const byStored = stored ? list.find(x => x.id === stored)?.id : null
      const only = list.length === 1 ? list[0].id : null
      setActiveRoomId(byStored ?? only ?? null)
    })
  }, [reloadRooms])

  useEffect(() => {
    if (typeof localStorage === 'undefined') return
    if (activeRoomId) localStorage.setItem(ROOM_LS, activeRoomId)
    else localStorage.removeItem(ROOM_LS)
  }, [activeRoomId])

  useEffect(() => {
    if (!rooms.length) return
    if (activeRoomId && !rooms.some(r => r.id === activeRoomId)) {
      setActiveRoomId(rooms.length === 1 ? rooms[0].id : null)
    }
  }, [rooms, activeRoomId])

  const fetchStatus = useCallback(() => {
    if (!activeRoomId) return
    getStatus(activeRoomId)
      .then(setStatus)
      .catch(err => console.warn('[HawaAI] Status poll error:', err))
  }, [activeRoomId])

  useEffect(() => {
    if (!activeRoomId) {
      setStatus(null)
      setStats(null)
      setSnapshots([])
      return
    }
    fetchStatus()
    getSessionStats(activeRoomId).then(setStats).catch(console.error)
    getSnapshots(120, activeRoomId).then(setSnapshots).catch(console.error)

    pollRef.current = setInterval(fetchStatus, 5_000)
    return () => clearInterval(pollRef.current)
  }, [fetchStatus, activeRoomId])

  useEffect(() => {
    if (!activeRoomId) return
    const { close } = connectLive(
      activeRoomId,
      (msg) => {
        if (msg?.room_id && msg.room_id !== activeRoomId) return
        if (msg?.type === 'tick') {
          const { type: _t, ...rest } = msg
          setStatus(prev => {
            if (!prev || prev.room_id !== activeRoomId) return prev
            return { ...prev, ...rest }
          })
        }
      },
      () => {},
    )
    return () => close()
  }, [activeRoomId])

  useEffect(() => {
    if (!activeRoomId) return
    const id = setInterval(() => {
      getSnapshots(120, activeRoomId).then(setSnapshots).catch(console.error)
    }, 30_000)
    return () => clearInterval(id)
  }, [activeRoomId])

  const activeRoom = rooms.find(r => r.id === activeRoomId)
  const configIncomplete = Boolean(activeRoomId && status && status.config_complete === false)

  return (
    <div className="flex flex-col h-full">
      <RoomStrip
        rooms={rooms}
        activeId={activeRoomId}
        onSelect={setActiveRoomId}
        onRoomAdded={() => reloadRooms().then(list => {
          const last = list[list.length - 1]
          if (last?.id) {
            setActiveRoomId(last.id)
            if (typeof localStorage !== 'undefined') localStorage.setItem(ROOM_LS, last.id)
            navigate(`/settings?room_id=${encodeURIComponent(last.id)}`)
          }
        })}
      />
      <LiveStatusBar status={status} />

      {!activeRoomId && rooms.length > 0 && (
        <div className="mx-6 mt-4 px-4 py-3 bg-gray-800/50 border border-gray-700 rounded-lg text-sm text-gray-300">
          Select a room above to load dashboard data. Each room is isolated — APIs require an explicit room.
        </div>
      )}

      {configIncomplete && <ConfigWarning roomId={activeRoomId} />}

      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        {/* Top cards row */}
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
          <TempGauge
            indoor={status?.indoor_temp ?? status?.ac_current_temp}
            outdoor={status?.outdoor_temp}
            target={status?.effective_target ?? status?.target_temp}
            indoorFromAC={status?.indoor_temp == null && status?.ac_current_temp != null}
          />
          <ACStatusCard
            acOn={status?.ac_on}
            acIdle={status?.ac_idle ?? false}
            sessionStart={status?.session_start || status?.runtime?.session_start}
            runtime={status?.runtime}
            wattDraw={status?.watt_draw}
            sessionKwh={status?.session_kwh}
            hasClimateEntity={!!(status?.climate_entity || status?.ac_entity)}
            acCurrentTemp={status?.ac_current_temp}
            acTargetTemp={status?.ac_target_temp}
            acMode={status?.ac_mode}
            acFanMode={status?.ac_fan_mode}
            acSwingMode={status?.ac_swing_mode}
            smartCoolingEnabled={status?.smart_cooling_enabled ?? false}
            smartMode={status?.smart_mode}
            smartFanMode={status?.smart_fan_mode}
            smartDelta={status?.smart_delta}
          />
          <SmartAdjustmentCard
            smartAdjustment={status?.smart_adjustment ?? status?.smart_temp_adjustment}
            targetTemp={status?.target_temp}
            effectiveTarget={status?.effective_target}
            reason={status?.smart_adjustment_reason}
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

        <AiStatusCard roomId={activeRoomId} />
        <RoomHealthCard health={status?.health} />

        {/* Climate card — only shown when a climate entity is configured */}
        {(status?.climate_entity || status?.ac_entity) && (
          <ClimateCard entityId={status.climate_entity || status.ac_entity} />
        )}

        {/* Live session card — visible only when a session is active */}
        <LiveSessionCard status={status} />

        <AiDecisionsCard roomId={activeRoomId} />

        {/* Insights — read-only analytics from completed sessions */}
        <InsightsCard roomId={activeRoomId} />

        {/* Real-time chart */}
        <div className="card">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-4">
            Real-time · Last 2 hours
          </p>
          {snapshots.length === 0 ? (
            <p className="text-sm text-gray-600 py-8 text-center">
              Waiting for telemetry — snapshots appear once the engine runs for this room
            </p>
          ) : (
            <EnergyChart snapshots={snapshots} targetTemp={status?.target_temp} />
          )}
        </div>

        {/* Session table + today/ML stats */}
        <StatsStrip stats={stats} roomName={activeRoom?.name} />

        <div className="card">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-4">Recent Sessions</p>
          <SessionTable limit={10} roomId={activeRoomId} />
        </div>
      </div>
    </div>
  )
}
