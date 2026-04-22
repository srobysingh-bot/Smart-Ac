import { useEffect, useRef, useState } from 'react'
import { connectLive, getSessionStats, getSnapshots } from '../api/smartcool.js'
import ACStatusCard  from '../components/ACStatusCard.jsx'
import TempGauge     from '../components/TempGauge.jsx'
import EnergyChart   from '../components/EnergyChart.jsx'
import PresenceBadge from '../components/PresenceBadge.jsx'
import SessionTable  from '../components/SessionTable.jsx'
import { Thermometer, Wind, Zap, Cloud } from 'lucide-react'

// ── Live status bar ───────────────────────────────────────────────────────────
function LiveStatusBar({ status }) {
  const { indoor_temp, outdoor_temp, presence, ac_on, watt_draw } = status || {}
  return (
    <div className="flex flex-wrap items-center gap-3 px-6 py-3 bg-gray-900 border-b border-gray-800 text-sm">
      <span className="flex items-center gap-1.5">
        <Thermometer size={15} className="text-orange-400" />
        Indoor: <strong>{indoor_temp != null ? `${indoor_temp.toFixed(1)}°C` : '—'}</strong>
      </span>
      <span className="text-gray-700">|</span>
      <span className="flex items-center gap-1.5">
        <Cloud size={15} className="text-sky-400" />
        Outside: <strong>{outdoor_temp != null ? `${outdoor_temp.toFixed(1)}°C` : '—'}</strong>
      </span>
      <span className="text-gray-700">|</span>
      <PresenceBadge present={presence} />
      <span className="text-gray-700">|</span>
      <span className="flex items-center gap-1.5">
        <Zap size={15} className={ac_on ? 'text-green-400' : 'text-gray-500'} />
        AC: <strong className={ac_on ? 'text-green-400' : 'text-gray-500'}>{ac_on ? 'ON' : 'OFF'}</strong>
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

// ── Page ──────────────────────────────────────────────────────────────────────
export default function Dashboard() {
  const [status,    setStatus]    = useState(null)
  const [snapshots, setSnapshots] = useState([])
  const [stats,     setStats]     = useState(null)
  const wsRef = useRef(null)

  // Initial data load
  useEffect(() => {
    getSessionStats().then(setStats).catch(console.error)
    getSnapshots(120).then(setSnapshots).catch(console.error)
  }, [])

  // WebSocket live updates
  useEffect(() => {
    wsRef.current = connectLive(
      (data) => setStatus(data),
      (err)  => console.warn('WS error', err),
    )
    return () => wsRef.current?.close()
  }, [])

  return (
    <div className="flex flex-col h-full">
      <LiveStatusBar status={status} />

      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        {/* Top cards row */}
        <div className="grid grid-cols-3 gap-4">
          <TempGauge
            indoor={status?.indoor_temp}
            outdoor={status?.outdoor_temp}
            target={status?.target_temp}
          />
          <ACStatusCard
            acOn={status?.ac_on}
            sessionStart={status?.session_start}
            wattDraw={status?.watt_draw}
            sessionKwh={status?.session_kwh}
          />
          <div className="card flex flex-col gap-3">
            <p className="text-xs text-gray-500 uppercase tracking-wide">Energy Now</p>
            <div className="flex-1 flex flex-col justify-center items-center gap-1">
              <span className="text-4xl font-bold text-yellow-400">
                {status?.watt_draw ? `${status.watt_draw.toFixed(0)} W` : '— W'}
              </span>
              <span className="text-sm text-gray-400">
                {status?.session_kwh ? `${status.session_kwh.toFixed(3)} kWh this session` : 'No active session'}
              </span>
            </div>
          </div>
        </div>

        {/* Real-time chart */}
        <div className="card">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-4">
            Real-time · Last 2 hours
          </p>
          <EnergyChart snapshots={snapshots} />
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
