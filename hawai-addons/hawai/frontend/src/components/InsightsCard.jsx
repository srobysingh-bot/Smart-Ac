/**
 * InsightsCard — displays cooling analytics + smart recommendations.
 *
 * Data comes from GET /api/insights (read-only, never affects control logic).
 * Handles both the new structured response (has_data / metrics) and the old
 * flat format for backward compatibility.
 */
import { useEffect, useState } from 'react'
import { getInsights } from '../api/smartcool.js'
import {
  TrendingUp, TrendingDown, Minus, Zap, Thermometer,
  BarChart2, Loader, AlertTriangle, Info, Lightbulb,
} from 'lucide-react'

// ── Helpers ───────────────────────────────────────────────────────────────────

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
        { label: 'Fast',   count: fast,   pct: pct(fast),   color: 'bg-green-500'  },
        { label: 'Normal', count: normal, pct: pct(normal), color: 'bg-blue-500'   },
        { label: 'Slow',   count: slow,   pct: pct(slow),   color: 'bg-orange-500' },
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

const REASON_LABELS = {
  no_sessions:       'No completed sessions yet',
  insufficient_data: 'Sessions too short or incomplete',
  no_usable_data:    'No sessions with positive cooling',
  error:             'Calculation error (check logs)',
}

// ── Recommendations engine (pure JS, read-only) ───────────────────────────────

