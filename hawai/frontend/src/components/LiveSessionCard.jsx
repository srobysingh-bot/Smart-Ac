/**
 * LiveSessionCard — real-time stats for the currently active cooling session.
 *
 * Rendered only when status.session_start is not null.
 * Data sourced exclusively from /api/status props (no extra API calls).
 * Read-only — does NOT modify backend logic.
 */
import { useEffect, useState } from 'react'
import { Activity, Thermometer, Zap, Clock } from 'lucide-react'

// ── Helpers ───────────────────────────────────────────────────────────────────

function useElapsed(startIso) {
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    if (!startIso) { setElapsed(0); return }
    const tick = () => setElapsed(Math.floor((Date.now() - new Date(startIso)) / 1000))
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [startIso])

  return elapsed
}

function fmtElapsed(secs) {
  if (secs < 60) return `${secs}s`
  const m = Math.floor(secs / 60)
  const s = secs % 60
  if (m < 60) return `${m}m ${s.toString().padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  return `${h}h ${(m % 60).toString().padStart(2, '0')}m`
}

function fmtTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

// ── Stat tile ─────────────────────────────────────────────────────────────────

function Tile({ icon: Icon, label, value, sub, color = 'text-white' }) {
  return (
    <div className="flex flex-col gap-0.5 min-w-0">
      <div className="flex items-center gap-1 text-xs text-gray-500">
        <Icon size={11} className={color} />
        <span className="uppercase tracking-wide truncate">{label}</span>
      </div>
      <span className={`text-xl font-bold leading-tight ${color}`}>{value ?? '—'}</span>
      {sub && <span className="text-xs text-gray-500">{sub}</span>}
    </div>
  )
}

// ── Card ──────────────────────────────────────────────────────────────────────

export default function LiveSessionCard({ status }) {
  const {
    session_start,
    runtime,
    indoor_temp,
    session_start_temp,   // may be null if backend doesn't expose it
    target_temp,
    effective_target,
    watt_draw,
    session_kwh,
    ac_on,
    ac_idle,
  } = status || {}

  const startIso = session_start || runtime?.session_start
  const elapsed = useElapsed(startIso)
  const isActive = !!(
    (startIso || runtime?.active)
    && (ac_on || ac_idle)
  )

  if (!isActive) return null

  // Delta from session start (if backend provides indoor_temp_start for active session)
  // Fallback: we don't store start_temp in /api/status by default, so show current temp gap
  const deltaTemp = session_start_temp != null && indoor_temp != null
    ? (session_start_temp - indoor_temp)
    : null

  // Live cooling rate in °C/min (needs at least 1 minute of data)
  const elapsedMin = elapsed / 60
  const coolingRate = deltaTemp != null && elapsedMin >= 1
    ? deltaTemp / elapsedMin
    : null

  // Temp gap from target
  const effT = effective_target != null ? effective_target : target_temp
  const tempGap = indoor_temp != null && effT != null
    ? indoor_temp - effT
    : null

  return (
    <div className="card border-blue-800/50 bg-blue-950/20 relative overflow-hidden">
      {/* Pulsing live indicator */}
      <div className="absolute top-3 right-3 flex items-center gap-1.5">
        <span className="relative flex h-2 w-2">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" />
          <span className="relative inline-flex rounded-full h-2 w-2 bg-blue-500" />
        </span>
        <span className="text-xs text-blue-400 font-medium">Live Session</span>
      </div>

      <p className="text-xs text-gray-500 uppercase tracking-wide mb-4">
        Active Session · Started {fmtTime(startIso)}
      </p>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <Tile
          icon={Clock}
          label="Elapsed"
          value={fmtElapsed(elapsed)}
          color="text-blue-400"
        />
        <Tile
          icon={Thermometer}
          label="Δ Temp cooled"
          value={deltaTemp != null ? `−${deltaTemp.toFixed(1)}°C` : (tempGap != null ? `${tempGap.toFixed(1)}° to go` : '—')}
          sub={deltaTemp == null && tempGap != null ? 'from target' : undefined}
          color="text-orange-400"
        />
        <Tile
          icon={Activity}
          label="Cooling rate"
          value={coolingRate != null ? `${coolingRate.toFixed(2)} °C/min` : (elapsedMin < 1 ? 'Warming up…' : '—')}
          sub={coolingRate != null
            ? coolingRate > 0.5 ? '⚡ Fast' : coolingRate > 0.2 ? 'Normal' : 'Slow'
            : undefined}
          color={coolingRate != null ? (coolingRate > 0.5 ? 'text-green-400' : coolingRate > 0.2 ? 'text-blue-400' : 'text-orange-400') : 'text-gray-500'}
        />
        <Tile
          icon={Zap}
          label="Energy used"
          value={session_kwh != null ? `${session_kwh.toFixed(3)} kWh` : (watt_draw != null ? `${watt_draw.toFixed(0)} W` : '—')}
          sub={session_kwh == null && watt_draw != null ? 'current draw' : 'this session'}
          color="text-yellow-400"
        />
      </div>
    </div>
  )
}
