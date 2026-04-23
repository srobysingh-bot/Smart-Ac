/**
 * InsightsCard — displays cooling analytics derived from completed sessions.
 *
 * Data comes from GET /api/insights (read-only, never affects control logic).
 * Requires at least one session > 5 min to show meaningful numbers.
 */
import { useEffect, useState } from 'react'
import { getInsights } from '../api/smartcool.js'
import { TrendingUp, TrendingDown, Minus, Zap, Thermometer, BarChart2, Loader, AlertTriangle } from 'lucide-react'

// ── Helpers ────────────────────────────────────────────────────────────────

function Stat({ label, value, unit, color = 'text-white', small = false }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs text-gray-500 uppercase tracking-wide">{label}</span>
      {value != null ? (
        <span className={`font-bold ${small ? 'text-lg' : 'text-2xl'} ${color}`}>
          {value}
          {unit && <span className="text-sm font-normal text-gray-400 ml-0.5">{unit}</span>}
        </span>
      ) : (
        <span className="text-sm text-gray-600">—</span>
      )}
    </div>
  )
}

function TypeBar({ fast = 0, normal = 0, slow = 0 }) {
  const total = fast + normal + slow
  if (total === 0) return <span className="text-xs text-gray-600">No data yet</span>
  const pct = (n) => Math.round((n / total) * 100)
  return (
    <div className="space-y-1.5">
      {[
        { label: 'Fast',   count: fast,   pct: pct(fast),   color: 'bg-green-500' },
        { label: 'Normal', count: normal, pct: pct(normal), color: 'bg-blue-500'  },
        { label: 'Slow',   count: slow,   pct: pct(slow),   color: 'bg-orange-500'},
      ].map(({ label, count, pct: p, color }) => (
        <div key={label} className="flex items-center gap-2 text-xs">
          <span className="w-12 text-gray-400 text-right">{label}</span>
          <div className="flex-1 h-2 bg-gray-800 rounded-full overflow-hidden">
            <div className={`h-full rounded-full ${color}`} style={{ width: `${p}%` }} />
          </div>
          <span className="w-8 text-gray-500">{count}</span>
        </div>
      ))}
    </div>
  )
}

function TrendBadge({ trend }) {
  if (!trend) return null
  const map = {
    improving: { icon: <TrendingUp  size={13} />, color: 'text-green-400  bg-green-900/40',  label: 'Improving'  },
    declining: { icon: <TrendingDown size={13}/>, color: 'text-orange-400 bg-orange-900/40', label: 'Declining'  },
    stable:    { icon: <Minus       size={13} />, color: 'text-blue-400   bg-blue-900/40',   label: 'Stable'     },
  }
  const t = map[trend]
  if (!t) return null
  return (
    <span className={`flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${t.color}`}>
      {t.icon} {t.label}
    </span>
  )
}

// ── Card ──────────────────────────────────────────────────────────────────────

export default function InsightsCard() {
  const [data,    setData]    = useState(null)
  const [loading, setLoading] = useState(true)
  const [error,   setError]   = useState(null)

  useEffect(() => {
    let alive = true
    const load = () => {
      getInsights()
        .then(d => { if (alive) { setData(d); setLoading(false) } })
        .catch(e => { if (alive) { setError(e.message); setLoading(false) } })
    }
    load()
    // Refresh every 5 minutes — insights don't change often
    const id = setInterval(load, 5 * 60 * 1000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  if (loading) {
    return (
      <div className="card flex items-center gap-2 text-xs text-gray-500">
        <Loader size={13} className="animate-spin" /> Loading insights…
      </div>
    )
  }

  if (error) {
    return (
      <div className="card flex items-center gap-2 text-xs text-red-400">
        <AlertTriangle size={13} /> Failed to load insights: {error}
      </div>
    )
  }

  const { sessions_analyzed, avg_cooling_rate, avg_efficiency,
          best_target_temp, best_outdoor_range, cooling_type_counts, trend } = data || {}
  const counts = cooling_type_counts || { fast: 0, normal: 0, slow: 0 }
  const hasData = sessions_analyzed > 0

  return (
    <div className="card space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs text-gray-500 uppercase tracking-wide">AC Insights</p>
          <p className="text-xs text-gray-600 mt-0.5">
            Based on {sessions_analyzed ?? 0} analyzed session{sessions_analyzed !== 1 ? 's' : ''}
            {!hasData && ' — run the AC for >5 min to generate data'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <BarChart2 size={16} className="text-purple-400" />
          <TrendBadge trend={trend} />
        </div>
      </div>

      {hasData ? (
        <>
          {/* Key metrics row */}
          <div className="grid grid-cols-3 gap-4">
            <Stat
              label="Avg Cooling Rate"
              value={avg_cooling_rate != null ? avg_cooling_rate.toFixed(3) : null}
              unit="°C/min"
              color="text-blue-400"
            />
            <Stat
              label="Avg Efficiency"
              value={avg_efficiency != null ? avg_efficiency.toFixed(1) : null}
              unit="°C/kWh"
              color="text-yellow-400"
            />
            <Stat
              label="Best Target Temp"
              value={best_target_temp != null ? `${best_target_temp}°` : null}
              color="text-green-400"
            />
          </div>

          {/* Secondary row */}
          <div className="grid grid-cols-2 gap-4">
            {/* Cooling type distribution */}
            <div className="space-y-2">
              <p className="text-xs text-gray-500 uppercase tracking-wide">Cooling Speed Distribution</p>
              <TypeBar fast={counts.fast} normal={counts.normal} slow={counts.slow} />
            </div>

            {/* Best conditions */}
            <div className="space-y-2">
              <p className="text-xs text-gray-500 uppercase tracking-wide">Best Conditions</p>
              <div className="space-y-1.5 text-sm">
                <div className="flex items-center gap-2">
                  <Thermometer size={13} className="text-orange-400 shrink-0" />
                  <span className="text-gray-400 text-xs">Outdoor range:</span>
                  <span className="text-xs font-semibold text-gray-200">
                    {best_outdoor_range ?? '—'}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <Zap size={13} className="text-yellow-400 shrink-0" />
                  <span className="text-gray-400 text-xs">Target temp:</span>
                  <span className="text-xs font-semibold text-green-300">
                    {best_target_temp != null ? `${best_target_temp}°C` : '—'}
                  </span>
                </div>
              </div>
            </div>
          </div>

          {/* Speed legend */}
          <div className="flex gap-4 text-xs text-gray-600 border-t border-gray-800 pt-3">
            <span><span className="text-green-400 font-semibold">Fast</span> &gt;0.5°C/min</span>
            <span><span className="text-blue-400 font-semibold">Normal</span> 0.2–0.5°C/min</span>
            <span><span className="text-orange-400 font-semibold">Slow</span> &lt;0.2°C/min</span>
            <span className="ml-auto text-gray-700">First 5 min excluded from analysis</span>
          </div>
        </>
      ) : (
        <div className="py-6 text-center space-y-2">
          <BarChart2 size={32} className="text-gray-700 mx-auto" />
          <p className="text-sm text-gray-500">No cooling data yet</p>
          <p className="text-xs text-gray-600">
            Insights appear after the AC runs for at least 5 minutes and a session completes.
          </p>
        </div>
      )}
    </div>
  )
}