function buildRecommendations({ avgRate, avgEff, bestTemp, bestRange, counts, trend }) {
  const tips = []

  // Cooling speed advice
  const total = (counts.fast || 0) + (counts.normal || 0) + (counts.slow || 0)
  const slowFraction = total > 0 ? (counts.slow || 0) / total : 0
  if (avgRate > 0 && avgRate < 0.2) {
    tips.push({
      icon: '🐢',
      text: `Cooling is slow (${avgRate.toFixed(3)} °C/min). Check if doors/windows are open or if AC filter needs cleaning.`,
    })
  } else if (avgRate >= 0.5) {
    tips.push({
      icon: '⚡',
      text: `Great cooling speed (${avgRate.toFixed(3)} °C/min). Your setup is performing well.`,
    })
  }

  // Best target temp highlight
  if (bestTemp != null) {
    tips.push({
      icon: '🎯',
      text: `Your AC is most effective at ${bestTemp}°C target. Setting it higher wastes energy; lower rarely cools faster.`,
      highlight: true,
    })
  }

  // Efficiency advice
  if (avgEff > 0) {
    if (avgEff < 0.01) {
      tips.push({
        icon: '✅',
        text: `Efficient cooling: ${avgEff.toFixed(4)} kWh per °C cooled. Keep it up!`,
      })
    } else if (avgEff > 0.05) {
      tips.push({
        icon: '⚠️',
        text: `High energy per °C (${avgEff.toFixed(4)} kWh/°C). Consider a shorter on-time or better insulation.`,
      })
    }
  }

  // Trend advice
  if (trend === 'declining') {
    tips.push({
      icon: '📉',
      text: 'Cooling efficiency is declining. Servicing the AC or improving room insulation may help.',
    })
  } else if (trend === 'improving') {
    tips.push({
      icon: '📈',
      text: 'Cooling efficiency is improving — recent sessions are performing better.',
    })
  }

  // Slow sessions proportion
  if (slowFraction >= 0.5 && total >= 3) {
    tips.push({
      icon: '🌡️',
      text: `${Math.round(slowFraction * 100)}% of sessions are slow. Try pre-cooling before peak heat hours.`,
    })
  }

  if (tips.length === 0) {
    tips.push({
      icon: '📊',
      text: 'Collect more sessions to unlock personalised recommendations.',
    })
  }

  return tips
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
        .then(d  => { if (alive) { setData(d);           setLoading(false) } })
        .catch(e => { if (alive) { setError(e.message); setLoading(false) } })
    }
    load()
    const id = setInterval(load, 5 * 60 * 1000)   // refresh every 5 min
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

  const hasData      = data?.has_data ?? (data?.sessions_analyzed > 0)
  const reason       = data?.reason
  const fallbackUsed = data?.fallback_used ?? false
  const n            = data?.sessions_analyzed ?? 0

  const m = data?.metrics ?? data ?? {}
  const avgRate    = m.avg_cooling_rate   ?? 0
  const avgEff     = m.avg_efficiency     ?? 0
  const avgCoolMin = m.avg_cool_time_min  ?? null
  const bestTemp   = m.best_target_temp   ?? null
  const bestRange  = m.best_outdoor_range ?? null
  const counts     = m.cooling_type_counts ?? { fast: 0, normal: 0, slow: 0 }
  const trend      = m.trend ?? null

  const tips = hasData
    ? buildRecommendations({ avgRate, avgEff, bestTemp, bestRange, counts, trend })
    : []

  return (
    <div className="card space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs text-gray-500 uppercase tracking-wide">AC Insights</p>
          <p className="text-xs text-gray-600 mt-0.5">
            {hasData
              ? `Based on ${n} session${n !== 1 ? 's' : ''}`
              : (REASON_LABELS[reason] ?? 'Run AC for ≥3 min to generate insights')}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <BarChart2 size={16} className="text-purple-400" />
          {hasData && <TrendBadge trend={trend} />}
        </div>
      </div>

      {/* Fallback notice */}
      {hasData && fallbackUsed && (
        <div className="flex items-start gap-2 px-3 py-2 bg-yellow-900/20 border border-yellow-800/40 rounded-lg text-xs text-yellow-300">
          <Info size={12} className="mt-0.5 shrink-0" />
          <span>
            Using approximate data — sessions are short or missing temperature readings.
            Insights will improve with longer cooling sessions.
          </span>
        </div>
      )}

      {hasData ? (
        <>
          {/* Key metrics row */}
          <div className="grid grid-cols-3 gap-4">
            <Stat
              label="Avg Cooling Rate"
              value={avgRate > 0 ? avgRate.toFixed(3) : null}
              unit="°C/min"
              color="text-blue-400"
            />
            <Stat
              label="Energy / °C"
              value={avgEff > 0 ? avgEff.toFixed(4) : null}
              unit="kWh/°C"
              color="text-yellow-400"
            />
            {/* Best target highlighted */}
            <div className="flex flex-col gap-0.5">
              <span className="text-xs text-gray-500 uppercase tracking-wide">Best Target</span>
              {bestTemp != null ? (
                <span className="font-bold text-2xl text-green-400">
                  {bestTemp}°
                  <span className="ml-2 text-xs font-normal text-green-600 bg-green-900/30 px-1.5 py-0.5 rounded">
                    Recommended
                  </span>
                </span>
              ) : (
                <span className="text-sm text-gray-600">—</span>
              )}
            </div>
          </div>

          {/* Avg cool time */}
          {avgCoolMin != null && avgCoolMin > 0 && (
            <div className="flex items-center gap-2 text-xs text-gray-500">
              <Thermometer size={12} className="text-orange-400" />
              Avg time to cool:{' '}
              <span className="font-semibold text-gray-300">{avgCoolMin.toFixed(1)} min</span>
            </div>
          )}

          {/* Bottom row: cooling speed + best conditions */}
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <p className="text-xs text-gray-500 uppercase tracking-wide">Cooling Speed</p>
              <TypeBar fast={counts.fast} normal={counts.normal} slow={counts.slow} />
            </div>
            <div className="space-y-2">
              <p className="text-xs text-gray-500 uppercase tracking-wide">Best Conditions</p>
              <div className="space-y-1.5 text-xs">
                <div className="flex items-center gap-2">
                  <Thermometer size={12} className="text-orange-400 shrink-0" />
                  <span className="text-gray-400">Outdoor:</span>
                  <span className="font-semibold text-gray-200">{bestRange ?? '—'}</span>
                </div>
                <div className="flex items-center gap-2">
                  <Zap size={12} className="text-yellow-400 shrink-0" />
                  <span className="text-gray-400">Target:</span>
                  <span className="font-semibold text-green-300">
                    {bestTemp != null ? `${bestTemp}°C` : '—'}
                  </span>
                </div>
              </div>
            </div>
          </div>

          {/* Recommendations */}
          <div className="border-t border-gray-800 pt-4 space-y-2">
            <div className="flex items-center gap-1.5 text-xs text-gray-500 uppercase tracking-wide mb-1">
              <Lightbulb size={12} className="text-yellow-400" />
              Recommendations
            </div>
            <div className="space-y-2">
              {tips.map((tip, i) => (
                <div
                  key={i}
                  className={`flex items-start gap-2.5 px-3 py-2 rounded-lg text-xs ${
                    tip.highlight
                      ? 'bg-green-900/20 border border-green-800/40 text-green-200'
                      : 'bg-gray-800/50 text-gray-300'
                  }`}
                >
                  <span className="shrink-0 mt-0.5">{tip.icon}</span>
                  <span>{tip.text}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Footer legend */}
          <div className="flex flex-wrap gap-4 text-xs text-gray-600 border-t border-gray-800 pt-3">
            <span><span className="text-green-400 font-semibold">Fast</span> &gt;0.5°C/min</span>
            <span><span className="text-blue-400 font-semibold">Normal</span> 0.2–0.5°C/min</span>
            <span><span className="text-orange-400 font-semibold">Slow</span> &lt;0.2°C/min</span>
            <span className="ml-auto text-gray-700">Min 3 min session · Δtemp ≥ 0.3°C</span>
          </div>
        </>
      ) : (
        <div className="py-6 text-center space-y-2">
          <BarChart2 size={32} className="text-gray-700 mx-auto" />
          <p className="text-sm text-gray-500">
            {REASON_LABELS[reason] ?? 'No cooling data yet'}
          </p>
          <p className="text-xs text-gray-600">
            Insights appear after the AC runs for at least 3 minutes and the room
            temperature drops by at least 0.3 °C.
          </p>
        </div>
      )}
    </div>
  )
}
